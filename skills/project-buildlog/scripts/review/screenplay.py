#!/usr/bin/env python3
"""ScreenMark (*.screen.md) -> a screenplay review page for the review hub.

The grammar is ScreenMark's (screenmark.py here, or the file named by the profile's "grammar"). Notes follow the
usual placement rules: a note inside or directly above an element belongs to it, a note standing alone applies to
the next line. `@key: value` tags in notes become cue chips (camera, sound, music, ...). The page shows:

  - a title page with the estimated runtime, line, scene and word counts and the cast (click to filter),
  - outline blocks (`# Title`) with a runtime estimate each, scene headings, action, character cues,
    parentheticals, dialogue, transitions and lyrics, laid out like a printed screenplay (paper or dark),
  - picture-only shots (action with a shot tag) and cue chips that can be hidden,
  - every line numbered by its source line (data-line), so comment threads anchor to it and survive re-renders.

Project specifics come from the "screenplay" profile in buildlog.json (review.screenplay), set by `configure()`:
  words_per_second (2.5), pause_per_line (0.35), shot_seconds (8), shot_tags ([cam, dur, shot]),
  caption_characters ({"NAME": "note shown above that character's lines"}: lines shown as on-screen captions),
  character_colors ({"NAME": "#hex"}), chip_labels ({"tag": "label"}), grammar (a repo path to another screenmark.py).

`render(text)` -> dict(html=..., stats=..., toc=..., title=...); `render_page(text, ...)` -> a full HTML page.
"""
import html
import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:            # python -I does not add the script folder
    sys.path.insert(0, str(HERE))

AUDIO_TAG_RE = re.compile(r"\[[a-z][a-z ,'-]{0,40}\]")
CHIP_LABEL = {"cam": "cam", "graphic": "gfx", "text": "card", "sfx": "sfx", "music": "music", "look": "look", "dur": "dur",
              "set": "set", "take": "take", "motion": "motion", "audio": "audio", "voice": "voice", "character": "on screen",
              "gap": "gap", "prelap": "prelap", "emotion": "emotion", "shot": "shot", "vfx": "vfx", "prop": "prop"}
CHIP_CLASS = {"cam": "cam", "shot": "cam", "graphic": "gfx", "text": "gfx", "vfx": "gfx", "sfx": "sfx", "music": "music",
              "look": "look", "dur": "dur"}
DEFAULTS = {"words_per_second": 2.5, "pause_per_line": 0.35, "shot_seconds": 8.0, "shot_tags": ["cam", "dur", "shot"],
            "caption_characters": {}, "character_colors": {}, "chip_labels": {}, "grammar": ""}
PROFILE = dict(DEFAULTS)
_grammar_path = HERE / "screenmark.py"
_sm = None


def configure(profile=None, root=None):
    """Apply a project's screenplay profile (buildlog.json review.screenplay)."""
    global PROFILE, _grammar_path, _sm
    PROFILE = dict(DEFAULTS)
    PROFILE.update({k: v for k, v in (profile or {}).items() if k in DEFAULTS})
    PROFILE["caption_characters"] = {str(k).upper(): str(v) for k, v in (PROFILE["caption_characters"] or {}).items()}
    PROFILE["character_colors"] = {str(k).upper(): str(v) for k, v in (PROFILE["character_colors"] or {}).items()}
    g = PROFILE.get("grammar")
    new = (Path(root) / g if root and not Path(g).is_absolute() else Path(g)) if g else HERE / "screenmark.py"
    if new != _grammar_path:
        _grammar_path, _sm = new, None


