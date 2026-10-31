"""ScreenMark grammar for *.screen.md screenplays, ported from tekierz/screen-mark (MIT) `src/core/parser.ts`
and the note tags of `src/core/breakdown.ts`, so the review hub reads them with the Python standard library.

`parse()` returns the same document as ScreenMark's `parse()` (frontmatter + elements, zero-based `line`), plus two
keys ScreenMark doesn't have: `end` on every element (its last source line) and `line` on every dialogue part.
`ts_ast()` drops them again, for comparing the two parsers on the same files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# parser.ts regexes (lines are trimmed first; JS `\w` is ASCII, hence re.ASCII where it matters)
SCENE_RE = re.compile(r"^##\s+(.*?)(?:\s+#([^#\s][^#]*?)#)?\s*$")
SECTION_RE = re.compile(r"^(#{1,6})\s+(.*)$")
CHARACTER_RE = re.compile(r"^\*\*([^*]+)\*\*\s*(\^)?$")
PARENTHETICAL_RE = re.compile(r"^_\((.*)\)_$")
CENTERED_RE = re.compile(r"^>\s?(.*?)\s?<$")
DIALOG_RE = re.compile(r"^>\s?(.*)$")
LYRIC_RE = re.compile(r"^~\s?(.*)$")
TRANSITION_RE = re.compile(r"^@\s?(.*)$")
PAGEBREAK_RE = re.compile(r"^-{3,}$")
FRONTMATTER_KEY_RE = re.compile(r"^\s*([A-Za-z][\w ]*?)\s*:\s*(.*)$", re.ASCII)
NOTE_RE = re.compile(r"<!--[\s\S]*?(?:-->|\Z)")  # an unclosed note runs to the end of the file
# breakdown.ts: `@category: item, item` inside a note; a value runs to the next `@` or the end of the note
TAG_RE = re.compile(r"@([A-Za-z][\w-]*)\s*:\s*([^@]+)", re.ASCII)
SLUG_RE = re.compile(r"^(INT\.?/EXT\.?|I/E|INT|EXT|EST)[.\s]+(.*)$", re.I)

# String.prototype.trim(): ECMAScript WhiteSpace + LineTerminator (Python's str.strip() differs at the edges)
_JS_WS = " \t\n\r\v\f ﻿     　" + "".join(chr(c) for c in range(0x2000, 0x200B))


def js_trim(s: str) -> str:
    return s.strip(_JS_WS)


def _remove_notes(text: str) -> tuple[str, set[int]]:
    """parser.ts removeNotes: blank out notes, keeping their newlines, and the zero-based lines they cover."""
    note_lines: set[int] = set()
    out: list[str] = []
    line, prev_end, last = 0, 0, 0
    for m in NOTE_RE.finditer(text):
        line += text.count("\n", prev_end, m.start())
        newlines = m.group(0).count("\n")
        end_line = line + newlines
        note_lines.update(range(line, end_line + 1))  # empty lines inside a note are hidden too
        line, prev_end = end_line, m.end()
        out.append(text[last:m.start()])
        out.append("\n" * newlines)
        last = m.end()
    out.append(text[last:])
    return "".join(out), note_lines


def strip_notes(text: str) -> str:
    """Blank out `<!-- ... -->` notes while preserving line numbers."""
    return _remove_notes(text)[0]


def parse(text: str) -> dict:
    """ScreenMark parse(): {frontmatter: {key: value}, elements: [...]} (see the module docstring)."""
    visible, note_lines = _remove_notes(text)
    lines = re.split(r"\r?\n", visible)
    frontmatter: dict[str, str] = {}
    elements: list[dict] = []

    i = 0
    while i in note_lines and i < len(lines) and js_trim(lines[i]) == "":
        i += 1
    if i < len(lines) and js_trim(lines[i]) == "---":
        end = -1
        for j in range(i + 1, len(lines)):
            if js_trim(lines[j]) == "---":
                end = j
                break
        if end > 0:
            for j in range(i + 1, end):
                m = FRONTMATTER_KEY_RE.match(lines[j])
                if m:
                    frontmatter[m.group(1).lower()] = js_trim(m.group(2))
            i = end + 1

    dialogue: dict | None = None
    action: dict | None = None

    def flush_action() -> None:
        nonlocal action
        if action:
            elements.append({"kind": "action", "text": "\n".join(action["lines"]), "line": action["line"],
                             "end": action["end"]})
            action = None

    def append_action(t: str, ln: int) -> None:
        nonlocal dialogue, action
        dialogue = None
        if action:
            action["lines"].append(t)
            action["end"] = ln
        else:
            action = {"lines": [t], "line": ln, "end": ln}

    while i < len(lines):
        t = js_trim(lines[i])
        if t == "":
            if i not in note_lines:
                dialogue = None
                flush_action()
            i += 1
            continue
        if PAGEBREAK_RE.match(t):
            dialogue = None
            flush_action()
            elements.append({"kind": "pagebreak", "line": i, "end": i})
        elif m := SCENE_RE.match(t):
            dialogue = None
            flush_action()
            elements.append({"kind": "scene", "text": js_trim(m.group(1)), "number": m.group(2), "line": i, "end": i})
        elif m := SECTION_RE.match(t):
            dialogue = None
            flush_action()
            elements.append({"kind": "section", "text": js_trim(m.group(2)), "depth": len(m.group(1)), "line": i, "end": i})
        elif m := CHARACTER_RE.match(t):
            flush_action()
            dialogue = {"kind": "dialogue", "character": js_trim(m.group(1)), "dual": m.group(2) == "^", "parts": [],
                        "line": i, "end": i}
            elements.append(dialogue)
        elif m := CENTERED_RE.match(t):
            dialogue = None
            flush_action()
            elements.append({"kind": "centered", "text": m.group(1), "line": i, "end": i})
        elif m := DIALOG_RE.match(t):
            if dialogue is not None:
                dialogue["parts"].append({"type": "line", "text": m.group(1), "line": i})
                dialogue["end"] = i
            else:
                append_action(t, i)  # orphan blockquote: fall back to action, keep raw
        elif m := PARENTHETICAL_RE.match(t):
            if dialogue is not None:
                dialogue["parts"].append({"type": "parenthetical", "text": js_trim(m.group(1)), "line": i})
                dialogue["end"] = i
            else:
                append_action(t, i)
        elif m := LYRIC_RE.match(t):
            if dialogue is not None:
                dialogue["parts"].append({"type": "lyric", "text": m.group(1), "line": i})
                dialogue["end"] = i
            else:
                flush_action()
                elements.append({"kind": "lyric", "text": m.group(1), "line": i, "end": i})
        elif m := TRANSITION_RE.match(t):
            dialogue = None
            flush_action()
            elements.append({"kind": "transition", "text": js_trim(m.group(1)), "line": i, "end": i})
        else:
            append_action(t, i)
        i += 1
    flush_action()
    return {"frontmatter": frontmatter, "elements": elements}


def ts_ast(doc: dict) -> dict:
    """The document exactly as ScreenMark's parser returns it (JSON.stringify drops an undefined scene number)."""
    els = []
    for e in doc["elements"]:
        e = {k: v for k, v in e.items() if k != "end"}
        if e["kind"] == "scene" and e.get("number") is None:
            e.pop("number", None)
        if e["kind"] == "dialogue":
            e["parts"] = [{k: v for k, v in p.items() if k != "line"} for p in e["parts"]]
        els.append(e)
    return {"frontmatter": dict(doc["frontmatter"]), "elements": els}


