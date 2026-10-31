#!/usr/bin/env python3
"""Local server for a project's build log page and its "Your tasks" panel.

Serves the project root read-only on 127.0.0.1 and adds a small JSON API. With "network": "lan" in
buildlog.json (opt-in) it listens on every interface but answers only loopback, Tailscale and private-LAN
clients, and only Host names that are this machine's (plus "extra_hosts"); see references/reference.md.

  GET  /api/tasks        tasks from the task file merged with local state
                         (done flag, note, last send, replies, last note)
  POST /api/tasks/<id>   save done / note
  POST /api/send         log a message, then forward it HMAC-signed to the
                         webhook (or keep it in the local inbox)
  GET  /api/messages     the general conversation: messages sent plus the
                         orchestrator's general replies
  GET  /api/version      mtimes of the page and the task file (auto-refresh)
  GET  /api/review/items the review hub's items (review_items, default
                         docs/review/items.json, written by review/review.py)
                         merged with the person's answers, snoozes and resolved
                         comment threads; ?id=ITEM returns one item
  POST /api/review/answer
                         an answer from the hub: approve / needs_changes /
                         reject / choice / pick / done / read / comment, with an
                         optional comment and a line or time anchor, is logged
                         and forwarded like a message. snooze / unsnooze /
                         resolve / unresolve are kept in state and the local
                         inbox only (agents see them; nothing wakes a thread)

Orchestrator replies live in the task file (written by buildlog_tasks.py
reply): per task in "replies", general ones in "general_replies", each
{at, text}. Each task also carries "last_note", the user's latest sent note.

Settings come from buildlog.json next to this script (name, port, root, page,
tasks, signature_header). State lives in ~/.local/state/<name>/ (state.json,
inbox.jsonl, read.json). The webhook URL and HMAC secret are read from
~/.config/<name>/webhook.json on every send and never appear in responses,
logs or state files. Undelivered messages stay queued and are retried on every
send and every few minutes. With "delivery": "local" in webhook.json nothing
is forwarded: messages wait in the inbox for `buildlog_tasks.py inbox`.

Python standard library only. Run it with: python3 -I buildlog_server.py
"""
import argparse
import base64
import collections
import datetime
import hashlib
import hmac
import http.server
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HOST = "127.0.0.1"
# "network": "lan" in buildlog.json: listen on every interface but answer only these clients (loopback, Tailscale,
# private LAN ranges). Anything else is refused before a byte of the request is read.
LAN_NETWORKS = ("127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48",
                "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fe80::/10")
DEFAULT_PORT = 33650
WEBHOOK_TIMEOUT = 5
RETRY_SECONDS = 180
MAX_BODY = 64 * 1024
MAX_NOTE = 8000
MAX_THREAD = 50
EVENT = "buildlog_message"
KINDS = ("task_update", "message", "test", "review")
REVIEW_DECISIONS = ("approve", "needs_changes", "reject", "choice", "pick", "done", "read", "comment",
                    "snooze", "unsnooze", "resolve", "unresolve")
QUIET_DECISIONS = ("snooze", "unsnooze", "resolve", "unresolve")    # recorded, never forwarded
MAX_ANSWERS = 60                 # answers kept per item in state
DELIVERED = ("sent", "local", "read")      # statuses that count as handed over
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
MSG_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SETTINGS_FILE = "buildlog.json"

TEXT = "text/plain; charset=utf-8"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".json": "application/json",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".ico": "image/x-icon", ".mp4": "video/mp4", ".webm": "video/webm",
    ".m4v": "video/mp4", ".mov": "video/quicktime", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/ogg", ".flac": "audio/flac",
    ".wasm": "application/wasm", ".pdf": "application/pdf",
}
for _ext in (".md", ".txt", ".log", ".py", ".sh", ".c", ".h", ".cc", ".cpp", ".hpp",
             ".inc", ".cmake", ".toml", ".yml", ".yaml", ".csv", ".tsv", ".glsl", ".go",
             ".rs", ".s", ".asm", ".patch", ".diff", ".ini", ".cfg", ".mk", ".ld", ".ts"):
    CONTENT_TYPES[_ext] = TEXT


def load_settings(path=None):
    """Project settings from buildlog.json next to this script. Paths resolved against the root."""
    here = Path(__file__).resolve().parent
    path = Path(path) if path else here / SETTINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise SystemExit("%s: expected a JSON object" % path)
    s = {"name": "buildlog", "title": "build log", "root": "..", "port": DEFAULT_PORT,
         "page": "docs/buildlog.html", "tasks": "docs/buildlog-tasks.json",
         "signature_header": "x-buildlog-signature",
         "review_items": "docs/review/items.json", "review_page": "docs/review/index.html"}
    s.update(data)
    s["root"] = str((path.parent / s["root"]).resolve())
    s["settings_path"] = str(path)
    return s


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_sec():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(text):
    """Sortable time for an ISO string (seconds or milliseconds, with Z); unknown sorts first."""
    try:
        when = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)


def _line_fp(text):
    """A line's content fingerprint: FNV-1a over its first 80 lowercase letters and digits (review/md_render.py line_fp)."""
    t = re.sub(r"[^a-z0-9]+", "", str(text or "").lower())[:80]
    if not t:
        return ""
    h = 0x811c9dc5
    for ch in t:
        h = ((h ^ ord(ch)) * 0x01000193) & 0xffffffff
    return "%08x" % h


