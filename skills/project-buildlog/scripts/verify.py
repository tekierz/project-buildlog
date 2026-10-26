#!/usr/bin/env python3
"""End-to-end checks for the build log + "Your tasks" + webhook system in a project.

  verify.py --project DIR              files, tests, fake-T3 round trip, installed service, CSRF
  verify.py --project DIR --real       also: a signed test message through the installed service
                                       to the real T3 webhook, plus unsigned and forged requests
                                       sent straight to it (they must not start a run)

The fake T3 webhook checks what T3 checks: the HMAC-SHA256 signature over the
exact raw body (header and "sha256=" prefix from the settings, hex digest),
the body.event filter and the body.id replay key. The fake round trip runs a
second, temporary server from the project's own buildlog_server.py with a
temporary key and state, so the real inbox, state and webhook are not touched.

Prints PASS / FAIL / NOTE lines and exits 1 if anything failed. It never
prints the webhook URL or the secret. Python standard library only.
"""
import argparse
import hashlib
import hmac
import http.client
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

EVENT = "buildlog_message"
RESULTS = {"pass": 0, "fail": 0, "note": 0}
SCRUB = []


def scrub(text):
    text = str(text)
    for s in sorted(SCRUB, key=len, reverse=True):
        if s:
            text = text.replace(s, "[redacted]")
    return text


def check(cond, what, detail=""):
    """One result line. The detail is only shown when the check fails."""
    RESULTS["pass" if cond else "fail"] += 1
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", scrub(what), ("  (%s)" % scrub(detail)) if detail and not cond else ""))
    return cond


def note(what):
    RESULTS["note"] += 1
    print("  NOTE  %s" % scrub(what))


def section(title):
    print("\n== %s" % title)


# ---- project ------------------------------------------------------------------------

def find_settings(project, explicit=None):
    if explicit:
        return Path(explicit).resolve()
    root = Path(project).resolve()
    p = root / "tools/buildlog/buildlog.json"
    if p.is_file():
        return p
    for cand in sorted(root.glob("**/buildlog.json")):
        if ".git" in cand.parts:
            continue
        try:
            if json.loads(cand.read_text()).get("generator") == "project-buildlog":
                return cand
        except (OSError, ValueError, AttributeError):
            continue
    raise SystemExit("no buildlog.json under %s: run setup.py init first" % root)


class Project:
    def __init__(self, settings_file):
        self.settings_file = settings_file
        self.s = json.loads(settings_file.read_text())
        self.tools = settings_file.parent
        self.root = (self.tools / self.s.get("root", "../..")).resolve()
        self.name = self.s["name"]
        self.port = int(self.s["port"])
        self.page = self.root / self.s["page"]
        self.tasks = self.root / self.s["tasks"]
        self.service = self.s.get("service", self.name)
        self.header = self.s.get("signature_header", "x-buildlog-signature")
        homes = self.s.get("_homes") or {}
        self.config = Path(homes.get("config_home") or Path.home() / ".config") / self.name / "webhook.json"
        self.state = Path(homes.get("state_home") or Path.home() / ".local/state") / self.name

    def load_module(self, name):
        spec = importlib.util.spec_from_file_location("bl_" + name, self.tools / ("%s.py" % name))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def config_data(self):
        try:
            d = json.loads(self.config.read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}


# ---- a fake T3 webhook -----------------------------------------------------------------