def sm():
    global _sm
    if _sm is None:
        if not _grammar_path.is_file():
            raise RuntimeError("ScreenMark grammar not found at %s" % _grammar_path)
        spec = importlib.util.spec_from_file_location("review_screenmark", _grammar_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod          # dataclasses with postponed annotations look the module up
        spec.loader.exec_module(mod)
        _sm = mod
    return _sm


def fmt_time(seconds):
    seconds = int(round(seconds))
    return "%d:%02d" % (seconds // 60, seconds % 60)


def speech_seconds(text):
    words = len([w for w in AUDIO_TAG_RE.sub(" ", text).split() if re.search(r"\w", w)])
    return words / float(PROFILE["words_per_second"]) + float(PROFILE["pause_per_line"]) if words else 0.0


def who_slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _attach_notes(doc, all_notes):
    """Map element index -> list of (key, value) tags and free text, per ScreenMark's note placement rules."""
    elements = doc["elements"]
    tags = {i: [] for i in range(len(elements))}
    free = {i: [] for i in range(len(elements))}
    header = []        # notes before the first element: segment tags
    first_line = elements[0]["line"] if elements else 10 ** 9
    for n in all_notes:
        pairs = sm().note_tags(n.body)
        text = sm().note_text(n.body)
        if n.end < first_line:
            header.append((pairs, text, n.start))
            continue
        idx = None
        for i, e in enumerate(elements):
            if e["line"] <= n.start <= e["end"]:
                idx = i
                break
        if idx is None:
            nxt = next((i for i, e in enumerate(elements) if e["line"] > n.end), None)
            prev = next((i for i in range(len(elements) - 1, -1, -1) if elements[i]["end"] < n.start), None)
            directly_above = nxt is not None and elements[nxt]["line"] == n.end + 1
            directly_below = prev is not None and elements[prev]["end"] == n.start - 1
            if directly_above:
                idx = nxt
            elif directly_below and elements[prev]["kind"] in ("dialogue", "scene"):
                idx = prev
            elif nxt is not None:
                idx = nxt      # standing alone: the next line
            else:
                idx = len(elements) - 1
        tags[idx].extend(pairs)
        if text:
            free[idx].append(text)
    return header, tags, free


def _chips(pairs):
    out = []
    labels = dict(CHIP_LABEL, **(PROFILE.get("chip_labels") or {}))
    for k, v in pairs:
        if k == "id":
            continue
        if k not in labels:
            out.append("<span class='chip other' title='@%s'><b>%s</b> %s</span>" % (html.escape(k), html.escape(k), html.escape(v)))
            continue
        val = v if k != "dur" else "%s s" % v
        out.append("<span class='chip %s'><b>%s</b> %s</span>" % (CHIP_CLASS.get(k, "misc"), html.escape(labels[k]), html.escape(val)))
    return "<span class='sp-chips'>%s</span>" % "".join(out) if out else ""


def _dialogue_html(text):
    """Escape a spoken line, showing inline audio direction tags ([sighs]) as muted stage marks."""
    parts = AUDIO_TAG_RE.split(text)
    marks = AUDIO_TAG_RE.findall(text)
    out = []
    for i, p in enumerate(parts):
        out.append(html.escape(p))
        if i < len(marks):
            out.append("<span class='atag'>%s</span>" % html.escape(marks[i]))
    return "".join(out)


def render(text):
    doc = sm().parse(text)
    notes = sm().notes(text)
    header, tags, free = _attach_notes(doc, notes)
    elements = doc["elements"]
    fm = doc["frontmatter"]
    E = html.escape
    captions = PROFILE["caption_characters"]
    shot_tags = set(PROFILE["shot_tags"] or ())
    out = []
    stats = {"lines": 0, "words": 0, "shots": 0, "blocks": [], "scenes": 0, "characters": {}}
    seg_tags = [p for pairs, _, _ in header for p in pairs]

    out.append("<section class='sp-title'>")
    if fm.get("title"):
        out.append("<h1>%s</h1>" % E(fm["title"]))
    sub = " &middot; ".join(E(fm[k]) for k in ("credit", "author", "draft", "date") if fm.get(k))
    if sub:
        out.append("<p class='sp-sub'>%s</p>" % sub)
    out.append("<!--STATS-->")
    head_notes = [(t, start) for _, t, start in header if t]
    if seg_tags or head_notes:
        out.append("<details class='sp-headnote'><summary>%s</summary>%s%s</details>" % (
            "Writer's note and details" if head_notes and seg_tags else "Writer's note" if head_notes else "Details",
            "".join("<p data-line='%d'>%s</p>" % (start + 1, E(t)) for t, start in head_notes),
            "<p class='sp-segtags'>%s</p>" % "".join("<span class='chip seg'><b>%s</b> %s</span>" % (E(k), E(v)) for k, v in seg_tags)
            if seg_tags else ""))
    out.append("</section>")

    block = None
    blocks = []
    scenes = []
    caption_noted = set()

    def close_block():
        if block is not None:
            blocks.append(block)
            out.append("</section>")

    for i, e in enumerate(elements):
        kind = e["kind"]
        line_no = e["line"] + 1
        pairs = tags[i]
        notes_txt = free[i]
        note_html = "".join("<p class='sp-note'>%s</p>" % E(t) for t in notes_txt)
        if kind == "section":
            if e.get("depth", 1) == 1:
                close_block()
                block = {"title": e["text"], "seconds": 0.0, "index": len(blocks), "lines": 0}
                out.append("<section class='sp-block' id='block-%d' data-block='%d'><h2 class='sp-blockhead' data-line='%d'>"
                           "<span class='sp-blocknum'>%d</span><span class='sp-blocktitle'>%s</span>"
                           "<span class='sp-blocktime' data-block='%d'></span></h2>"
                           % (len(blocks) + 1, len(blocks), line_no, len(blocks) + 1, E(e["text"]), len(blocks)))
            else:
                out.append("<h3 class='sp-outline' data-line='%d'>%s</h3>" % (line_no, E(e["text"])))
        elif kind == "scene":
            stats["scenes"] += 1
            sid = "scene-%d" % stats["scenes"]
            scenes.append((sid, e["text"], block["index"] if block else -1))
            num = "<span class='sp-scenenum'>%s</span>" % E(e["number"]) if e.get("number") else ""
            out.append("<div class='sp-scene' id='%s' data-n='%s' data-line='%d'>%s<span class='sp-slug'>%s</span>%s</div>%s"
                       % (sid, E(e.get("number") or str(stats["scenes"]), True), line_no, num, E(e["text"]), _chips(pairs), note_html))
        elif kind == "action":
            is_shot = any(k in shot_tags for k, _ in pairs)
            text_html = E(e["text"]).replace("\n", "<br>")
            if is_shot:
                stats["shots"] += 1
                dur = next((v for k, v in pairs if k == "dur"), None)
                try:
                    secs = float(dur) if dur else float(PROFILE["shot_seconds"])
                except ValueError:
                    secs = float(PROFILE["shot_seconds"])
                if block is not None:
                    block["seconds"] += secs
                out.append("<div class='sp-shot' data-line='%d'><span class='sp-shotlabel'>SHOT</span>"
                           "<p class='sp-action'>%s</p>%s</div>%s" % (line_no, text_html, _chips(pairs), note_html))
            else:
                out.append("<p class='sp-action' data-line='%d'>%s%s</p>%s" % (line_no, text_html, _chips(pairs), note_html))
        elif kind == "dialogue":
            char = e["character"]
            base = re.sub(r"\s*\(.*?\)\s*", " ", char).strip().upper()
            ext = re.findall(r"\((.*?)\)", char)
            caption = base in captions
            stats["characters"][base] = stats["characters"].get(base, 0) + 1
            cls = "sp-dialogue" + (" dual" if e.get("dual") else "") + (" caption" if caption else "")
            color = PROFILE["character_colors"].get(base)
            style = " style='--who:%s'" % E(color, True) if color and re.match(r"^#[0-9a-fA-F]{3,8}$", color) else ""
            out.append("<div class='%s' data-who='%s'%s>" % (cls, E(base, True), style))
            out.append("<div class='sp-cue' data-line='%d' data-who='%s'><span class='sp-char'>%s</span>%s%s%s</div>" % (
                line_no, E(base, True), E(base), "".join(" <span class='sp-ext'>(%s)</span>" % E(x) for x in ext),
                " <span class='sp-dual' title='spoken over the previous line'>(CONT'D, overlapping)</span>" if e.get("dual") else "",
                _chips(pairs)))
            if caption:
                key = block["index"] if block else -1
                first = key not in caption_noted
                caption_noted.add(key)
                out.append("<div class='sp-caption-note%s'>%s</div>" % (" first" if first else "", E(captions[base])))
            for p in e["parts"]:
                pl = p["line"] + 1
                if p["type"] == "parenthetical":
                    out.append("<div class='sp-paren' data-line='%d'>(%s)</div>" % (pl, E(p["text"])))
                elif p["type"] == "line":
                    stats["lines"] += 1
                    secs = speech_seconds(p["text"])
                    stats["words"] += len(AUDIO_TAG_RE.sub(" ", p["text"]).split())
                    if block is not None:
                        block["seconds"] += secs
                        block["lines"] += 1
                    out.append("<div class='sp-line' data-line='%d' data-excerpt='%s: %s' data-who='%s'>%s</div>"
                               % (pl, E(base, True), E(p["text"][:120], True), E(base, True), _dialogue_html(p["text"])))
                else:
                    out.append("<div class='sp-lyric' data-line='%d'>%s</div>" % (pl, E(p["text"])))
            out.append(note_html)
            out.append("</div>")
        elif kind == "transition":
            out.append("<div class='sp-transition' data-line='%d'>%s</div>" % (line_no, E(e["text"])))
        elif kind == "centered":
            out.append("<div class='sp-centered' data-line='%d'>%s</div>" % (line_no, E(e["text"])))
        elif kind == "pagebreak":
            out.append("<hr class='sp-break'>")
        elif kind == "lyric":
            out.append("<div class='sp-lyric' data-line='%d'>%s</div>" % (line_no, E(e["text"])))
    close_block()

    total = sum(b["seconds"] for b in blocks)
    stats["runtime_s"] = total
    stats["blocks"] = [{"title": b["title"], "seconds": b["seconds"], "lines": b["lines"]} for b in blocks]
    body = "\n".join(out)
    for b in blocks:
        body = body.replace("<span class='sp-blocktime' data-block='%d'></span>" % b["index"],
                            "<span class='sp-blocktime'>%s &middot; %d line%s</span>" % (fmt_time(b["seconds"]), b["lines"],
                                                                                       "" if b["lines"] == 1 else "s"))
    cast = sorted(stats["characters"].items(), key=lambda kv: (-kv[1], kv[0]))
    stat_html = ("<div class='sp-stats'><span><b>%s</b> est. runtime</span><span><b>%d</b> spoken lines</span>"
                 "<span><b>%d</b> scenes</span><span><b>%d</b> words</span></div>"
                 "<div class='sp-cast' aria-label='Cast: click a name to follow that character'>%s</div>"
                 % (fmt_time(total), stats["lines"], stats["scenes"], stats["words"],
                    "".join("<button type='button' class='cast-chip' data-who='%s' aria-pressed='false'%s>%s <em>%d</em></button>"
                            % (E(c, True), (" style='--who:%s'" % E(PROFILE["character_colors"][c], True))
                               if c in PROFILE["character_colors"] else "", E(c), n) for c, n in cast)))
    body = body.replace("<!--STATS-->", stat_html)
    toc = [(b["title"], "block-%d" % (k + 1), fmt_time(b["seconds"])) for k, b in enumerate(blocks)]
    return {"html": body, "stats": stats, "toc": toc, "title": fm.get("title", ""), "scenes": scenes,
            "cast": [c for c, _ in cast]}


SCREENPLAY_CSS = """
/* the page: a printed screenplay (US letter proportions, Courier 12pt scaled) on paper or dark */
body.script{--paper:#fbfaf6;--ink:#1d1d1f;--ink2:#4a4a50;--ink3:#8a8a90;--rule:rgba(0,0,0,.1);--hl:rgba(255,214,10,.28);--who:var(--ink)}
body.script.dark-page{--paper:#16181c;--ink:#eceef1;--ink2:#b9bec6;--ink3:#7d838c;--rule:rgba(255,255,255,.1);--hl:rgba(45,212,191,.16)}
body.script article.doc{background:var(--paper);color:var(--ink);border:0;border-radius:4px;box-shadow:0 1px 0 rgba(0,0,0,.4),0 18px 50px rgba(0,0,0,.45);
  padding:56px 0 72px;font:16px/1.25 'Courier Prime','Courier New',Courier,monospace;width:100%;max-width:860px;justify-self:center}
.sp-page{width:62ch;max-width:100%;margin:0 auto;padding:0 2ch;position:relative}
.sp-title{text-align:center;padding:4px 0 26px;margin:0 0 18px;border-bottom:1px solid var(--rule);font-family:var(--font)}
.sp-title h1{font:700 26px/1.2 'Courier Prime','Courier New',monospace;text-transform:uppercase;letter-spacing:.5px;margin:0 0 10px;color:var(--ink)}
.sp-sub{color:var(--ink2);margin:0 0 14px;font-size:14px}
.sp-stats{display:flex;flex-wrap:wrap;gap:6px 20px;justify-content:center;color:var(--ink3);font-size:13px}
.sp-stats b{color:var(--ink);font-size:18px;font-weight:650;margin-right:4px;font-variant-numeric:tabular-nums}
.sp-cast{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin:12px 0 4px}
.cast-chip{font:600 12px var(--font);letter-spacing:.6px;text-transform:uppercase;border:1px solid var(--rule);background:transparent;color:var(--who);border-radius:99px;padding:3px 10px;cursor:pointer}
.cast-chip em{font-style:normal;color:var(--ink3);font-weight:400;margin-left:3px}
.cast-chip:hover{border-color:var(--ink3)}.cast-chip[aria-pressed=true]{background:var(--hl);border-color:var(--ink2)}
.sp-segtags{display:flex;flex-wrap:wrap;gap:5px;justify-content:center;margin:10px 0 0}
details.sp-headnote{margin:14px auto 0;text-align:left;font:14px/1.5 var(--font);color:var(--ink2);border:1px solid var(--rule);border-radius:8px;padding:0 12px;max-width:62ch}
details.sp-headnote summary{cursor:pointer;padding:7px 0;font-weight:600;color:var(--ink2)}
details.sp-headnote p{margin:0 0 10px}
.sp-block{padding:0 0 8px}.sp-block.off{display:none}
.sp-blockhead{display:flex;align-items:baseline;gap:10px;margin:34px 0 18px;padding:8px 0 6px;border-bottom:1px solid var(--rule);font:700 13px var(--font);letter-spacing:1.4px;text-transform:uppercase;color:var(--ink3)}
.sp-blocknum{font:700 11px var(--mono);border:1px solid var(--rule);border-radius:5px;padding:1px 7px;color:var(--ink2)}
.sp-blocktitle{color:var(--ink2)}
.sp-blocktime{margin-left:auto;font:500 12px var(--mono);text-transform:none;letter-spacing:0}
.sp-outline{font:600 12px var(--font);letter-spacing:1px;text-transform:uppercase;color:var(--ink3);margin:18px 0 8px}
.sp-scene{font-weight:700;text-transform:uppercase;margin:24px 0 12px;display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 10px}
.sp-scenenum{font-weight:400;color:var(--ink3);min-width:3ch;margin-left:-5ch;text-align:right}
.sp-action{margin:0 0 12px;white-space:normal}
.sp-shot{position:relative;margin:0 0 12px;padding:6px 10px 6px 12px;border-left:2px dashed var(--ink3);background:transparent}
.sp-shot .sp-action{margin:0}
.sp-shotlabel{display:block;font:700 10px var(--mono);letter-spacing:1.5px;color:var(--ink3);margin-bottom:2px}
.sp-dialogue{margin:0 0 12px}
.sp-dialogue.dual{border-left:2px solid var(--ink3);padding-left:1ch;margin-left:-1ch}
.sp-cue{margin-left:20ch;text-transform:uppercase}
.sp-char{color:var(--who);font-weight:700}
.sp-ext,.sp-dual{color:var(--ink2)}
.sp-paren{margin-left:15ch;max-width:25ch;color:var(--ink2)}
.sp-line{margin-left:10ch;max-width:35ch}
.sp-dialogue.caption .sp-line{border-left:2px solid var(--who);padding-left:1ch;margin-left:9ch}
.sp-dialogue.caption .sp-line::after{content:" (caption)";color:var(--ink3);font-size:13px}
.sp-caption-note{display:none;margin:2px 0 2px 10ch;font:12px var(--font);color:var(--ink3)}
.sp-caption-note.first{display:block}
.sp-lyric{margin-left:10ch;font-style:italic}
.sp-transition{text-align:right;text-transform:uppercase;margin:12px 0}
.sp-centered{text-align:center;margin:14px 0}
.sp-note{font:12.5px/1.45 var(--font);color:var(--ink3);margin:6px 0 14px;padding:2px 0 2px 10px;border-left:2px solid var(--rule);max-width:52ch}
.sp-note::before{content:"NOTE  ";font:700 9.5px var(--mono);letter-spacing:1px;color:var(--ink3)}
body.no-notes .sp-note,body.no-notes .sp-headnote{display:none}
.sp-dialogue .sp-note{margin-left:10ch}
.sp-break{border:0;border-top:1px dashed var(--rule);margin:30px 0}
.atag{color:var(--ink3);font-style:italic;margin-right:3px}
/* production cues */
.sp-chips{display:inline-flex;flex-wrap:wrap;gap:4px;margin-left:8px;vertical-align:1px;font-family:var(--font)}
.sp-cue .sp-chips{display:flex;margin:1px 0 2px;text-transform:none;opacity:.85}
.sp-scene .sp-chips{font-weight:400}
.sp-shot .sp-chips,.sp-action .sp-chips{display:flex;margin:4px 0 0}
body.no-cues .sp-chips,body.no-cues .sp-shotlabel{display:none}
body.no-cues .sp-shot{border-left:0;padding:0}
.chip{display:inline-flex;align-items:center;gap:4px;font:500 11px var(--mono);text-transform:none;letter-spacing:0;padding:0 7px;border-radius:99px;border:1px solid var(--rule);color:var(--ink2);background:transparent;white-space:nowrap;max-width:44ch;overflow:hidden;text-overflow:ellipsis;font-weight:500}
.chip b{font-weight:700;letter-spacing:.6px;text-transform:uppercase;font-size:9.5px;color:var(--ink3)}
.chip.cam{border-color:rgba(70,110,220,.45)}.chip.gfx{border-color:rgba(20,150,130,.45)}.chip.sfx{border-color:rgba(200,140,20,.5)}.chip.music{border-color:rgba(140,100,220,.5)}
/* source line numbers */
body.numbers .sp-page [data-line]{position:relative}
body.numbers .sp-page [data-line]::before{content:attr(data-line);position:absolute;left:calc(-6ch - var(--indent, 0ch));width:4ch;text-align:right;
  font:400 16px/1.25 'Courier Prime','Courier New',Courier,monospace;color:var(--ink3);opacity:.75;text-transform:none;background:none}
body.numbers .sp-cue{--indent:20ch}body.numbers .sp-line,body.numbers .sp-lyric{--indent:10ch}body.numbers .sp-paren{--indent:15ch}
body.numbers .sp-dialogue.caption .sp-line{--indent:calc(9ch + 2px)}
/* filters */
body.filtering .dim{opacity:.28}
body.filtering .sp-dialogue.match .sp-line{background:var(--hl)}
body.filter-hide .dim,body.filter-hide .sp-block.empty{display:none}
/* comments on the page */
body.script .commentable:hover{background:var(--hl)!important;box-shadow:none!important}
body.script .commentable.has-thread{background:none;box-shadow:none;text-decoration:underline wavy rgba(214,140,20,.55);text-decoration-skip-ink:none;text-underline-offset:4px}
body.script .commentable.has-thread.all-resolved{background:none}
body.script .commentable.is-target,body.script .commentable.flash{background:var(--hl)!important;box-shadow:none!important}
body.script .marker{right:-2ch}
body.script .th{font-family:var(--font);margin:6px 0 14px;max-width:none}
body.script:not(.dark-page) .th{background:#fff;color:#1d1d1f;border-color:rgba(0,0,0,.15)}
body.script:not(.dark-page) .th textarea{background:#fbfaf6;color:#1d1d1f;border-color:rgba(0,0,0,.18)}
body.script:not(.dark-page) .th .who{color:#2c5fd6}body.script:not(.dark-page) .th .m.agent .who{color:#0f8a4a}
body.script:not(.dark-page) .th .m{border-top-color:rgba(0,0,0,.08)}
body.script:not(.dark-page) .th .btn.ghost{color:#1d1d1f;border-color:rgba(0,0,0,.2)}
body.script:not(.dark-page) .th .kbd{background:#f0efe9;color:#444;border-color:rgba(0,0,0,.15)}
.sp-dialogue .th{margin-left:10ch}
.filters{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.filters .count{font:12px var(--mono);color:var(--muted)}
.sp-toc a{display:flex;justify-content:space-between;gap:8px}.sp-toc a span{color:var(--muted);font:12px var(--mono)}
.sp-toc .sc{padding-left:12px;font-size:12.5px;color:var(--muted)}
@media (max-width:760px){body.script article.doc{padding:28px 0 40px;font-size:14.5px}.sp-cue{margin-left:12ch}.sp-paren{margin-left:8ch}.sp-line{margin-left:4ch}
  .sp-caption-note,.sp-lyric,.sp-dialogue .sp-note,.sp-dialogue .th{margin-left:4ch}.sp-scenenum{margin-left:0}}
"""


def render_page(text, title=None, ctx=None, doc_rel="", source_note=""):
    from md_render import page_shell     # the same shell, breadcrumb and comment threads as the Markdown pages
    r = render(text)
    E = html.escape
    title = title or r["title"] or "Script"
    toc_rows = []
    for k, (t, hid, secs) in enumerate(r["toc"]):
        toc_rows.append("<a href='#%s'>%s<span>%s</span></a>" % (hid, E(t), secs))
        inside = [(sid, st) for sid, st, b in r["scenes"] if b == k]
        if len(inside) > 1:                    # one scene per block is the block itself
            toc_rows += ["<a class='sc' href='#%s'>%s</a>" % (sid, E(st)) for sid, st in inside]
    if not r["toc"]:
        toc_rows = ["<a class='sc' href='#%s'>%s</a>" % (sid, E(st)) for sid, st, _ in r["scenes"]]
    toc = "<nav class='toc sp-toc'><h2>Blocks &middot; %s</h2>%s</nav>" % (fmt_time(r["stats"]["runtime_s"]), "".join(toc_rows)) \
        if toc_rows else ""
    tools = ("<div class='filters'><select class='tbtn' id='f-who' aria-label='Follow one character'><option value=''>All characters</option>%s</select>"
             "<select class='tbtn' id='f-block' aria-label='Show one block'><option value=''>Whole script</option>%s</select>"
             "<button type='button' class='tbtn' id='f-only' aria-pressed='false' title='Hide everything the filter does not match'>Only matches</button>"
             "<span class='count' id='f-count'></span></div><span class='grow'></span>"
             "<button type='button' class='tbtn' data-pref='paper' aria-pressed='true' title='Paper or dark page'>Paper page</button>"
             "<button type='button' class='tbtn' data-pref='cues' aria-pressed='true' title='Camera, graphic, sound and music cues'>Production cues</button>"
             "<button type='button' class='tbtn' data-pref='notes' aria-pressed='true' title=\"Writers' notes and fact checks\">Notes</button>"
             "<button type='button' class='tbtn' data-pref='numbers' aria-pressed='false' title='Source line numbers (comments are anchored to them)'>Line numbers</button>"
             % ("".join("<option value='%s'>%s</option>" % (E(c, True), E(c)) for c in r["cast"]),
                "".join("<option value='%s'>%d. %s</option>" % (hid, k + 1, E(t)) for k, (t, hid, _) in enumerate(r["toc"]))))
    body = "<div class='sp-page'>%s</div>" % r["html"]
    return page_shell(title, body, ctx=ctx, doc_rel=doc_rel, toc_html=toc, tools_html=tools, extra_css=SCREENPLAY_CSS,
                      body_class="script", source_note=source_note,
                      foot_hint="Click any line to comment on it; the chips are the camera, graphic, sound and music cues.")


if __name__ == "__main__":
    src = open(sys.argv[1], encoding="utf-8").read()
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(render_page(src, doc_rel=sys.argv[1]))
