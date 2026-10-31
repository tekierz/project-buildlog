#!/usr/bin/env python3
"""Compact agent handoffs, work records and deterministic buildlog rendering. Stdlib only."""
import argparse
import datetime
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

STATUSES = ("planned", "running", "blocked", "review", "done", "cancelled")
PRIORITY = {s: i for i, s in enumerate(("blocked", "review", "running", "planned", "done", "cancelled"))}


def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        for attempt in range(40):  # Windows: a reader (the server) may briefly hold the target open
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 39:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def load_state(path):
    data = read_json(path, {"version": 1, "work": [], "notes": []})
    if (not isinstance(data, dict) or data.get("version") != 1 or
            not isinstance(data.get("work"), list) or not isinstance(data.get("notes"), list)):
        raise ValueError("unsupported work state; expected version 1 with work/notes arrays")
    ids = set()
    for row in data["work"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or
                row["id"] in ids or row.get("status") not in STATUSES or not isinstance(row.get("title"), str)):
            raise ValueError("invalid or duplicate work record")
        ids.add(row["id"])
    if any(not isinstance(row, dict) or not isinstance(row.get("text"), str) or
           not isinstance(row.get("at"), str) for row in data["notes"]):
        raise ValueError("invalid note record")
    return data


def settings_path(project=None):
    if project:
        root = Path(project).resolve()
    else:
        local = Path(__file__).resolve().with_name("buildlog.json")
        if local.is_file():
            return local
        root = Path.cwd().resolve()
    # Match nearest project; do not scan the repository or load unrelated docs.
    for parent in (root, *root.parents):
        candidate = parent / "tools/buildlog/buildlog.json"
        if candidate.is_file():
            return candidate
    raise ValueError("no buildlog settings; pass --settings for a custom tools directory")


def context(path):
    settings = read_json(path, {})
    root = (path.parent / settings["root"]).resolve()
    page = root / settings["page"]
    state = root / settings.get("agent_state", str(Path(settings["page"]).with_name("buildlog-state.json")))
    return settings, root, page, state


def short(value, size=160):
    text = " ".join(str(value or "").split())
    return text if len(text) <= size else text[:size - 1] + "…"


def head(root):
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True, timeout=5)
    return result.stdout.strip() if result.returncode == 0 else "uncommitted"


