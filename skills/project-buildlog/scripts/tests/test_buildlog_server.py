#!/usr/bin/env python3
"""Tests for buildlog_server.py and buildlog_tasks.py.

Everything runs in temporary directories against a local fake webhook that
verifies the HMAC. Nothing touches the real state, config or network. When a
buildlog.json sits next to the scripts (a set-up project), the project's own
page and task file are checked too.
Run: python3 -I <tools dir>/tests/test_buildlog_server.py
"""
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
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

TOOLS = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bs = load("buildlog_server", "buildlog_server.py")
bt = load("buildlog_tasks", "buildlog_tasks.py")

NAME = "demo-buildlog"
SECRET = "s3cr3t-hmac-key-0123456789"
URL_TOKEN = "TOKEN-abc123"
URL_QUERY = "querysecret99"
SIG_HEADER = "x-demo-signature"
PAGE = "docs/buildlog.html"
TASKS_REL = "docs/buildlog-tasks.json"

TASKS = {"tasks": [
    {"id": "alpha", "kind": "decision", "title": "Approve alpha?", "details": "Because.", "how": "",
     "added": "2026-10-25T10:00:00.000Z", "status": "open", "done_at": None},
    {"id": "beta", "kind": "action", "title": "Plug in the pad", "details": "USB.", "how": "ls",
     "added": "2026-10-25T10:00:00.000Z", "status": "open", "done_at": None},
    {"id": "gamma", "kind": "action", "title": "Already finished", "details": "", "how": "",
     "added": "2026-10-24T10:00:00.000Z", "status": "done", "done_at": "2026-10-24T11:00:00.000Z"},
]}




def clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


class FakeWebhook:
    """Local webhook that checks the HMAC of every raw body it receives."""

    def __init__(self, secret=SECRET):
        self.secret = secret
        self.mode = "ok"            # ok | error (503) | reject (401) | hang
        self.received = []          # {"body", "headers", "sig_ok", "json"}
        self.release = threading.Event()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                sent = self.headers.get(SIG_HEADER, "")
                want = "sha256=" + hmac.new(fake.secret.encode(), body, hashlib.sha256).hexdigest()
                fake.received.append({"body": body, "headers": dict(self.headers), "path": self.path,
                                      "sig_ok": hmac.compare_digest(sent, want), "json": json.loads(body)})
                if fake.mode == "hang":
                    fake.release.wait(10)
                code = {"ok": 200, "error": 503, "reject": 401, "hang": 200}[fake.mode]
                data = b'{"ok":true}'
                try:
                    self.send_response(code)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass                # the client gave up (timeout test)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d/hook/%s?key=%s" % (self.port, URL_TOKEN, URL_QUERY)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def stop(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "root"
        (self.root / "docs").mkdir(parents=True)
        (self.root / PAGE).write_text("<!doctype html><title>x</title>hello page", encoding="utf-8", newline="\n")
        (self.root / "docs" / "notes.md").write_text("# notes\n", encoding="utf-8", newline="\n")
        (self.root / TASKS_REL).write_text(json.dumps(TASKS), encoding="utf-8", newline="\n")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("[core]\n", encoding="utf-8", newline="\n")
        self.outside = self.tmp / "outside-secret.txt"
        self.outside.write_text("TOP-SECRET-OUTSIDE-ROOT", encoding="utf-8", newline="\n")
        os.symlink(self.outside, self.root / "link-out.txt")
        self.state = self.tmp / "state"
        self.config = self.tmp / "webhook.json"
        self.hook = FakeWebhook()
        self.addCleanup(self.hook.stop)
        self.write_config()
        self.start()

    def write_config(self, url=None, secret=SECRET, **extra):
        cfg = {"url": self.hook.url if url is None else url, "secret": secret, "header": SIG_HEADER, "prefix": "sha256="}
        cfg.update(extra)
        self.config.write_text(json.dumps(cfg), encoding="utf-8", newline="\n")

    def start(self, timeout=2, **app_kw):
        self.logs = io.StringIO()
        self.server = bs.make_server(self.root, self.state, self.config, port=0, log=self.logs, webhook_timeout=timeout,
                                     page=PAGE, tasks=TASKS_REL, name=NAME, signature_header=SIG_HEADER, **app_kw)
        self.app = self.server.app
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self._stop, self.server)

    @staticmethod
    def _stop(server):
        server.shutdown()
        server.server_close()

    def restart(self, **kw):
        self._stop(self.server)
        self.start(**kw)

    # ---- raw HTTP helpers ----

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            hdrs = {"Host": host or "127.0.0.1:%d" % self.port}
            hdrs.update(headers or {})
            data = None
            if body is not None:
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                hdrs.setdefault("Content-Length", str(len(data)))
            for k, v in hdrs.items():
                conn.putheader(k, v)
            conn.endheaders(data)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, dict((k.lower(), v) for k, v in resp.getheaders()), raw
        finally:
            conn.close()

    def post(self, path, body, **extra):
        headers = {"Content-Type": "application/json", "X-Buildlog": "1",
                   "Origin": "http://127.0.0.1:%d" % self.port}
        headers.update(extra.pop("headers", {}))
        status, hdrs, raw = self.request("POST", path, body, headers, **extra)
        self.transcript.append(raw.decode("utf-8", "replace") + repr(hdrs))
        return status, json.loads(raw) if raw else None

    def get_json(self, path):
        status, hdrs, raw = self.request("GET", path)
        self.transcript.append(raw.decode("utf-8", "replace") + repr(hdrs))
        return status, json.loads(raw)

    @property
    def transcript(self):
        if not hasattr(self, "_transcript"):
            self._transcript = []
        return self._transcript

    def task(self, task_id):
        status, data = self.get_json("/api/tasks")
        self.assertEqual(status, 200)
        return [t for t in data["tasks"] if t["id"] == task_id][0]

    def send(self, **body):
        body.setdefault("kind", "message")
        body.setdefault("note", "hello")
        status, data = self.post("/api/send", body)
        self.assertEqual(status, 200, data)
        return data

    def inbox(self):
        p = self.state / "inbox.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def cli(self, *args, check=True):
        cmd = [sys.executable, "-I", str(TOOLS / "buildlog_tasks.py"), "--settings", str(self.tmp / "none.json")]
        r = subprocess.run(cmd + list(args), capture_output=True, text=True, encoding="utf-8", env=clean_env())
        if check:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r


