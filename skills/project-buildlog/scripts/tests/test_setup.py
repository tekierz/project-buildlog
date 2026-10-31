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
import shlex
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]


def load_setup():
    import importlib.util
    spec = importlib.util.spec_from_file_location("bl_setup", SCRIPTS / "setup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
        (self.proj / "README.md").write_text("# P\n\n## Status\n\n- [x] Works ✓ (docs/evidence/a.txt)\n- [ ] Later\n", encoding="utf-8", newline="\n")
        (self.proj / "docs" / "evidence").mkdir(parents=True)
        (self.proj / "docs" / "evidence" / "a.txt").write_text("ok\n", encoding="utf-8", newline="\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.proj)] + list(args), capture_output=True, text=True, encoding="utf-8",
                              env=self.env, check=True).stdout

    def setup(self, *args, stdin=None, check=True):
        r = subprocess.run([sys.executable, "-I", str(SCRIPTS / "setup.py")] + list(args), input=stdin,
                           capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=300)
        if check:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_full_cycle(self):
        out = self.setup("init", "--project", str(self.proj), "--port", "33991", "--no-service", "--commit",
                         "--sections", "current,cards,timeline", *self.homes).stdout
        self.assertNotIn("FAIL", out)
        name = "my-project-buildlog"
        cfg_dir = Path(self._tmp.name) / "cfg" / name
        cfg = json.loads((cfg_dir / "webhook.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cfg["secret"]), 64)
        self.assertNotIn(cfg["secret"], out)
        if os.name == "nt":                            # an access list with only this account
            self.assertIsNone(load_setup().private_problem(cfg_dir / "webhook.json"))
            self.assertIsNone(load_setup().private_problem(cfg_dir, folder=True))
        else:
            self.assertEqual((cfg_dir / "webhook.json").stat().st_mode & 0o777, 0o600)
        args = json.loads((cfg_dir / "t3-webhook-task.json").read_text(encoding="utf-8"))["arguments"]
        sch = args["schedule"]
        self.assertEqual(sch["signature"], {"header": "x-my-project-signature", "encoding": "hex", "prefix": "sha256=",
                                            "secret": cfg["secret"]})
        self.assertEqual(sch["dedupe"], {"replayKey": "{{body.id}}"})
        self.assertEqual(sch["filters"], [{"path": "body.event", "op": "equals", "value": "buildlog_message"}])
        for field in ("kind", "task_id", "task_title", "done", "note", "sent_at"):
            self.assertIn("{{body.%s}}" % field, args["prompt"])
        self.assertNotIn("@@", args["prompt"])
        hb = json.loads((cfg_dir / "t3-heartbeat-task.json").read_text(encoding="utf-8"))["arguments"]
        self.assertEqual(hb["schedule"], {"type": "interval", "everyMs": 3600000})
        page = (self.proj / "docs" / "buildlog.html").read_text(encoding="utf-8")
        self.assertNotIn("@@", page)
        self.assertIn("<!-- CARDS:START -->", page)
        self.assertNotIn("<!-- GOALS:START -->", page)
        self.assertIn("<b class=\"n-s\">1</b> of 2 checks shown", page)
        self.assertIn('data-live-url="http://127.0.0.1:33991/docs/buildlog.html"', page)
        if os.name == "nt":                            # a logon task instead of a unit, plus a start script
            self.assertEqual(json.loads((self.proj / "tools" / "buildlog" / "buildlog.json").read_text(
                encoding="utf-8"))["service_manager"], "schtasks")
            script = (self.proj / "tools" / "buildlog" / "start-server.cmd").read_text(encoding="utf-8")
            self.assertIn('"%~dp0buildlog_server.py"', script)
        else:
            unit = (self.proj / "tools" / "buildlog" / "my-project-buildlog.service").read_text(encoding="utf-8")
            self.assertIn('"%s"' % (self.proj / "tools" / "buildlog" / "buildlog_server.py"), unit)   # quoted: space
        self.assertEqual(self.git("log", "-1", "--format=%B").strip(), "buildlog: build log and Your tasks panel")
        self.assertEqual(self.git("status", "--porcelain").strip(), "")
        for instruction in ("AGENTS.md", "CLAUDE.md"):
            text = (self.proj / instruction).read_text(encoding="utf-8")
            self.assertIn("buildlog-agent.md", text)
            self.assertLess(len(text), 700)
        safe = self.setup("t3-args", "--project", str(self.proj), "--redacted", *self.homes).stdout
        self.assertNotIn(cfg["secret"], safe)
        self.assertNotIn("secret", json.loads(safe)["schedule"]["signature"])

        v = subprocess.run([sys.executable, "-I", str(SCRIPTS / "verify.py"), "--project", str(self.proj), "--no-service"],
                           capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=600)
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        self.assertIn("0 failed", v.stdout)
        self.assertNotIn(cfg["secret"], v.stdout)

        self.assertIn("already up to date", self.setup("upgrade-page", "--project", str(self.proj), *self.homes).stdout)
        # Older pages gain controls while keeping project sections and snapshots.
        import re
        old_page = re.sub(r"<!-- DISPLAY:START -->.*?<!-- DISPLAY:END -->", "", page, flags=re.S)
        old_page = re.sub(r"/\* DISPLAY-CSS:START \*/.*?/\* DISPLAY-CSS:END \*/", "", old_page, flags=re.S)
        (self.proj / "docs/buildlog.html").write_text(old_page, encoding="utf-8")
        self.setup("upgrade-page", "--project", str(self.proj), *self.homes)
        upgraded = (self.proj / "docs/buildlog.html").read_text(encoding="utf-8")
        self.assertIn('id="display-theme"', upgraded)
        for marker in ("CURRENT", "CARDS", "TIMELINE", "TASKS-SNAPSHOT"):
            pattern = r"<!-- %s:START -->.*?<!-- %s:END -->" % (marker, marker)
            self.assertEqual(re.search(pattern, page, re.S).group(0), re.search(pattern, upgraded, re.S).group(0))
        self.setup("set-url", "--project", str(self.proj), *self.homes, stdin="ftp://bad", check=False)
        self.assertNotEqual(self.setup("set-url", "--project", str(self.proj), *self.homes, stdin="ftp://bad", check=False).returncode, 0)
        out = self.setup("set-url", "--project", str(self.proj), *self.homes, stdin="https://relay.example/hook/SECRETPATH123456\n").stdout
        self.assertNotIn("SECRETPATH", out)
        self.assertFalse((cfg_dir / "t3-webhook-task.json").exists())
        self.assertEqual(json.loads((cfg_dir / "webhook.json").read_text(encoding="utf-8"))["url"], "https://relay.example/hook/SECRETPATH123456")
        # Printing safe arguments must not recreate the key-bearing file that set-url removed.
        self.setup("t3-args", "--project", str(self.proj), "--redacted", *self.homes)
        self.assertFalse((cfg_dir / "t3-webhook-task.json").exists())
        old = cfg["secret"]
        self.setup("rotate-secret", "--project", str(self.proj), *self.homes)
        new = json.loads((cfg_dir / "webhook.json").read_text(encoding="utf-8"))["secret"]
        self.assertNotEqual(old, new)
        rot = json.loads((cfg_dir / "t3-rotate-secret.json").read_text(encoding="utf-8"))
        self.assertEqual(rot["arguments"]["schedule"]["signature"]["secret"], new)
        self.setup("delivery", "--project", str(self.proj), "local", *self.homes)
        self.assertEqual(json.loads((cfg_dir / "webhook.json").read_text(encoding="utf-8"))["delivery"], "local")

        self.setup("remove", "--project", str(self.proj), "--purge", "--files", *self.homes)
        self.assertFalse(cfg_dir.exists())
        self.assertFalse((self.proj / "tools" / "buildlog").exists())
        self.assertFalse((self.proj / "docs/buildlog-agent.md").exists())
        self.assertFalse((self.proj / "AGENTS.md").exists())        # init created it for the block alone
        self.assertNotIn("project-buildlog", (self.proj / ".gitignore").read_text(encoding="utf-8"))

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

    def test_instruction_blocks_preserve_user_text_and_refresh_once(self):
        (self.proj / "AGENTS.md").write_text("# My rules\nPush only when asked.\n", encoding="utf-8")
        (self.proj / "CLAUDE.md").write_text("# Claude rules\nKeep replies brief.\n", encoding="utf-8")
        args = ("init", "--project", str(self.proj), "--no-service", "--seed", "none", *self.homes)
        self.setup(*args)
        self.setup(*args)
        for name, original in (("AGENTS.md", "Push only when asked."), ("CLAUDE.md", "Keep replies brief.")):
            text = (self.proj / name).read_text(encoding="utf-8")
            self.assertIn(original, text)
            self.assertEqual(text.count("<!-- project-buildlog:START -->"), 1)
        self.assertEqual(self.git("log", "--format=%s").strip(), "init")

    def test_change_port_refreshes_rules_and_remove_keeps_user_text(self):
        (self.proj / "CLAUDE.md").write_text("# Claude rules\nKeep replies brief.\n", encoding="utf-8")
        self.setup("init", "--project", str(self.proj), "--port", "33992", "--no-service", "--seed", "none", *self.homes)
        self.setup("change-port", "--project", str(self.proj), "--port", "33993", *self.homes)
        for name in ("AGENTS.md", "CLAUDE.md", "docs/buildlog-agent.md"):
            text = (self.proj / name).read_text(encoding="utf-8")
            self.assertIn("127.0.0.1:33993/", text)
            self.assertNotIn(":33992/", text)
        self.setup("remove", "--project", str(self.proj), "--files", *self.homes)
        self.assertEqual((self.proj / "CLAUDE.md").read_text(encoding="utf-8"), "# Claude rules\nKeep replies brief.\n")
        self.assertFalse((self.proj / "AGENTS.md").exists())

    def test_refresh_keeps_custom_identity_paths_and_records(self):
        result = self.setup("init", "--project", str(self.proj), "--no-service", "--delivery", "local",
                            "--name", "chosen-log", "--title", "Chosen title", "--page", "site/status.html",
                            "--tools-dir", "scripts/status", "--seed", "none", "--sections", "current,timeline", *self.homes)
        cli = self.proj / "scripts/status/buildlog_agent.py"
        subprocess.run([sys.executable, "-I", str(cli), "work", "a", "--title", "Keep this work"], check=True, capture_output=True)
        subprocess.run([sys.executable, "-I", str(cli.with_name("buildlog_tasks.py")), "add", "--kind", "decision",
                        "--title", "Keep this decision", "--details", "Pick later"], check=True, capture_output=True)
        paths = [self.proj / "site/buildlog-state.json", self.proj / "site/buildlog-tasks.json",
                 Path(self._tmp.name) / "cfg/chosen-log/webhook.json"]
        before = [p.read_bytes() for p in paths]
        self.setup("init", "--project", str(self.proj), "--no-service", "--seed", "none")
        settings = json.loads((self.proj / "scripts/status/buildlog.json").read_text(encoding="utf-8"))
        self.assertEqual((settings["name"], settings["title"], settings["page"], settings["tools_dir"]),
                         ("chosen-log", "Chosen title", "site/status.html", "scripts/status"))
        self.assertEqual(settings["sections"], ["current", "timeline"])
        self.assertEqual([p.read_bytes() for p in paths], before)
        self.assertFalse((self.proj / "docs/buildlog.html").exists())
        command = result.stdout.split("Start it by hand with: ", 1)[1].splitlines()[0]
        last = shlex.split(command, posix=os.name != "nt")[-1].strip('"')
        self.assertEqual(last, str(cli.with_name("buildlog_server.py")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
