#!/usr/bin/env python3
"""Behavior checks for compact handoffs, hooks and concurrent work/render writes."""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "buildlog_agent.py"


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = self.root / "tools/buildlog"
        self.tools.mkdir(parents=True)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        self.settings = self.tools / "buildlog.json"
        self.settings.write_text(json.dumps({"root": "../..", "name": "test", "title": "Test project",
                                             "page": "docs/buildlog.html", "tasks": "docs/buildlog-tasks.json",
                                             "_homes": {"state_home": str(self.root / "state")}}))
        self.tasks = self.docs / "buildlog-tasks.json"
        self.tasks.write_text(json.dumps({"tasks": [{"id": "choose", "kind": "decision", "status": "open",
                                                    "title": "Choose release", "details": "PRIVATE_LONG_BODY"}]}))
        self.page = self.docs / "buildlog.html"
        self.page.write_text("<h2>Timeline</h2>\n" + "\n".join("<!-- %s:START -->%s<!-- %s:END -->" % (marker, marker, marker)
                                       for marker in ("CURRENT", "TIMELINE", "UPDATED", "TASKS", "CARDS", "GOALS")))

    def run_cli(self, *args, stdin=None, check=True, settings=True):
        command = [sys.executable, "-I", str(SCRIPT)]
        if settings:
            command += ["--settings", str(self.settings)]
        result = subprocess.run(command + list(args), input=stdin, capture_output=True, text=True,
                                cwd=self.root, timeout=15)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def brief(self, *args):
        return json.loads(self.run_cli("brief", "--format", "json", *args).stdout)

    def test_upsert_hierarchy_and_invalid_update_does_not_write(self):
        self.run_cli("work", "parent", "--title", "Parent", "--owner", "orchestrator")
        self.run_cli("work", "child", "--title", "Child", "--parent", "parent", "--task-id", "real:t3-id")
        self.run_cli("work", "child", "--status", "blocked", "--summary", "Need answer")
        record = json.loads(self.run_cli("show", "child").stdout)
        self.assertEqual(record["parent"], "parent")
        self.assertEqual(record["task_id"], "real:t3-id")
        before = self.run_cli("show").stdout
        self.assertNotEqual(self.run_cli("work", "parent", "--parent", "child", check=False).returncode, 0)
        self.assertEqual(self.run_cli("show").stdout, before)
        self.assertNotEqual(self.run_cli("work", "orphan", "--title", "Orphan", "--parent", "absent", check=False).returncode, 0)

    def test_budget_priority_and_revision_covers_omitted_rows(self):
        for index in range(12):
            self.run_cli("work", "work-%02d" % index, "--title", "Long title " * 20,
                         "--summary", "Large summary " * 80, "--status", "blocked" if index == 11 else "running")
        report = self.brief("--limit", "1")
        self.assertEqual(report["work"][0]["id"], "work-11")
        self.assertEqual(report["omitted"]["work"], 11)
        revision = report["revision"]
        self.assertEqual(self.run_cli("brief", "--since", revision).stdout, "")
        self.run_cli("work", "work-10", "--summary", "Changed outside the projection")
        self.assertNotEqual(self.brief("--limit", "1")["revision"], revision)
        for mode in ("json", "text"):
            result = self.run_cli("brief", "--format", mode, "--max-chars", "600", "--limit", "20").stdout.rstrip("\n")
            self.assertLessEqual(len(result), 600)
            self.assertNotIn("PRIVATE_LONG_BODY", result)
            if mode == "json":
                json.loads(result)
            else:
                self.assertIn("truncated", result)

    def test_render_escapes_and_preserves_unowned_sections(self):
        self.run_cli("work", "a", "--title", '<script>alert("x")</script>', "--status", "running")
        self.run_cli("note", "<img src=x onerror=alert(1)>")
        self.run_cli("render")
        text = self.page.read_text()
        self.assertNotIn("<script>", text)
        self.assertNotIn("<img", text)
        self.assertIn("&lt;script&gt;", text)
        for marker in ("TASKS", "CARDS", "GOALS"):
            self.assertIn("<!-- %s:START -->%s<!-- %s:END -->" % (marker, marker, marker), text)
        self.assertEqual(text.count("<h2>Timeline</h2>"), 1)

    def test_parallel_writers_keep_every_record(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda i: self.run_cli("work", "job-%d" % i, "--title", "Job %d" % i), range(18)))
        self.assertEqual(len(results), 18)
        self.assertEqual(len(json.loads(self.run_cli("show").stdout)["work"]), 18)

    def test_unknown_schema_is_preserved_and_reported(self):
        state = self.docs / "buildlog-state.json"
        original = '{"version":2,"work":[],"notes":[]}'
        state.write_text(original)
        result = self.run_cli("work", "a", "--title", "A", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("unsupported work state", result.stderr)
        self.assertEqual(state.read_text(), original)

    def test_hook_protocol_and_silent_unconfigured_project(self):
        event = json.dumps({"hook_event_name": "SessionStart", "cwd": str(self.root), "session_id": "a"})
        result = self.run_cli("hook", stdin=event, settings=False)
        report = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(report["hookEventName"], "SessionStart")
        self.assertLessEqual(len(report["additionalContext"]), 2400)
        self.assertNotIn("PRIVATE_LONG_BODY", result.stdout)
        self.assertFalse((self.docs / "buildlog-state.json").exists())
        self.settings.unlink()
        result = self.run_cli("hook", stdin=event, settings=False)
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        result = self.run_cli("hook", stdin="not json", settings=False)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("not json", result.stderr)

    def test_render_reports_missing_sections_and_brief_skips_zero_counts(self):
        self.page.write_text("<!-- CURRENT:START --><!-- CURRENT:END --><!-- UPDATED:START --><!-- UPDATED:END -->")
        self.run_cli("work", "a", "--title", "A", "--status", "running")
        out = self.run_cli("render").stdout
        self.assertIn("CURRENT, UPDATED", out)
        self.assertIn("no TIMELINE section", out)
        self.assertEqual(self.brief()["counts"], {"running": 1})
        self.assertNotIn("omitted", self.run_cli("brief").stdout)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_commit_flag_commits_only_buildlog_files(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        git = lambda *args: subprocess.run(["git", "-C", str(self.root)] + list(args), env=env, check=True,
                                           capture_output=True, text=True).stdout
        git("init", "-q")
        git("config", "commit.gpgsign", "false")
        shutil.copy2(SCRIPT.with_name("buildlog_tasks.py"), self.tools / "buildlog_tasks.py")
        (self.root / "other.txt").write_text("unrelated")
        git("add", "-A")
        git("commit", "-q", "-m", "init")
        (self.root / "other.txt").write_text("changed, not to be committed")
        command = [sys.executable, "-I", str(SCRIPT), "--settings", str(self.settings)]
        for args in (("work", "a", "--title", "A", "--commit"), ("render", "--commit")):
            result = subprocess.run(command + list(args), capture_output=True, text=True, cwd=self.root, env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(git("log", "--format=%s").splitlines()[:2], ["buildlog: update", "buildlog: work a"])
        self.assertEqual(git("show", "--name-only", "--format=", "HEAD~1").split(), ["docs/buildlog-state.json"])
        self.assertEqual(git("show", "--name-only", "--format=", "HEAD").split(), ["docs/buildlog.html"])
        self.assertIn("other.txt", git("status", "--porcelain"))

    def test_state_dir_override_matches_task_cli(self):
        directory = self.root / "elsewhere"
        directory.mkdir()
        (directory / "inbox.jsonl").write_text(json.dumps({"type": "message", "payload": {"id": "m1"}}))
        result = subprocess.run([sys.executable, "-I", str(SCRIPT), "--settings", str(self.settings), "brief",
                                 "--format", "json"], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, BUILDLOG_STATE_DIR=str(directory)))
        self.assertEqual(json.loads(result.stdout)["inbox_pending"], 1)

    def test_inbox_count_excludes_delivered_and_read_messages(self):
        directory = self.root / "state/test"
        directory.mkdir(parents=True)
        records = [{"type": "message", "payload": {"id": key, "note": "SECRET_MESSAGE"}} for key in ("local", "sent", "read")]
        records += [{"type": "delivered", "id": "local", "via": "local"}, {"type": "delivered", "id": "sent"}]
        (directory / "inbox.jsonl").write_text("\n".join(json.dumps(row) for row in records))
        (directory / "read.json").write_text(json.dumps({"read": ["read"]}))
        report = self.brief()
        self.assertEqual(report["inbox_pending"], 1)
        self.assertNotIn("SECRET_MESSAGE", json.dumps(report))


if __name__ == "__main__":
    unittest.main(verbosity=2)