def write_atomic(path, data, mode=0o600):
    """Write bytes next to `path`, then rename over it."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        for i in range(40):                   # Windows: a reader may briefly hold the target open
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if os.name != "nt" or i == 39:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class BuildlogApp:
    """All state and logic; the HTTP handler is a thin layer on top."""

    def __init__(self, root, state_dir, config_path, log=None, webhook_timeout=WEBHOOK_TIMEOUT,
                 page="docs/buildlog.html", tasks="docs/buildlog-tasks.json", name="buildlog",
                 signature_header="x-buildlog-signature", review_items="docs/review/items.json",
                 review_page="docs/review/index.html"):
        self.root = Path(root).resolve()
        self.review_rel = Path(review_items)
        self.review_page_rel = Path(review_page)
        self.page_rel = Path(page)
        self.tasks_rel = Path(tasks)
        self.name = name
        self.user_agent = "%s/1" % name      # the T3 relay refuses urllib's default User-Agent
        self.default_header = signature_header
        self.state_dir = Path(state_dir)
        self.config_path = Path(config_path)
        self.webhook_timeout = webhook_timeout
        self.lock = threading.RLock()
        self.flush_lock = threading.Lock()
        self.logstream = log if log is not None else sys.stderr
        self.allowed_hosts = set()
        self.allowed_origins = set()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path = self.state_dir / "state.json"
        self.inbox_path = self.state_dir / "inbox.jsonl"
        self.read_path = self.state_dir / "read.json"
        self.state = self._load_state()
        self.msgs = {}                       # id -> {"payload", "status"}
        self.pending = collections.OrderedDict()  # id -> {"attempts", "error"}
        self.last_error = None               # why the latest delivery attempt failed
        self._load_inbox()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    # ---- logging -------------------------------------------------------

    def _scrub(self, text):
        cfg = self._read_webhook()
        for secret in (cfg or {}).get("_secrets", ()):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text

    def log(self, text):
        line = "%s %s\n" % (now_sec(), self._scrub(str(text)))
        try:
            self.logstream.write(line)
            self.logstream.flush()
        except (OSError, ValueError):
            pass

    def set_origin(self, port, names=()):
        """Host headers (and Origins) the server answers to: loopback, plus `names` (this machine's LAN / Tailscale
        addresses and host names) when it listens on the network. A Host outside this set is refused (DNS rebinding)."""
        hosts = {"127.0.0.1", "localhost"} | {n.lower().rstrip(".") for n in names if n}
        self.allowed_hosts = {("[%s]" % h if ":" in h else h) + ":%d" % port for h in hosts}
        self.allowed_origins = {"http://" + h for h in self.allowed_hosts}

    # ---- webhook config ------------------------------------------------

    def _read_config_file(self):
        try:
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return cfg if isinstance(cfg, dict) else {}

    def delivery_mode(self):
        return "local" if self._read_config_file().get("delivery") == "local" else "webhook"

    def _read_webhook(self):
        """Config dict with url/secret/header/prefix, or None. Read each time."""
        cfg = self._read_config_file()
        try:
            url, secret = str(cfg["url"]), str(cfg["secret"])
        except (KeyError, TypeError):
            return None
        if not url or not secret:
            return None
        try:
            parts = urllib.parse.urlsplit(url)
            pairs = urllib.parse.parse_qsl(parts.query)
        except ValueError:
            return None
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return None
        pieces = {url, secret, parts.query, parts.path.strip("/"), parts.netloc}
        pieces.update(parts.path.split("/"))
        pieces.update(v for _, v in pairs)
        # Longest first, so a whole URL is replaced before its parts. Short bits stay out.
        secrets = sorted((p for p in pieces if len(p) >= 8), key=len, reverse=True)
        return {"url": url, "secret": secret, "_secrets": secrets,
                "header": str(cfg.get("header") or self.default_header),
                "prefix": str(cfg.get("prefix", "sha256="))}

    # ---- state / inbox -------------------------------------------------

    def _load_state(self):
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("tasks"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "tasks": {}}

    def _save_state(self):
        write_atomic(self.state_path, json.dumps(self.state, indent=1, sort_keys=True).encode() + b"\n")

    def _append_inbox(self, record):
        line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        fd = os.open(self.inbox_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def _load_inbox(self):
        try:
            lines = self.inbox_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("type") == "message" and isinstance(rec.get("payload"), dict):
                payload = rec["payload"]
                mid = payload.get("id")
                if mid:
                    self.msgs[mid] = {"payload": payload, "status": "queued"}
                    self.pending[mid] = {"attempts": 0, "error": None}
            elif rec.get("type") == "delivered" and rec.get("id") in self.msgs:
                via = rec.get("via")
                self.msgs[rec["id"]]["status"] = via if via in ("local", "read") else "sent"
                self.pending.pop(rec["id"], None)

    def _read_ids(self):
        """Message ids an agent already handled from the local inbox (buildlog_tasks.py inbox --mark-read)."""
        try:
            data = json.loads(self.read_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        ids = data.get("read") if isinstance(data, dict) else None
        return set(i for i in ids if isinstance(i, str)) if isinstance(ids, list) else set()

    # ---- tasks ---------------------------------------------------------

    def _read_tasks_data(self):
        path = self.root / self.tasks_rel
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"tasks": []}
        except (OSError, ValueError):
            raise ApiError(503, "task file unreadable")
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise ApiError(503, "task file has no tasks list")
        return data

    def _read_tasks_file(self):
        tasks = self._read_tasks_data()["tasks"]
        return [t for t in tasks if isinstance(t, dict) and isinstance(t.get("id"), str)]

    @staticmethod
    def _clean_replies(value):
        """Orchestrator replies from the task file, oldest first; anything malformed is skipped."""
        out = []
        for r in value if isinstance(value, list) else []:
            if isinstance(r, dict) and isinstance(r.get("text"), str) and r["text"].strip() and isinstance(r.get("at"), str):
                out.append({"at": r["at"], "text": r["text"]})
        out.sort(key=lambda r: _ts(r["at"]))
        return out

    def _last_notes(self):
        """Latest non-empty note the person sent for each task, from the inbox log."""
        notes = {}
        for mid, m in self.msgs.items():
            p = m["payload"]
            if p.get("kind") == "task_update" and str(p.get("note", "")).strip():
                notes[p.get("task_id")] = {"id": mid, "at": p["sent_at"], "text": p["note"], "status": m["status"]}
        return notes

    @staticmethod
    def _merge(task, st):
        st = st or {}
        done = task.get("status") == "done"
        # The person's tick wins unless the file changed after it (an agent used the CLI).
        if "done" in st and st.get("done_updated", "") >= (task.get("updated") or ""):
            done = bool(st["done"])
        merged = dict(task)
        merged.update({"done": done, "note": st.get("note", ""), "updated": st.get("updated"),
                       "last_send": st.get("last_send"), "replies": BuildlogApp._clean_replies(task.get("replies"))})
        return merged

    def tasks_payload(self):
        file_tasks = self._read_tasks_file()
        with self.lock:
            notes = self._last_notes()
            tasks = []
            for t in file_tasks:
                merged = self._merge(t, self.state["tasks"].get(t["id"]))
                merged["last_note"] = notes.get(t["id"])
                tasks.append(merged)
            return {"tasks": tasks, "pending": len(self.pending), "delivery": self.delivery_mode()}

    def messages_payload(self):
        """The general conversation: messages the person sent (not task notes, not tests)
        and the orchestrator's general replies, oldest first, last MAX_THREAD items."""
        try:
            replies = self._clean_replies(self._read_tasks_data().get("general_replies"))
        except ApiError:
            replies = []                      # the thread still shows what was sent
        items = [{"type": "orchestrator", "at": r["at"], "text": r["text"]} for r in replies]
        with self.lock:
            for mid, m in self.msgs.items():
                p = m["payload"]
                if p.get("kind") == "message":
                    items.append({"type": "user", "id": mid, "at": p["sent_at"], "text": p.get("note", ""),
                                  "status": m["status"]})
        items.sort(key=lambda i: _ts(i["at"]))      # stable: ties keep inbox order
        return {"messages": items[-MAX_THREAD:], "total": len(items)}

    def save_task(self, task_id, body):
        file_tasks = {t["id"]: t for t in self._read_tasks_file()}
        if task_id not in file_tasks:
            raise ApiError(404, "unknown task")
        if "done" in body and not isinstance(body["done"], bool):
            raise ApiError(400, "done must be true or false")
        if "note" in body and not isinstance(body["note"], str):
            raise ApiError(400, "note must be text")
        if "note" in body and len(body["note"]) > MAX_NOTE:
            raise ApiError(400, "note too long")
        if "done" not in body and "note" not in body:
            raise ApiError(400, "nothing to save")
        with self.lock:
            st = self.state["tasks"].setdefault(task_id, {})
            stamp = now_iso()
            if "done" in body:
                st["done"], st["done_updated"] = body["done"], stamp
            if "note" in body:
                st["note"] = body["note"]
            st["updated"] = stamp
            self._save_state()
            return self._merge(file_tasks[task_id], st)

    def version(self):
        def mtime(rel):
            try:
                return (self.root / rel).stat().st_mtime
            except OSError:
                return None
        return {"mtime": mtime(self.page_rel), "tasks_mtime": mtime(self.tasks_rel), "name": self.name,
                "review_mtime": mtime(self.review_rel), "review_page_mtime": mtime(self.review_page_rel)}

    # ---- review hub ----------------------------------------------------

    def _review_state(self):
        return self.state.setdefault("review", {})

    def _read_review_items(self):
        path = self.root / self.review_rel
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            raise ApiError(503, "review items unreadable")
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ApiError(503, "review items file has no items list")
        return [i for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)]

    @staticmethod
    def _newer(a, b):
        """The later of two {at, ...} records; either may be missing."""
        if not isinstance(a, dict):
            return b if isinstance(b, dict) else None
        if not isinstance(b, dict):
            return a
        return b if _ts(b.get("at")) >= _ts(a.get("at")) else a

    def review_payload(self, only=None):
        items = self._read_review_items()
        with self.lock:
            st = self._review_state()
            out = []
            for it in items:
                if only and it["id"] != only:
                    continue
                row = {k: v for k, v in it.items() if k != "on_answer"}
                rs = st.get(it["id"]) or {}
                answers = []
                for a in rs.get("answers") or []:
                    a = dict(a)
                    m = self.msgs.get(a.get("id"))
                    a["status"] = m["status"] if m else a.get("status", "queued")
                    answers.append(a)
                row["answers"] = answers
                latest = None
                for a in answers:                 # the latest real decision; comments and snoozes don't count
                    if a.get("decision") not in ("comment",) + QUIET_DECISIONS:
                        latest = a
                row["answer"] = latest
                # the agent's snooze (items.json, set by review.py) and the person's (state): the newer one wins
                snooze = self._newer(it.get("snooze"), rs.get("snooze"))
                row["snooze"] = snooze if snooze and not snooze.get("cleared") else None
                resolved = dict(it["resolved"]) if isinstance(it.get("resolved"), dict) else {}
                for key, rec in (rs.get("resolved") or {}).items():
                    resolved[key] = self._newer(resolved.get(key), rec)
                row["resolved"] = resolved
                out.append(row)
            if only and not out:
                raise ApiError(404, "unknown item")
            return {"items": out, "pending": len(self.pending), "delivery": self.delivery_mode(), "now": now_sec()}

    @staticmethod
    def _clean_anchor(anchor):
        if not isinstance(anchor, dict) or anchor.get("type") not in ("line", "time", "msg"):
            raise ApiError(400, "anchor must be {type: line|time|msg, ...}")
        clean = {"type": anchor["type"]}
        if anchor["type"] == "msg":                 # a note without a line or time: its own thread
            if not (isinstance(anchor.get("id"), str) and MSG_ID_RE.match(anchor["id"])):
                raise ApiError(400, "anchor.id must be a message id")
            clean["id"] = anchor["id"]
        elif anchor["type"] == "line":
            try:
                clean["line"] = int(anchor.get("line"))
            except (TypeError, ValueError):
                raise ApiError(400, "anchor.line must be a number")
            clean["doc"] = str(anchor.get("doc", ""))[:300]
        else:
            try:
                clean["time_s"] = round(float(anchor.get("time_s")), 2)
            except (TypeError, ValueError):
                raise ApiError(400, "anchor.time_s must be a number")
            clean["media"] = str(anchor.get("media", ""))[:300]
        clean["excerpt"] = str(anchor.get("excerpt", ""))[:300]
        if clean["type"] == "line":
            # the line's content too, so the note finds its line again after the document is rewritten
            # (review/md_render.py line_fp: the same fingerprint the pages and review.py compute)
            fp = anchor.get("fp")
            clean["fp"] = fp if isinstance(fp, str) and re.match(r"^[0-9a-f]{8}$", fp) else _line_fp(clean["excerpt"])
            for k in ("who", "block"):
                if isinstance(anchor.get(k), (str, int)) and str(anchor[k]).strip():
                    clean[k] = str(anchor[k])[:80]
        return clean

    @staticmethod
    def thread_key(anchor):
        """The comment thread an anchor belongs to: line:DOC#LINE or time:MEDIA@SECONDS (review.py uses the same)."""
        if anchor["type"] == "line":
            return "line:%s#%d" % (anchor.get("doc", ""), anchor["line"])
        if anchor["type"] == "msg":
            return "msg:%s" % anchor["id"]
        return "time:%s@%.1f" % (anchor.get("media", ""), anchor["time_s"])

    @staticmethod
    def _made_on(item, anchor, seen):
        """The version a note was made on, and why it is not the current one ('' when it is). The page says what it
        showed (seen: the item's rev and version, or the document's content hash); the item file says what is current
        (review.py writes version, rev, docs[].hash and superseded_by)."""
        current = {"item": item["id"], "version": str(item.get("version") or ""), "rev": str(item.get("rev") or "")}
        if anchor and anchor.get("type") == "line":
            current["doc_hash"] = next((str(d.get("hash") or "") for d in item.get("docs") or []
                                        if isinstance(d, dict) and d.get("path") == anchor.get("doc")), "")
        made = dict(current)
        claimed = {}
        if isinstance(seen, dict):
            for k in ("version", "rev", "doc_hash"):
                v = seen.get(k)
                if isinstance(v, str) and v and len(v) <= 60:
                    made[k] = claimed[k] = v
        why = ""
        if item.get("superseded_by"):
            why = "made on %s, which %s replaced" % (made.get("version") or item["id"], item["superseded_by"])
        elif claimed.get("doc_hash") and current.get("doc_hash") and claimed["doc_hash"] != current["doc_hash"]:
            why = "the page showed an older copy of the document"
        elif claimed.get("rev") and current["rev"] and claimed["rev"] != current["rev"]:
            why = "the page showed an older revision of the item"
        return made, why

    def review_answer(self, body):
        item_id = body.get("item_id")
        items = {i["id"]: i for i in self._read_review_items()}
        if not isinstance(item_id, str) or item_id not in items:
            raise ApiError(404, "unknown item")
        item = items[item_id]
        decision = body.get("decision")
        if decision not in REVIEW_DECISIONS:
            raise ApiError(400, "decision must be one of %s" % ", ".join(REVIEW_DECISIONS))
        comment = body.get("comment", "")
        if not isinstance(comment, str):
            raise ApiError(400, "comment must be text")
        if len(comment) > MAX_NOTE:
            raise ApiError(400, "comment too long")
        choice = body.get("choice")
        if decision in ("choice", "pick"):
            options = [c.get("id") for c in (item.get("choices") or []) + (item.get("candidates") or []) if isinstance(c, dict)]
            if not isinstance(choice, str) or choice not in options:
                raise ApiError(400, "choice must be one of the item's options")
        elif choice not in (None, ""):
            raise ApiError(400, "choice only goes with a choice or pick decision")
        if decision == "comment" and not comment.strip():
            raise ApiError(400, "comment is empty")
        anchor = body.get("anchor")
        if anchor is not None:
            anchor = self._clean_anchor(anchor)
        if decision in ("resolve", "unresolve") and anchor is None:
            raise ApiError(400, "resolve needs the anchor of the comment thread")
        until, later = None, False
        if decision == "snooze":
            later = body.get("later") is True
            if not later:
                until = body.get("until")
                if not isinstance(until, str) or _ts(until) == _ts(""):
                    raise ApiError(400, "snooze needs until (an ISO date or time) or later: true")
                until = _ts(until).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        mid = body.get("id")
        if mid is not None and not (isinstance(mid, str) and MSG_ID_RE.match(mid)):
            raise ApiError(400, "id must be 32 hex characters")
        quiet = decision in QUIET_DECISIONS
        with self.lock:
            if mid and mid in self.msgs:
                return self._answer_result(mid, item_id, flush=not quiet)
        payload = {"event": EVENT, "id": mid or uuid.uuid4().hex, "kind": "review",
                   "item_id": item_id, "item_title": str(item.get("title", "")), "item_kind": str(item.get("kind", "")),
                   "decision": decision, "choice": choice or "", "comment": comment, "anchor": anchor,
                   "task_id": "", "task_title": "", "done": "", "note": comment, "sent_at": now_sec()}
        record = {"id": payload["id"], "at": payload["sent_at"], "decision": decision, "choice": choice or "",
                  "comment": comment, "anchor": anchor, "status": "queued"}
        made, why = self._made_on(item, anchor, body.get("seen"))
        payload["made_on"] = record["made_on"] = made
        if why:
            payload["outdated"] = record["outdated"] = why
        if decision == "snooze":
            payload["until"] = record["until"] = until
            payload["later"] = record["later"] = later
        with self.lock:
            self._append_inbox({"type": "message", "payload": payload})
            self.msgs[payload["id"]] = {"payload": payload, "status": "queued"}
            self.pending[payload["id"]] = {"attempts": 0, "error": None}
            rs = self._review_state().setdefault(item_id, {"answers": []})
            rs["answers"] = (rs.get("answers") or [])[-(MAX_ANSWERS - 1):] + [record]
            if decision == "snooze":
                rs["snooze"] = {"until": until, "later": later, "at": payload["sent_at"], "by": "you"}
            elif decision == "unsnooze":
                rs["snooze"] = {"cleared": True, "at": payload["sent_at"], "by": "you"}
            elif decision in ("resolve", "unresolve"):
                rs.setdefault("resolved", {})[self.thread_key(anchor)] = {
                    "resolved": decision == "resolve", "at": payload["sent_at"], "by": "you"}
            if quiet:                             # agents find it in the local inbox; no webhook wake-up
                self._mark_delivered(payload["id"], None, "local")
            self._save_state()
        return self._answer_result(payload["id"], item_id, flush=not quiet)

    def _answer_result(self, mid, item_id, flush=True):
        res = self._send_result(mid, flush=flush)
        with self.lock:
            rs = self._review_state().get(item_id) or {}
            rec = next((dict(a) for a in rs.get("answers") or [] if a.get("id") == mid), None)
            if rec:
                rec["status"] = self.msgs[mid]["status"]
            res["answer"] = rec
        return res

    # ---- sending -------------------------------------------------------

    def send(self, body):
        kind = body.get("kind")
        if kind not in KINDS or kind == "review":
            raise ApiError(400, "kind must be task_update, message or test")
        note = body.get("note", "")
        if not isinstance(note, str):
            raise ApiError(400, "note must be text")
        if len(note) > MAX_NOTE:
            raise ApiError(400, "note too long")
        mid = body.get("id")
        if mid is not None and not (isinstance(mid, str) and MSG_ID_RE.match(mid)):
            raise ApiError(400, "id must be 32 hex characters")
        with self.lock:
            if mid and mid in self.msgs:   # a retry of the same click: report, don't add
                return self._send_result(mid, flush=True)

        task_id = title = done = ""
        if kind == "task_update":
            task_id = body.get("task_id")
            file_tasks = {t["id"]: t for t in self._read_tasks_file()}
            if not isinstance(task_id, str) or task_id not in file_tasks:
                raise ApiError(404, "unknown task")
            title = str(file_tasks[task_id].get("title", ""))
            d = body.get("done")
            if isinstance(d, str):
                d = {"true": True, "false": False}.get(d.lower())
            if not isinstance(d, bool):
                raise ApiError(400, "done must be true or false")
            done = "true" if d else "false"
        elif kind == "message" and not note.strip():
            raise ApiError(400, "message is empty")

        payload = {"event": EVENT, "id": mid or uuid.uuid4().hex, "kind": kind,
                   "task_id": task_id, "task_title": title, "done": done,
                   "note": note, "sent_at": now_sec()}
        with self.lock:
            self._append_inbox({"type": "message", "payload": payload})
            self.msgs[payload["id"]] = {"payload": payload, "status": "queued"}
            self.pending[payload["id"]] = {"attempts": 0, "error": None}
            if kind == "task_update":
                st = self.state["tasks"].setdefault(task_id, {})
                stamp = now_iso()
                st.update({"done": done == "true", "done_updated": stamp, "note": note, "updated": stamp,
                           "last_send": {"id": payload["id"], "at": payload["sent_at"], "status": "queued",
                                         "done": done, "note": note}})
                self._save_state()
        return self._send_result(payload["id"], flush=True)

    def _send_result(self, mid, flush):
        if flush:
            self.flush()
        with self.lock:
            m = self.msgs[mid]
            info = self.pending.get(mid) or {}
            ok = m["status"] in DELIVERED
            return {"ok": True, "id": mid, "delivered": ok, "status": m["status"],
                    "http_status": info.get("http") or m.get("http"),
                    "error": None if ok else (info.get("error") or self.last_error),
                    "pending": len(self.pending), "sent_at": m["payload"]["sent_at"]}

    def flush(self):
        """Try every undelivered message, oldest first. Stops at the first network failure."""
        with self.flush_lock:
            with self.lock:
                order = list(self.pending)
            if not order:
                return
            read = self._read_ids()
            local = self.delivery_mode() == "local"
            for mid in order:
                with self.lock:
                    if mid not in self.pending:
                        continue
                    payload = self.msgs[mid]["payload"]
                if mid in read:
                    ok, http_status, error, via = True, None, None, "read"
                elif local:
                    ok, http_status, error, via = True, None, None, "local"
                else:
                    ok, http_status, error = self._deliver(payload)
                    via = "webhook"
                with self.lock:
                    info = self.pending.get(mid)
                    if info is None:
                        continue
                    info["attempts"] += 1
                    info["http"], info["error"] = http_status, error
                    self.last_error = None if ok else error
                    if ok:
                        self._mark_delivered(mid, http_status, via)
                if ok:
                    self.log("message %s %s" % (mid, {"webhook": "delivered (HTTP %s)" % http_status,
                                                      "local": "kept in the local inbox",
                                                      "read": "already read from the local inbox"}[via]))
                else:
                    self.log("message %s queued: %s" % (mid, error))
                    if http_status is None:
                        break

    def _mark_delivered(self, mid, http_status, via="webhook"):
        status = via if via in ("local", "read") else "sent"
        self._append_inbox({"type": "delivered", "id": mid, "at": now_sec(), "http": http_status, "via": via})
        self.pending.pop(mid, None)
        m = self.msgs[mid]
        m["status"], m["http"] = status, http_status
        payload = m["payload"]
        st = self.state["tasks"].get(payload.get("task_id"))
        if st and (st.get("last_send") or {}).get("id") == mid:
            st["last_send"]["status"] = status
            self._save_state()

    def _deliver(self, payload):
        """POST one signed message. Returns (ok, http_status, short_error)."""
        cfg = self._read_webhook()
        if cfg is None:
            return False, None, "webhook not configured"
        # Sign the exact bytes that are sent. The key is the secret text itself (UTF-8).
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        sig = cfg["prefix"] + hmac.new(cfg["secret"].encode(), body, hashlib.sha256).hexdigest()
        try:
            req = urllib.request.Request(cfg["url"], data=body, method="POST", headers={
                "Content-Type": "application/json", cfg["header"]: sig,
                "User-Agent": self.user_agent})
            with self._opener.open(req, timeout=self.webhook_timeout) as resp:
                resp.read(1024)
                code = resp.status
            return (200 <= code < 300), code, (None if 200 <= code < 300 else "HTTP %d" % code)
        except urllib.error.HTTPError as e:
            e.close()
            return False, e.code, "HTTP %d" % e.code
        except TimeoutError:
            return False, None, "timeout"
        except urllib.error.URLError as e:
            return False, None, "timeout" if isinstance(e.reason, TimeoutError) else "connection failed"
        except OSError:
            return False, None, "connection failed"
        except Exception as e:       # never echo str(e): it can contain the URL
            return False, None, "webhook error (%s)" % type(e).__name__

    def retry_loop(self, stop, interval):
        while not stop.wait(interval):
            with self.lock:
                waiting = bool(self.pending)
            if waiting:
                try:
                    self.flush()
                except Exception as e:
                    self.log("retry failed (%s)" % type(e).__name__)

    # ---- static files --------------------------------------------------

    def resolve_static(self, url_path):
        """Map a URL path to a file under the root, or raise ApiError."""
        path = urllib.parse.unquote(urllib.parse.urlsplit(url_path).path)
        if "\x00" in path or "\\" in path:
            raise ApiError(400, "bad path")
        segs = [s for s in path.split("/") if s not in ("", ".")]
        if ".." in segs:
            raise ApiError(403, "path traversal refused")
        if any(s.startswith(".") for s in segs):
            raise ApiError(404, "not found")
        if os.name == "nt" and any(":" in s or s != s.rstrip(" .") for s in segs):
            raise ApiError(404, "not found")       # drive letters, NTFS streams, names Windows trims
        target = self.root.joinpath(*segs)
        try:
            real = target.resolve()
        except (OSError, RuntimeError):
            raise ApiError(404, "not found")
        if real != self.root and self.root not in real.parents:
            raise ApiError(403, "path traversal refused")
        if real.is_dir():
            real = real / "index.html"
        if not real.is_file():
            raise ApiError(404, "not found")
        return real