def pending(settings):
    homes = settings.get("_homes") or {}
    directory = Path(os.environ.get("BUILDLOG_STATE_DIR") or
                     Path(homes.get("state_home") or Path.home() / ".local/state") / settings["name"])
    # Scan the journal locally; never include message bodies in hook context.
    statuses = {}
    try:
        with (directory / "inbox.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if record.get("type") == "message" and isinstance(payload, dict) and payload.get("id"):
                    statuses[payload["id"]] = True
                elif record.get("type") == "delivered" and record.get("via") not in ("local",):
                    statuses.pop(record.get("id"), None)
    except FileNotFoundError:
        pass
    read = read_json(directory / "read.json", {}).get("read", [])
    return len(set(statuses) - set(read))


def packet(settings, root, state, limit):
    data = load_state(state)
    tasks = read_json(root / settings["tasks"], {"tasks": []})["tasks"]
    work = sorted(data["work"], key=lambda row: (PRIORITY[row["status"]], row["id"]))
    active = [row for row in work if row["status"] not in ("done", "cancelled")]
    counts = {status: sum(row["status"] == status for row in work) for status in STATUSES}
    counts = {status: count for status, count in counts.items() if count}
    open_tasks = [row for row in tasks if row.get("status") != "done"]
    result = {
        "version": 1, "project": short(settings.get("title", settings["name"]), 80),
        "head": head(root), "counts": counts, "inbox_pending": pending(settings),
        "work": [{key: short(row[key], 160 if key == "summary" else 80)
                  for key in ("id", "title", "status", "owner", "parent", "task_id", "summary", "evidence")
                  if row.get(key)} for row in active[:limit]],
        "user_tasks": [{"id": short(row["id"], 80), "kind": row["kind"], "title": short(row["title"], 120)}
                       for row in open_tasks[:limit]],
        "latest": short(data.get("notes", [{}])[-1].get("text", "")) if data.get("notes") else "",
        "omitted": {"work": max(0, len(active) - limit), "user_tasks": max(0, len(open_tasks) - limit)},
    }
    # Hash the full source, not the clipped projection: changes beyond the limit still invalidate it.
    result["revision"] = hashlib.sha256(json.dumps([data, tasks, result["head"], result["inbox_pending"]],
                                                   sort_keys=True).encode()).hexdigest()[:16]
    return result


def format_packet(data, mode, budget):
    if mode == "json":
        # Keep JSON parseable: remove rows before clipping individual content.
        while len(json.dumps(data, ensure_ascii=False, separators=(",", ":"))) > budget:
            key = "user_tasks" if data["user_tasks"] else "work"
            if data[key]:
                data[key].pop()
                data["omitted"][key] += 1
            elif data["latest"]:
                data["latest"] = ""
            else:
                raise ValueError("max-chars too small for JSON envelope")
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    counts = " ".join("%s=%s" % (k, v) for k, v in data["counts"].items()) or "no work"
    lines = ["buildlog v1 rev=%s head=%s %s" % (data["revision"], data["head"], data["project"]),
             "%s | inbox=%s | user_tasks=%s" % (counts, data["inbox_pending"],
                                                len(data["user_tasks"]) + data["omitted"]["user_tasks"])]
    for row in data["work"]:
        lines.append("work %s [%s] %s | owner=%s parent=%s task=%s | %s%s" %
                     (row["id"], row["status"], row["title"], row.get("owner", "-"), row.get("parent", "-"),
                      row.get("task_id", "-"), row.get("summary", ""),
                      " | evidence=" + row["evidence"] if row.get("evidence") else ""))
    for row in data["user_tasks"]:
        lines.append("user %s [%s] %s" % (row["id"], row["kind"], row["title"]))
    if data["latest"]:
        lines.append("latest " + data["latest"])
    if data["omitted"]["work"] or data["omitted"]["user_tasks"]:
        lines.append("omitted work=%d user_tasks=%d; details: buildlog_agent.py show ID" %
                     (data["omitted"]["work"], data["omitted"]["user_tasks"]))
    text = "\n".join(lines)
    suffix = "\n[truncated; use show or a larger --max-chars]"
    return text if len(text) <= budget else text[:budget - len(suffix)] + suffix


def render_page(page, data, commit):
    escape = html.escape
    rows = sorted(data["work"], key=lambda row: (PRIORITY[row["status"]], row["id"]))
    active = [row for row in rows if row["status"] not in ("done", "cancelled")]
    body = '<h2>Current work</h2>\n'
    if active:
        body += '<table tabindex="0" aria-label="Active agent work"><thead><tr><th>Work</th><th>Status</th><th>Owner</th><th>Update</th></tr></thead><tbody>'
        for row in active[:30]:
            body += "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % tuple(
                escape(str(value)) for value in (row["title"], row["status"], row.get("owner", "—"), row.get("summary", "")))
        body += "</tbody></table>"
        if len(active) > 30:
            body += "<p>%d more work items; use the work CLI for details.</p>" % (len(active) - 30)
    else:
        body += "<p>No active work recorded.</p>"
    notes = '<ol class="timeline">' + "".join(
        "<li><time>%s</time> %s</li>" % (escape(row["at"]), escape(row["text"]))
        for row in reversed(data.get("notes", [])[-20:])) + "</ol>"
    text = page.read_text(encoding="utf-8")
    found = []
    for marker, content in (("CURRENT", body), ("TIMELINE", notes),
                            ("UPDATED", '<div class="muted">Updated %s · commit <code>%s</code></div>' %
                             (datetime.datetime.now().astimezone().isoformat(timespec="seconds"), escape(commit)))):
        pattern = r"(<!-- %s:START -->).*?(<!-- %s:END -->)" % (marker, marker)
        count = len(re.findall(pattern, text, re.S))
        if count > 1:
            raise ValueError("duplicate " + marker + " markers")
        if count:
            found.append(marker)
            text = re.sub(pattern, lambda match: match[1] + "\n" + content + "\n" + match[2], text, flags=re.S)
    atomic(page, text)
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--project", help="default: installed script's project, or nearest ancestor of cwd")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("brief", "hook"):
        p = sub.add_parser(command)
        p.add_argument("--format", choices=("text", "json"), default="text")
        p.add_argument("--max-chars", type=int, default=2400)
        p.add_argument("--limit", type=int, default=5)
        p.add_argument("--since", help="emit only when revision differs (unchanged emits nothing)")
    p = sub.add_parser("work", help="upsert an agent work record; does not launch agents")
    p.add_argument("id")
    p.add_argument("--title")
    p.add_argument("--status", choices=STATUSES)
    for field in ("owner", "parent", "task-id", "summary", "evidence"):
        p.add_argument("--" + field)
    p = sub.add_parser("note", help="append a short timeline update")
    p.add_argument("text")
    p = sub.add_parser("show", help="full record for one item, or the complete work state")
    p.add_argument("id", nargs="?")
    p = sub.add_parser("render", help="render CURRENT, TIMELINE and UPDATED; preserve tasks and cards")
    for p in (sub.choices["work"], sub.choices["note"], p):
        p.add_argument("--commit", action="store_true", help="commit the changed buildlog files (private index)")
    args = parser.parse_args(argv)
    try:
        if args.command == "hook":
            event = json.load(sys.stdin)
            if not isinstance(event, dict):
                raise ValueError("hook input must be an object")
            if event.get("hook_event_name") != "SessionStart":
                return 0
            try:
                path = args.settings or settings_path(event.get("cwd") or os.getcwd())
            except ValueError:
                return 0  # No buildlog in this project: optional hooks stay silent.
        else:
            path = args.settings or settings_path(args.project)
        settings, root, page, state = context(path.resolve())
        # A shared directory lock matches the task/progress writers, including render.
        with directory_lock(page.parent):
            if args.command in ("brief", "hook"):
                if args.max_chars < 600 or args.limit < 1:
                    raise ValueError("max-chars must be >=600 and limit >=1")
                report = packet(settings, root, state, args.limit)
                if report["revision"] == args.since:
                    return 0
                output = format_packet(report, args.format, args.max_chars)
                if args.command == "hook":
                    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": output}}))
                else:
                    print(output)
                return 0
            data = load_state(state)
            if args.command == "show":
                result = next((row for row in data["work"] if row["id"] == args.id), None) if args.id else data
                if result is None:
                    raise ValueError("unknown work id: " + args.id)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "render":
                found = render_page(page, data, head(root))
                missing = [marker for marker in ("CURRENT", "TIMELINE", "UPDATED") if marker not in found]
                print("rendered %s (%s)%s" % (settings["page"], ", ".join(found) or "nothing",
                                              "; page has no %s section" % ", ".join(missing) if missing else ""))
                changed, message = [page, state], "buildlog: update"
            elif args.command == "work":
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", args.id):
                    raise ValueError("id must be 1-80 letters, digits, dots, underscores or dashes")
                row = next((row for row in data["work"] if row["id"] == args.id), None)
                if row is None:
                    if not args.title:
                        raise ValueError("new work needs --title")
                    row = {"id": args.id, "title": args.title, "status": "planned"}
                    data["work"].append(row)
                for field in ("title", "status", "owner", "parent", "task_id", "summary", "evidence"):
                    value = getattr(args, field)
                    if value is not None:
                        if len(value) > (2000 if field == "summary" else 300):
                            raise ValueError(field + " is too long")
                        row[field] = value
                by_id = {item["id"]: item for item in data["work"]}
                seen, current = {row["id"]}, row
                while current.get("parent"):
                    parent = current["parent"]
                    if parent in seen or parent not in by_id:
                        raise ValueError("parent must exist and must not create a cycle")
                    seen.add(parent)
                    current = by_id[parent]
                row["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
                changed, message = [state], "buildlog: work " + args.id
            else:
                if not args.text.strip() or len(args.text) > 500:
                    raise ValueError("note must contain 1-500 characters")
                data["notes"].append({"at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                                      "text": args.text})
                changed, message = [state], "buildlog: note"
            if args.command != "render":
                atomic(state, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                print("saved " + (args.id if args.command == "work" else "note"))
        return commit(args, path, changed, message)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        if args.command == "hook":
            # Optional enrichment never blocks startup. Report without dumping input.
            print("buildlog: context unavailable (run brief to diagnose)", file=sys.stderr)
            return 0
        print("buildlog: " + str(error), file=sys.stderr)
        return 1
    return 0


def commit(args, settings, paths, message):
    """Commit only these buildlog files through the task CLI's private index, after the lock is released."""
    if not getattr(args, "commit", False):
        return 0
    tasks_cli = settings.parent / "buildlog_tasks.py"
    if not tasks_cli.is_file():
        tasks_cli = Path(__file__).resolve().with_name("buildlog_tasks.py")
    result = subprocess.run([sys.executable, "-I", str(tasks_cli), "--settings", str(settings), "commit", "-m", message]
                            + [str(path) for path in paths if path.exists()], capture_output=True, text=True, timeout=60)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


class directory_lock:
    """The task CLI's exclusive directory lock (buildlog_tasks.Locked): flock on the directory, or on Windows
    msvcrt on a lock file named after it in the temp folder."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        if fcntl:
            self.fd = os.open(self.path, os.O_RDONLY)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self.fd
        key = hashlib.sha256(os.path.normcase(os.path.abspath(str(self.path))).encode("utf-8")).hexdigest()[:20]
        self.fd = os.open(os.path.join(tempfile.gettempdir(), "buildlog-%s.lock" % key), os.O_RDWR | os.O_CREAT, 0o600)
        while True:
            try:
                msvcrt.locking(self.fd, msvcrt.LK_LOCK, 1)
                return self.fd
            except OSError:
                continue

    def __exit__(self, *args):
        if fcntl:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        else:
            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
        os.close(self.fd)


if __name__ == "__main__":
    sys.exit(main())