class TaskTests(Base):
    def test_merge_and_persistence(self):
        status, data = self.get_json("/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual([t["id"] for t in data["tasks"]], ["alpha", "beta", "gamma"])
        done = {t["id"]: t["done"] for t in data["tasks"]}
        self.assertEqual(done, {"alpha": False, "beta": False, "gamma": True})
        self.assertEqual(data["delivery"], "webhook")
        self.assertEqual(self.task("alpha")["note"], "")

        status, out = self.post("/api/tasks/alpha", {"done": True, "note": "yes please"})
        self.assertEqual(status, 200)
        self.assertTrue(out["task"]["done"])
        self.assertEqual(out["task"]["note"], "yes please")
        self.assertTrue(out["task"]["updated"])
        self.post("/api/tasks/gamma", {"done": False})

        self.restart()            # state survives a restart
        self.assertTrue(self.task("alpha")["done"])
        self.assertEqual(self.task("alpha")["note"], "yes please")
        self.assertFalse(self.task("gamma")["done"])
        self.assertFalse(self.task("beta")["done"])
        saved = json.loads((self.state / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["tasks"]["alpha"]["note"], "yes please")
        if os.name != "nt":                            # Windows has no file modes (the folder's access list applies)
            self.assertEqual(oct((self.state / "state.json").stat().st_mode & 0o777), "0o600")

    def test_note_only_save_keeps_tick(self):
        self.post("/api/tasks/beta", {"done": True})
        self.post("/api/tasks/beta", {"note": "later"})
        t = self.task("beta")
        self.assertTrue(t["done"])
        self.assertEqual(t["note"], "later")

    def test_agent_change_after_tick_wins(self):
        f = ["--file", str(self.root / TASKS_REL), "--page", str(self.tmp / "none.html")]
        self.post("/api/tasks/alpha", {"done": True})
        self.assertTrue(self.task("alpha")["done"])
        time.sleep(0.01)
        # An agent finishes and then reopens the task through the CLI: the file is newer than the tick.
        self.cli(*f, "done", "alpha")
        self.cli(*f, "reopen", "alpha")
        self.assertFalse(self.task("alpha")["done"])
        time.sleep(0.01)
        self.post("/api/tasks/alpha", {"done": True})        # the person ticks again, newer still
        self.assertTrue(self.task("alpha")["done"])
        self.cli(*f, "done", "beta")                         # an agent closes a task nobody ticked
        self.assertTrue(self.task("beta")["done"])

    def test_bad_task_requests(self):
        self.assertEqual(self.post("/api/tasks/nope", {"done": True})[0], 404)
        self.assertEqual(self.post("/api/tasks/alpha", {"done": "yes"})[0], 400)
        self.assertEqual(self.post("/api/tasks/alpha", {"note": 5})[0], 400)
        self.assertEqual(self.post("/api/tasks/alpha", {})[0], 400)
        self.assertEqual(self.post("/api/tasks/alpha", {"note": "x" * 9000})[0], 400)
        self.assertEqual(self.post("/api/tasks/alpha", [1])[0], 400)
        self.assertEqual(self.post("/api/tasks/alpha", b"{not json")[0], 400)
        self.assertEqual(self.post("/api/tasks/..%2fx", {"done": True})[0], 404)
        self.assertFalse(self.task("alpha")["done"])

    def test_version_reports_mtimes_and_name(self):
        status, v = self.get_json("/api/version")
        self.assertEqual(status, 200)
        page = self.root / PAGE
        self.assertAlmostEqual(v["mtime"], page.stat().st_mtime, places=3)
        self.assertIsNotNone(v["tasks_mtime"])
        self.assertEqual(v["name"], NAME)
        os.utime(page, (1, 1000000))
        self.assertEqual(self.get_json("/api/version")[1]["mtime"], 1000000)


class CsrfTests(Base):
    def assert_rejected(self, status, **kw):
        self.assertEqual(status, 403, kw)
        self.assertEqual(self.task("alpha")["note"], "")
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.hook.received, [])

    def test_bad_origin(self):
        for origin in ("http://evil.example", "http://127.0.0.1:1", "null", "https://127.0.0.1:%d" % self.port,
                       "http://127.0.0.1.evil.example:%d" % self.port):
            s, _ = self.post("/api/tasks/alpha", {"note": "x"}, headers={"Origin": origin})
            self.assert_rejected(s, origin=origin)
            s, _ = self.post("/api/send", {"kind": "message", "note": "x"}, headers={"Origin": origin})
            self.assert_rejected(s, origin=origin)

    def test_missing_custom_header(self):
        for path, body in (("/api/tasks/alpha", {"note": "x"}), ("/api/send", {"kind": "message", "note": "x"})):
            s, _, _ = self.request("POST", path, body, {"Content-Type": "application/json",
                                                       "Origin": "http://127.0.0.1:%d" % self.port})
            self.assert_rejected(s)
            s, _, _ = self.request("POST", path, body, {"Content-Type": "application/json", "X-Buildlog": "0"})
            self.assert_rejected(s)

    def test_form_content_types(self):
        for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x", ""):
            headers = {"X-Buildlog": "1", "Origin": "http://127.0.0.1:%d" % self.port}
            if ctype:
                headers["Content-Type"] = ctype
            s, _, _ = self.request("POST", "/api/send", b'{"kind":"message","note":"x"}', headers)
            self.assert_rejected(s, ctype=ctype)

    def test_bad_host_is_refused_for_everything(self):
        for method, path in (("GET", "/" + PAGE), ("GET", "/api/tasks"), ("GET", "/api/version"), ("GET", "/")):
            s, _, raw = self.request(method, path, host="evil.example:%d" % self.port)
            self.assertEqual(s, 403)
            self.assertNotIn(b"hello page", raw)
        s, _ = self.post("/api/send", {"kind": "message", "note": "x"}, host="rebind.example")
        self.assert_rejected(s)

    def test_good_requests_accepted(self):
        # curl-style (no Origin), 127.0.0.1 and localhost origins, charset suffix.
        h = {"Content-Type": "application/json; charset=utf-8", "X-Buildlog": "1"}
        s, _, _ = self.request("POST", "/api/tasks/alpha", {"note": "a"}, h)
        self.assertEqual(s, 200)
        h["Origin"] = "http://localhost:%d" % self.port
        s, _, _ = self.request("POST", "/api/tasks/alpha", {"note": "b"}, dict(h), host="localhost:%d" % self.port)
        self.assertEqual(s, 200)
        self.assertEqual(self.task("alpha")["note"], "b")

    def test_no_cors_preflight(self):
        for path in ("/api/send", "/api/tasks/alpha", "/" + PAGE):
            s, hdrs, _ = self.request("OPTIONS", path, None, {
                "Origin": "http://evil.example", "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-buildlog"})
            self.assertEqual(s, 405)
            self.assertFalse([h for h in hdrs if h.startswith("access-control-")], hdrs)
        for method in ("PUT", "DELETE", "PATCH"):
            self.assertEqual(self.request(method, "/api/send", b"{}", {"Content-Type": "application/json", "X-Buildlog": "1"})[0], 405)
        _, hdrs, _ = self.request("GET", "/api/tasks", None, {"Origin": "http://evil.example"})
        self.assertFalse([h for h in hdrs if h.startswith("access-control-")])

    def test_post_to_static_path_refused(self):
        s, _, _ = self.request("POST", "/" + PAGE, b"{}", {"Content-Type": "application/json", "X-Buildlog": "1"})
        self.assertIn(s, (404, 405))


class StaticTests(Base):
    def raw_get(self, target):
        """Send a request line byte for byte, so nothing normalizes the path."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(("GET %s HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n" % (target, self.port)).encode())
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        return int(head.split()[1]), body

    def test_serves_files_with_types_and_no_store(self):
        s, hdrs, raw = self.request("GET", "/" + PAGE)
        self.assertEqual(s, 200)
        self.assertEqual(raw, b"<!doctype html><title>x</title>hello page")
        self.assertEqual(hdrs["content-type"], "text/html; charset=utf-8")
        self.assertEqual(hdrs["cache-control"], "no-store")
        self.assertEqual(hdrs["x-content-type-options"], "nosniff")
        self.assertEqual(hdrs["server"], NAME + "/1")
        s, hdrs, raw = self.request("GET", "/docs/notes.md")
        self.assertEqual(hdrs["content-type"], "text/plain; charset=utf-8")
        s, hdrs, raw = self.request("GET", "/" + TASKS_REL)
        self.assertEqual(hdrs["content-type"], "application/json")
        s, hdrs, raw = self.request("HEAD", "/" + PAGE)
        self.assertEqual((s, raw), (200, b""))
        self.assertEqual(int(hdrs["content-length"]), len(b"<!doctype html><title>x</title>hello page"))
        self.assertEqual(self.request("GET", "/docs/missing.html")[0], 404)
        self.assertEqual(self.request("GET", "/docs/")[0], 404)

    def test_root_redirects_to_the_page(self):
        s, hdrs, _ = self.request("GET", "/")
        self.assertEqual((s, hdrs["location"]), (302, "/" + PAGE))
        (self.root / "index.html").write_text("index", encoding="utf-8", newline="\n")
        self.assertEqual(self.request("GET", "/")[2], b"index")

    def test_path_traversal_refused(self):
        attempts = ["/../outside-secret.txt", "/docs/../../outside-secret.txt", "/%2e%2e/outside-secret.txt",
                    "/%2E%2E/%2e%2e/outside-secret.txt", "/docs/%2e%2e%2f%2e%2e%2foutside-secret.txt",
                    "/..%2foutside-secret.txt", "/docs/..%5c..%5coutside-secret.txt", "/....//outside-secret.txt",
                    "/docs/../../../../../../etc/passwd", "/%00/../outside-secret.txt", "//outside-secret.txt",
                    "/link-out.txt", "/docs/./../link-out.txt"]
        for target in attempts:
            status, body = self.raw_get(target)
            self.assertIn(status, (400, 403, 404), target)
            self.assertNotIn(b"TOP-SECRET-OUTSIDE-ROOT", body, target)
            self.assertNotIn(b"root:", body, target)
        self.assertEqual(self.raw_get("/../outside-secret.txt")[0], 403)
        self.assertEqual(self.raw_get("/link-out.txt")[0], 403)   # a symlink out of the root

    def test_dotfiles_hidden(self):
        self.assertEqual(self.raw_get("/.git/config")[0], 404)

    def test_state_and_config_not_reachable(self):
        self.send(note="queued note")
        for target in ("/state/inbox.jsonl", "/webhook.json", "/inbox.jsonl"):
            self.assertEqual(self.raw_get(target)[0], 404)


class NetworkTests(Base):
    """buildlog.json "network": "lan": every interface, but only LAN / Tailscale clients and this machine's names."""

    def test_local_mode_is_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.app.allowed_hosts, {"127.0.0.1:%d" % self.port, "localhost:%d" % self.port})

    def test_lan_mode_filters_clients_and_hosts(self):
        self.restart(network="lan", extra_hosts=("build-box.example",))
        self.assertEqual(self.server.server_address[0], "0.0.0.0")
        ok = self.server.verify_request
        for ip in ("127.0.0.1", "100.77.115.67", "192.168.1.20", "10.0.0.7", "172.20.1.1", "::ffff:192.168.1.20",
                   "fd7a:115c:a1e0::1"):
            self.assertTrue(ok(None, (ip, 5555)), ip)
        for ip in ("8.8.8.8", "100.128.0.1", "2001:4860::8888", "not-an-ip"):
            self.assertFalse(ok(None, (ip, 5555)), ip)
        s, _, _ = self.request("GET", "/" + PAGE, host="build-box.example:%d" % self.port)
        self.assertEqual(s, 200)
        s, _, _ = self.request("GET", "/" + PAGE, host="evil.example:%d" % self.port)
        self.assertEqual(s, 403)

    def test_pages_link_to_the_host_the_browser_used(self):
        self.restart(network="lan", extra_hosts=("build-box.example",))
        (self.root / "docs" / "links.html").write_text(
            "<a href='http://127.0.0.1:%d/docs/x.html'>x</a>" % self.port, encoding="utf-8", newline="\n")
        s, hdrs, raw = self.request("GET", "/docs/links.html", host="build-box.example:%d" % self.port)
        self.assertEqual(s, 200)
        self.assertEqual(raw, b"<a href='http://build-box.example:%d/docs/x.html'>x</a>" % self.port)
        self.assertEqual(int(hdrs["content-length"]), len(raw))
        s, _, raw = self.request("GET", "/docs/links.html")
        self.assertIn(b"http://127.0.0.1:%d/" % self.port, raw)