def byte_range(header, size):
    """(start, end) for one "bytes=A-B", "bytes=A-" or "bytes=-N" Range header; None sends the whole file;
    "unsatisfiable" for a range past the end."""
    m = re.match(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", header or "")
    if not m or not (m.group(1) or m.group(2)):
        return None
    if not m.group(1):
        n = int(m.group(2))
        return (max(0, size - n), size - 1) if n and size else "unsatisfiable"
    start = int(m.group(1))
    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    if start >= size or end < start:
        return "unsatisfiable"
    return start, end


def content_type(path):
    ext = path.suffix.lower()
    if ext in CONTENT_TYPES:
        return CONTENT_TYPES[ext]
    guess = mimetypes.guess_type(str(path))[0]
    if guess and guess.startswith("text/"):
        return guess + "; charset=utf-8"
    return guess or "application/octet-stream"


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "buildlog/1"
    sys_version = ""
    app = None   # set on the subclass created by make_server

    def version_string(self):
        return "%s/1" % self.app.name

    def log_message(self, fmt, *args):
        self.app.log("%s %s" % (self.address_string(), fmt % args))

    def log_request(self, code="-", size="-"):
        # Log the path without any query string.
        path = urllib.parse.urlsplit(self.path).path
        self.app.log("%s %s %s -> %s" % (self.address_string(), self.command, path, code))

    # ---- helpers -------------------------------------------------------

    def _headers(self, status, ctype, length, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")   # the review hub shows rendered docs in a frame
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()

    def _json(self, status, obj, extra=()):
        data = json.dumps(obj).encode()
        self._headers(status, "application/json", len(data), extra)
        if self.command != "HEAD":
            self.wfile.write(data)

    def _check_host(self):
        if self.headers.get("Host", "") not in self.app.allowed_hosts:
            raise ApiError(403, "bad host")

    def _check_csrf(self):
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(403, "content type must be application/json")
        if self.headers.get("X-Buildlog") != "1":
            raise ApiError(403, "missing X-Buildlog header")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.app.allowed_origins:
            raise ApiError(403, "bad origin")

    def _read_json(self, limit=MAX_BODY):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ApiError(411, "content length required")
        if length < 0:
            raise ApiError(400, "bad content length")
        if length > limit:
            self._body_read = True
            self.rfile.read(min(length, 1 << 20))   # drain, so the client can read the answer
            raise ApiError(413, "body too large")
        try:
            self._body_read = True
            body = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            raise ApiError(400, "body is not valid JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        return body

    def _drain(self):
        """Read a request body that was refused unread (CSRF, host, unknown path, method) before answering.
        Closing a socket with unread data resets the connection on Windows, and the client then
        sees a connection error instead of the 403."""
        if self.command in ("GET", "HEAD") or getattr(self, "_body_read", False):
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return
        if 0 < length <= 1 << 20:
            self.rfile.read(length)

    def _guard(self, fn):
        self._body_read = False
        try:
            self._check_host()
            fn()
        except ApiError as e:
            try:
                self._drain()
            except OSError:
                pass
            self._json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.app.log("internal error (%s)" % type(e).__name__)
            try:
                self._json(500, {"error": "internal error"})
            except OSError:
                pass

    # ---- methods -------------------------------------------------------

    def do_GET(self):
        self._guard(self._get)

    do_HEAD = do_GET

    def do_POST(self):
        self._guard(self._post)

    def _method_not_allowed(self):
        def refuse():
            self._drain()
            self._json(405, {"error": "method not allowed"}, [("Allow", "GET, HEAD, POST")])
        self._guard(refuse)

    # No CORS preflight is answered: OPTIONS gets a plain 405 without CORS headers.
    do_OPTIONS = do_PUT = do_DELETE = do_PATCH = _method_not_allowed

    def _get(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/tasks":
            return self._json(200, self.app.tasks_payload())
        if path == "/api/version":
            return self._json(200, self.app.version())
        if path == "/api/messages":
            return self._json(200, self.app.messages_payload())
        if path == "/api/review/items":
            only = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("id", [None])[0]
            return self._json(200, self.app.review_payload(only))
        if path.startswith("/api/"):
            raise ApiError(404, "not found")
        if path == "/" and not (self.app.root / "index.html").is_file():
            self.send_response(302)
            self.send_header("Location", "/" + self.app.page_rel.as_posix())
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        real = self.app.resolve_static(self.path)
        size = real.stat().st_size
        ctype = content_type(real)
        host = self.headers.get("Host", "")
        local = "127.0.0.1:%d" % self.server.server_address[1]
        if ctype.startswith("text/html") and host and host != local:
            # pages are generated with http://127.0.0.1:PORT links; a LAN / Tailscale browser gets its own host
            # (already checked against allowed_hosts) so the links between pages keep working
            body = real.read_bytes()
            for old in (b"http://" + local.encode(), b"http://localhost:%d" % self.server.server_address[1]):
                body = body.replace(old, b"http://" + host.encode("ascii", "replace"))
            self._headers(200, ctype, len(body), [("Last-Modified", self.date_time_string(real.stat().st_mtime))])
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        start, end = 0, size - 1
        status = 200
        extra = [("Last-Modified", self.date_time_string(real.stat().st_mtime)), ("Accept-Ranges", "bytes")]
        span = byte_range(self.headers.get("Range"), size)
        if span == "unsatisfiable":
            self._headers(416, TEXT, 0, [("Content-Range", "bytes */%d" % size)])
            return
        if span:                                  # players seek with ranges (one range; others get the whole file)
            start, end = span
            status = 206
            extra.append(("Content-Range", "bytes %d-%d/%d" % (start, end, size)))
        self._headers(status, ctype, end - start + 1, extra)
        if self.command == "HEAD":
            return
        with open(real, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _post(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/send":
            self._check_csrf()
            return self._json(200, self.app.send(self._read_json()))
        if path == "/api/review/answer":
            self._check_csrf()
            return self._json(200, self.app.review_answer(self._read_json()))
        if path.startswith("/api/tasks/"):
            self._check_csrf()
            task_id = urllib.parse.unquote(path[len("/api/tasks/"):])
            if not ID_RE.match(task_id):
                raise ApiError(404, "unknown task")
            return self._json(200, {"ok": True, "task": self.app.save_task(task_id, self._read_json())})
        if path.startswith("/api/"):
            raise ApiError(404, "not found")
        raise ApiError(405, "method not allowed")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    allowed_clients = ()                      # ip_network objects; empty = anyone who can reach the socket

    def verify_request(self, request, client_address):
        if not self.allowed_clients:
            return True
        try:
            ip = ipaddress.ip_address(client_address[0].split("%")[0])
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return any(ip in n for n in self.allowed_clients)


def machine_names():
    """This machine's names and addresses for the Host check: host name, its .local name, every address it
    resolves to, and the Tailscale MagicDNS name and addresses when the tailscale CLI is there."""
    names = set()
    host = socket.gethostname()
    names |= {host, host + ".local"}
    try:
        names |= {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except OSError:
        pass
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        me = json.loads(out.stdout or b"{}").get("Self") or {}
        if me.get("DNSName"):
            dns = me["DNSName"].rstrip(".")
            names |= {dns, dns.split(".")[0]}
        names |= set(me.get("TailscaleIPs") or ())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {n.split("%")[0] for n in names}


def make_server(root, state_dir, config_path, port=DEFAULT_PORT, log=None, webhook_timeout=WEBHOOK_TIMEOUT,
                network="local", extra_hosts=(), **app_kw):
    """network "local" (default): 127.0.0.1 only. "lan": every interface, answering only LAN_NETWORKS clients and
    this machine's own names (plus `extra_hosts`)."""
    app = BuildlogApp(root, state_dir, config_path, log=log, webhook_timeout=webhook_timeout, **app_kw)
    handler = type("BoundHandler", (Handler,), {"app": app})
    lan = network == "lan"
    server = Server(("0.0.0.0" if lan else HOST, port), handler)
    if lan:
        server.allowed_clients = tuple(ipaddress.ip_network(n) for n in LAN_NETWORKS)
    app.set_origin(server.server_address[1], (machine_names() if lan else set()) | set(extra_hosts))
    server.app = app
    return server


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--settings", help="settings file (default: buildlog.json next to this script)")
    ap.add_argument("--root", help="directory to serve (default: from the settings)")
    ap.add_argument("--port", type=int, help="port (default: from the settings; buildlog.json \"network\": \"lan\" listens beyond 127.0.0.1)")
    ap.add_argument("--state-dir", help="default: ~/.local/state/<name>")
    ap.add_argument("--config", help="webhook config (default: ~/.config/<name>/webhook.json)")
    ap.add_argument("--retry-seconds", type=float, default=RETRY_SECONDS)
    args = ap.parse_args(argv)

    s = load_settings(args.settings)
    root = args.root or s["root"]
    port = args.port if args.port is not None else int(s["port"])
    homes = s.get("_homes") or {}             # only set by the skill's own tests
    state_dir = args.state_dir or str(Path(homes.get("state_home") or Path.home() / ".local/state") / s["name"])
    config = args.config or str(Path(homes.get("config_home") or Path.home() / ".config") / s["name"] / "webhook.json")
    log = None
    if sys.stderr is None:                    # pythonw (the Windows logon task): log to a file in the state folder
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        log_path = Path(state_dir) / "server.log"
        if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
            os.replace(str(log_path), str(log_path) + ".1")
        log = sys.stderr = sys.stdout = open(log_path, "a", encoding="utf-8", buffering=1)
    server = make_server(root, state_dir, config, port, log=log, page=s["page"], tasks=s["tasks"], name=s["name"],
                         signature_header=s["signature_header"], review_items=s["review_items"],
                         review_page=s["review_page"], network=s.get("network", "local"),
                         extra_hosts=s.get("extra_hosts", ()))
    app = server.app
    stop = threading.Event()
    threading.Thread(target=app.retry_loop, args=(stop, args.retry_seconds), daemon=True).start()

    def quit_(signum, frame):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, quit_)
    signal.signal(signal.SIGINT, quit_)
    app.log("serving %s on http://%s:%d/%s (%s delivery, %d message(s) waiting)" % (
        app.root, server.server_address[0], server.server_address[1], app.page_rel.as_posix(), app.delivery_mode(),
        len(app.pending)))
    if s.get("network") == "lan":
        app.log("network: lan; answering %s" % ", ".join(sorted(h for h in app.allowed_hosts if not h.startswith("["))))
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
