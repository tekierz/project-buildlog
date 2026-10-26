#!/usr/bin/env python3
"""Tests for the skill's own setup.py and verify.py (not copied into projects).

Runs init in a temporary git repository with private config/state homes and no
systemd, then verify (fake T3 round trip), upgrade-page, set-url, rotate-secret
and remove. Run: python3 -I scripts/tests/test_setup.py
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]


def clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
    return env


@unittest.skipUnless(shutil.which("git"), "git not installed")
class SetupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.proj = base / "My Project"                 # a space in the path on purpose
        self.proj.mkdir()
        self.homes = ["--config-home", str(base / "cfg"), "--state-home", str(base / "state"),
                      "--systemd-dir", str(base / "systemd")]
        self.env = clean_env()
        self.git("init", "-q")
        self.git("config", "commit.gpgsign", "false")
        (self.proj / "README.md").write_text("# P\n\n## Status\n\n- [x] Works ✓ (docs/evidence/a.txt)\n- [ ] Later\n")
        (self.proj / "docs" / "evidence").mkdir(parents=True)
        (self.proj / "docs" / "evidence" / "a.txt").write_text("ok\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.proj)] + list(args), capture_output=True, text=True,
                              env=self.env, check=True).stdout

    def setup(self, *args, stdin=None, check=True):
        r = subprocess.run([sys.executable, "-I", str(SCRIPTS / "setup.py")] + list(args), input=stdin,
                           capture_output=True, text=True, env=self.env, timeout=300)
        if check:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_full_cycle(self):
        out = self.setup("init", "--project", str(self.proj), "--port", "33991", "--no-service", "--commit",
                         "--sections", "current,cards,timeline", *self.homes).stdout
        self.assertNotIn("FAIL", out)
        name = "my-project-buildlog"
        cfg_dir = Path(self._tmp.name) / "cfg" / name
        cfg = json.loads((cfg_dir / "webhook.json").read_text())
        self.assertEqual(len(cfg["secret"]), 64)
        self.assertNotIn(cfg["secret"], out)
        self.assertEqual((cfg_dir / "webhook.json").stat().st_mode & 0o777, 0o600)
        args = json.loads((cfg_dir / "t3-webhook-task.json").read_text())["arguments"]
        sch = args["schedule"]
        self.assertEqual(sch["signature"], {"header": "x-my-project-signature", "encoding": "hex", "prefix": "sha256=",
                                            "secret": cfg["secret"]})
        self.assertEqual(sch["dedupe"], {"replayKey": "{{body.id}}"})
        self.assertEqual(sch["filters"], [{"path": "body.event", "op": "equals", "value": "buildlog_message"}])
        for field in ("kind", "task_id", "task_title", "done", "note", "sent_at"):
            self.assertIn("{{body.%s}}" % field, args["prompt"])
        self.assertNotIn("@@", args["prompt"])
        hb = json.loads((cfg_dir / "t3-heartbeat-task.json").read_text())["arguments"]
        self.assertEqual(hb["schedule"], {"type": "interval", "everyMs": 3600000})
        page = (self.proj / "docs" / "buildlog.html").read_text()
        self.assertNotIn("@@", page)
        self.assertIn("<!-- CARDS:START -->", page)
        self.assertNotIn("<!-- GOALS:START -->", page)
        self.assertIn("<b class=\"n-s\">1</b> of 2 checks shown", page)
        self.assertIn('data-live-url="http://127.0.0.1:33991/docs/buildlog.html"', page)
        unit = (self.proj / "tools" / "buildlog" / "my-project-buildlog.service").read_text()
        self.assertIn('"%s"' % (self.proj / "tools" / "buildlog" / "buildlog_server.py"), unit)   # quoted: space
        self.assertEqual(self.git("log", "-1", "--format=%B").strip(), "buildlog: build log and Your tasks panel")
        self.assertEqual(self.git("status", "--porcelain").strip(), "")

        v = subprocess.run([sys.executable, "-I", str(SCRIPTS / "verify.py"), "--project", str(self.proj), "--no-service"],
                           capture_output=True, text=True, env=self.env, timeout=600)
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        self.assertIn("0 failed", v.stdout)
        self.assertNotIn(cfg["secret"], v.stdout)

        self.assertIn("already up to date", self.setup("upgrade-page", "--project", str(self.proj), *self.homes).stdout)
        self.setup("set-url", "--project", str(self.proj), *self.homes, stdin="ftp://bad", check=False)
        self.assertNotEqual(self.setup("set-url", "--project", str(self.proj), *self.homes, stdin="ftp://bad", check=False).returncode, 0)
        out = self.setup("set-url", "--project", str(self.proj), *self.homes, stdin="https://relay.example/hook/SECRETPATH123456\n").stdout
        self.assertNotIn("SECRETPATH", out)
        self.assertFalse((cfg_dir / "t3-webhook-task.json").exists())
        self.assertEqual(json.loads((cfg_dir / "webhook.json").read_text())["url"], "https://relay.example/hook/SECRETPATH123456")
        old = cfg["secret"]
        self.setup("rotate-secret", "--project", str(self.proj), *self.homes)
        new = json.loads((cfg_dir / "webhook.json").read_text())["secret"]
        self.assertNotEqual(old, new)
        rot = json.loads((cfg_dir / "t3-rotate-secret.json").read_text())
        self.assertEqual(rot["arguments"]["schedule"]["signature"]["secret"], new)
        self.setup("delivery", "--project", str(self.proj), "local", *self.homes)
        self.assertEqual(json.loads((cfg_dir / "webhook.json").read_text())["delivery"], "local")

        self.setup("remove", "--project", str(self.proj), "--purge", "--files", *self.homes)
        self.assertFalse(cfg_dir.exists())
        self.assertFalse((self.proj / "tools" / "buildlog").exists())
        self.assertNotIn("project-buildlog", (self.proj / ".gitignore").read_text())

    def test_busy_port_is_refused_with_a_hint(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        r = self.setup("init", "--project", str(self.proj), "--port", str(s.getsockname()[1]), "--no-service",
                       *self.homes, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("in use by something else", r.stdout)
        self.assertIn("ad-hoc server", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
