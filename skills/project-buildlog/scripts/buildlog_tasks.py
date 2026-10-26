#!/usr/bin/env python3
"""Manage the "Your tasks" list shown at the top of the build log page.

Tasks are things only the user can do (actions) or decide (decisions). They
live in the task file (default docs/buildlog-tasks.json, see buildlog.json)
and change only through this tool:

  buildlog_tasks.py list [--all] [--json]
  buildlog_tasks.py add --kind action|decision --title T --details D [--how CMDS] [--id ID] [--commit]
  buildlog_tasks.py done ID [--commit]
  buildlog_tasks.py reopen ID [--commit]
  buildlog_tasks.py reply ID TEXT [--commit]          answer under a task on the page
  buildlog_tasks.py reply --general TEXT [--commit]   answer in the page's conversation
  buildlog_tasks.py sync [--commit]       rewrite the page's static snapshot only
  buildlog_tasks.py inbox [--all] [--json] [--mark-read]
  buildlog_tasks.py commit -m MSG PATH...  commit only these paths (private index)

The user reads the page, not the chat: when they send a note or message,
answer it with `reply`. TEXT "-" reads the reply from stdin. Replies are kept
in the task file (task["replies"] and the top-level "general_replies", each
{at, text}); they never change a task's status or its "updated" stamp, so a
reply cannot undo a user's tick.

After every change the tool also refreshes the JSON snapshot embedded in the
page (between the TASKS-SNAPSHOT markers), which the page shows when it is
opened without the server. Run it by absolute path in the main checkout (the
one the server serves), not in a worktree copy.

--commit commits only the task file and the page, through a private git
index (GIT_INDEX_FILE), so a stale shared index can never sneak other changes
into the commit. The message is short and has no trailers. `commit` does the
same for any paths (relative to the project root), e.g. after a page update.
If the shared index was in step with HEAD for a path, it is moved to the new
commit for that path only; a shared index that was already stale is left alone.

`inbox` lists messages from the panel that no agent has handled yet: ones
still queued for the webhook, and (in local-only delivery) ones not yet
marked read. --mark-read records the listed ids, so the server will not
forward them later either. Python standard library only.
"""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = "buildlog.json"
KINDS = ("action", "decision")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SNAP_RE = re.compile(r"(<!-- TASKS-SNAPSHOT:START -->).*?(<!-- TASKS-SNAPSHOT:END -->)", re.S)
SNAP_FIELDS = ("id", "kind", "title", "details", "how", "status")
MAX_REPLY = 8000


def load_settings(path=None):
    path = Path(path) if path else HERE / SETTINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    s = {"name": "buildlog", "root": "..", "page": "docs/buildlog.html", "tasks": "docs/buildlog-tasks.json"}
    s.update(data if isinstance(data, dict) else {})
    s["root"] = str((path.parent / s["root"]).resolve())
    return s


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def slug(title):
    words = re.sub(r"[^a-z0-9]+", " ", title.lower()).split()
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > 40 and out:
            break
        out = (out + "-" + w) if out else w
    return out[:40] or "task"