class FakeT3:
    """Accepts a request only with a valid signature, the right event and a new body.id (like T3)."""

    def __init__(self, secret, header, prefix="sha256=", port=0, verbose=False):
        self.secret, self.header, self.prefix = secret, header.lower(), prefix
        self.runs, self.rejected, self.filtered, self.duplicates, self.raw = [], [], [], [], []
        self.seen = set()
        self.mode = "ok"
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                sig = self.headers.get(fake.header, "")
                want = fake.prefix + hmac.new(fake.secret.encode(), body, hashlib.sha256).hexdigest()
                rec = {"body": body, "headers": {k.lower(): v for k, v in self.headers.items()}}
                fake.raw.append(rec)
                if fake.mode == "down":
                    code, out = 503, {"error": "down"}
                elif not sig or not hmac.compare_digest(sig, want):
                    fake.rejected.append(rec)
                    code, out = 401, {"error": "bad signature"}
                else:
                    try:
                        data = json.loads(body)
                    except ValueError:
                        data = {}
                    rec["json"] = data
                    if not isinstance(data, dict) or data.get("event") != EVENT:
                        fake.filtered.append(rec)
                        code, out = 202, {"status": "filtered"}
                    elif data.get("id") in fake.seen:
                        fake.duplicates.append(rec)
                        code, out = 202, {"status": "duplicate"}
                    else:
                        fake.seen.add(data.get("id"))
                        fake.runs.append(rec)
                        code, out = 202, {"status": "accepted"}
                if verbose:
                    j = rec.get("json") or {}
                    print("%s fake T3: HTTP %d %s kind=%s id=%s note=%r" % (time.strftime("%H:%M:%S"), code,
                          out.get("status") or out.get("error"), j.get("kind"), j.get("id"), str(j.get("note", ""))[:80]),
                          flush=True)
                raw = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d/hook/%s" % (self.httpd.server_address[1], uuid.uuid4().hex)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ---- HTTP helpers ---------------------------------------------------------------------------

