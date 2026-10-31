#!/usr/bin/env python3
"""Tests for buildlog_progress.py: counts from a fixture status file, and the page rewrite.

Everything runs in temporary directories. When a buildlog.json sits next to the
scripts (a set-up project), a copy of the project's page is rewritten too.
Run: python3 -I <tools dir>/tests/test_buildlog_progress.py
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("buildlog_progress", TOOLS / "buildlog_progress.py")
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)

README = """# Demo part

## Build

- [x] not a status item: it is outside the Status section

## Status

Intro text. A tick means the sentence was seen.

- [x] Plain tick, no mark. Counts only in the total.
- [x] Shown line. ✓ Boot exits 0 ([log](../../docs/evidence/2026-10-25/demo/boot.log)).
- [x] Shown over a wrapped line. ✓ Run on main
  ([hash](../../docs/evidence/2026-10-25/demo/hash.txt#L3)).
- [x] Mark but the file is not there. ✓ ([log](../../docs/evidence/2026-10-25/demo/missing.log))
- [x] Mark, no link at all ✓ (same log)
- [x] Mark, but the link is not evidence ✓ ([notes](../../docs/RULES.md))
- [x] Plain path counts too. ✓ See docs/evidence/2026-10-25/demo/boot.log.
- [x] Partly line. Partly shown: one scripted run ([log](../../docs/evidence/2026-10-25/demo/boot.log)).
- [x] Mark and partly. ✓ (partly shown: frames are not in git) ([log](../../docs/evidence/2026-10-25/demo/boot.log))
- [~] Tilde line with no words
- [ ] Unticked, partly shown: seen once on an older build
- [ ] Unticked line, not yet checked
  - nested bullet with a box inside: [x] is not a new item
- Bullet without a box: not an item
  ✓ and with a mark that must not be attributed to the previous item
- [ ] Item after a bullet
```
- [x] inside a code fence is not an item
```

Paragraph after the list.

## Later section

- [x] also not a status item
"""

EXPECT = {"shown": 3, "partly": 4, "total": 13, "ticked_only": 1}   # see README above
ROOTS = ("docs/evidence",)


def make_repo(root, with_evidence=True):
    (root / "parts" / "demo").mkdir(parents=True)
    (root / "parts" / "demo" / "README.md").write_text(README, encoding="utf-8", newline="\n")
    (root / "parts" / "other").mkdir(parents=True)
    (root / "parts" / "other" / "README.md").write_text("# Other\n\n## Status\n\n- [x] one ✓ ([l](../../docs/evidence/x/y.txt))\n- [ ] two\n",
                                                       encoding="utf-8", newline="\n")
    (root / "parts" / "bare").mkdir(parents=True)
    (root / "parts" / "bare" / "README.md").write_text("# Bare\n\nNo status list here.\n", encoding="utf-8", newline="\n")
    (root / "docs").mkdir()
    (root / "docs" / "RULES.md").write_text("rules\n", encoding="utf-8", newline="\n")
    if with_evidence:
        ev = root / "docs" / "evidence" / "2026-10-25" / "demo"
        ev.mkdir(parents=True)
        (ev / "boot.log").write_text("exit 0\n", encoding="utf-8", newline="\n")
        (ev / "hash.txt").write_text("abc\n", encoding="utf-8", newline="\n")
        ev2 = root / "docs" / "evidence" / "x"
        ev2.mkdir(parents=True)
        (ev2 / "y.txt").write_text("y\n", encoding="utf-8", newline="\n")


CARD = """  <div class="card">
    <div class="body">
      <h3>%s</h3>
      <div><span class="tag">area</span></div>
      <div class="stage">Stage line</div>
      <ul><li class="ok">a thing</li></ul>
      <div class="stage">Evidence: %s</div>
    </div>
  </div>
"""


def page(cards, tail="<p>untouched tail</p>\n"):
    return ("<!doctype html>\n<!-- rules comment -->\n<style>.x{}</style>\n"
            "<!-- TASKS:START -->\n<p>tasks panel</p>\n<!-- TASKS:END -->\n"
            "<h2>Status</h2>\n<!-- CARDS:START -->\n<p class=\"muted\">intro</p>\n<div class=\"cards\">\n\n"
            + "\n".join(cards) + "\n</div>\n<!-- CARDS:END -->\n<h2>Timeline</h2>\n" + tail)


def link(part, text="status list"):
    return '<a class="status-src" href="../parts/%s/README.md">%s</a>' % (part, text)


def clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


class CountTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        make_repo(self.root)

    def test_items_only_from_status_section(self):
        items = bp.status_items(README)
        self.assertEqual(len(items), EXPECT["total"])
        self.assertTrue(all(i.startswith(("- [x]", "- [ ]", "- [~]")) for i in items))
        self.assertFalse(any("outside the Status" in i or "also not a status" in i or "code fence" in i.split("\n")[0] for i in items))
        self.assertIn("nested bullet", items[-2])             # nested bullet stays inside its parent
        self.assertFalse(any("must not be attributed" in i for i in items))

    def test_counts(self):
        ev = bp.Evidence(self.root, ROOTS, use_git=False)
        got = bp.count_file(self.root / "parts" / "demo" / "README.md", self.root, ev)
        for k, v in EXPECT.items():
            self.assertEqual(got[k], v, k)
        self.assertEqual(len(got["marked_without_evidence"]), 3)   # missing file, no link, link that is not evidence
        self.assertEqual(got["shown"] + got["partly"] + got["ticked_only"] + 3 + 2, got["total"])  # 2 open lines left

    def test_partly_beats_mark(self):
        ev = bp.Evidence(self.root, ROOTS, use_git=False)
        r = self.root / "parts" / "demo" / "README.md"
        item = "- [x] Mark and partly. ✓ (partly shown) ([log](../../docs/evidence/2026-10-25/demo/boot.log))"
        self.assertEqual(bp.classify(item, r, self.root, ev), "partly")

    def test_custom_evidence_folder(self):
        (self.root / "proof").mkdir()
        (self.root / "proof" / "run.txt").write_text("ok\n", encoding="utf-8", newline="\n")
        ev = bp.Evidence(self.root, ("proof",), use_git=False)
        r = self.root / "parts" / "demo" / "README.md"
        self.assertEqual(bp.classify("- [x] Done ✓ see proof/run.txt", r, self.root, ev), "shown")
        self.assertEqual(bp.classify("- [x] Done ✓ see docs/evidence/x/y.txt", r, self.root, ev), "marked-no-evidence")

    def test_no_evidence_files_means_nothing_shown(self):
        shutil.rmtree(self.root / "docs" / "evidence")
        ev = bp.Evidence(self.root, ROOTS, use_git=False)
        got = bp.count_file(self.root / "parts" / "demo" / "README.md", self.root, ev)
        self.assertEqual((got["shown"], got["partly"], got["total"]), (0, 4, 13))

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_evidence_must_be_in_git(self):
        def git(*a):
            subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"] + list(a),
                           check=True, capture_output=True, env=clean_env())
        git("init", "-q")
        git("add", "parts", "docs/RULES.md", "docs/evidence/x")      # boot.log and hash.txt stay untracked
        git("commit", "-q", "-m", "x")
        with mock.patch.dict(os.environ, clean_env(), clear=True):
            ev = bp.Evidence(self.root, ROOTS)
        self.assertEqual(ev.mode, "git HEAD")
        got = bp.count_file(self.root / "parts" / "demo" / "README.md", self.root, ev)
        self.assertEqual(got["shown"], 0)
        self.assertEqual(bp.count_file(self.root / "parts" / "other" / "README.md", self.root, ev)["shown"], 1)
        git("add", "-f", "docs/evidence")
        git("commit", "-q", "-m", "evidence")
        with mock.patch.dict(os.environ, clean_env(), clear=True):
            ev = bp.Evidence(self.root, ROOTS)
        got = bp.count_file(self.root / "parts" / "demo" / "README.md", self.root, ev)
        self.assertEqual(got["shown"], EXPECT["shown"])


class PageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        make_repo(self.root)
        self.page = self.root / "docs" / "buildlog.html"
        self.page.write_text(page([
            CARD % ("Demo &amp; friends", link("demo") + " · " + link("other", "second list") + " · "
                    '<a href="../parts/demo/README.md">a plain link is not a source</a>'),
            CARD % ("Bare", link("bare")),
            CARD % ("No link", "nothing"),
        ]), encoding="utf-8", newline="\n")
        self.none = self.root / "none.json"

    def run_cli(self, *args):
        r = subprocess.run([sys.executable, "-I", str(TOOLS / "buildlog_progress.py"), "--settings", str(self.none),
                            "--repo", str(self.root), "--no-git"] + list(args), capture_output=True, text=True, encoding="utf-8", env=clean_env())
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_json_output(self):
        before = self.page.read_text(encoding="utf-8")
        data = json.loads(self.run_cli().stdout)
        self.assertEqual(self.page.read_text(encoding="utf-8"), before, "no --write, no change")
        cards = {c["title"]: c for c in data["cards"]}
        d = cards["Demo & friends"]
        self.assertEqual(d["sources"], ["parts/demo/README.md", "parts/other/README.md"])
        self.assertEqual((d["shown"], d["partly"], d["total"]), (4, 4, 15))
        self.assertEqual((cards["Bare"]["shown"], cards["Bare"]["total"]), (0, 0))
        self.assertIsNone(cards["No link"]["total"])
        self.assertEqual(data["files"]["parts/demo/README.md"]["total"], 13)

    def test_write_edits_only_the_cards_section(self):
        before = self.page.read_text(encoding="utf-8")
        self.run_cli("--write")
        after = self.page.read_text(encoding="utf-8")
        a, b = before.index(bp.START), before.index(bp.END)
        a2, b2 = after.index(bp.START), after.index(bp.END)
        self.assertEqual(before[:a], after[:a2])
        self.assertEqual(before[b:], after[b2:])
        self.assertIn("<p>tasks panel</p>", after)
        section = after[a2:b2]
        self.assertEqual(section.count('<div class="prog"'), 2)       # the card with no source link gets none
        self.assertIn("4</b> of 15 checks shown, <b class=\"n-p\">4</b> partly", section)
        self.assertIn("no status checklist found", section)
        self.assertEqual(section.count('id="prog-how"'), 1)
        self.assertIn('--w:53.33%', section)
        self.assertRegex(section, r'<i class="s" style="flex:4"></i><i class="p" style="flex:4"></i>')
        # the bar goes right after the tags row, before the stage text
        self.assertRegex(section, r'<span class="tag">area</span></div>\n\s+<div class="prog".*\n\s+<div class="stage">Stage line')

    def test_second_run_changes_nothing_and_stale_bars_are_replaced(self):
        self.run_cli("--write")
        once = self.page.read_text(encoding="utf-8")
        r = self.run_cli("--write")
        self.assertIn("unchanged", r.stderr)
        self.assertEqual(self.page.read_text(encoding="utf-8"), once)
        readme = self.root / "parts" / "other" / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "- [x] three\n", encoding="utf-8", newline="\n")
        self.run_cli("--write")
        now = self.page.read_text(encoding="utf-8")
        self.assertEqual(now.count('<div class="prog"'), 2)
        self.assertEqual(now.count('class="muted prog-note"'), 1)
        self.assertIn("of 16 checks shown", now)
        self.assertNotIn("of 15 checks shown", now)

    def test_helper_removing_a_bar_gets_it_back(self):
        self.run_cli("--write")
        once = self.page.read_text(encoding="utf-8")
        stripped = re.sub(r'^[ \t]*<div class="prog".*\n', "", once, count=1, flags=re.M)
        stripped = re.sub(r'^<p class="muted prog-note".*\n', "", stripped, flags=re.M)
        self.page.write_text(stripped, encoding="utf-8", newline="\n")
        self.run_cli("--write")
        self.assertEqual(self.page.read_text(encoding="utf-8"), once)

    def test_write_keeps_file_mode_and_leaves_no_temp_files(self):
        os.chmod(self.page, 0o640)
        self.run_cli("--write")
        if os.name != "nt":                            # Windows has no file modes
            self.assertEqual(self.page.stat().st_mode & 0o777, 0o640)
        self.assertEqual(sorted(p.name for p in self.page.parent.iterdir() if p.is_file()), ["RULES.md", "buildlog.html"])

    def test_page_without_markers_is_an_error(self):
        self.page.write_text("<html>no markers</html>", encoding="utf-8", newline="\n")
        r = subprocess.run([sys.executable, "-I", str(TOOLS / "buildlog_progress.py"), "--settings", str(self.none),
                            "--repo", str(self.root), "--no-git", "--write"], capture_output=True, text=True, encoding="utf-8", env=clean_env())
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.page.read_text(encoding="utf-8"), "<html>no markers</html>")


SETTINGS = TOOLS / "buildlog.json"


@unittest.skipUnless(SETTINGS.is_file(), "no buildlog.json next to the scripts (not a set-up project)")
class ProjectPageTests(unittest.TestCase):
    """The project's page: a rewrite touches only the CARDS section, and the CSS respects reduced motion."""

    @classmethod
    def setUpClass(cls):
        s = json.loads(SETTINGS.read_text(encoding="utf-8"))
        cls.root = (TOOLS / s.get("root", "../..")).resolve()
        cls.page = cls.root / s["page"]
        cls.has_cards = "cards" in s.get("sections", [])

    def test_rewrite_of_a_copy_stays_inside_the_cards_section(self):
        if not self.has_cards:
            self.skipTest("no CARDS section in this project")
        text = self.page.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(dir=str(self.page.parent)) as d:
            copy = Path(d) / "buildlog.html"
            copy.write_text(text, encoding="utf-8", newline="\n")
            r = subprocess.run([sys.executable, "-I", str(TOOLS / "buildlog_progress.py"), "--page", str(copy), "--write"],
                               capture_output=True, text=True, encoding="utf-8", env=clean_env())
            self.assertEqual(r.returncode, 0, r.stderr)
            new = copy.read_text(encoding="utf-8")
        a, b = text.index(bp.START), text.index(bp.END)
        a2, b2 = new.index(bp.START), new.index(bp.END)
        self.assertEqual(text[:a], new[:a2])
        self.assertEqual(text[b:], new[b2:])

    def test_css_has_reduced_motion_rules_for_the_bars(self):
        text = self.page.read_text(encoding="utf-8")
        css = text[text.index("<style>"):text.index("</style>")]
        self.assertIn("prefers-reduced-motion: reduce", css)
        block = css[css.index("prefers-reduced-motion: reduce"):]
        for needle in (".prog-fill", "animation: none", ".cards .card:hover { transform: none; }"):
            self.assertIn(needle, block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