def write_atomic(path, text, mode=None):
    if mode is None:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(path):
    if not path.exists():
        return {"tasks": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise SystemExit("%s: expected {\"tasks\": [...]}" % path)
    return data


def dump(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def snapshot_json(data):
    tasks = []
    for t in data["tasks"]:
        row = {k: t.get(k, "") for k in SNAP_FIELDS}
        row["replies"] = [{"at": r["at"], "text": r["text"]} for r in t.get("replies") or []]
        tasks.append(row)
    general = [{"at": r["at"], "text": r["text"]} for r in data.get("general_replies") or []]
    # Safe inside <script>: no raw "<" can appear.
    return json.dumps({"tasks": tasks, "general_replies": general}, ensure_ascii=True,
                      separators=(",", ":")).replace("<", "\\u003c")


def sync_page(data, page):
    """Replace the snapshot between the markers. Returns True if the page changed."""
    if not page.exists():
        return False
    html = page.read_text(encoding="utf-8")
    if not SNAP_RE.search(html):
        print("note: %s has no TASKS-SNAPSHOT markers; page snapshot not updated" % page, file=sys.stderr)
        return False
    block = ("<!-- TASKS-SNAPSHOT:START -->\n"
             '<script type="application/json" id="tasks-snapshot">%s</script>\n' % snapshot_json(data))
    new = SNAP_RE.sub(lambda m: block + m.group(2), html, count=1)
    if new == html:
        return False
    write_atomic(page, new)
    return True


class Locked:
    """Exclusive lock on a directory, held across read-modify-write (the progress script takes it too)."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.fd = os.open(str(self.path), os.O_RDONLY)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def find(data, task_id):
    for t in data["tasks"]:
        if t["id"] == task_id:
            return t
    raise SystemExit("no task with id %r (try: list --all)" % task_id)


# ---- private-index commit ---------------------------------------------------

def git(args, cwd, env=None, check=True):
    r = subprocess.run(["git"] + args, cwd=str(cwd), env=env, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("git %s: %s" % (args[0], (r.stderr or r.stdout).strip()))
    return r


def commit_paths(paths, message, cwd):
    """Commit exactly `paths` on top of HEAD through a throwaway index. Returns the short hash or None.

    Afterwards the shared index is moved to the new commit for each committed file
    whose shared entry matched the old HEAD; entries that were already stale or
    staged by someone else are left alone.
    """
    top = Path(git(["rev-parse", "--show-toplevel"], cwd).stdout.strip())
    rels = [os.path.relpath(str(p), str(top)) for p in paths if p.exists()]
    if not rels:
        return None
    has_head = git(["rev-parse", "-q", "--verify", "HEAD"], top, check=False).returncode == 0
    if has_head:   # files under these paths where the shared index differs from HEAD
        out = git(["diff", "--cached", "--name-only", "-z", "HEAD", "--"] + rels, top, check=False).stdout
    else:
        out = git(["ls-files", "-z", "--"] + rels, top, check=False).stdout
    stale = set(x for x in out.split("\0") if x)
    fd, index = tempfile.mkstemp(prefix="buildlog-index.")
    os.close(fd)
    os.unlink(index)                         # git creates it; an empty file is not a valid index
    env = dict(os.environ, GIT_INDEX_FILE=index)
    try:
        git(["read-tree", "HEAD"] if has_head else ["read-tree", "--empty"], top, env)
        git(["add", "--"] + rels, top, env)
        if has_head and git(["diff", "--cached", "--quiet", "HEAD", "--"] + rels, top, env, check=False).returncode == 0:
            print("nothing to commit for %s" % ", ".join(rels), file=sys.stderr)
            return None
        git(["commit", "-q", "-m", message], top, env)
    finally:
        try:
            os.unlink(index)
        except OSError:
            pass
    if has_head:
        out = git(["diff", "--name-only", "-z", "HEAD~1", "HEAD"], top, check=False).stdout
    else:
        out = git(["ls-tree", "-r", "-z", "--name-only", "HEAD"], top, check=False).stdout
    committed = [x for x in out.split("\0") if x]
    follow = [f for f in committed if f not in stale]
    for i in range(0, len(follow), 200):     # the shared index follows HEAD for these files only
        git(["reset", "-q", "HEAD", "--"] + follow[i:i + 200], top, check=False)
    left = [f for f in committed if f in stale]
    if left:
        print("note: the shared index was already out of step for %s; left as it was" % ", ".join(left[:5]), file=sys.stderr)
    return git(["rev-parse", "--short", "HEAD"], top).stdout.strip()


# ---- commands ---------------------------------------------------------------

def cmd_list(args, data):
    tasks = data["tasks"]
    if args.id:
        tasks = [find(data, args.id)]
    if not args.all:
        tasks = [t for t in tasks if t.get("status") != "done"]
    if args.json:
        print(json.dumps(tasks, indent=2, ensure_ascii=False))
        return None
    tasks = sorted(tasks, key=lambda t: t.get("status") == "done")
    for t in tasks:
        print("%-9s %-5s %s  %s" % (t["kind"], t.get("status", "open"), t["id"], t["title"]))
    if not tasks:
        print("(no open tasks)")
    return None


def cmd_add(args, data):
    ids = {t["id"] for t in data["tasks"]}
    if args.id:
        if not ID_RE.match(args.id):
            raise SystemExit("--id must be lowercase letters, digits and hyphens")
        if args.id in ids:
            raise SystemExit("id %r already exists" % args.id)
        task_id = args.id
    else:
        base = slug(args.title)
        task_id, n = base, 2
        while task_id in ids:
            task_id, n = "%s-%d" % (base, n), n + 1
    stamp = now_iso()
    data["tasks"].append({"id": task_id, "kind": args.kind, "title": args.title.strip(),
                          "details": args.details.strip(), "how": (args.how or "").strip(),
                          "added": stamp, "status": "open", "done_at": None, "updated": stamp})
    print(task_id)
    return "buildlog tasks: add %s" % task_id


def cmd_done(args, data):
    t = find(data, args.id)
    if t.get("status") == "done":
        print("already done: %s" % t["id"])
        return None
    stamp = now_iso()
    t.update({"status": "done", "done_at": stamp, "updated": stamp})
    print("done: %s" % t["id"])
    return "buildlog tasks: done %s" % t["id"]


def cmd_reopen(args, data):
    t = find(data, args.id)
    if t.get("status") != "done":
        print("already open: %s" % t["id"])
        return None
    t.update({"status": "open", "done_at": None, "updated": now_iso()})
    print("reopened: %s" % t["id"])
    return "buildlog tasks: reopen %s" % t["id"]


def cmd_sync(args, data):
    return None


def cmd_reply(args, data):
    text = args.args[-1]
    if text == "-":
        text = sys.stdin.read()
    text = text.strip()
    if not text:
        raise SystemExit("reply text is empty")
    if len(text) > MAX_REPLY:
        raise SystemExit("reply is too long (%d characters; the limit is %d)" % (len(text), MAX_REPLY))
    entry = {"at": now_iso(), "text": text}
    if args.general:
        if len(args.args) != 1:
            raise SystemExit('usage: reply --general "text"')
        data.setdefault("general_replies", []).append(entry)
        print("replied: general")
        return "buildlog tasks: reply"
    if len(args.args) != 2:
        raise SystemExit('usage: reply ID "text"   (or: reply --general "text")')
    t = find(data, args.args[0])
    t.setdefault("replies", []).append(entry)     # not "updated": a reply must not undo a tick
    print("replied: %s" % t["id"])
    return "buildlog tasks: reply to %s" % t["id"]


def inbox_messages(state_dir):
    """Messages from inbox.jsonl with their status, oldest first."""
    msgs, order = {}, []
    try:
        lines = (state_dir / "inbox.jsonl").read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("type") == "message" and isinstance(rec.get("payload"), dict) and rec["payload"].get("id"):
            p = rec["payload"]
            if p["id"] not in msgs:
                order.append(p["id"])
            msgs[p["id"]] = dict(p, status="queued")
        elif rec.get("type") == "delivered" and rec.get("id") in msgs:
            via = rec.get("via")
            msgs[rec["id"]]["status"] = via if via in ("local", "read") else "sent"
    return [msgs[i] for i in order]


def read_ids(state_dir):
    try:
        data = json.loads((state_dir / "read.json").read_text())
        return [i for i in data.get("read", []) if isinstance(i, str)]
    except (OSError, ValueError, AttributeError):
        return []


def cmd_inbox(args, settings):
    homes = settings.get("_homes") or {}      # only set by the skill's own tests
    state_dir = Path(args.state_dir or os.environ.get("BUILDLOG_STATE_DIR") or
                     Path(homes.get("state_home") or Path.home() / ".local/state") / settings["name"])
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with Locked(state_dir):
        read = read_ids(state_dir)
        seen = set(read)
        msgs = inbox_messages(state_dir)
        for m in msgs:
            if m["id"] in seen and m["status"] in ("queued", "local"):
                m["status"] = "read"
        shown = msgs if args.all else [m for m in msgs if m["status"] in ("queued", "local")]
        if args.id:
            shown = [m for m in shown if m["id"] in args.id]
        if args.limit is not None:
            shown = shown[:args.limit]
        if args.json:
            print(json.dumps(shown, indent=2, ensure_ascii=False))
        else:
            for m in shown:
                head = "%s %-6s %-11s" % (m.get("sent_at", "?"), m["status"], m.get("kind", "?"))
                if m.get("kind") == "task_update":
                    head += " %s done=%s" % (m.get("task_id"), m.get("done"))
                head += " id=%s" % m["id"]
                print(head)
                note = str(m.get("note", "")).strip()
                if note:
                    print("    " + note.replace("\n", "\n    "))
            if not shown:
                print("(no unhandled messages)")
        if args.mark_read:
            new = [m["id"] for m in shown if m["id"] not in seen]
            if new:
                write_atomic(state_dir / "read.json", json.dumps({"read": read + new}, indent=1) + "\n", mode=0o600)
                print("marked read: %d" % len(new), file=sys.stderr)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Manage the build log's \"Your tasks\" list.")
    ap.add_argument("--settings", help="settings file (default: buildlog.json next to this script)")
    ap.add_argument("--file", default=os.environ.get("BUILDLOG_TASKS"), help="task file (default: from the settings)")
    ap.add_argument("--page", help="page whose snapshot to refresh (default: from the settings)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="show tasks")
    p.add_argument("--all", action="store_true", help="include done tasks")
    p.add_argument("--json", action="store_true")
    p.add_argument("--id", help="read one task by ID")
    p = sub.add_parser("add", help="add an open task")
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--title", required=True)
    p.add_argument("--details", required=True)
    p.add_argument("--how", help="copy-paste commands (optional)")
    p.add_argument("--id", help="explicit id (default: made from the title)")
    p.add_argument("--commit", action="store_true", help="commit the task file and page (private index)")
    for name in ("done", "reopen"):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.add_argument("--commit", action="store_true", help="commit the task file and page (private index)")
    p = sub.add_parser("reply", help="answer the user under a task, or in the general thread")
    p.add_argument("--general", action="store_true", help="reply in the general message thread")
    p.add_argument("args", nargs="+", metavar="ID_AND_TEXT", help='ID "text", or just "text" with --general ("-" reads stdin)')
    p.add_argument("--commit", action="store_true", help="commit the task file and page (private index)")
    p = sub.add_parser("sync", help="refresh the page snapshot only")
    p.add_argument("--commit", action="store_true")
    p = sub.add_parser("inbox", help="messages from the panel no agent has handled yet")
    p.add_argument("--all", action="store_true", help="every message, with its status")
    p.add_argument("--json", action="store_true")
    p.add_argument("--mark-read", action="store_true", help="record the listed messages as handled")
    p.add_argument("--state-dir", help="default: ~/.local/state/<name>")
    p.add_argument("--id", action="append", help="select a message ID; repeat for several (also limits --mark-read)")
    p.add_argument("--limit", type=int, help="show/mark at most this many messages")
    p = sub.add_parser("commit", help="commit only the given paths, through a private index")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("paths", nargs="+", help="paths relative to the project root (or absolute)")
    args = ap.parse_args(argv)
    if args.cmd == "inbox" and args.limit is not None and args.limit < 1:
        ap.error("--limit must be positive")

    settings = load_settings(args.settings)
    if args.cmd == "inbox":
        return cmd_inbox(args, settings)
    if args.cmd == "commit":
        root = Path(settings["root"])
        paths = [Path(p) if os.path.isabs(p) else root / p for p in args.paths]
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            print("no such path: %s" % ", ".join(missing), file=sys.stderr)
            return 1
        try:
            short = commit_paths(paths, args.message, root)
        except RuntimeError as e:
            print("commit failed: %s" % e, file=sys.stderr)
            return 1
        if short:
            print("committed %s: %s" % (short, args.message))
        return 0
    root = Path(settings["root"])
    path = Path(args.file).resolve() if args.file else (root / settings["tasks"]).resolve()
    if args.page:
        page = Path(args.page).resolve()
    elif args.file:
        page = path.with_name("buildlog.html")
    else:
        page = (root / settings["page"]).resolve()
    handler = {"list": cmd_list, "add": cmd_add, "done": cmd_done, "reopen": cmd_reopen, "sync": cmd_sync,
               "reply": cmd_reply}[args.cmd]
    path.parent.mkdir(parents=True, exist_ok=True)
    with Locked(page.parent if page.parent.exists() else path.parent):
        data = load(path)
        message = handler(args, data)
        if message:
            write_atomic(path, dump(data))
        page_changed = False
        if message or args.cmd == "sync":
            page_changed = sync_page(data, page)
            if page_changed:
                print("page snapshot updated: %s" % page, file=sys.stderr)
    if getattr(args, "commit", False):
        msg = message or "buildlog tasks: sync"
        try:
            short = commit_paths([path, page], msg, path.parent)
        except RuntimeError as e:
            print("commit failed: %s" % e, file=sys.stderr)
            return 1
        if short:
            print("committed %s: %s" % (short, msg), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