def request(port, method, path, body=None, headers=None, host=None, timeout=10):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        hdrs = {"Host": host or "127.0.0.1:%d" % port}
        hdrs.update(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            hdrs.setdefault("Content-Length", str(len(data)))
        for k, v in hdrs.items():
            conn.putheader(k, v)
        conn.endheaders(data)
        r = conn.getresponse()
        raw = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, raw
    finally:
        conn.close()


def api_post(port, path, body, extra=None):
    h = {"Content-Type": "application/json", "X-Buildlog": "1", "Origin": "http://127.0.0.1:%d" % port}
    h.update(extra or {})
    status, _, raw = request(port, "POST", path, body, h)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def post_direct(url, body, headers, timeout=15):
    """POST raw bytes to a URL without following redirects. Returns (status or None, short error)."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with opener.open(req, timeout=timeout) as r:
            r.read(2048)
            return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError) as e:
        return None, type(e).__name__


def run(cmd, cwd=None, timeout=600):
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)


# ---- checks ---------------------------------------------------------------------------------

def check_files(p):
    section("Files")
    html = p.page.read_text(encoding="utf-8") if p.page.exists() else ""
    check(bool(html), "page exists: %s" % p.page.relative_to(p.root))
    if html:
        for m in ("UPDATED", "TASKS", "TASKS-SNAPSHOT"):
            check(html.count("<!-- %s:START -->" % m) == 1 and html.count("<!-- %s:END -->" % m) == 1,
                  "page has one %s START/END pair" % m)
        head = html[:html.index("<style>")] if "<style>" in html else html
        check(head.count("<!--") == 1 and head.count("-->") == 1,
              "editing-rules comment is a single comment (no nested comment that leaks text)")
        check('http-equiv="refresh"' not in html, "no meta refresh (it would wipe typed drafts)")
        tasks_mod = p.load_module("buildlog_tasks")
        try:
            data = tasks_mod.load(p.tasks)
            check(True, "task file is valid: %d task(s)" % len(data["tasks"]))
            check(tasks_mod.snapshot_json(data) in html, "page snapshot matches the task file",
                  "run: python3 -I %s sync" % (p.tools / "buildlog_tasks.py"))
        except SystemExit as e:
            check(False, "task file is valid", str(e))
    cfg = p.config_data()
    check(p.config.exists(), "private config exists: %s" % p.config)
    if p.config.exists():
        check(p.config.stat().st_mode & 0o777 == 0o600, "config mode is 600", oct(p.config.stat().st_mode & 0o777))
        check(p.config.parent.stat().st_mode & 0o077 == 0, "config folder is private (700)")
        sec = str(cfg.get("secret", ""))
        check(bool(re.fullmatch(r"[0-9a-f]{64}", sec)), "secret is 64 hex characters (32 random bytes)")
        inside = run(["git", "-C", str(p.config.parent), "rev-parse", "--show-toplevel"])
        if inside.returncode == 0:
            note("the config folder is inside a git work tree (%s): it must be ignored there" % inside.stdout.strip())
        else:
            check(True, "config is outside every git work tree")
    in_git = run(["git", "-C", str(p.root), "rev-parse"]).returncode == 0
    if in_git:
        probe = [str(p.root / "webhook.json"), str(p.root / "docs" / "webhook.json")]
        ign = all(run(["git", "-C", str(p.root), "check-ignore", "-q", "--no-index", x]).returncode == 0 for x in probe)
        check(ign, ".gitignore guard ignores webhook.json anywhere in the repo")
        needles = [x for x in (cfg.get("secret"), cfg.get("url")) if x]
        parts = urllib.parse.urlsplit(cfg.get("url", "")) if cfg.get("url") else None
        if parts:
            needles += [seg for seg in parts.path.split("/") if len(seg) >= 16]
        if needles:
            fd, pat = tempfile.mkstemp(prefix="buildlog-verify.")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write("\n".join(needles) + "\n")
                hits = run(["git", "-C", str(p.root), "grep", "-l", "-I", "-F", "-f", pat]).stdout.split()
                hits += run(["git", "-C", str(p.root), "grep", "-l", "-I", "-F", "-f", pat, "--untracked"]).stdout.split()
            finally:
                os.unlink(pat)
            check(not hits, "the secret and webhook URL appear in no file of the repo", ", ".join(sorted(set(hits))))
    else:
        note("not a git repository: skipped the .gitignore and leak checks")


def check_tests(p):
    section("Unit tests")
    for t in sorted((p.tools / "tests").glob("test_*.py")):
        r = run([sys.executable, "-I", str(t)], cwd=p.root)
        tail = [l for l in (r.stderr or "").splitlines() if l.startswith(("Ran ", "OK", "FAILED"))]
        check(r.returncode == 0, "%s: %s" % (t.name, " ".join(tail[-2:]) or "exit %d" % r.returncode))
        if r.returncode != 0:
            print("\n".join("        " + l for l in (r.stderr or "").splitlines()[-25:]))


def check_fake_roundtrip(p):
    section("Round trip through a fake T3 webhook (temporary server, key and state)")
    secret = secrets.token_hex(32)
    fake = FakeT3(secret, p.header)
    tmp = tempfile.TemporaryDirectory(prefix="buildlog-verify.")
    try:
        cfg = Path(tmp.name) / "webhook.json"
        cfg.write_text(json.dumps({"url": fake.url, "secret": secret, "header": p.header, "prefix": "sha256="}))
        os.chmod(cfg, 0o600)
        bs = p.load_module("buildlog_server")
        log = io.StringIO()
        server = bs.make_server(str(p.root), Path(tmp.name) / "state", str(cfg), port=0, log=log, webhook_timeout=3,
                                page=p.s["page"], tasks=p.s["tasks"], name=p.name, signature_header=p.header)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            st, data = api_post(port, "/api/send", {"kind": "test", "note": "verify: signed test"})
            got = fake.runs[-1] if fake.runs else None
            check(st == 200 and data and data.get("delivered") and got is not None,
                  "signed test message is accepted by the fake T3 (signature, filter, new id)",
                  "HTTP %s, %s" % (st, data and data.get("error")))
            if got:
                body = got["json"]
                check(sorted(body) == ["done", "event", "id", "kind", "note", "sent_at", "task_id", "task_title"],
                      "payload has exactly the fields the T3 prompt reads", ",".join(sorted(body)))
                check(body.get("event") == EVENT and re.fullmatch(r"[0-9a-f]{32}", body.get("id", "")) is not None,
                      "event is %s and id is 32 hex characters (the replay key)" % EVENT)
                ua = got["headers"].get("user-agent", "")
                check(ua == "%s/1" % p.name and "urllib" not in ua.lower(),
                      "User-Agent is %s/1, not urllib's default (the T3 relay refuses that)" % p.name, ua)
                check(got["headers"].get("content-type") == "application/json", "Content-Type is application/json")
            tasks = json.loads(request(port, "GET", "/api/tasks")[2]).get("tasks", [])
            if tasks:
                t = tasks[0]
                n = len(fake.runs)
                st, data = api_post(port, "/api/send", {"kind": "task_update", "task_id": t["id"], "done": True,
                                                        "note": "verify: task update"})
                ok = len(fake.runs) == n + 1 and fake.runs[-1]["json"].get("task_title") == t["title"]
                check(st == 200 and ok, "task update carries task_id, task_title and done", "HTTP %s" % st)
                if ok:
                    check(fake.runs[-1]["json"]["done"] == "true", "done arrives as the text \"true\"")
            else:
                note("no tasks in the task file: skipped the task update check")
            mid = uuid.uuid4().hex
            api_post(port, "/api/send", {"kind": "message", "note": "verify: once", "id": mid})
            n = len(fake.raw)
            api_post(port, "/api/send", {"kind": "message", "note": "verify: once", "id": mid})
            check(len(fake.raw) == n, "a repeated click (same id) is not sent twice")

            payload = {"event": EVENT, "id": uuid.uuid4().hex, "kind": "test", "task_id": "", "task_title": "",
                       "done": "", "note": "verify: forged", "sent_at": "2026-01-01T00:00:00Z"}
            raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
            base = {"Content-Type": "application/json", "User-Agent": "%s/1" % p.name}
            runs = len(fake.runs)
            s1, _ = post_direct(fake.url, raw, base)
            wrong = "sha256=" + hmac.new(secrets.token_hex(32).encode(), raw, hashlib.sha256).hexdigest()
            s2, _ = post_direct(fake.url, raw, dict(base, **{p.header: wrong}))
            good = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            s3, _ = post_direct(fake.url, raw.replace(b"forged", b"FORGED"), dict(base, **{p.header: good}))
            check((s1, s2, s3) == (401, 401, 401) and len(fake.runs) == runs,
                  "unsigned, wrong-key and tampered requests are rejected (HTTP %s %s %s)" % (s1, s2, s3))
            replay = fake.runs[0] if fake.runs else None
            if replay:
                s4, _ = post_direct(fake.url, replay["body"], dict(base, **{p.header: replay["headers"][p.header.lower()]}))
                check(len(fake.runs) == runs and fake.duplicates, "a replayed signed request does not start a second run",
                      "HTTP %s" % s4)
            other = dict(payload, event="something_else", id=uuid.uuid4().hex)
            raw2 = json.dumps(other, separators=(",", ":"), sort_keys=True).encode()
            post_direct(fake.url, raw2, dict(base, **{p.header: "sha256=" + hmac.new(secret.encode(), raw2, hashlib.sha256).hexdigest()}))
            check(len(fake.runs) == runs and fake.filtered, "a signed request with another event is filtered out")

            before = len(fake.raw)
            refusals = []
            body = {"kind": "message", "note": "verify: csrf"}
            refusals.append(request(port, "POST", "/api/send", body, {"Content-Type": "application/json"})[0])
            refusals.append(request(port, "POST", "/api/send", body, {"Content-Type": "text/plain", "X-Buildlog": "1"})[0])
            refusals.append(request(port, "POST", "/api/send", body, {"Content-Type": "application/json", "X-Buildlog": "1",
                                                                     "Origin": "http://evil.example"})[0])
            refusals.append(request(port, "POST", "/api/send", body, {"Content-Type": "application/json", "X-Buildlog": "1"},
                                    host="evil.example")[0])
            check(refusals == [403, 403, 403, 403] and len(fake.raw) == before,
                  "CSRF-bad requests (no X-Buildlog, form type, foreign Origin, foreign Host) are refused", str(refusals))

            fake.mode = "down"
            st, data = api_post(port, "/api/send", {"kind": "message", "note": "verify: while down"})
            queued = data and not data.get("delivered") and data.get("pending", 0) >= 1
            fake.mode = "ok"
            server.app.flush()
            check(queued and not server.app.pending and fake.runs[-1]["json"]["note"] == "verify: while down",
                  "a message sent while the webhook is down is queued, then delivered on retry")
            st, _, raw = request(port, "GET", "/api/messages")
            convo = json.loads(raw).get("messages", []) if st == 200 else []
            check(st == 200 and any(m.get("type") == "user" and m.get("text") == "verify: once" for m in convo)
                  and not any(m.get("text") == "verify: signed test" for m in convo),
                  "/api/messages lists the user's messages (not tests) for the conversation", "HTTP %s" % st)
            leaked = secret in log.getvalue() or fake.url in log.getvalue()
            state_text = "".join(f.read_text() for f in (Path(tmp.name) / "state").glob("*") if f.is_file())
            check(not leaked and secret not in state_text and fake.url not in state_text,
                  "the key and URL appear in no log or state file")
        finally:
            server.shutdown()
            server.server_close()
    finally:
        fake.stop()
        tmp.cleanup()


def check_service(p):
    section("Installed service %s on port %d" % (p.service, p.port))
    active = run(["systemctl", "--user", "is-active", "%s.service" % p.service]).stdout.strip()
    check(active == "active", "%s.service is active" % p.service, active)
    pid = run(["systemctl", "--user", "show", "-p", "MainPID", "--value", "%s.service" % p.service]).stdout.strip()
    owner = run(["ss", "-ltnpH", "sport = :%d" % p.port]).stdout
    check(bool(pid) and pid != "0" and ("pid=%s," % pid) in owner,
          "the port belongs to the service's own process (not an old ad-hoc server)", owner.strip() or "nothing listening")
    try:
        st, hdrs, raw = request(p.port, "GET", "/api/version")
        v = json.loads(raw)
        check(st == 200 and v.get("name") == p.name, "/api/version answers as %s" % p.name)
        check(hdrs.get("server", "").startswith(p.name), "Server header is %s/1" % p.name, hdrs.get("server"))
        st, hdrs, raw = request(p.port, "GET", "/" + p.s["page"])
        check(st == 200 and hdrs.get("cache-control") == "no-store" and raw == p.page.read_bytes(),
              "serves the page from disk with Cache-Control: no-store")
        st, _, raw = request(p.port, "GET", "/api/tasks")
        check(st == 200 and "tasks" in json.loads(raw), "/api/tasks answers")
        st, _, raw = request(p.port, "GET", "/api/messages")
        check(st == 200 and "messages" in json.loads(raw), "/api/messages answers (orchestrator replies)",
              "HTTP %s: the service runs an older server; re-run setup.py init to refresh it" % st)
        check(request(p.port, "GET", "/.git/config")[0] == 404, "dotfiles are not served")
        check(request(p.port, "GET", "/api/tasks", host="evil.example")[0] == 403, "a foreign Host header is refused")
        st = request(p.port, "POST", "/api/send", {"kind": "message", "note": "csrf"},
                     {"Content-Type": "text/plain", "Origin": "http://evil.example"})[0]
        check(st == 403, "a cross-site form POST to the live server is refused")
    except (OSError, ValueError) as e:
        check(False, "the service answers on 127.0.0.1:%d" % p.port, type(e).__name__)


def check_real(p, wait):
    section("Real T3 webhook")
    cfg = p.config_data()
    url, secret = cfg.get("url"), cfg.get("secret")
    SCRUB.extend(x for x in (url, secret) if x)
    if not check(bool(url and secret), "webhook URL and secret are stored", "run setup.py set-url"):
        return
    if cfg.get("delivery") == "local":
        check(False, "delivery is webhook", "delivery is local: run setup.py delivery webhook")
        return
    nonce = secrets.token_hex(4)
    st, data = api_post(p.port, "/api/send", {"kind": "test", "note": "buildlog verify %s: signed test from verify.py, "
                                                                     "please acknowledge" % nonce})
    delivered = bool(st == 200 and data and data.get("delivered"))
    check(delivered, "signed test message delivered through the installed service (HTTP %s)" % (data or {}).get("http_status"),
          "status %s, error %s" % ((data or {}).get("status"), (data or {}).get("error")))
    if not delivered and data:
        code = data.get("http_status")
        hint = {401: "T3 refused the signature: the secret in T3 differs from webhook.json (rotate or re-create).",
                403: "the relay refused the request: check the User-Agent and the URL.",
                404: "unknown URL: the task was deleted or the URL is wrong (set-url again)."}.get(code)
        note(hint or "not delivered: %s. It stays queued and is retried every 3 minutes." % data.get("error"))
    base = {"Content-Type": "application/json", "User-Agent": "%s/1" % p.name}
    results = []
    for label, key in (("unsigned", None), ("wrong key", secrets.token_hex(32))):
        payload = {"event": EVENT, "id": uuid.uuid4().hex, "kind": "test", "task_id": "", "task_title": "", "done": "",
                   "note": "buildlog verify %s: FORGED (%s), must not start a run" % (nonce, label),
                   "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        h = dict(base)
        if key:
            h[p.header] = "sha256=" + hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()
        status, err = post_direct(url, raw, h)
        results.append((label, status, err))
    for label, status, err in results:
        if status is not None and 400 <= status < 500:
            check(True, "%s request refused by T3 (HTTP %d)" % (label, status))
        elif status is not None and 200 <= status < 300:
            note("%s request got HTTP %d: the relay accepts first and checks later. Confirm that no run saying "
                 "'FORGED' appeared (list_scheduled_tasks shows the last run)." % (label, status))
        else:
            check(False, "%s request refused by T3" % label, "HTTP %s %s" % (status, err or ""))
    print("\n  Next, in the orchestrator thread: a run must arrive with the note 'buildlog verify %s: signed test',\n"
          "  and none with 'buildlog verify %s: FORGED'. Check within %d seconds." % (nonce, nonce, wait))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", default=".", help="project root (default: the current folder)")
    ap.add_argument("--settings", help="path to buildlog.json (default: found under the project)")
    ap.add_argument("--real", action="store_true", help="also test the real T3 webhook (sends one signed test)")
    ap.add_argument("--skip-tests", action="store_true", help="skip the unit tests")
    ap.add_argument("--no-service", action="store_true", help="skip the installed-service checks")
    ap.add_argument("--wait", type=int, default=120, help="seconds to tell the orchestrator to wait for the run")
    ap.add_argument("--serve-fake-t3", type=int, metavar="PORT",
                    help="only run a fake T3 webhook on this port with the project's secret, logging each request "
                         "(for testing without T3: set-url http://127.0.0.1:PORT/hook/x)")
    args = ap.parse_args(argv)
    p = Project(find_settings(args.project, args.settings))
    SCRUB.extend(x for x in (p.config_data().get("url"), p.config_data().get("secret")) if x)
    if args.serve_fake_t3:
        secret = p.config_data().get("secret")
        if not secret:
            raise SystemExit("no secret in %s" % p.config)
        fake = FakeT3(secret, p.header, port=args.serve_fake_t3, verbose=True)
        print("fake T3 webhook on http://127.0.0.1:%d/hook/<anything> (header %s); Ctrl-C to stop"
              % (args.serve_fake_t3, p.header), flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            fake.stop()
        return 0
    print("Verifying %s (%s, port %d)" % (p.root, p.name, p.port))
    check_files(p)
    if not args.skip_tests:
        check_tests(p)
    check_fake_roundtrip(p)
    if not args.no_service:
        check_service(p)
    if args.real:
        check_real(p, args.wait)
    print("\n%d passed, %d failed, %d notes" % (RESULTS["pass"], RESULTS["fail"], RESULTS["note"]))
    return 1 if RESULTS["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
