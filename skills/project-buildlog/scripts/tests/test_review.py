#!/usr/bin/env python3
"""Tests for the review hub: review.py (CLI), md_render.py and screenplay.py (document pages), the hub page
template, and the build log server's /api/review endpoints. Standard library only.

Runs from the skill (scripts/tests/), from a project the skill installed (<tools>/tests/) and from a project
that keeps the hub in its own folder (tools/review/tests/ beside tools/buildlog/).
"""
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import unittest

HERE = Path(__file__).resolve().parent
if (HERE.parent / "review.py").is_file():                       # tools/review/tests (hub in its own folder)
    REVIEW_SRC, BUILDLOG_SRC = HERE.parent, HERE.parent.parent / "buildlog"
else:                                                           # <tools>/tests or the skill's scripts/tests
    REVIEW_SRC, BUILDLOG_SRC = HERE.parent / "review", HERE.parent
TEMPLATE = next(p for p in (REVIEW_SRC / "hub_template.html", BUILDLOG_SRC.parent / "templates" / "review-hub.html")
                if p.is_file())
PNG_1PX = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63f8cfc0f01f0005030101d6c1ad1a0000000049454e44ae426082")
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32

SCRIPT = """---
title: Test Episode
credit: written by
author: The Writers
---

<!-- @id: seg_test @set: studio -->
<!-- Writers' draft for the tests. -->

# Cold open

## INT. STUDIO - NIGHT #1#

Lights half up.

<!-- @cam: host_push @sfx: impact_boom -->
**HOST**
_(intense)_
> Seven weeks. Zero trades.

**ROBOT**
_(earnest)_
> Maybe they're shy.

<!-- @cam: graphic @graphic: slam @text: ZERO TRADES @dur: 2 -->
Full-frame slam graphic.

# Sign-off

## INT. STUDIO - NIGHT #2#

**HOST**
_(deadpan)_
> [sighs] Sit down.
"""

MD = """# Title here

Intro paragraph with **bold**, _em_, `code` and a [link](other.md) plus a [clip](../media/x.mp4).

## Section A

- one
- two
  - nested
- [x] done task

| a | b |
|---|---|
| 1 | 2 |

```py
x = 1
```

> quoted

### Sub
Text <b>raw</b> stays escaped.
"""


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def fresh(*names):
    for k in list(sys.modules):
        if k in names:
            del sys.modules[k]