class SendTests(Base):
    def test_delivery_is_signed_and_complete(self):
        out = self.send(kind="task_update", task_id="alpha", done=True, note="approved, go ahead")
        self.assertTrue(out["delivered"])
        self.assertEqual((out["status"], out["http_status"], out["error"]), ("sent", 200, None))
        self.assertEqual(out["pending"], 0)
        self.assertEqual(len(self.hook.received), 1)
        got = self.hook.received[0]
        self.assertTrue(got["sig_ok"])
        self.assertEqual(got["path"], "/hook/%s?key=%s" % (URL_TOKEN, URL_QUERY))
        self.assertEqual(got["headers"]["Content-Type"], "application/json")
        self.assertEqual(got["headers"]["User-Agent"], NAME + "/1")   # not Python-urllib: the relay refuses that
        p = got["json"]
        self.assertEqual(sorted(p), ["done", "event", "id", "kind", "note", "sent_at", "task_id", "task_title"])
        self.assertEqual((p["event"], p["kind"], p["task_id"], p["task_title"], p["done"], p["note"]),
                         ("buildlog_message", "task_update", "alpha", "Approve alpha?", "true", "approved, go ahead"))
        self.assertRegex(p["id"], r"^[0-9a-f]{32}$")
        self.assertRegex(p["sent_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        t = self.task("alpha")
        self.assertTrue(t["done"])
        self.assertEqual(t["note"], "approved, go ahead")
        self.assertEqual((t["last_send"]["status"], t["last_send"]["id"]), ("sent", p["id"]))
        self.assertEqual(t["last_note"]["text"], "approved, go ahead")

    def test_signature_is_over_the_exact_bytes(self):
        self.send(note="ünïcödé \"quotes\" and\nnewlines")
        got = self.hook.received[0]
        self.assertTrue(got["sig_ok"])
        self.assertEqual(json.loads(got["body"])["note"], "ünïcödé \"quotes\" and\nnewlines")

    def test_message_and_test_kinds(self):
        self.send(kind="message", note="Please look at the audio bug")
        self.send(kind="test", note="Build log panel test, please ignore")
        a, b = [r["json"] for r in self.hook.received]
        self.assertEqual((a["kind"], a["task_id"], a["task_title"], a["done"]), ("message", "", "", ""))
        self.assertEqual((b["kind"], b["note"]), ("test", "Build log panel test, please ignore"))
        self.assertNotEqual(a["id"], b["id"])
        self.assertTrue(all(r["sig_ok"] for r in self.hook.received))
        # tests are not part of the conversation shown on the page
        thread = self.get_json("/api/messages")[1]["messages"]
        self.assertEqual([(m["type"], m["text"]) for m in thread], [("user", "Please look at the audio bug")])

    def test_validation(self):
        bad = [({"kind": "nope"}, 400), ({"kind": "message", "note": "  "}, 400), ({"note": "x"}, 400),
               ({"kind": "message", "note": 7}, 400), ({"kind": "message", "note": "x" * 9000}, 400),
               ({"kind": "task_update", "task_id": "nope", "done": True}, 404),
               ({"kind": "task_update", "done": True}, 404),
               ({"kind": "task_update", "task_id": "alpha"}, 400),
               ({"kind": "task_update", "task_id": "alpha", "done": "maybe"}, 400),
               ({"kind": "message", "note": "x", "id": "not-hex"}, 400)]
        for body, want in bad:
            self.assertEqual(self.post("/api/send", body)[0], want, body)
        s, _ = self.post("/api/send", b"x" * (bs.MAX_BODY + 10))
        self.assertEqual(s, 413)
        self.assertEqual(self.hook.received, [])
        self.assertEqual(self.inbox(), [])

    def test_same_id_is_not_sent_twice(self):
        mid = "0123456789abcdef0123456789abcdef"
        a = self.send(id=mid, note="once")
        b = self.send(id=mid, note="once")
        self.assertEqual((a["id"], b["id"]), (mid, mid))
        self.assertTrue(b["delivered"])
        self.assertEqual(len(self.hook.received), 1)
        self.assertEqual(len([r for r in self.inbox() if r["type"] == "message"]), 1)

    def test_queue_and_retry_when_webhook_fails(self):
        self.hook.mode = "error"
        first = self.send(note="first, webhook is down")
        self.assertFalse(first["delivered"])
        self.assertEqual((first["status"], first["http_status"], first["error"]), ("queued", 503, "HTTP 503"))
        self.assertEqual(first["pending"], 1)
        second = self.send(kind="task_update", task_id="beta", done=True, note="second")
        self.assertFalse(second["delivered"])
        self.assertEqual(second["pending"], 2)
        self.assertEqual(self.task("beta")["last_send"]["status"], "queued")
        kinds = [(r["type"], r.get("payload", {}).get("note")) for r in self.inbox()]
        self.assertEqual(kinds, [("message", "first, webhook is down"), ("message", "second")])
        self.assertEqual(self.get_json("/api/tasks")[1]["pending"], 2)

        # Webhook recovers: the next send retries the older ones first, same ids and bytes.
        self.hook.mode = "ok"
        third = self.send(note="third")
        self.assertTrue(third["delivered"])
        self.assertEqual(third["pending"], 0)
        last_three = [r["json"]["note"] for r in self.hook.received][-3:]
        self.assertEqual(last_three, ["first, webhook is down", "second", "third"])
        retried = [r for r in self.hook.received if r["json"]["id"] == first["id"]]
        self.assertGreaterEqual(len(retried), 2)                  # failed tries, then the delivery
        self.assertEqual(len({r["body"] for r in retried}), 1)    # identical signed bytes every time
        self.assertTrue(all(r["sig_ok"] for r in self.hook.received))
        self.assertEqual(self.task("beta")["last_send"]["status"], "sent")
        done_ids = [r["id"] for r in self.inbox() if r["type"] == "delivered"]
        self.assertEqual(done_ids, [first["id"], second["id"], third["id"]])

    def test_rejected_by_webhook_stays_queued(self):
        self.hook.mode = "reject"
        out = self.send(note="will be rejected")
        self.assertEqual((out["delivered"], out["status"], out["http_status"]), (False, "queued", 401))
        self.assertEqual(out["pending"], 1)

    def test_background_retry_loop(self):
        self.hook.mode = "error"
        out = self.send(note="retry me in the background")
        self.assertEqual(out["pending"], 1)
        self.hook.mode = "ok"
        stop = threading.Event()
        self.addCleanup(stop.set)
        threading.Thread(target=self.app.retry_loop, args=(stop, 0.1), daemon=True).start()
        deadline = time.time() + 5
        while time.time() < deadline and self.app.pending:
            time.sleep(0.05)
        self.assertEqual(len(self.app.pending), 0)
        self.assertEqual(self.get_json("/api/tasks")[1]["pending"], 0)
        self.assertEqual(self.hook.received[-1]["json"]["id"], out["id"])

    def test_queue_survives_restart(self):
        self.hook.mode = "error"
        out = self.send(note="survive a restart")
        self.restart()
        self.assertEqual(len(self.app.pending), 1)
        self.hook.mode = "ok"
        self.app.flush()
        self.assertEqual(len(self.app.pending), 0)
        self.assertEqual(self.hook.received[-1]["json"]["id"], out["id"])
        self.restart()
        self.assertEqual(len(self.app.pending), 0)   # the receipt was recorded

    def test_webhook_timeout_is_bounded(self):
        self.hook.mode = "hang"
        self.restart(timeout=0.5)
        t0 = time.time()
        out = self.send(note="slow webhook")
        self.assertLess(time.time() - t0, 4)
        self.assertEqual((out["delivered"], out["status"], out["error"]), (False, "queued", "timeout"))
        self.hook.release.set()

    def test_no_config_or_dead_webhook_queues(self):
        self.config.unlink()
        out = self.send(note="no config")
        self.assertEqual((out["delivered"], out["error"]), (False, "webhook not configured"))
        self.write_config(url="http://127.0.0.1:1/never")
        out = self.send(note="dead port")
        self.assertFalse(out["delivered"])
        # Windows retries a refused localhost connect for about 2 s, so a short webhook timeout can win
        self.assertIn(out["error"], ("connection failed", "timeout") if os.name == "nt" else ("connection failed",))
        self.assertEqual(out["pending"], 2)
        self.write_config(url="")                      # set-url not run yet
        self.assertEqual(self.send(note="empty url")["error"], "webhook not configured")

    def test_redirects_are_not_followed(self):
        target = FakeWebhook()
        self.addCleanup(target.stop)

        class Redirect(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(307)
                self.send_header("Location", target.url)
                self.send_header("Content-Length", "0")
                self.end_headers()
        httpd = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.write_config(url="http://127.0.0.1:%d/r" % httpd.server_address[1])
        out = self.send(note="redirect")
        self.assertFalse(out["delivered"])
        self.assertEqual(target.received, [])


class ReplyTests(Base):
    """Orchestrator replies, the user's last note and the general thread (same contract as recomp)."""

    def cli(self, *args, check=True):
        return subprocess.run([sys.executable, "-I", str(TOOLS / "buildlog_tasks.py"), "--settings", str(self.tmp / "none.json"),
                               "--file", str(self.root / TASKS_REL), "--page", str(self.tmp / "none.html")] + list(args),
                              check=check, capture_output=True, text=True, encoding="utf-8", env=clean_env())

    def edit_tasks_file(self, fn):
        path = self.root / TASKS_REL
        data = json.loads(path.read_text(encoding="utf-8"))
        fn(data)
        path.write_text(json.dumps(data), encoding="utf-8", newline="\n")

    def test_task_replies_are_served_oldest_first(self):
        self.assertEqual(self.task("alpha")["replies"], [])
        self.cli("reply", "alpha", "first answer")
        time.sleep(0.01)
        self.cli("reply", "alpha", "second answer, with `code`")
        reps = self.task("alpha")["replies"]
        self.assertEqual([r["text"] for r in reps], ["first answer", "second answer, with `code`"])
        self.assertTrue(all(r["at"].endswith("Z") for r in reps))
        self.assertEqual(self.task("beta")["replies"], [])

    def test_reply_from_stdin(self):
        r = subprocess.run([sys.executable, "-I", str(TOOLS / "buildlog_tasks.py"), "--settings", str(self.tmp / "none.json"),
                            "--file", str(self.root / TASKS_REL), "--page", str(self.tmp / "none.html"), "reply", "beta", "-"],
                           input="Rumble works.\n\n- left motor\n- right motor\n", capture_output=True, text=True, encoding="utf-8", env=clean_env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.task("beta")["replies"][-1]["text"], "Rumble works.\n\n- left motor\n- right motor")

    def test_malformed_replies_are_skipped(self):
        def junk(data):
            data["tasks"][0]["replies"] = [{"at": "2026-10-25T10:00:00.000Z", "text": "good"}, {"text": "no time"},
                                           {"at": "x", "text": ""}, "string", {"at": 5, "text": "t"}, None]
            data["tasks"][1]["replies"] = "not a list"
            data["general_replies"] = {"bad": True}
        self.edit_tasks_file(junk)
        self.assertEqual([r["text"] for r in self.task("alpha")["replies"]], ["good"])
        self.assertEqual(self.task("beta")["replies"], [])
        self.assertEqual(self.get_json("/api/messages")[1]["messages"], [])

    def test_a_reply_never_changes_a_tick_or_note(self):
        self.post("/api/tasks/alpha", {"done": True, "note": "my draft"})
        before = self.task("alpha")
        time.sleep(0.01)
        self.cli("reply", "alpha", "an answer")
        after = self.task("alpha")
        self.assertTrue(after["done"])
        self.assertEqual((after["note"], after["updated"]), ("my draft", before["updated"]))
        self.assertEqual(len(after["replies"]), 1)
        self.assertEqual(json.loads((self.state / "state.json").read_text(encoding="utf-8"))["tasks"]["alpha"]["note"], "my draft")

    def test_last_note_comes_from_the_inbox(self):
        self.assertIsNone(self.task("alpha")["last_note"])
        self.hook.mode = "error"
        out = self.send(kind="task_update", task_id="alpha", done=False, note="ctrl-d isn't working!")
        ln = self.task("alpha")["last_note"]
        self.assertEqual((ln["id"], ln["text"], ln["status"]), (out["id"], "ctrl-d isn't working!", "queued"))
        self.assertEqual(ln["at"], out["sent_at"])
        self.assertIsNone(self.task("beta")["last_note"])
        self.hook.mode = "ok"                           # a tick without a note keeps the last real note
        self.send(kind="task_update", task_id="alpha", done=True, note="")
        ln = self.task("alpha")["last_note"]
        self.assertEqual((ln["text"], ln["status"]), ("ctrl-d isn't working!", "sent"))   # delivered by the retry
        self.send(kind="task_update", task_id="alpha", done=True, note="still broken")
        self.restart()
        self.assertEqual(self.task("alpha")["last_note"]["text"], "still broken")
        self.send(kind="message", note="general words")  # messages and tests are not task notes
        self.assertIsNone(self.task("beta")["last_note"])

    def test_general_thread_interleaves_messages_and_replies(self):
        def seed(data):
            data["general_replies"] = [{"at": "2999-01-01T00:00:00.000Z", "text": "later reply"},
                                       {"at": "2000-01-01T00:00:00.000Z", "text": "early reply"}]
        self.edit_tasks_file(seed)
        self.hook.mode = "error"
        mine = self.send(kind="message", note="ctrl-d isn't working!")
        self.send(kind="test", note="Build log panel test, please ignore")
        self.send(kind="task_update", task_id="beta", done=True, note="a task note")
        status, data = self.get_json("/api/messages")
        self.assertEqual(status, 200)
        got = [(m["type"], m["text"]) for m in data["messages"]]
        self.assertEqual(got, [("orchestrator", "early reply"), ("user", "ctrl-d isn't working!"),
                               ("orchestrator", "later reply")])
        user = data["messages"][1]
        self.assertEqual((user["id"], user["status"], user["at"]), (mine["id"], "queued", mine["sent_at"]))
        self.assertEqual(data["total"], 3)
        self.hook.mode = "ok"
        self.app.flush()
        self.assertEqual(self.get_json("/api/messages")[1]["messages"][1]["status"], "sent")
        self.restart()                                  # the user's side is rebuilt from inbox.jsonl
        self.assertEqual([m["text"] for m in self.get_json("/api/messages")[1]["messages"]],
                         ["early reply", "ctrl-d isn't working!", "later reply"])

    def test_general_replies_from_the_cli(self):
        self.send(kind="message", note="hello")
        time.sleep(1.1)                                 # sent_at has one-second resolution
        self.cli("reply", "--general", "Hi, seen it.")
        got = [(m["type"], m["text"]) for m in self.get_json("/api/messages")[1]["messages"]]
        self.assertEqual(got, [("user", "hello"), ("orchestrator", "Hi, seen it.")])

    def test_thread_is_limited(self):
        old = bs.MAX_THREAD
        bs.MAX_THREAD = 3
        self.addCleanup(setattr, bs, "MAX_THREAD", old)
        for i in range(5):
            self.send(kind="message", note="m%d" % i)
        data = self.get_json("/api/messages")[1]
        self.assertEqual([m["text"] for m in data["messages"]], ["m2", "m3", "m4"])
        self.assertEqual(data["total"], 5)

    def test_thread_without_a_task_file(self):
        (self.root / TASKS_REL).unlink()
        self.send(kind="message", note="still listed")
        self.assertEqual([m["text"] for m in self.get_json("/api/messages")[1]["messages"]], ["still listed"])

    def test_messages_endpoint_checks_the_host(self):
        s, _, raw = self.request("GET", "/api/messages", host="evil.example:%d" % self.port)
        self.assertEqual(s, 403)
        self.assertEqual(self.request("POST", "/api/messages", b"{}", {"Content-Type": "application/json", "X-Buildlog": "1"})[0], 404)


class LocalInboxTests(Base):
    """delivery "local": nothing is forwarded; agents read the inbox with the CLI."""

    def inbox_cli(self, *args):
        return self.cli("inbox", "--state-dir", str(self.state), *args)

    def test_selective_acknowledgment_preserves_other_messages(self):
        self.write_config(delivery="local")
        first = self.send(note="first")
        second = self.send(note="second")
        self.assertIn("id=" + first["id"], self.inbox_cli("--limit", "1").stdout)
        self.assertEqual([m["id"] for m in json.loads(self.inbox_cli("--json", "--limit", "1").stdout)], [first["id"]])
        self.inbox_cli("--id", second["id"], "--mark-read")
        self.assertEqual([m["id"] for m in json.loads(self.inbox_cli("--json").stdout)], [first["id"]])
        self.inbox_cli("--limit", "1", "--mark-read")
        self.assertEqual(json.loads(self.inbox_cli("--json").stdout), [])

    def test_local_mode_keeps_messages_for_the_cli(self):
        self.write_config(delivery="local")
        out = self.send(note="please check the logs")
        self.assertEqual((out["delivered"], out["status"]), (True, "local"))
        t = self.send(kind="task_update", task_id="alpha", done=True, note="yes")
        self.assertEqual(t["status"], "local")
        self.assertEqual(self.task("alpha")["last_send"]["status"], "local")
        self.assertEqual(self.hook.received, [])
        self.assertEqual(self.get_json("/api/tasks")[1]["delivery"], "local")
        listed = json.loads(self.inbox_cli("--json").stdout)
        self.assertEqual([m["note"] for m in listed], ["please check the logs", "yes"])
        self.assertIn("alpha done=true", self.inbox_cli().stdout)
        self.inbox_cli("--mark-read")
        self.assertEqual(json.loads(self.inbox_cli("--json").stdout), [])
        self.assertIn("(no unhandled messages)", self.inbox_cli().stdout)
        everything = json.loads(self.inbox_cli("--all", "--json").stdout)
        self.assertEqual([m["status"] for m in everything], ["read", "read"])
        if os.name != "nt":
            self.assertEqual(oct((self.state / "read.json").stat().st_mode & 0o777), "0o600")
        self.restart()                                 # statuses survive a restart
        self.assertEqual([m["status"] for m in self.get_json("/api/messages")[1]["messages"]], ["local"])
        self.assertEqual(self.task("alpha")["last_note"]["status"], "local")

    def test_messages_read_locally_are_not_forwarded_later(self):
        self.hook.mode = "error"
        out = self.send(note="sent while T3 was down")
        self.assertEqual(out["status"], "queued")
        listed = json.loads(self.inbox_cli("--json").stdout)
        self.assertEqual([m["status"] for m in listed], ["queued"])
        self.inbox_cli("--mark-read")                  # an agent handled it from the inbox
        self.hook.mode = "ok"
        n = len(self.hook.received)
        self.app.flush()
        self.assertEqual(len(self.hook.received), n)    # not forwarded twice
        self.assertEqual(self.app.pending, {})
        self.assertEqual(json.loads(self.inbox_cli("--all", "--json").stdout)[0]["status"], "read")

    def test_webhook_mode_lists_only_stuck_messages(self):
        self.send(note="delivered fine")
        self.assertEqual(json.loads(self.inbox_cli("--json").stdout), [])
        self.hook.mode = "error"
        self.send(note="stuck")
        self.assertEqual([m["note"] for m in json.loads(self.inbox_cli("--json").stdout)], ["stuck"])


class SecretTests(Base):
    """The webhook URL, its secret and the signatures must never leak."""

    def leaks(self):
        texts = list(self.transcript) + [self.logs.getvalue()]
        for p in self.state.rglob("*"):
            if p.is_file():
                texts.append(p.read_text(encoding="utf-8"))
        needles = [SECRET, URL_TOKEN, URL_QUERY, self.hook.url, "sha256="]
        needles += [hmac.new(SECRET.encode(), r["body"], hashlib.sha256).hexdigest() for r in self.hook.received]
        found = []
        for text in texts:
            for n in needles:
                if n in text:
                    found.append(n)
        return found

    def test_secret_never_appears(self):
        self.send(note="ok path")
        self.send(kind="task_update", task_id="alpha", done=True, note="task path")
        self.hook.mode = "error"
        self.send(note="error path")
        self.hook.mode = "reject"
        self.send(note="reject path")
        self.hook.mode = "ok"
        self.write_config(url="http://127.0.0.1:1/%s?key=%s" % (URL_TOKEN, URL_QUERY))
        self.send(note="dead path")
        self.write_config(url="http://[not-a-valid-url/%s?key=%s" % (URL_TOKEN, URL_QUERY))
        self.send(note="bad url path")
        self.write_config(url="ftp://example.invalid/%s?key=%s" % (URL_TOKEN, URL_QUERY))
        self.send(note="bad scheme path")
        self.get_json("/api/tasks")
        self.get_json("/api/version")
        self.post("/api/send", {"kind": "bogus"})
        self.request("GET", "/api/tasks?%s=1" % SECRET)
        self.app.flush()
        self.assertTrue(self.logs.getvalue())            # the log is not empty, so the check means something
        self.assertEqual(self.leaks(), [])
        self.assertTrue(all(r["sig_ok"] for r in self.hook.received))

    def test_scrubber_catches_url_in_log_text(self):
        self.app.log("oops %s and %s and %s" % (SECRET, self.hook.url, URL_TOKEN))
        self.assertNotIn(SECRET, self.logs.getvalue())
        self.assertNotIn(URL_QUERY, self.logs.getvalue())
        self.assertNotIn(URL_TOKEN, self.logs.getvalue())

    def test_request_log_omits_query_string(self):
        self.request("GET", "/api/version?token=%s" % SECRET)
        self.assertNotIn(SECRET, self.logs.getvalue())
        self.assertIn("GET /api/version -> 200", self.logs.getvalue())


class TasksCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        self.tasks = d / "buildlog-tasks.json"
        self.page = d / "buildlog.html"
        self.page.write_text("<html><!-- TASKS-SNAPSHOT:START -->\n<script type=\"application/json\" id=\"tasks-snapshot\">{\"tasks\":[]}</script>\n"
                             "<!-- TASKS-SNAPSHOT:END --><p>rest of page</p></html>", encoding="utf-8", newline="\n")
        self.none = d / "none.json"

    def run_cli(self, *args, check=True):
        r = subprocess.run([sys.executable, "-I", str(TOOLS / "buildlog_tasks.py"), "--settings", str(self.none),
                            "--file", str(self.tasks)] + list(args), capture_output=True, text=True, encoding="utf-8", env=clean_env())
        if check:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def data(self):
        return json.loads(self.tasks.read_text(encoding="utf-8"))["tasks"]

    def test_add_list_done_reopen(self):
        out = self.run_cli("add", "--kind", "action", "--title", "Plug in the DualSense!", "--details", "USB first.", "--how", "ls -l")
        tid = out.stdout.strip()
        self.assertEqual(tid, "plug-in-the-dualsense")
        t = self.data()[0]
        self.assertEqual((t["id"], t["kind"], t["status"], t["how"], t["done_at"]), (tid, "action", "open", "ls -l", None))
        self.assertTrue(t["added"].endswith("Z"))
        self.assertEqual(self.run_cli("add", "--kind", "decision", "--title", "Plug in the DualSense!", "--details", "x").stdout.strip(),
                         "plug-in-the-dualsense-2")
        self.assertEqual(self.run_cli("add", "--kind", "decision", "--title", "x", "--details", "y", "--id", "my-id").stdout.strip(), "my-id")
        self.assertNotEqual(self.run_cli("add", "--kind", "decision", "--title", "x", "--details", "y", "--id", "my-id", check=False).returncode, 0)
        self.assertNotEqual(self.run_cli("add", "--kind", "bogus", "--title", "x", "--details", "y", check=False).returncode, 0)

        self.run_cli("done", tid)
        t = self.data()[0]
        self.assertEqual(t["status"], "done")
        self.assertTrue(t["done_at"])
        open_ids = [t["id"] for t in json.loads(self.run_cli("list", "--json").stdout)]
        self.assertEqual(open_ids, ["plug-in-the-dualsense-2", "my-id"])
        self.assertIn(tid, self.run_cli("list", "--all").stdout)
        self.run_cli("reopen", tid)
        t = self.data()[0]
        self.assertEqual((t["status"], t["done_at"]), ("open", None))
        self.assertNotEqual(self.run_cli("done", "no-such-id", check=False).returncode, 0)
        self.assertEqual([x["id"] for x in self.data()][:1], [tid])      # ids never change

    def test_page_snapshot_follows_the_file(self):
        self.run_cli("add", "--kind", "decision", "--title", "Use </script> safely?", "--details", "a < b", "--id", "xss")
        html = self.page.read_text(encoding="utf-8")
        m = re.search(r'id="tasks-snapshot">(.*?)</script>', html, re.S)
        self.assertNotIn("</script", m.group(1))
        snap = json.loads(m.group(1))
        self.assertEqual([t["id"] for t in snap["tasks"]], ["xss"])
        self.assertEqual(snap["tasks"][0]["title"], "Use </script> safely?")
        self.assertIn("rest of page", html)
        self.run_cli("done", "xss")
        self.assertEqual(json.loads(re.search(r'id="tasks-snapshot">(.*?)</script>', self.page.read_text(encoding="utf-8"), re.S).group(1))["tasks"][0]["status"], "done")

    def test_missing_file_starts_empty(self):
        self.assertIn("no open tasks", self.run_cli("list").stdout)

    def test_reply_to_a_task(self):
        tid = self.run_cli("add", "--kind", "action", "--title", "Key file", "--details", "d").stdout.strip()
        before = self.data()[0]
        out = self.run_cli("reply", tid, "  Use the updated two-line command above.  ")
        self.assertIn("replied: key-file", out.stdout)
        time.sleep(0.01)
        self.run_cli("reply", tid, "Second answer\nwith a second line")
        t = self.data()[0]
        self.assertEqual([r["text"] for r in t["replies"]],
                         ["Use the updated two-line command above.", "Second answer\nwith a second line"])
        self.assertEqual(sorted(t["replies"][0]), ["at", "text"])
        self.assertLess(t["replies"][0]["at"], t["replies"][1]["at"])
        for key in ("status", "updated", "added", "done_at", "title", "details", "how", "id", "kind"):
            self.assertEqual(t[key], before[key], key)          # a reply never changes the task itself
        self.run_cli("done", tid)
        self.run_cli("reopen", tid)
        self.assertEqual(len(self.data()[0]["replies"]), 2)
        other = self.run_cli("add", "--kind", "decision", "--title", "Other", "--details", "d").stdout.strip()
        self.assertNotIn("replies", self.data()[1])
        self.assertEqual(other, "other")

    def test_reply_general(self):
        self.run_cli("add", "--kind", "action", "--title", "T", "--details", "d")
        self.run_cli("reply", "--general", "A general answer")
        self.run_cli("reply", "--general", "Another")
        data = json.loads(self.tasks.read_text(encoding="utf-8"))
        self.assertEqual([r["text"] for r in data["general_replies"]], ["A general answer", "Another"])
        self.assertEqual(len(data["tasks"]), 1)
        self.assertNotIn("replies", data["tasks"][0])
        self.run_cli("add", "--kind", "action", "--title", "T2", "--details", "d")      # other keys survive edits
        self.assertEqual(len(json.loads(self.tasks.read_text(encoding="utf-8"))["general_replies"]), 2)

    def test_reply_errors(self):
        self.run_cli("add", "--kind", "action", "--title", "T", "--details", "d", "--id", "t")
        bad = [("reply", "t"), ("reply", "t", "   "), ("reply", "nope", "text"), ("reply", "t", "a", "b"),
               ("reply", "--general"), ("reply", "--general", "a", "b"), ("reply", "--general", " "),
               ("reply", "t", "x" * 9000), ("reply",)]
        for args in bad:
            r = self.run_cli(*args, check=False)
            self.assertNotEqual(r.returncode, 0, args)
        self.assertNotIn("replies", self.data()[0])
        self.assertNotIn("general_replies", json.loads(self.tasks.read_text(encoding="utf-8")))

    def test_snapshot_carries_replies(self):
        self.run_cli("add", "--kind", "action", "--title", "T", "--details", "d", "--id", "t")
        self.run_cli("reply", "t", "Done </script><b> & more")
        self.run_cli("reply", "--general", "General <i>answer</i>")
        html = self.page.read_text(encoding="utf-8")
        m = re.search(r'id="tasks-snapshot">(.*?)</script>', html, re.S)
        self.assertNotIn("</script", m.group(1))
        snap = json.loads(m.group(1))
        self.assertEqual(snap["tasks"][0]["replies"][0]["text"], "Done </script><b> & more")
        self.assertEqual(snap["general_replies"][0]["text"], "General <i>answer</i>")
        self.assertEqual(sorted(snap["tasks"][0]["replies"][0]), ["at", "text"])
        self.assertEqual(snap, json.loads(bt.snapshot_json(json.loads(self.tasks.read_text(encoding="utf-8")))))


@unittest.skipUnless(shutil.which("git"), "git not installed")
class CommitTests(unittest.TestCase):
    """--commit and `commit` use a private index and never pick up other staged work."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.env = clean_env()
        self.env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
        self.git("init", "-q")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "docs").mkdir()
        self.tasks = self.repo / TASKS_REL
        self.page = self.repo / PAGE
        self.page.write_text("<html><!-- TASKS-SNAPSHOT:START -->\n<script type=\"application/json\" id=\"tasks-snapshot\">{\"tasks\":[]}</script>\n"
                             "<!-- TASKS-SNAPSHOT:END --></html>\n", encoding="utf-8", newline="\n")
        (self.repo / "other.txt").write_text("v1\n", encoding="utf-8", newline="\n")
        self.settings = self.repo / "buildlog.json"
        self.settings.write_text(json.dumps({"root": ".", "page": PAGE, "tasks": TASKS_REL}), encoding="utf-8", newline="\n")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo)] + list(args), capture_output=True, text=True, encoding="utf-8",
                              env=self.env, check=True).stdout

    def cli(self, *args):
        r = subprocess.run([sys.executable, "-I", str(TOOLS / "buildlog_tasks.py"), "--settings", str(self.settings)] + list(args),
                           capture_output=True, text=True, encoding="utf-8", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_first_commit_in_an_empty_repo(self):
        self.cli("add", "--kind", "action", "--title", "First", "--details", "d", "--commit")
        self.assertEqual(sorted(self.git("ls-tree", "-r", "--name-only", "HEAD").split()), [TASKS_REL, PAGE])
        self.assertEqual(self.git("log", "-1", "--format=%B").strip(), "buildlog tasks: add first")

    def test_only_task_paths_are_committed_and_shared_index_stays_right(self):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        (self.repo / "other.txt").write_text("v2, staged by someone else\n", encoding="utf-8", newline="\n")
        self.git("add", "other.txt")                       # another thread's staged work
        self.cli("add", "--kind", "decision", "--title", "Ship it?", "--details", "d", "--commit")
        msg = self.git("log", "-1", "--format=%B").strip()
        self.assertEqual(msg, "buildlog tasks: add ship-it")
        self.assertNotIn("Co-Authored-By", msg)
        self.assertEqual(sorted(self.git("show", "--name-only", "--format=", "HEAD").split()), [TASKS_REL, PAGE])
        # the shared index still has the other staged change, and is in step for our two files
        staged = self.git("diff", "--cached", "--name-only").split()
        self.assertEqual(staged, ["other.txt"])
        self.assertEqual(self.git("status", "--porcelain", "--", TASKS_REL, PAGE).strip(), "")

    def test_stale_shared_index_is_left_alone(self):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.git("rm", "-q", "--cached", PAGE)             # shared index out of step with HEAD for the page
        before = self.git("ls-files", "-s")
        self.cli("add", "--kind", "action", "--title", "Two", "--details", "d", "--commit")
        self.assertIn(PAGE, self.git("show", "--name-only", "--format=", "HEAD").split())
        after = self.git("ls-files", "-s")
        self.assertNotIn(PAGE, after)                      # not touched
        self.assertIn(TASKS_REL, after)                    # new file, index was in step (absent = absent)
        self.assertEqual([l for l in before.splitlines() if PAGE not in l],
                         [l for l in after.splitlines() if TASKS_REL not in l])

    def test_directory_commits_keep_the_shared_index_in_step(self):
        tools = self.repo / "tools"
        tools.mkdir()
        (tools / "a.py").write_text("1\n", encoding="utf-8", newline="\n")
        (tools / "b.py").write_text("1\n", encoding="utf-8", newline="\n")
        self.cli("commit", "-m", "tools: first", "tools")
        self.assertEqual(self.git("status", "--porcelain").split("\n")[0][:2], "??")   # only untracked leftovers
        (tools / "a.py").write_text("2\n", encoding="utf-8", newline="\n")
        self.cli("commit", "-m", "tools: second", "tools")          # a directory path, second time
        self.assertEqual(self.git("status", "--porcelain", "--", "tools").strip(), "")
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD").split(), ["tools/a.py"])

    def test_reply_commit(self):
        self.cli("add", "--kind", "action", "--title", "Plug in", "--details", "d", "--id", "plug", "--commit")
        self.cli("reply", "plug", "Seen it, thanks.", "--commit")
        self.assertEqual(self.git("log", "-1", "--format=%B").strip(), "buildlog tasks: reply to plug")
        self.cli("reply", "--general", "All good.", "--commit")
        self.assertEqual(self.git("log", "-1", "--format=%B").strip(), "buildlog tasks: reply")
        data = json.loads(self.git("show", "HEAD:" + TASKS_REL))
        self.assertEqual([r["text"] for r in data["general_replies"]], ["All good."])

    def test_commit_subcommand(self):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.page.write_text(self.page.read_text(encoding="utf-8") + "<p>update</p>\n", encoding="utf-8", newline="\n")
        (self.repo / "other.txt").write_text("unrelated edit\n", encoding="utf-8", newline="\n")
        out = self.cli("commit", "-m", "buildlog: update", PAGE).stdout
        self.assertIn("committed", out)
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD").split(), [PAGE])
        self.assertIn("other.txt", self.git("status", "--porcelain"))
        self.assertIn("nothing to commit", subprocess.run(
            [sys.executable, "-I", str(TOOLS / "buildlog_tasks.py"), "--settings", str(self.settings), "commit", "-m", "x", PAGE],
            capture_output=True, text=True, encoding="utf-8", env=self.env).stderr)


SETTINGS = TOOLS / "buildlog.json"


@unittest.skipUnless(SETTINGS.is_file(), "no buildlog.json next to the scripts (not a set-up project)")
class ProjectFilesTests(unittest.TestCase):
    """The project's own task file and page stay consistent."""

    @classmethod
    def setUpClass(cls):
        cls.s = json.loads(SETTINGS.read_text(encoding="utf-8"))
        cls.root = (TOOLS / cls.s.get("root", "../..")).resolve()
        cls.page = cls.root / cls.s["page"]
        cls.tasks = cls.root / cls.s["tasks"]

    def test_tasks_file_is_valid(self):
        data = json.loads(self.tasks.read_text(encoding="utf-8"))
        ids = [t["id"] for t in data["tasks"]]
        self.assertEqual(len(ids), len(set(ids)))
        for t in data["tasks"]:
            required = {"id", "kind", "title", "details", "how", "added", "status", "done_at", "updated"}
            self.assertTrue(required <= set(t) <= required | {"replies"}, sorted(t))
            for r in t.get("replies", []) + data.get("general_replies", []):
                self.assertEqual(sorted(r), ["at", "text"])
            self.assertIn(t["kind"], ("action", "decision"))
            self.assertIn(t["status"], ("open", "done"))
            self.assertRegex(t["id"], r"^[a-z0-9][a-z0-9-]*$")

    def test_page_markers_snapshot_and_no_meta_refresh(self):
        html = self.page.read_text(encoding="utf-8")
        data = json.loads(self.tasks.read_text(encoding="utf-8"))
        for marker in ("<!-- TASKS:START -->", "<!-- TASKS:END -->", "<!-- TASKS-SNAPSHOT:START -->",
                       "<!-- TASKS-SNAPSHOT:END -->", "<!-- UPDATED:START -->", "<!-- UPDATED:END -->"):
            self.assertEqual(html.count(marker), 1, marker)
        for sec in self.s.get("sections", []):
            for edge in ("START", "END"):
                self.assertEqual(html.count("<!-- %s:%s -->" % (sec.upper(), edge)), 1, sec)
        self.assertLess(html.index("</header>"), html.index("<!-- TASKS:START -->"))
        self.assertNotIn('http-equiv="refresh"', html)
        for needle in ("/api/messages", "Orchestrator", "TASKS-CSS:START", "TASKS-CSS:END"):
            self.assertIn(needle, html, "the panel is older than the skill's template: run setup.py upgrade-page")
        self.assertIn(bt.snapshot_json(data), html, "page snapshot is stale: run buildlog_tasks.py sync")
        self.assertIn('data-live-url="http://127.0.0.1:%d/' % int(self.s["port"]), html)
        # the editing-rules comment is one comment: no stray comment opener or closer inside it
        head = html[:html.index("<style>")]
        self.assertEqual(head.count("<!--"), 1)
        self.assertEqual(head.count("-->"), 1)

    def test_page_and_tasks_file_have_no_secret(self):
        homes = self.s.get("_homes") or {}
        cfg_path = Path(homes.get("config_home") or Path.home() / ".config") / self.s["name"] / "webhook.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
        needles = [v for v in (cfg.get("secret"), cfg.get("url")) if v]
        for p in (self.page, self.tasks, SETTINGS):
            text = p.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"https?://[^\s\"'<>]*[A-Za-z0-9_-]{32,}", p.name)
            for n in needles:
                self.assertNotIn(n, text, "%s contains the webhook secret or URL" % p.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