# ---- notes and tags ------------------------------------------------------------------------------------

@dataclass
class Note:
    start: int        # zero-based first line
    end: int          # zero-based last line
    body: str         # text between <!-- and -->
    closed: bool = True


def notes(text: str) -> list[Note]:
    out = []
    for m in NOTE_RE.finditer(text):
        raw = m.group(0)
        closed = raw.endswith("-->") and len(raw) >= 7
        start = text.count("\n", 0, m.start())
        out.append(Note(start, start + raw.count("\n"), raw[4:-3] if closed else raw[4:], closed))
    return out


def note_tags(body: str) -> list[tuple[str, str]]:
    """`@key: value` pairs in a note, in order; keys lower-cased, whitespace in values collapsed (breakdown.ts)."""
    return [(m.group(1).lower(), re.sub(r"\s+", " ", m.group(2)).strip()) for m in TAG_RE.finditer(body)]


def note_text(body: str) -> str:
    """The free text of a note (everything that isn't a tag)."""
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", body)).strip()


def tag_items(value: str) -> list[str]:
    return [s for s in (re.sub(r"\s+", " ", x).strip() for x in value.split(",")) if s]


def parse_slug(text: str) -> dict:
    """breakdown.ts parseSlug: {int_ext, location, time_of_day} of a scene heading."""
    m = SLUG_RE.match(text)
    if not m:
        return {"int_ext": "OTHER", "location": text.upper(), "time_of_day": ""}
    prefix = m.group(1).upper()
    int_ext = "INT/EXT" if "/" in prefix else "EXT" if prefix == "EST" else prefix
    segs = re.split(r"\s+-\s+", m.group(2))
    if len(segs) > 1:
        return {"int_ext": int_ext, "location": " - ".join(segs[:-1]).upper().strip(), "time_of_day": segs[-1].upper().strip()}
    return {"int_ext": int_ext, "location": m.group(2).upper().strip(), "time_of_day": ""}


# pdf.ts preflight: Courier's WinAnsi coverage (printable ASCII, Latin-1 and these extras)
_WIN_ANSI_EXTRA = set("ƒ–—‘’‚“”„†‡•…€‰‹›ˆ™Œœ˜ŠšŸŽž")


def pdf_unsupported(text: str) -> list[str]:
    """Characters ScreenMark's PDF export refuses (it fails the whole export on the first one)."""
    bad = []
    for ch in text:
        c = ord(ch)
        if ch in "\n\r\t" or 0x20 <= c <= 0x7E or 0xA0 <= c <= 0xFF or ch in _WIN_ANSI_EXTRA:
            continue
        if ch not in bad:
            bad.append(ch)
    return bad