class RendererTests(unittest.TestCase):
    def setUp(self):
        fresh("md_render", "screenplay", "review_screenmark")
        sys.path.insert(0, str(REVIEW_SRC))
        self.addCleanup(sys.path.remove, str(REVIEW_SRC))
        self.md = load("md_render", REVIEW_SRC / "md_render.py")
        self.sp = load("screenplay", REVIEW_SRC / "screenplay.py")
        self.sp.configure({})

    def test_markdown_blocks_and_inline(self):
        body, toc, title = self.md.render(MD)
        self.assertEqual(title, "Title here")
        for want in ("<strong>bold</strong>", "<em>em</em>", "<code>code</code>", "<ul>", "<li>nested",
                     "type='checkbox' disabled checked", "<table>", "<pre><code class='lang-py'>x = 1</code></pre>", "<blockquote>"):
            self.assertIn(want, body)
        self.assertIn("&lt;b&gt;raw&lt;/b&gt;", body)        # raw HTML is escaped, never passed through
        self.assertEqual([(lvl, label) for lvl, _, label in toc], [(2, "Section A"), (3, "Sub")])

    def test_markdown_resolver_rewrites_links(self):
        seen = []

        def resolve(url, kind):
            seen.append((url, kind))
            return "R:" + url
        body, _, _ = self.md.render(MD, resolve=resolve)
        self.assertIn(("other.md", "link"), seen)
        self.assertIn("href='R:other.md'", body)
        self.assertIn("class='media-link' href='R:../media/x.mp4'", body)

    def test_markdown_page_has_breadcrumb_rail_and_line_anchors(self):
        ctx = {"brand": "Acme", "hub": "../index.html", "item_id": "it-1", "item_title": "Read the plan"}
        page = self.md.render_page(MD, ctx=ctx, doc_rel="docs/x.md")
        self.assertIn("<nav class='toc'>", page)
        self.assertIn("data-line='1'", page)
        self.assertIn("data-item='it-1'", page)
        self.assertIn("data-doc='docs/x.md'", page)
        self.assertIn("/api/review/answer", page)
        self.assertIn("href='../index.html#item=it-1'", page)          # breadcrumb back to the item
        self.assertIn(">Read the plan</a>", page)
        self.assertIn("id='rail-list'", page)                          # the comments rail
        self.assertIn("'review-nav'", page)                            # framed pages ask the hub to navigate

    def test_screenplay_render(self):
        r = self.sp.render(SCRIPT)
        html = r["html"]
        self.assertEqual(r["title"], "Test Episode")
        self.assertEqual(r["stats"]["lines"], 3)
        self.assertEqual(len(r["stats"]["blocks"]), 2)
        self.assertEqual(r["stats"]["scenes"], 2)
        self.assertEqual(r["cast"], ["HOST", "ROBOT"])
        self.assertIn("<b>cam</b> host_push", html)                   # cue chips
        self.assertIn("<b>sfx</b> impact_boom", html)
        self.assertIn("sp-shot", html)                                # picture-only shot
        self.assertIn("<b>card</b> ZERO TRADES", html)
        self.assertIn("<span class='atag'>[sighs]</span>", html)      # audio tags shown as stage marks
        self.assertIn("Writer's note", html)
        self.assertIn("data-line='17' data-who='HOST'", html)        # the HOST cue line, 1-based
        self.assertIn("data-line='19' data-excerpt='HOST: Seven weeks. Zero trades.'", html)
        self.assertIn("class='cast-chip' data-who='ROBOT'", html)
        self.assertGreater(r["stats"]["runtime_s"], 2)
        self.assertEqual(r["toc"][0][0], "Cold open")

    def test_screenplay_profile_captions_and_colors(self):
        self.sp.configure({"caption_characters": {"Robot": "The robot beeps; the caption reads:"},
                           "character_colors": {"HOST": "#ff8800"}, "words_per_second": 1})
        r = self.sp.render(SCRIPT)
        self.assertIn("sp-dialogue caption", r["html"])
        self.assertIn("The robot beeps; the caption reads:", r["html"])
        self.assertIn("--who:#ff8800", r["html"])
        slow = r["stats"]["runtime_s"]
        self.sp.configure({})
        self.assertLess(self.sp.render(SCRIPT)["stats"]["runtime_s"], slow)

    def test_screenplay_page(self):
        page = self.sp.render_page(SCRIPT, ctx={"brand": "Acme", "hub": "../index.html", "item_id": "ep1", "item_title": "Episode 1"},
                                   doc_rel="docs/show/x.screen.md")
        self.assertIn("<nav class='toc sp-toc'>", page)
        self.assertIn("href='../index.html#item=ep1'", page)
        self.assertIn("data-item='ep1'", page)
        self.assertIn("<body class='script'", page)
        self.assertIn("id='f-who'", page)                             # follow one character
        self.assertIn("<option value='block-2'>2. Sign-off</option>", page)
        self.assertIn("data-pref='paper'", page)


class TemplateTests(unittest.TestCase):
    """The hub page and review.py must agree on what a clip, a sound and an image are."""

    def setUp(self):
        self.tpl = TEMPLATE.read_text(encoding="utf-8")

    def test_media_kind_tables_match(self):
        m = re.search(r"var MEDIA_KINDS = \{video: \[(.*?)\], audio: \[(.*?)\],\s*image: \[(.*?)\]\}", self.tpl, re.S)
        self.assertTrue(m, "MEDIA_KINDS not found in the hub template")
        page = [sorted("." + x.strip(" '") for x in g.split(",")) for g in m.groups()]
        src = (REVIEW_SRC / "review.py").read_text(encoding="utf-8")

        def tup(name):
            return sorted(re.findall(r"\"(\.[a-z0-9]+)\"", re.search(r"^%s = \((.*?)\)$" % name, src, re.M).group(1)))
        self.assertEqual(page, [tup("VIDEO_EXT"), tup("AUDIO_EXT"), tup("IMAGE_EXT")])

    def test_candidates_and_compares_use_the_shared_kind_lookup(self):
        # Regression: audition candidates with .mp4 media were drawn as audio players (an extension regex in one
        # place and another table elsewhere). Every media decision goes through mediaKind() now.
        self.assertIn("var k = mediaKind(p)", self.tpl)
        self.assertNotRegex(self.tpl, r"/\\\.\((mp4|mp3|png)\|")
        self.assertNotIn("Stu", self.tpl)
        for placeholder in ("@@BRAND@@", "@@LIVE_URL@@", "@@SNAPSHOT@@"):
            self.assertIn(placeholder, self.tpl)

    def test_hash_routes_and_keys(self):
        for key in ("view", "kind", "prio", "tag", "q", "item", "doc", "line", "media", "t"):
            self.assertIn("'%s'" % key, self.tpl.split("function buildHash")[1][:400])
        for k in ("case 'j'", "case 'k'", "case 'a'", "case 'n'", "case 'c'", "case 'o'", "case 's'", "case 'x'", "case '/'", "case ','"):
            self.assertIn(k, self.tpl)


class CliBase(unittest.TestCase):
    """A temporary project with the build log scripts in tools/buildlog and the hub in tools/buildlog/review."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "repo"
        tools = self.root / "tools" / "buildlog"
        (tools / "review").mkdir(parents=True)
        for f in ("buildlog_tasks.py", "buildlog_server.py"):
            shutil.copy2(BUILDLOG_SRC / f, tools / f)
        for f in ("review.py", "md_render.py", "screenplay.py", "screenmark.py"):
            shutil.copy2(REVIEW_SRC / f, tools / "review" / f)
        shutil.copy2(TEMPLATE, tools / "review" / "hub_template.html")
        (tools / "buildlog.json").write_text(json.dumps({
            "name": "demo-review", "title": "Demo", "root": "../..", "port": 1, "page": "docs/buildlog.html",
            "tasks": "docs/buildlog-tasks.json", "review_hub": True,
            "review": {"pages": [{"src": "docs/LOG.md", "out": "log.html", "title": "Timeline", "nav": "Timeline"}]}}), encoding="utf-8")
        for d in ("docs/show", "docs/media"):
            (self.root / d).mkdir(parents=True)
        (self.root / "docs" / "LOG.md").write_text("# Build log\n\n## 2026-10-30 Thing\n- done, see [notes](show/notes.md)\n", encoding="utf-8")
        (self.root / "docs" / "show" / "ep.screen.md").write_text(SCRIPT, encoding="utf-8")
        (self.root / "docs" / "show" / "notes.md").write_text(MD.replace("other.md", "ep.screen.md"), encoding="utf-8")
        for name in ("a.png", "b.png"):
            (self.root / "docs" / "media" / name).write_bytes(PNG_1PX)
        for name in ("reel.mp4", "x.mp4", "take1.mp4", "take2.mp4"):
            (self.root / "docs" / "media" / name).write_bytes(MP4)
        for name in ("v1.mp3", "v2.wav"):
            (self.root / "docs" / "media" / name).write_bytes(b"ID3" + b"\x00" * 64)
        self.outside = Path(self._tmp.name) / "outside.png"
        self.outside.write_bytes(PNG_1PX)
        self.state = Path(self._tmp.name) / "state"
        fresh("review", "md_render", "screenplay", "buildlog_tasks", "review_screenmark")
        self.review = load("review", tools / "review" / "review.py")
        self.assertEqual(self.review.ROOT, self.root.resolve())

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = self.review.main(["--state-dir", str(self.state)] + list(args))
        except SystemExit as e:
            code = e.code
        finally:
            sys.stdout, sys.stderr = old
        return code, out.getvalue(), err.getvalue()

    def ok(self, *args):
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 0, "%s\n%s%s" % (args, out, err))
        return out, err

    def items(self):
        return json.loads((self.root / "docs" / "review" / "items.json").read_text(encoding="utf-8"))["items"]

    def hub(self):
        return (self.root / "docs" / "review" / "index.html").read_text(encoding="utf-8")


class CliTests(CliBase):
    def test_add_renders_docs_media_and_hub(self):
        out, _ = self.ok("add", "--id", "ep1", "--kind", "approve", "--title", "Episode 1", "--summary", "Read it.",
                         "--body", "See [notes](docs/show/notes.md).", "--priority", "high", "--tag", "Show, scripts",
                         "--doc", "docs/show/ep.screen.md::The script", "--doc", "docs/show/notes.md",
                         "--media", "docs/media/reel.mp4::Reel", "--compare", "docs/media/a.png::docs/media/b.png::Cards")
        self.assertEqual(out.strip(), "ep1")
        it = self.items()[0]
        self.assertEqual((it["kind"], it["priority"], it["tags"]), ("approve", "high", ["show", "scripts"]))
        self.assertEqual(it["docs"][0]["kind"], "script")
        self.assertEqual(it["docs"][0]["html"], "docs/review/docs/show--ep.screen.html")
        self.assertEqual(it["docs"][1]["kind"], "md")
        self.assertEqual(it["media"][0]["kind"], "video")
        self.assertEqual(it["compare"][0]["kind"], "image")
        self.assertIn("href='docs/show--notes.html?item=ep1'", it["body_html"])
        script_page = (self.root / it["docs"][0]["html"]).read_text(encoding="utf-8")
        self.assertIn("data-item='ep1'", script_page)
        self.assertIn(">Episode 1</a>", script_page)                   # breadcrumb: the item
        self.assertIn("<span class='brand'>Demo</span>", script_page)  # breadcrumb: the hub, branded from settings
        notes_page = (self.root / it["docs"][1]["html"]).read_text(encoding="utf-8")
        self.assertIn("href='show--ep.screen.html?item=ep1'", notes_page)      # a linked .md becomes its page
        self.assertIn("href='../../../docs/media/x.mp4'", notes_page)         # media links point at the file
        hub = self.hub()
        self.assertIn('id="items-snapshot"', hub)
        self.assertIn("Episode 1", hub)
        self.assertIn("<title>Demo review</title>", hub)
        self.assertNotIn("@@", hub.split("<script")[0])
        snap = json.loads(re.search(r'id="items-snapshot">(.*?)</script>', hub, re.S).group(1))
        self.assertEqual((snap["root"], snap["docs_dir"], snap["hub_dir"]), ("../../", "docs", "docs/review"))
        self.assertEqual([n["label"] for n in snap["nav"]], ["Build log", "Timeline"])

    def test_audition_candidates_keep_clip_and_sound_media(self):
        self.ok("add", "--id", "aud", "--kind", "audition", "--title", "T", "--summary", "S",
                "--candidate", "R0::Original::docs/media/take1.mp4", "--candidate", "R1::Graded::docs/media/take2.mp4",
                "--candidate", "V::Voice::docs/media/v1.mp3", "--recommend", "R1")
        cands = {c["id"]: c for c in self.items()[0]["candidates"]}
        self.assertEqual([self.review.media_kind(p) for c in cands.values() for p in c["media"]], ["video", "video", "audio"])
        self.assertTrue(cands["R1"]["recommended"])
        self.assertEqual(cands["R1"]["rank"], 1)

    def test_missing_path_fails_loudly(self):
        code, out, err = self.run_cli("add", "--id", "x", "--kind", "info", "--title", "T", "--summary", "S", "--doc", "docs/show/nope.md")
        self.assertNotEqual(code, 0)
        self.assertIn("no such file", str(code) + err)
        self.assertFalse((self.root / "docs" / "review" / "items.json").exists())

    def test_body_link_to_missing_file_fails(self):
        code, _, _ = self.run_cli("add", "--id", "x", "--kind", "info", "--title", "T", "--summary", "S", "--body", "see [x](docs/nope.md)")
        self.assertIn("not a file in the repo", str(code))

    def test_media_outside_repo_is_copied_in(self):
        _, err = self.ok("add", "--id", "x", "--kind", "info", "--title", "T", "--summary", "S", "--media", str(self.outside))
        path = self.items()[0]["media"][0]["path"]
        self.assertTrue(path.startswith("docs/review/media/"), path)
        self.assertTrue((self.root / path).is_file())
        self.assertIn("copied", err)

    def test_doc_outside_repo_is_refused(self):
        other = Path(self._tmp.name) / "o.md"
        other.write_text("# o\n", encoding="utf-8")
        code, _, _ = self.run_cli("add", "--id", "x", "--kind", "info", "--title", "T", "--summary", "S", "--doc", str(other))
        self.assertIn("outside the repo", str(code))

    def test_validation(self):
        bad = [(["--kind", "choice", "--choices", "Only one"], "at least two"),
               (["--kind", "info", "--priority", "whenever"], "invalid choice"),
               (["--kind", "info", "--tag", "Bad!Tag"], "--tag"),
               (["--kind", "info", "--compare", "docs/media/a.png::docs/media/x.mp4"], "like with like"),
               (["--kind", "audition", "--candidate", "A::a::docs/media/v1.mp3"], "at least two")]
        for extra, want in bad:
            err = io.StringIO()
            old = sys.stderr
            sys.stderr = err
            try:
                code, _, e2 = self.run_cli("add", "--id", "c", "--title", "T", "--summary", "S", *extra)
            finally:
                sys.stderr = old
            self.assertIn(want, str(code) + e2 + err.getvalue(), extra)
        self.ok("add", "--id", "c", "--kind", "choice", "--title", "T", "--summary", "S", "--choices", "Keep $15|Raise to $50")
        self.assertEqual([c["id"] for c in self.items()[0]["choices"]], ["keep-15", "raise-to-50"])

    def test_json_spec_update_close_reply_list(self):
        spec = {"id": "j", "kind": "choice", "title": "J", "summary": "S", "choices": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                "on_answer": "run x", "tags": ["voice"]}
        p = Path(self._tmp.name) / "spec.json"
        p.write_text(json.dumps(spec), encoding="utf-8")
        self.ok("add", "--json", str(p))
        self.ok("update", "j", "--title", "J2", "--importance", "urgent")
        it = self.items()[0]
        self.assertEqual((it["title"], it["priority"], it["on_answer"], it["tags"]), ("J2", "urgent", "run x", ["voice"]))
        self.ok("reply", "j", "working on it", "--media", "docs/media/a.png")
        self.ok("close", "j", "--reply", "done: picked B")
        it = self.items()[0]
        self.assertEqual(it["status"], "closed")
        self.assertEqual([m["text"] for m in it["thread"]], ["working on it", "done: picked B"])
        self.assertEqual(it["thread"][0]["media"][0]["path"], "docs/media/a.png")
        out, _ = self.ok("list")
        self.assertIn("(no open items)", out)
        out, _ = self.ok("list", "--all", "--json")
        self.assertEqual((json.loads(out)[0]["id"], json.loads(out)[0]["bucket"]), ("j", "closed"))
        self.assertNotIn("run x", self.hub())      # agent-only field never reaches the page

    def test_snooze_list_and_due(self):
        for i, prio in enumerate(("low", "urgent", "normal")):
            self.ok("add", "--id", "i%d" % i, "--kind", "info", "--title", "Item %d" % i, "--summary", "S", "--priority", prio)
        self.ok("snooze", "i0", "--for", "3d", "--reply", "after the release")
        self.ok("snooze", "i2", "--until", "2020-01-01")                   # already due: back in the list
        it0, it2 = self.items()[0], self.items()[2]
        self.assertEqual((it0["snooze"]["by"], it0["snooze"]["later"]), ("agent", False))
        self.assertEqual(it0["thread"][-1]["text"], "after the release")
        out, _ = self.ok("list")
        self.assertLess(out.index("Back from snooze"), out.index("Needs the user"))
        self.assertLess(out.index("Needs the user"), out.index("Snoozed"))
        self.assertRegex(out, r"Back from snooze.*\n.*i2")
        self.assertRegex(out, r"== Snoozed \(1\)\n.*i0 .*until 20")
        rows = {r["id"]: r for r in json.loads(self.ok("list", "--json")[0])}
        self.assertEqual({k: v["bucket"] for k, v in rows.items()}, {"i0": "snoozed", "i1": "open", "i2": "due"})
        self.ok("snooze", "i1", "--later")
        self.assertEqual(json.loads(self.ok("show", "i1")[0])["bucket"], "snoozed")
        self.ok("unsnooze", "i1")
        self.assertEqual(json.loads(self.ok("show", "i1")[0])["bucket"], "open")
        code, _, _ = self.run_cli("snooze", "i1", "--for", "soon")
        self.assertIn("--for takes", str(code))
        # the person's snooze from the page (server state) counts too, and the newer one wins
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "state.json").write_text(json.dumps({"tasks": {}, "review": {"i1": {"snooze": {
            "later": True, "until": None, "at": "2099-01-01T00:00:00Z", "by": "you"}}}}), encoding="utf-8")
        self.assertEqual(json.loads(self.ok("show", "i1")[0])["bucket"], "snoozed")

    def test_thread_replies_and_resolve(self):
        self.ok("add", "--id", "ep1", "--kind", "approve", "--title", "Episode 1", "--summary", "S", "--doc", "docs/show/ep.screen.md")
        out, _ = self.ok("reply", "ep1", "Cut it.", "--line", "19")
        self.assertIn("line:docs/show/ep.screen.md#19", out)
        entry = self.items()[0]["thread"][-1]
        self.assertEqual(entry["anchor"], {"type": "line", "line": 19, "doc": "docs/show/ep.screen.md", "excerpt": "> Seven weeks. Zero trades."})
        self.ok("reply", "ep1", "Graded.", "--at", "1.5", "--clip", "docs/media/reel.mp4")
        self.assertEqual(self.items()[0]["thread"][-1]["anchor"]["time_s"], 1.5)
        self.ok("resolve", "ep1", "--line", "19", "--reply", "Done in v2.")
        it = self.items()[0]
        self.assertTrue(it["resolved"]["line:docs/show/ep.screen.md#19"]["resolved"])
        self.ok("resolve", "ep1", "--line", "19", "--undo")
        self.assertFalse(self.items()[0]["resolved"]["line:docs/show/ep.screen.md#19"]["resolved"])
        code, _, _ = self.run_cli("resolve", "ep1")
        self.assertIn("resolve needs", str(code))
        code, _, _ = self.run_cli("reply", "ep1", "x", "--at", "2")
        self.assertIn("--at needs --clip", str(code))

    def test_sync_preview_and_extra_pages(self):
        self.ok("sync")
        log = (self.root / "docs" / "review" / "log.html").read_text(encoding="utf-8")
        self.assertIn("Timeline", log)
        self.assertIn("href='docs/show--notes.html'", log)          # links from the page next to the hub
        self.assertTrue((self.root / "docs" / "review" / "docs" / "show--notes.html").is_file())
        out, _ = self.ok("preview", "docs/show/ep.screen.md")
        self.assertTrue(out.strip().endswith("docs/review/docs/show--ep.screen.html"))


class AnswerPathTests(CliBase):
    """POST /api/review/answer on the build log server -> inbox.jsonl -> review inbox --json."""

    def setUp(self):
        super().setUp()
        fresh("buildlog_server")
        self.bs = load("buildlog_server", self.root / "tools" / "buildlog" / "buildlog_server.py")
        self.ok("add", "--id", "ep1", "--kind", "approve", "--title", "Episode 1", "--summary", "S",
                "--doc", "docs/show/ep.screen.md", "--media", "docs/media/reel.mp4")
        self.ok("add", "--id", "aud", "--kind", "audition", "--title", "Voice", "--summary", "S",
                "--candidate", "E::Fargo::docs/media/v1.mp3", "--candidate", "F::Growl::docs/media/v2.wav")
        cfg = Path(self._tmp.name) / "webhook.json"
        cfg.write_text(json.dumps({"delivery": "local"}), encoding="utf-8")
        (self.root / "docs" / "buildlog-tasks.json").write_text('{"tasks": []}', encoding="utf-8")
        self.server = self.bs.make_server(self.root, self.state, cfg, port=0, log=io.StringIO(), name="demo-review",
                                          page="docs/buildlog.html", tasks="docs/buildlog-tasks.json")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": "127.0.0.1:%d" % self.port}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h.update({"Content-Type": "application/json", "X-Buildlog": "1"})
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, json.loads(raw) if raw and r.getheader("Content-Type", "").startswith("application/json") else raw, r

    def answer(self, body, want=200):
        status, j, _ = self.request("POST", "/api/review/answer", body)
        self.assertEqual(status, want, (body, j))
        return j

    def test_items_api_merges_answers_and_hides_agent_fields(self):
        status, j, _ = self.request("GET", "/api/review/items")
        self.assertEqual(status, 200)
        self.assertEqual([i["id"] for i in j["items"]], ["ep1", "aud"])
        self.assertIsNone(j["items"][0]["answer"])
        self.assertNotIn("on_answer", j["items"][0])
        self.assertEqual((j["items"][0]["snooze"], j["items"][0]["resolved"]), (None, {}))
        self.assertEqual(j["delivery"], "local")
        status, one, _ = self.request("GET", "/api/review/items?id=aud")
        self.assertEqual([i["id"] for i in one["items"]], ["aud"])
        self.assertEqual(self.request("GET", "/api/review/items?id=nope")[0], 404)

    def test_answer_round_trip(self):
        j = self.answer({"item_id": "ep1", "decision": "needs_changes", "comment": "Cut the sit down line.",
                         "anchor": {"type": "line", "line": 32, "excerpt": "HOST: Sit down.", "doc": "docs/show/ep.screen.md"}})
        self.assertTrue(j["delivered"])
        self.assertEqual((j["status"], j["answer"]["decision"], j["answer"]["anchor"]["line"]), ("local", "needs_changes", 32))
        self.answer({"item_id": "aud", "decision": "pick", "choice": "E"})
        j3 = self.answer({"item_id": "ep1", "decision": "comment", "comment": "music too loud",
                          "anchor": {"type": "time", "time_s": 12.34, "media": "docs/media/reel.mp4", "excerpt": "Reel @ 0:12"}})
        self.assertEqual(j3["answer"]["anchor"]["time_s"], 12.34)
        status, items, _ = self.request("GET", "/api/review/items")
        ep1 = items["items"][0]
        self.assertEqual(ep1["answer"]["decision"], "needs_changes")     # comments do not replace the decision
        self.assertEqual([a["decision"] for a in ep1["answers"]], ["needs_changes", "comment"])
        self.assertEqual(items["items"][1]["answer"]["choice"], "E")
        lines = [json.loads(l) for l in (self.state / "inbox.jsonl").read_text(encoding="utf-8").splitlines()]
        msgs = [l["payload"] for l in lines if l["type"] == "message"]
        self.assertEqual([m["kind"] for m in msgs], ["review"] * 3)
        self.assertEqual((msgs[0]["item_id"], msgs[0]["decision"], msgs[0]["anchor"]["type"]), ("ep1", "needs_changes", "line"))
        out, _ = self.ok("inbox", "--json")
        shown = json.loads(out)
        self.assertEqual(len(shown), 3)
        self.assertEqual((shown[0]["item_title"], shown[0]["comment"]), ("Episode 1", "Cut the sit down line."))
        self.assertEqual(shown[0]["thread"], "line:docs/show/ep.screen.md#32")
        _, err = self.ok("inbox", "--mark-read")
        self.assertIn("marked read: 3", err)
        self.assertEqual(json.loads(self.ok("inbox", "--json")[0]), [])
        # a retry of the same click (same id) does not add a second answer
        self.answer({"item_id": "aud", "decision": "pick", "choice": "E", "id": msgs[1]["id"]})
        status, items, _ = self.request("GET", "/api/review/items")
        self.assertEqual(len(items["items"][1]["answers"]), 1)
        # the agent answers in the thread and the page sees it on the item
        self.ok("reply", "ep1", "Cut in v2.", "--to", msgs[0]["id"])
        status, items, _ = self.request("GET", "/api/review/items?id=ep1")
        self.assertEqual(items["items"][0]["thread"][-1]["anchor"]["line"], 32)

    def test_snooze_and_resolve_are_kept_not_forwarded(self):
        hook = Path(self._tmp.name) / "webhook.json"
        hook.write_text(json.dumps({"url": "http://127.0.0.1:9/hook", "secret": "s" * 64}), encoding="utf-8")   # nothing listens
        j = self.answer({"item_id": "ep1", "decision": "snooze", "until": "2030-01-02T09:00:00+01:00"})
        self.assertEqual((j["status"], j["delivered"]), ("local", True))   # kept for agents; no webhook wake-up
        self.assertEqual(j["answer"]["until"], "2030-01-02T08:00:00Z")
        status, items, _ = self.request("GET", "/api/review/items?id=ep1")
        self.assertEqual(items["items"][0]["snooze"]["until"], "2030-01-02T08:00:00Z")
        self.assertIsNone(items["items"][0]["answer"])                    # a snooze is not an answer
        self.answer({"item_id": "ep1", "decision": "unsnooze"})
        status, items, _ = self.request("GET", "/api/review/items?id=ep1")
        self.assertIsNone(items["items"][0]["snooze"])
        self.answer({"item_id": "ep1", "decision": "snooze", "later": True})
        anchor = {"type": "line", "line": 19, "doc": "docs/show/ep.screen.md", "excerpt": "HOST: Seven weeks."}
        self.answer({"item_id": "ep1", "decision": "resolve", "anchor": anchor})
        status, items, _ = self.request("GET", "/api/review/items?id=ep1")
        it = items["items"][0]
        self.assertTrue(it["snooze"]["later"])
        self.assertTrue(it["resolved"]["line:docs/show/ep.screen.md#19"]["resolved"])
        self.assertEqual(json.loads(self.ok("show", "ep1")[0])["bucket"], "snoozed")
        out, _ = self.ok("inbox")
        self.assertIn("snooze for later", out)
        self.assertIn("resolve", out)
        self.ok("snooze", "ep1", "--for", "1h")                           # the agent's newer snooze wins
        status, items, _ = self.request("GET", "/api/review/items?id=ep1")
        self.assertEqual(items["items"][0]["snooze"]["by"], "agent")
        for bad in ({"item_id": "ep1", "decision": "snooze"}, {"item_id": "ep1", "decision": "snooze", "until": "soon"},
                    {"item_id": "ep1", "decision": "resolve"}):
            self.answer(bad, 400)

    def test_answer_validation(self):
        bad = [({"item_id": "nope", "decision": "approve"}, 404),
               ({"item_id": "ep1", "decision": "maybe"}, 400),
               ({"item_id": "aud", "decision": "pick", "choice": "Z"}, 400),
               ({"item_id": "ep1", "decision": "approve", "choice": "E"}, 400),
               ({"item_id": "ep1", "decision": "comment", "comment": "  "}, 400),
               ({"item_id": "ep1", "decision": "approve", "anchor": {"type": "page"}}, 400),
               ({"item_id": "ep1", "decision": "approve", "anchor": {"type": "line", "line": "x"}}, 400)]
        for body, want in bad:
            self.answer(body, want)
        status, j, _ = self.request("POST", "/api/review/answer", {"item_id": "ep1", "decision": "approve"}, headers={"X-Buildlog": ""})
        self.assertEqual(status, 403)
        status, j, _ = self.request("POST", "/api/send", {"kind": "review", "note": "x"})
        self.assertEqual(status, 400)

    def test_version_framing_and_ranges(self):
        status, j, _ = self.request("GET", "/api/version")
        self.assertIsNotNone(j["review_mtime"])
        status, raw, r = self.request("GET", "/docs/review/docs/show--ep.screen.html")
        self.assertEqual(status, 200)
        self.assertEqual(r.getheader("X-Frame-Options"), "SAMEORIGIN")   # the hub shows documents in a frame
        status, raw, r = self.request("GET", "/docs/media/reel.mp4", headers={"Range": "bytes=4-11"})
        self.assertEqual((status, raw, r.getheader("Content-Range")), (206, MP4[4:12], "bytes 4-11/%d" % len(MP4)))
        self.assertEqual(r.getheader("Accept-Ranges"), "bytes")
        status, raw, r = self.request("GET", "/docs/media/reel.mp4", headers={"Range": "bytes=-4"})
        self.assertEqual((status, raw), (206, MP4[-4:]))
        status, raw, r = self.request("GET", "/docs/media/reel.mp4", headers={"Range": "bytes=999-"})
        self.assertEqual(status, 416)
        status, raw, r = self.request("GET", "/docs/media/v1.mp3")
        self.assertEqual((status, r.getheader("Content-Type")), (200, "audio/mpeg"))


if __name__ == "__main__":
    unittest.main()
