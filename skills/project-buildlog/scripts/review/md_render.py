#!/usr/bin/env python3
"""Markdown to HTML for the review hub (standard library only).

Agents write Markdown; the user never opens it. `render(text)` turns the common
subset used in this repo's docs into clean HTML with heading ids and a table of
contents: ATX headings, paragraphs, fenced code, blockquotes, nested bullet and
numbered lists, task lists, GFM tables, horizontal rules, HTML comments
(dropped), and inline code, emphasis, strikethrough, links, images and bare
URLs. Raw HTML in the source is escaped, never passed through.

`render_page(text, title, ...)` wraps the result in the review page shell
(`page_shell`, shared with screenplay.py): a breadcrumb back to the hub and the
item, comment threads on any paragraph, list item, heading or table row, and a
comments rail. Links are rewritten through a resolver so a `.md` link becomes
its rendered page and media links play inline.
"""
import html
import re

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^(```+|~~~+)\s*([\w+-]*)\s*$")
HR_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})\s*$")
UL_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
OL_RE = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
TASK_RE = re.compile(r"^\[([ xX])\]\s+(.*)$")
MEDIA_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif")
VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v")
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac")

INLINE_RE = re.compile(
    r"(?P<code>`+)(?P<codetext>.+?)(?P=code)"
    r"|!\[(?P<imgalt>[^\]]*)\]\((?P<imgsrc>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|\[(?P<linktext>[^\]]+)\]\((?P<linkhref>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|(?P<url>https?://[^\s<>\"')\]]+)"
    r"|\*\*(?P<bold>[^*\n]+?)\*\*"
    r"|__(?P<bold2>[^_\n]+?)__"
    r"|(?<![\w*])\*(?P<em>[^*\n]+?)\*(?![\w*])"
    r"|(?<![\w_])_(?P<em2>[^_\n]+?)_(?![\w_])"
    r"|~~(?P<strike>[^~\n]+?)~~"
)


def slugify(text, used=None):
    s = re.sub(r"<[^>]+>", "", text)
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "section"
    if used is not None:
        base, n = s, 2
        while s in used:
            s, n = "%s-%d" % (base, n), n + 1
        used.add(s)
    return s


def inline(text, resolve=None):
    """Inline markdown to HTML. `resolve(url, kind)` maps link/image targets ("link" or "media")."""
    out, pos = [], 0
    esc = html.escape
    for m in INLINE_RE.finditer(text):
        out.append(esc(text[pos:m.start()]))
        pos = m.end()
        if m.group("code"):
            out.append("<code>%s</code>" % esc(m.group("codetext").strip()))
        elif m.group("imgsrc") is not None:
            src = resolve(m.group("imgsrc"), "media") if resolve else m.group("imgsrc")
            out.append(media_tag(src, m.group("imgalt")))
        elif m.group("linkhref") is not None:
            href = m.group("linkhref")
            label = inline(m.group("linktext"), resolve)
            target = resolve(href, "link") if resolve else href
            low = target.lower().split("#")[0].split("?")[0]
            if low.endswith(VIDEO_EXT + AUDIO_EXT + MEDIA_EXT) and not low.startswith("http"):
                out.append("<a class='media-link' href='%s' data-media='1'>%s</a>" % (esc(target, True), label))
            else:
                ext = " target='_blank' rel='noopener'" if target.startswith(("http://", "https://")) else ""
                out.append("<a href='%s'%s>%s</a>" % (esc(target, True), ext, label))
        elif m.group("url"):
            url = m.group("url")
            tail = ""
            while url and url[-1] in ".,;:!?":
                tail = url[-1] + tail
                url = url[:-1]
            out.append("<a href='%s' target='_blank' rel='noopener'>%s</a>%s" % (esc(url, True), esc(url), esc(tail)))
        elif m.group("bold") is not None or m.group("bold2") is not None:
            out.append("<strong>%s</strong>" % inline(m.group("bold") or m.group("bold2"), resolve))
        elif m.group("em") is not None or m.group("em2") is not None:
            out.append("<em>%s</em>" % inline(m.group("em") or m.group("em2"), resolve))
        elif m.group("strike") is not None:
            out.append("<del>%s</del>" % inline(m.group("strike"), resolve))
    out.append(esc(text[pos:]))
    return "".join(out)


def media_tag(src, alt=""):
    low = src.lower().split("#")[0].split("?")[0]
    s = html.escape(src, True)
    if low.endswith(VIDEO_EXT):
        return "<video controls preload='metadata' playsinline src='%s'></video>" % s
    if low.endswith(AUDIO_EXT):
        return "<audio controls preload='none' src='%s'></audio>" % s
    return "<img src='%s' alt='%s' loading='lazy'>" % (s, html.escape(alt or "", True))


def strip_comments(text):
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


class _Doc:
    def __init__(self, resolve):
        self.resolve = resolve
        self.out = []
        self.toc = []
        self.ids = set()
        self.title = None


def render(text, resolve=None, min_toc_level=2, max_toc_level=3):
    """Markdown → (body_html, toc, title). toc = [(level, id, text)]."""
    doc = _Doc(resolve)
    lines = strip_comments(text.replace("\r\n", "\n")).split("\n")
    _blocks(lines, doc, top=True, min_toc=min_toc_level, max_toc=max_toc_level)
    return "\n".join(doc.out), doc.toc, doc.title


def _blocks(lines, doc, top=False, min_toc=2, max_toc=3):
    i, n = 0, len(lines)
    para = []

    def flush_para():
        if para:
            doc.out.append("<p>%s</p>" % inline(" ".join(s.strip() for s in para), doc.resolve))
            para.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush_para()
            i += 1
            continue
        m = FENCE_RE.match(stripped)
        if m:
            flush_para()
            fence, lang = m.group(1), m.group(2)
            j = i + 1
            code = []
            while j < n and not (lines[j].strip().startswith(fence[0] * 3) and FENCE_RE.match(lines[j].strip())):
                code.append(lines[j])
                j += 1
            cls = " class='lang-%s'" % html.escape(lang) if lang else ""
            doc.out.append("<pre><code%s>%s</code></pre>" % (cls, html.escape("\n".join(code))))
            i = j + 1
            continue
        m = HEADING_RE.match(stripped)
        if m and not para:
            level = len(m.group(1))
            text = m.group(2)
            hid = slugify(text, doc.ids)
            if level == 1 and doc.title is None and top:
                doc.title = re.sub(r"[*_`]", "", text)
            if min_toc <= level <= max_toc:
                doc.toc.append((level, hid, re.sub(r"<[^>]+>", "", inline(text))))
            doc.out.append("<h%d id='%s'>%s<a class='anchor' href='#%s' aria-hidden='true'>#</a></h%d>"
                           % (level, hid, inline(text, doc.resolve), hid, level))
            i += 1
            continue
        if HR_RE.match(stripped) and not para:
            doc.out.append("<hr>")
            i += 1
            continue
        if stripped.startswith(">"):
            flush_para()
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            doc.out.append("<blockquote>")
            _blocks(quote, doc)
            doc.out.append("</blockquote>")
            continue
        if "|" in stripped and i + 1 < n and TABLE_SEP_RE.match(lines[i + 1]) and not para:
            flush_para()
            i = _table(lines, i, doc)
            continue
        if UL_RE.match(line) or OL_RE.match(line):
            flush_para()
            i = _list(lines, i, doc)
            continue
        para.append(line)
        i += 1
    flush_para()


def _cells(row):
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


def _table(lines, i, doc):
    head = _cells(lines[i])
    aligns = []
    for c in _cells(lines[i + 1]):
        c = c.strip()
        aligns.append("center" if c.startswith(":") and c.endswith(":") else "right" if c.endswith(":") else "left")
    j = i + 2
    rows = []
    while j < len(lines) and lines[j].strip() and "|" in lines[j]:
        rows.append(_cells(lines[j]))
        j += 1
    out = ["<div class='table-wrap'><table><thead><tr>"]
    for k, c in enumerate(head):
        out.append("<th style='text-align:%s'>%s</th>" % (aligns[k] if k < len(aligns) else "left", inline(c, doc.resolve)))
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>")
        for k in range(len(head)):
            c = r[k] if k < len(r) else ""
            out.append("<td style='text-align:%s'>%s</td>" % (aligns[k] if k < len(aligns) else "left", inline(c, doc.resolve)))
        out.append("</tr>")
    out.append("</tbody></table></div>")
    doc.out.append("".join(out))
    return j


def _indent(s):
    return len(s) - len(s.lstrip(" \t").replace("\t", "    "))


def _list(lines, i, doc):
    """Parse one list (bullets or numbers) starting at lines[i]; nested lists by indentation."""
    first = UL_RE.match(lines[i]) or OL_RE.match(lines[i])
    ordered = bool(OL_RE.match(lines[i])) and not UL_RE.match(lines[i])
    base = len(first.group(1).replace("\t", "    "))
    items = []       # each: {"text": [...lines], "children": [...raw lines]}
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            # a blank line ends the list unless the next non-blank line is still indented into it
            j = i
            while j < n and not lines[j].strip():
                j += 1
            if j < n and (_indent(lines[j]) > base or ((UL_RE.match(lines[j]) or OL_RE.match(lines[j]))
                                                        and _indent(lines[j]) == base)):
                if items and _indent(lines[j]) > base:
                    items[-1]["children"].append("")
                i = j
                continue
            break
        m = UL_RE.match(line) or OL_RE.match(line)
        ind = _indent(line)
        if m and ind == base and bool(OL_RE.match(line) and not UL_RE.match(line)) == ordered:
            items.append({"text": [m.group(3)], "children": []})
            i += 1
            continue
        if ind > base and items:
            items[-1]["children"].append(line[min(len(line) - len(line.lstrip()), base + 2):] if not m else line)
            i += 1
            continue
        if m and ind == base:
            break           # list type changed at the same level: a new list
        break
    tag = "ol" if ordered else "ul"
    start = ""
    if ordered:
        try:
            s = int(first.group(2))
            if s != 1:
                start = " start='%d'" % s
        except ValueError:
            pass
    doc.out.append("<%s%s>" % (tag, start))
    for it in items:
        text = " ".join(t.strip() for t in it["text"])
        tm = TASK_RE.match(text)
        if tm:
            box = "<input type='checkbox' disabled%s> " % (" checked" if tm.group(1).lower() == "x" else "")
            doc.out.append("<li class='task'>%s%s" % (box, inline(tm.group(2), doc.resolve)))
        else:
            doc.out.append("<li>%s" % inline(text, doc.resolve))
        if it["children"]:
            kids = it["children"]
            # continuation paragraphs vs nested lists: dedent and recurse
            min_ind = min((_indent(k) for k in kids if k.strip()), default=0)
            kids = [k[min_ind:] if k.strip() else "" for k in kids]
            _blocks(kids, doc)
        doc.out.append("</li>")
    doc.out.append("</%s>" % tag)
    return i


# ---- page shell --------------------------------------------------------------------------------------------

PAGE_CSS = """
:root{color-scheme:dark;--bg:#0b0c0e;--panel:#121316;--panel2:#191b1f;--raise:#22252a;--line:rgba(255,255,255,.09);--line-strong:rgba(255,255,255,.16);
--text:#f2f3f5;--text2:#c3c7ce;--muted:#8a9099;--accent:#2dd4bf;--accent-fg:#06201c;--accent-soft:rgba(45,212,191,.12);--info:#8fb3ff;--good:#4ade80;--warn:#f5b942;--bad:#ff6b6b;
--font:'Inter Variable',Inter,ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;--mono:ui-monospace,'Cascadia Code','JetBrains Mono',Menlo,Consolas,monospace;--top:58px}
body.script{--top:96px}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:calc(var(--top) + 24px)}
body{margin:0;background:var(--bg);color:var(--text);font:17px/1.6 var(--font);-webkit-font-smoothing:antialiased}
a{color:var(--info)}a:hover{color:var(--accent)}
[hidden]{display:none!important}
header.top{position:sticky;top:0;z-index:30;background:rgba(11,12,14,.94);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
header.top .row1{display:flex;align-items:center;gap:8px 14px;min-height:50px;padding:6px 20px}
header.top .subbar{display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:0 20px 8px}
@media (max-width:760px){header.top .row1{padding:6px 10px}header.top .subbar{flex-wrap:nowrap;overflow-x:auto;padding:0 10px 8px;scrollbar-width:none}
  header.top .subbar > *{flex:0 0 auto}.crumbs .mid{display:none}.crumbs .mid + .sep{display:none}}
.crumbs{display:flex;align-items:center;gap:6px;min-width:0;flex:1 1 auto;font-size:14px;color:var(--muted);white-space:nowrap}
.crumbs a{color:var(--text2);text-decoration:none;padding:4px 8px;border-radius:7px;overflow:hidden;text-overflow:ellipsis;max-width:46ch;flex:0 1 auto;min-width:4ch}
.crumbs a:hover{background:var(--panel2);color:var(--text)}
.crumbs .sep{color:var(--muted);opacity:.7}
.crumbs .here{color:var(--text);font-weight:650;overflow:hidden;text-overflow:ellipsis;padding:4px 2px}
.crumbs .brand{color:var(--accent);font-weight:650}
.tools{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.tbtn{background:var(--panel2);color:var(--text2);border:1px solid var(--line);border-radius:8px;padding:5px 10px;font:500 13px var(--font);cursor:pointer;display:inline-flex;align-items:center;gap:6px;text-decoration:none}
.tbtn:hover{border-color:var(--line-strong);color:var(--text)}
.tbtn[aria-pressed=true]{background:var(--accent-soft);border-color:rgba(45,212,191,.45);color:var(--text)}
.tbtn .n{font:700 11px var(--mono);background:var(--accent);color:var(--accent-fg);border-radius:99px;padding:0 6px}
.tbtn .n.zero{background:var(--raise);color:var(--muted)}
.subbar .grow{flex:1}.subbar .count{font:12.5px var(--font);color:var(--muted);padding:0 4px}
select.tbtn{appearance:auto;max-width:190px}
body.framed .only-top{display:none}body:not(.framed) .only-framed{display:none}
.layout{max-width:1500px;margin:0 auto;padding:22px;display:grid;grid-template-columns:230px minmax(0,1fr) 340px;gap:24px;align-items:start}
.layout.no-toc{grid-template-columns:minmax(0,1fr) 340px;max-width:1240px}
body.rail-off .layout{grid-template-columns:230px minmax(0,1fr)}body.rail-off .layout.no-toc{grid-template-columns:minmax(0,1fr);max-width:980px}
body.rail-off .rail{display:none}
@media (max-width:1180px){.layout,.layout.no-toc{grid-template-columns:minmax(0,1fr)}.toc{position:static!important;max-height:none!important}
  .rail{position:fixed!important;right:0;top:var(--top);bottom:0;width:min(380px,92vw);max-height:none!important;z-index:25;border-radius:0!important;box-shadow:-20px 0 50px rgba(0,0,0,.5);transform:translateX(105%);transition:transform .2s}
  body.rail-open .rail{transform:none}body.rail-off .rail{display:block}}
.toc{position:sticky;top:calc(var(--top) + 18px);max-height:calc(100vh - var(--top) - 36px);overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;font-size:14px}
.toc h2{margin:0 0 8px;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted)}
.toc a{display:block;color:var(--text2);text-decoration:none;padding:3px 0;line-height:1.35}
.toc a.l3{padding-left:14px;color:var(--muted);font-size:13px}.toc a:hover{color:var(--accent)}
article{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:30px 36px 40px;min-width:0}
@media (max-width:640px){article{padding:20px 16px}header.top{padding:8px 12px}.layout{padding:10px}.crumbs a{max-width:20ch}}
article h1{font-size:30px;line-height:1.2;margin:0 0 18px;font-weight:700;letter-spacing:-.2px}
article h2{font-size:22px;margin:36px 0 10px;padding-top:14px;border-top:1px solid var(--line);font-weight:650}
article h3{font-size:18px;margin:26px 0 8px;font-weight:650}article h4{font-size:16px;margin:20px 0 6px}
article h1:first-child{margin-top:0}
.anchor{opacity:0;margin-left:8px;color:var(--muted);text-decoration:none;font-weight:400}h1:hover .anchor,h2:hover .anchor,h3:hover .anchor,h4:hover .anchor{opacity:1}
article p{margin:10px 0}article li{margin:4px 0}article ul,article ol{padding-left:26px}
article li.task{list-style:none;margin-left:-22px}
article code{font:14px var(--mono);background:var(--panel2);border:1px solid var(--line);padding:1px 6px;border-radius:5px;overflow-wrap:anywhere}
article pre{background:#0d0f12;border:1px solid var(--line);border-radius:10px;padding:14px 16px;overflow:auto;font:13.5px/1.5 var(--mono)}
article pre code{background:none;border:0;padding:0;font-size:inherit}
article blockquote{margin:12px 0;padding:6px 18px;border-left:3px solid var(--accent);color:var(--text2);background:rgba(45,212,191,.05);border-radius:0 8px 8px 0}
.table-wrap{overflow-x:auto;margin:14px 0;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:15px}th,td{padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:var(--panel2);font-weight:650;text-align:left;position:sticky;top:0}tr:last-child td{border-bottom:0}
article img,article video{max-width:100%;height:auto;border-radius:10px;border:1px solid var(--line);display:block;margin:12px 0;background:#000}
article audio{width:100%;max-width:520px;display:block;margin:8px 0}
hr{border:0;border-top:1px solid var(--line);margin:28px 0}
.media-link::after{content:" \\25B6";color:var(--accent);font-size:.8em}
.docfoot{color:var(--muted);font-size:13px;margin:24px 0 0;text-align:center}
.docfoot code{font:12px var(--mono)}
.kbd{font:12px var(--mono);background:var(--panel2);border:1px solid var(--line-strong);border-bottom-width:2px;border-radius:5px;padding:0 5px;color:var(--text2)}
"""

COMMENT_CSS = """
.commentable{cursor:text;border-radius:6px;position:relative;transition:background .12s}
body.live .commentable:hover{background:rgba(143,179,255,.07);box-shadow:inset 3px 0 0 rgba(143,179,255,.6)}
.commentable.is-target,.commentable.flash{background:rgba(45,212,191,.12)!important;box-shadow:inset 3px 0 0 var(--accent)!important}
.commentable.has-thread{box-shadow:inset 3px 0 0 var(--warn)}
.commentable.has-thread.all-resolved{box-shadow:inset 3px 0 0 rgba(74,222,128,.55)}
.marker{position:absolute;right:-6px;top:2px;transform:translateX(100%);font:700 11px var(--mono);background:var(--warn);color:#2a1d02;border:0;border-radius:99px;padding:1px 7px;cursor:pointer;line-height:1.5}
.marker.ok{background:rgba(74,222,128,.25);color:var(--good)}
tr .marker{position:static;transform:none;margin-left:6px}
.th{margin:6px 0 14px;background:var(--panel2);border:1px solid var(--line-strong);border-left:3px solid var(--warn);border-radius:10px;padding:10px 12px;font:15px/1.5 var(--font);color:var(--text);cursor:default;text-align:left;white-space:normal;max-width:none}
.th.resolved{border-left-color:var(--good)}
.th.collapsed{padding:4px 10px;display:inline-flex;gap:8px;align-items:center;cursor:pointer;color:var(--muted);font-size:13px}
.th-head{display:flex;align-items:center;gap:8px;font-size:12px;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.th-head .grow{flex:1}
.th-head .state{font-weight:700}.th.resolved .th-head .state{color:var(--good)}
.th .m{padding:6px 0;border-top:1px solid var(--line)}.th .m:first-of-type{border-top:0}
.th .who{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--info);margin-right:8px}
.th .m.agent .who{color:var(--good)}
.th time{color:var(--muted);font-size:12px}
.th .txt{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:1px}
.th .dec{display:inline-block;font:700 10.5px var(--mono);letter-spacing:.6px;text-transform:uppercase;color:var(--warn);margin-right:6px}
.th textarea{width:100%;min-height:58px;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit;font-size:15px;resize:vertical;margin-top:6px}
.th textarea:focus{outline:2px solid var(--accent);border-color:transparent}
.th-row{display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap}
.btn{background:var(--accent);color:var(--accent-fg);border:0;border-radius:8px;padding:6px 14px;font:600 14px var(--font);cursor:pointer;min-height:34px}
.btn.ghost{background:transparent;color:var(--text);border:1px solid var(--line-strong)}
.btn.small{min-height:28px;padding:3px 10px;font-size:13px}
.btn:disabled{opacity:.45;cursor:default}
.th .st{color:var(--muted);font-size:13px}
.th .m .from,.ri .from{font:600 10.5px var(--mono);color:var(--warn);margin-left:6px}
.th .edited{font:600 10.5px var(--mono);color:var(--warn);text-transform:none;letter-spacing:0}
.inline-media{margin:8px 0}.inline-media video,.inline-media img{max-width:100%;border-radius:10px}.inline-media audio{width:100%}
/* the comments rail */
.rail{position:sticky;top:calc(var(--top) + 18px);max-height:calc(100vh - var(--top) - 36px);overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:12px;font-size:14px}
.rail-head{position:sticky;top:0;background:var(--panel);padding:12px 14px 10px;border-bottom:1px solid var(--line);z-index:1}
.rail-head h2{margin:0 0 8px;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);display:flex;align-items:center;gap:8px}
.rail-head h2 .grow{flex:1}
.seg{display:flex;gap:2px;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:2px}
.seg button{flex:1;background:none;border:0;color:var(--text2);font:500 12.5px var(--font);padding:4px 6px;border-radius:6px;cursor:pointer}
.seg button[aria-pressed=true]{background:var(--panel2);color:var(--text);font-weight:650}
.rail-list{padding:6px}
.ri{display:block;width:100%;text-align:left;background:none;border:1px solid transparent;border-radius:9px;padding:8px 10px;color:var(--text);cursor:pointer;font:inherit}
.ri:hover{background:var(--panel2);border-color:var(--line)}
.ri.current{border-color:var(--accent);background:var(--accent-soft)}
.ri .where{display:flex;gap:6px;align-items:center;font:600 12px var(--font);color:var(--muted)}
.ri .where .ln{color:var(--text2)}.ri .where .grow{flex:1}
.ri .ok{color:var(--good)}.ri .open{color:var(--warn)}
.ri .ex{color:var(--muted);font-size:12.5px;font-style:italic;margin:2px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ri .first{font-size:14px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.ri .meta{color:var(--muted);font-size:12px;margin-top:2px}
.ri .gone{color:var(--warn);font-size:11.5px;font-family:var(--font)}
.media-chip{display:inline-block;font:600 12.5px var(--mono);background:var(--panel);border:1px solid var(--line-strong);border-radius:99px;padding:0 8px;text-decoration:none;margin:0 2px}
.rail-empty{color:var(--muted);padding:18px 14px;font-size:14px;line-height:1.5}
.vbanner{max-width:1180px;margin:14px auto 0;padding:11px 16px;border-radius:10px;font:15px/1.5 var(--font);color:var(--text);display:block}
.vbanner[hidden]{display:none}
.vbanner.old{background:#2a2108;border:1px solid rgba(245,185,66,.7);position:sticky;top:calc(var(--top) + 8px);z-index:5;box-shadow:0 8px 24px rgba(0,0,0,.4)}
.crumbs .ver{font:600 12px var(--mono);color:var(--accent);border:1px solid rgba(45,212,191,.45);border-radius:99px;padding:0 8px;margin-left:8px;white-space:nowrap}
.crumbs .ver.old{color:var(--warn);border-color:rgba(245,185,66,.55)}
body.closed-version .rail-help{display:none}
body.closed-version .rail-list::before{content:attr(data-note);display:block;margin:4px 4px 8px;padding:7px 9px;border-radius:8px;background:rgba(245,185,66,.1);color:var(--warn);font-size:12.5px;line-height:1.45}
body.closed-version .rail{opacity:.92}
.vbanner.stale{background:rgba(143,179,255,.1);border:1px solid rgba(143,179,255,.45)}
.vbanner b{margin-right:4px}.vbanner a{color:var(--info);font-weight:650}.vbanner .btn{margin-left:6px}
.th .st.closed{margin-top:8px;color:var(--text2)}.th .st.closed a{color:var(--info)}
body.closed-version .commentable{cursor:default}
.rail-help{color:var(--muted);font-size:12.5px;padding:8px 14px 14px;border-top:1px solid var(--line);line-height:1.6}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--panel2);border:1px solid var(--line-strong);border-radius:10px;padding:8px 14px;font-size:14px;z-index:60;box-shadow:0 10px 30px rgba(0,0,0,.4)}
"""

DOC_JS = r"""
(function () {
  'use strict';
  var B = document.body, qs = new URLSearchParams(location.search);
  var ITEM = B.getAttribute('data-item') || qs.get('item') || '', DOC = B.getAttribute('data-doc') || '';
  var REV = B.getAttribute('data-rev') || '', VER = B.getAttribute('data-version') || '';
  var CLOSED = null;        // {id, label, doc}: a newer version replaced this one, so its comment boxes are closed
  var LIVE = location.protocol !== 'file:', FRAMED = window.parent !== window;
  if (FRAMED) B.classList.add('framed');
  if (LIVE && ITEM) B.classList.add('live');
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (k === 'text') n.textContent = v; else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v === true ? '' : v);
    });
    (kids || []).forEach(function (c) { if (c) n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return n;
  }
  function when(iso) { try { return new Date(iso).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}); } catch (e) { return iso || ''; } }
  function toast(t, ms) { var n = el('div', {class: 'toast', role: 'status', text: t}); B.appendChild(n); setTimeout(function () { n.remove(); }, ms || 2200); }
  function tell(msg) { if (FRAMED) try { parent.postMessage(msg, location.origin); } catch (e) {} }

  /* inline media links */
  document.querySelectorAll('a.media-link').forEach(function (a) {
    a.addEventListener('click', function (ev) {
      ev.preventDefault();
      var href = a.getAttribute('href'), low = href.toLowerCase().split('#')[0].split('?')[0], next = a.nextElementSibling;
      if (next && next.classList.contains('inline-media')) { next.remove(); return; }
      var m = /\.(mp4|webm|mov|m4v)$/.test(low) ? el('video', {src: href, controls: true, autoplay: true, playsinline: true})
        : /\.(mp3|wav|m4a|aac|ogg|oga|opus|flac)$/.test(low) ? el('audio', {src: href, controls: true, autoplay: true}) : el('img', {src: href});
      a.insertAdjacentElement('afterend', el('div', {class: 'inline-media'}, [m]));
    });
  });

  /* breadcrumbs: inside the hub's frame, ask the hub to go back instead of loading it again */
  document.querySelectorAll('[data-nav]').forEach(function (a) {
    a.addEventListener('click', function (ev) {
      if (!FRAMED || ev.ctrlKey || ev.metaKey) return;
      ev.preventDefault(); tell({type: 'review-nav', to: a.getAttribute('data-nav')});
    });
  });
  var newTab = document.getElementById('open-tab'); if (newTab) newTab.href = location.href.split('#')[0] + (ITEM && !qs.get('item') ? '?item=' + encodeURIComponent(ITEM) : '');

  /* reading preferences (scripts): paper or dark, production cues, line numbers */
  var PREF_KEY = 'review.doc.prefs', prefs = {};
  try { prefs = JSON.parse(localStorage.getItem(PREF_KEY) || '{}') || {}; } catch (e) {}
  function applyPrefs() {
    B.classList.toggle('dark-page', !!prefs.dark); B.classList.toggle('no-cues', !!prefs.noCues); B.classList.toggle('numbers', !!prefs.numbers);
    B.classList.toggle('no-notes', !!prefs.noNotes);
    document.querySelectorAll('[data-pref]').forEach(function (b) {
      var k = b.getAttribute('data-pref'), on = k === 'paper' ? !prefs.dark : k === 'cues' ? !prefs.noCues : k === 'notes' ? !prefs.noNotes : !!prefs[k];
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      if (k === 'paper') b.textContent = prefs.dark ? 'Dark page' : 'Paper page';
    });
  }
  document.querySelectorAll('[data-pref]').forEach(function (b) {
    b.addEventListener('click', function () {
      var k = b.getAttribute('data-pref');
      if (k === 'paper') prefs.dark = !prefs.dark; else if (k === 'cues') prefs.noCues = !prefs.noCues; else if (k === 'notes') prefs.noNotes = !prefs.noNotes; else prefs[k] = !prefs[k];
      try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch (e) {}
      applyPrefs();
    });
  });
  applyPrefs();

  /* script filters: one character, one block */
  var who = document.getElementById('f-who'), blk = document.getElementById('f-block'), only = document.getElementById('f-only');
  function applyFilter() {
    var w = who ? who.value : '', b = blk ? blk.value : '', hide = only && only.getAttribute('aria-pressed') === 'true';
    B.classList.toggle('filtering', !!(w || b)); B.classList.toggle('filter-hide', hide && !!(w || b));
    document.querySelectorAll('.sp-block').forEach(function (s) { s.classList.toggle('off', !!b && s.id !== b); });
    var shown = 0;
    document.querySelectorAll('.sp-dialogue').forEach(function (d) {
      var match = !w || d.getAttribute('data-who') === w;
      d.classList.toggle('match', !!w && match); d.classList.toggle('dim', !match);
      if (w && match && !(d.closest('.sp-block') || d).classList.contains('off')) shown += d.querySelectorAll('.sp-line').length;
    });
    document.querySelectorAll('.sp-action,.sp-shot,.sp-scene,.sp-transition,.sp-centered,.sp-note').forEach(function (x) { x.classList.toggle('dim', !!w && !x.closest('.match')); });
    document.querySelectorAll('.sp-block').forEach(function (s) { s.classList.toggle('empty', !!w && !s.querySelector('.sp-dialogue.match')); });
    document.querySelectorAll('.cast-chip').forEach(function (c) { c.setAttribute('aria-pressed', c.getAttribute('data-who') === w ? 'true' : 'false'); });
    var cnt = document.getElementById('f-count'); if (cnt) cnt.textContent = w ? w + ': ' + shown + ' line' + (shown === 1 ? '' : 's') : '';
    if (b) { var target = document.getElementById(b); if (target) target.scrollIntoView({block: 'start'}); }
  }
  if (who) who.addEventListener('change', applyFilter);
  if (blk) blk.addEventListener('change', applyFilter);
  if (only) only.addEventListener('click', function () { only.setAttribute('aria-pressed', only.getAttribute('aria-pressed') === 'true' ? 'false' : 'true'); applyFilter(); });
  document.querySelectorAll('.cast-chip').forEach(function (c) {
    c.addEventListener('click', function () { if (!who) return; who.value = who.value === c.getAttribute('data-who') ? '' : c.getAttribute('data-who'); applyFilter(); });
  });

  /* lines: one target element per source line (the innermost wins) */
  var lineEls = {};
  document.querySelectorAll('[data-line]').forEach(function (e) {
    e.classList.add('commentable');
    var n = e.getAttribute('data-line'), cur = lineEls[n];
    if (!cur || cur.contains(e)) lineEls[n] = e;
  });
  function cleanText(e) {
    if (e.getAttribute('data-excerpt')) return e.getAttribute('data-excerpt');
    var c = e.cloneNode(true);
    c.querySelectorAll('audio,video,button,.th,.marker,.sp-chips,.chip,.anchor,input').forEach(function (x) { x.remove(); });
    if (c.tagName === 'TR') return Array.prototype.map.call(c.children, function (td) { return td.textContent.replace(/\s+/g, ' ').trim(); }).filter(Boolean).join(' · ').slice(0, 140);
    return c.textContent.replace(/\s+/g, ' ').trim().slice(0, 140);
  }
  function kindOf(e) {
    return e.getAttribute('data-who') ? e.getAttribute('data-who') + "'s line" : e.tagName === 'LI' ? 'list item' : e.tagName === 'TR' ? 'table row'
      : /^H\d$/.test(e.tagName) ? 'heading' : e.classList.contains('sp-scene') ? 'scene heading' : e.classList.contains('sp-shot') ? 'shot'
      : e.classList.contains('sp-action') ? 'action' : 'paragraph';
  }

  /* comment threads: the person's line comments (answers) and agent replies with a line anchor. A note is placed by
     its line's content, not only its number: when the document changed since, it goes to the line that now holds
     its text (moved) or the closest match (changed); a note whose line is gone stays in the comments list as "line
     removed". Notes on earlier versions of the item (it replaced one that showed this document) are placed the same
     way and marked with their version. */
/*@@LINE_JS@@*/
  var threads = {}, order = [], filter = 'open', current = null, INDEX = null;
  function tkey(line) { return 'line:' + DOC + '#' + line; }
  function sameDoc(path) { return path === DOC || (!!path && stem(path) === stem(DOC) && stem(DOC) !== DOC); }
  function index() {
    if (!INDEX) INDEX = Object.keys(lineEls).map(function (n) { var t = cleanText(lineEls[n]); return [Number(n), lineFp(t), lineEls[n].getAttribute('data-who') || '', normText(t).slice(0, 48)]; });
    return INDEX;
  }
  function elOf(t) { return t.gone ? null : lineEls[t.line]; }
  function thread(line, anchor, gone) {
    var k = gone ? 'gone:' + gone : tkey(line);
    if (!threads[k]) threads[k] = {key: k, line: Number(line), excerpt: (anchor && anchor.excerpt) || '', msgs: [], keys: {}, resolved: false, open: false, gone: !!gone};
    return threads[k];
  }
  function place(src, an) {
    var loc = relocate(an, index()), raw = 'line:' + (an.doc || '') + '#' + an.line;
    var t = loc && loc.status === 'removed' ? thread(an.line, an, src.id + '|' + raw) : thread(loc ? loc.line : an.line, an);
    var k = src.id + '|' + raw, r = src.resolved && src.resolved[raw];
    if (!t.keys[k]) t.keys[k] = {item: src.id, anchor: an, resolved: !!(r && r.resolved)};
    if (loc && loc.status !== 'same' && !t.was) { t.was = {status: loc.status, line: Number(an.line)}; if (!t.excerpt) t.excerpt = an.excerpt || ''; }
    if (src.id !== ITEM) t.from = src.version || 'an earlier version';
    return t;
  }
  function build(it, olds) {
    var keep = {}; Object.keys(threads).forEach(function (k) { keep[k] = threads[k].open; });
    threads = {};
    [it].concat(olds || []).forEach(function (src) {
      var from = src.id !== ITEM ? (src.version || 'earlier') : '';
      (src.answers || []).forEach(function (a) {
        if (!a.anchor || a.anchor.type !== 'line' || !sameDoc(a.anchor.doc || '')) return;
        var t = place(src, a.anchor);
        if (a.decision === 'resolve' || a.decision === 'unresolve') return;
        t.msgs.push({who: 'you', at: a.at, text: a.comment || '', decision: a.decision, status: a.status, from: from});
      });
      (src.thread || []).forEach(function (m) {
        if (!m.anchor || m.anchor.type !== 'line' || !sameDoc(m.anchor.doc || '')) return;
        place(src, m.anchor).msgs.push({who: m.who === 'you' ? 'you' : 'agent', at: m.at, text: m.text, from: from});
      });
    });
    Object.keys(threads).forEach(function (k) {
      var t = threads[k], ks = Object.keys(t.keys);
      t.msgs.sort(function (a, b) { return (new Date(a.at).getTime() || 0) - (new Date(b.at).getTime() || 0); });
      t.resolved = ks.length > 0 && ks.every(function (x) { return t.keys[x].resolved; });
      t.open = !!keep[k];
      if (!t.msgs.length) delete threads[k];
    });
    order = Object.keys(threads).map(function (k) { return threads[k]; }).sort(function (a, b) { return a.line - b.line; });
  }
  /* what happened to a thread's line since the comment: '' (nothing), moved, changed or removed */
  function fate(t) {
    if (t.gone) return 'line removed';
    if (t.was && t.was.status === 'moved') return 'moved from line ' + t.was.line;
    if ((t.was && t.was.status === 'changed') || edited(t)) return 'line changed since';
    return '';
  }
  function linked(text) {
    var out = [], re = /https?:\/\/[^\s<>"')\]]+/g, last = 0, m;
    text = String(text || '');
    while ((m = re.exec(text))) {
      var u = m[0].replace(/[.,;:!?]+$/, ''), local = null;
      out.push(text.slice(last, m.index)); last = m.index + u.length;
      try { var pu = new URL(u); if (pu.hostname === location.hostname && /\.(mp4|webm|mov|m4v|mp3|wav|m4a|aac|ogg|oga|opus|flac|png|jpe?g|gif|webp)$/i.test(pu.pathname)) local = pu.pathname; } catch (e) {}
      if (!local) { out.push(el('a', {href: u, target: '_blank', rel: 'noopener', text: u})); continue; }
      var a = el('a', {href: local, class: 'media-chip', text: '▶ ' + decodeURIComponent(local.split('/').pop())});
      a.addEventListener('click', (function (href, link) { return function (ev) {
        ev.preventDefault(); var next = link.nextElementSibling;
        if (next && next.classList.contains('inline-media')) { next.remove(); return; }
        var v = /\.(mp4|webm|mov|m4v)$/i.test(href), au = /\.(mp3|wav|m4a|aac|ogg|oga|opus|flac)$/i.test(href);
        link.insertAdjacentElement('afterend', el('div', {class: 'inline-media'}, [v ? el('video', {src: href, controls: true, autoplay: true}) : au ? el('audio', {src: href, controls: true, autoplay: true}) : el('img', {src: href})]));
      }; })(local, a));
      out.push(a);
    }
    out.push(text.slice(last));
    return out;
  }
  function msgEl(m) {
    var dec = m.decision && m.decision !== 'comment' ? el('span', {class: 'dec', text: m.decision.replace('_', ' ')}) : null;
    return el('div', {class: 'm ' + m.who}, [el('span', {class: 'who', text: m.who === 'you' ? 'You' : 'Orchestrator'}), el('time', {text: when(m.at)}),
      m.from ? el('span', {class: 'from', text: 'on ' + m.from}) : null,
      el('div', {class: 'txt'}, [dec].concat(linked(m.text)))]);
  }
  function placeBox(e, box) {
    if (e.tagName === 'TR') { var tr = el('tr', {class: 'th-tr'}, [el('td', {colspan: e.children.length || 1}, [box])]); e.insertAdjacentElement('afterend', tr); return tr; }
    e.insertAdjacentElement('afterend', box); return box;
  }
  function clearInline() {
    document.querySelectorAll('.th, .th-tr, .marker').forEach(function (x) { x.remove(); });
    document.querySelectorAll('.has-thread').forEach(function (x) { x.classList.remove('has-thread', 'all-resolved'); });
  }
  function norm(s) { return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ''); }
  function edited(t) {
    /* the line's text no longer matches what the comment quoted (older excerpts may carry extra text around it) */
    var e = elOf(t); if (!e || !t.excerpt) return false;
    var now = norm(cleanText(e)).slice(0, 40), was = norm(t.excerpt);
    return !!now && was.indexOf(now) < 0 && now.indexOf(was.slice(0, 40)) < 0;
  }
  function renderInline() {
    clearInline();
    order.forEach(function (t) {
      var e = elOf(t); if (!e) return;
      if (!t.msgs.length) { if (t.open || t.draft) placeBox(e, threadBox(t)); return; }      // a comment not sent yet
      e.classList.add('has-thread'); e.classList.toggle('all-resolved', t.resolved);
      var mk = el('button', {type: 'button', class: 'marker' + (t.resolved ? ' ok' : ''), title: (t.resolved ? 'Resolved: ' : 'Open: ') + t.msgs.length + ' comment(s)', text: (t.resolved ? '✓ ' : '💬 ') + t.msgs.length});
      mk.addEventListener('click', function (ev) { ev.stopPropagation(); t.open = !t.open; renderInline(); renderRail(); });
      (e.tagName === 'TR' ? e.lastElementChild || e : e).appendChild(mk);
      if (t.resolved && !t.open) return;
      if (!t.open && !t.draft) return;
      placeBox(e, threadBox(t));
    });
  }
  function threadBox(t) {
    var box = el('div', {class: 'th' + (t.resolved ? ' resolved' : ''), 'data-key': t.key});
    var head = el('div', {class: 'th-head'}, [el('span', {class: 'state', text: t.resolved ? '✓ Resolved' : t.msgs.length ? 'Open thread' : 'New comment'}),
      el('span', {text: 'line ' + t.line}), fate(t) ? el('span', {class: 'edited', text: fate(t)}) : null, el('span', {class: 'grow'})]);
    var close = el('button', {type: 'button', class: 'btn ghost small', text: 'Hide'});
    close.addEventListener('click', function () { t.open = false; delete t.draft; if (!t.msgs.length) { delete threads[t.key]; order = order.filter(function (x) { return x !== t; }); } renderInline(); renderRail(); });
    head.appendChild(close);
    box.appendChild(head);
    t.msgs.forEach(function (m) { box.appendChild(msgEl(m)); });
    if (!(LIVE && ITEM)) { box.appendChild(el('div', {class: 'st', text: 'Read-only copy: open the live page to reply.'})); return box; }
    if (CLOSED) { box.appendChild(el('div', {class: 'st closed'}, ['Comments are closed on this old version. ', currentLink('Comment on ' + CLOSED.label + ' →')])); return box; }
    var ta = el('textarea', {rows: 2, placeholder: t.msgs.length ? 'Reply in this thread…' : 'What should change here?', 'aria-label': 'Comment on line ' + t.line});
    if (t.draft) ta.value = t.draft;
    ta.addEventListener('input', function () { t.draft = ta.value || ' '; });
    var send = el('button', {type: 'button', class: 'btn small', text: t.msgs.length ? 'Reply' : 'Comment'});
    var res = t.msgs.length ? el('button', {type: 'button', class: 'btn ghost small', text: t.resolved ? 'Reopen' : 'Resolve'}) : null;
    var st = el('span', {class: 'st'});
    box.appendChild(ta);
    box.appendChild(el('div', {class: 'th-row'}, [send, res, el('span', {class: 'kbd', text: 'Ctrl+Enter'}), st]));
    function anchor() {           /* the line's number and content: its text, fingerprint, speaker and block */
      var e = elOf(t);
      if (!e) { var k0 = Object.keys(t.keys)[0]; return k0 ? t.keys[k0].anchor : {type: 'line', line: t.line, doc: DOC, excerpt: t.excerpt}; }
      var x = cleanText(e), blk = e.closest('[data-block]');
      return {type: 'line', line: t.line, doc: DOC, excerpt: x, fp: lineFp(x), who: e.getAttribute('data-who') || (e.closest('[data-who]') || {getAttribute: function () { return ''; }}).getAttribute('data-who') || '',
        block: blk ? blk.getAttribute('data-block') : ''};
    }
    send.addEventListener('click', function () {
      var text = ta.value.trim(); if (!text) { st.textContent = 'Write something first.'; ta.focus(); return; }
      send.disabled = true; st.textContent = 'Sending…';
      post({decision: 'comment', comment: text, anchor: anchor()}).then(function (r) {
        delete t.draft; t.open = true; if (!t.excerpt) t.excerpt = anchor().excerpt;
        t.msgs.push({who: 'you', at: (r.answer && r.answer.at) || new Date().toISOString(), text: text, decision: 'comment'});
        if (order.indexOf(t) < 0) { order.push(t); order.sort(function (a, b) { return a.line - b.line; }); }
        if (t.resolved) t.resolved = false;
        renderInline(); renderRail();
        if (r.answer && r.answer.outdated) { toast('Saved, but a newer version replaced this one while you were reading. Your note is kept and listed on it.', 6000); load(); }
        else toast(r.delivered ? 'Comment sent.' : 'Comment saved; it will be retried.');
      }, function (e) { send.disabled = false; st.textContent = 'Not sent: ' + e.message; });
    });
    if (res) res.addEventListener('click', function () {
      res.disabled = true;
      /* a thread can hold notes from several anchors (moved lines, earlier versions): resolve each where it lives */
      var ks = Object.keys(t.keys), d = t.resolved ? 'unresolve' : 'resolve';
      (ks.length ? Promise.all(ks.map(function (k) { return post({decision: d, anchor: t.keys[k].anchor}, t.keys[k].item); })) : post({decision: d, anchor: anchor()})).then(function () {
        t.resolved = !t.resolved; t.open = !t.resolved; renderInline(); renderRail(); toast(t.resolved ? 'Thread resolved.' : 'Thread reopened.');
      }, function (e) { res.disabled = false; st.textContent = 'Failed: ' + e.message; });
    });
    ta.addEventListener('keydown', function (ev) { ev.stopPropagation(); if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) send.click(); if (ev.key === 'Escape') close.click(); });
    if (t.focus) { setTimeout(function () { ta.focus(); }, 0); delete t.focus; }
    return box;
  }
  function real() { return order.filter(function (t) { return t.msgs.length; }); }
  function counts() { var o = 0, r = 0; real().forEach(function (t) { if (t.resolved) r++; else o++; }); return {open: o, resolved: r, all: o + r}; }
  function renderRail() {
    var list = document.getElementById('rail-list'); if (!list) return;
    var c = counts();
    ['open', 'resolved', 'all'].forEach(function (k) { var b = document.querySelector('[data-rail="' + k + '"]'); if (b) { b.setAttribute('aria-pressed', filter === k ? 'true' : 'false'); b.querySelector('b').textContent = c[k]; } });
    var n = document.getElementById('c-count'); if (n) { n.textContent = c.open; n.classList.toggle('zero', !c.open); }
    list.textContent = '';
    var shown = real().filter(function (t) { return filter === 'all' || (filter === 'open') !== t.resolved; });
    if (!shown.length) {
      list.appendChild(el('div', {class: 'rail-empty', text: !(LIVE && ITEM) ? 'Comments show on the live page.' : c.all ? 'Nothing ' + filter + '.' : 'No comments yet. Click any line to start a thread.'}));
    }
    shown.forEach(function (t) {
      var first = t.msgs[0] || {}, last = t.msgs[t.msgs.length - 1] || {}, e = elOf(t), fa = fate(t);
      var b = el('button', {type: 'button', class: 'ri' + (current === t ? ' current' : ''), title: 'Source line ' + t.line}, [
        el('div', {class: 'where'}, [el('span', {class: 'ln', text: where(t)}), el('span', {class: 'grow'}), t.from ? el('span', {class: 'from', text: t.from}) : null, fa || !e ? el('span', {class: 'gone', text: fa || 'line removed'}) : null,
          el('span', {class: t.resolved ? 'ok' : 'open', text: t.resolved ? '✓ resolved' : 'open'})]),
        el('div', {class: 'ex', text: e ? about(e) : t.excerpt}),
        el('div', {class: 'first', text: first.text || ''}),
        el('div', {class: 'meta', text: t.msgs.length + ' message' + (t.msgs.length === 1 ? '' : 's') + ' · last ' + (last.who === 'agent' ? 'orchestrator' : 'you') + ', ' + when(last.at)})]);
      b.addEventListener('click', function () { go(t); });
      list.appendChild(b);
    });
  }
  /* "Scene 4 · HOST" for scripts, the heading above for documents */
  function where(t) {
    var e = elOf(t); if (!e) return 'Line ' + t.line;
    if (e.classList.contains('sp-blockhead')) return 'Block ' + ((e.querySelector('.sp-blocknum') || {}).textContent || '') + ' · ' + ((e.querySelector('.sp-blocktitle') || {}).textContent || '');
    var all = Array.prototype.slice.call(document.querySelectorAll(B.classList.contains('script') ? '.sp-scene' : 'article h2, article h3'));
    var scene = null;
    all.forEach(function (s) { if (s.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING || s === e) scene = s; });
    var who = e.getAttribute('data-who') || (e.closest('[data-who]') || {getAttribute: function () { return ''; }}).getAttribute('data-who');
    if (scene && scene.classList.contains('sp-scene')) return 'Scene ' + (scene.getAttribute('data-n') || '?') + (who ? ' · ' + who : '');
    return scene ? scene.textContent.replace(/#$/, '').trim().slice(0, 40) : 'Line ' + t.line;
  }
  function about(e) {
    if (e.classList.contains('sp-blockhead')) return (e.querySelector('.sp-blocktitle') || e).textContent;
    if (e.classList.contains('sp-cue')) { var first = e.parentNode.querySelector('.sp-line'); if (first) return cleanText(first); }
    return cleanText(e);
  }
  function go(t) {
    current = t; t.open = true; renderInline(); renderRail();
    var e = elOf(t);
    if (e) { e.scrollIntoView({block: 'center', behavior: 'smooth'}); flash(e); tell({type: 'review-line', line: t.line}); }
    else toast('That line is gone from this version; the thread is kept in the list.');
  }
  function flash(e) { e.classList.add('flash'); setTimeout(function () { e.classList.remove('flash'); }, 1600); }
  document.querySelectorAll('[data-rail]').forEach(function (b) { b.addEventListener('click', function () { filter = b.getAttribute('data-rail'); renderRail(); }); });
  var railBtn = document.getElementById('rail-toggle');
  if (railBtn) railBtn.addEventListener('click', function () {
    if (window.matchMedia('(max-width:1180px)').matches) { B.classList.toggle('rail-open'); railBtn.setAttribute('aria-pressed', B.classList.contains('rail-open') ? 'true' : 'false'); }
    else { B.classList.toggle('rail-off'); railBtn.setAttribute('aria-pressed', B.classList.contains('rail-off') ? 'false' : 'true'); }
  });

  function post(body, item) {
    body.item_id = item || ITEM;
    body.seen = {doc_hash: REV, version: VER};          // the server records which version the note was made on
    return fetch('/api/review/answer', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Buildlog': '1'}, body: JSON.stringify(body)})
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status)); return j; }); },
            function () { throw new Error('cannot reach the build log server'); });
  }

  /* click a line to start (or open) its thread */
  Object.keys(lineEls).forEach(function (n) {
    var e = lineEls[n];
    e.addEventListener('click', function (ev) {
      if (ev.target.closest('a,button,textarea,audio,video,.th,input,select')) return;
      if (window.getSelection && String(window.getSelection()).length > 2) return;    // selecting text to copy
      ev.stopPropagation();
      var t = threads[tkey(n)];
      tell({type: 'review-line', line: Number(n)});
      if (!(LIVE && ITEM)) { if (!t) toast('Open the live page to comment.'); }
      if (CLOSED && !t) { toast('This version was replaced by ' + CLOSED.label + '. Comment there.'); return; }
      if (!t) { t = thread(n, {excerpt: cleanText(e)}); t.draft = ' '; order.push(t); order.sort(function (a, b) { return a.line - b.line; }); }
      t.open = true; t.focus = true; current = t;
      renderInline(); renderRail();
    });
  });

  /* keyboard: j / k next and previous thread, escape closes an empty draft */
  document.addEventListener('keydown', function (ev) {
    var tag = (ev.target.tagName || '').toLowerCase();
    if (tag === 'textarea' || tag === 'input' || tag === 'select' || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (ev.key === 'Escape') { if (FRAMED) tell({type: 'review-nav', to: 'item'}); return; }
    if (ev.key !== 'j' && ev.key !== 'k') return;
    var list = real().filter(function (t) { return filter === 'all' || (filter === 'open') !== t.resolved; });
    if (!list.length) return;
    var i = list.indexOf(current); i = ev.key === 'j' ? Math.min(list.length - 1, i + 1) : Math.max(0, i < 0 ? 0 : i - 1);
    go(list[i]);
  });

  /* deep links: #L42 (or #line=42) scrolls to and highlights that line */
  function fromHash() {
    var m = /^#(?:L|line=)(\d+)$/.exec(location.hash);
    if (!m) return;
    var e = lineEls[m[1]]; if (!e) return;
    e.scrollIntoView({block: 'center'}); flash(e);
    var t = threads[tkey(m[1])]; if (t) { current = t; t.open = true; renderInline(); renderRail(); }
  }
  window.addEventListener('hashchange', fromHash);

  /* live data */
  var stamp = null;
  function load() {
    if (!(LIVE && ITEM)) { renderRail(); return Promise.resolve(); }
    return fetch('/api/review/items', {cache: 'no-store'}).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
      var all = {}; ((j && j.items) || []).forEach(function (x) { all[x.id] = x; });
      var it = all[ITEM]; if (!it) return;
      var crumb = document.getElementById('crumb-item'); if (crumb && !crumb.textContent.trim()) crumb.textContent = it.title;
      var olds = [], seen = {}, o = all[it.supersedes];            /* the versions it replaced, for their notes */
      seen[it.id] = 1;
      while (o && !seen[o.id]) { seen[o.id] = 1; olds.push(o); o = all[o.supersedes]; }
      build(it, olds); renderInline(); renderRail();
      return versionBanner(it);
    }, function () { renderRail(); });
  }
  /* versions: a newer item replaced this one (comments close, with a link to it), or this page is an older copy
     of the document than the hub's (the file changed and was rendered again) */
  function hubHref(id, path) {
    var h = document.querySelector('[data-nav=hub]');
    return (h ? h.getAttribute('href').split('#')[0] : '../index.html') + '#item=' + encodeURIComponent(id) + (path ? '&doc=' + encodeURIComponent(path) : '');
  }
  function currentLink(text) { return el('a', {href: hubHref(CLOSED.id, CLOSED.doc), target: '_top', text: text}); }
  function versionBanner(it) {
    var bn = document.getElementById('vbanner'); if (!bn) return;
    var mine = (it.docs || []).filter(function (d) { return d.path === DOC; })[0];
    if (it.superseded_by) {
      return fetch('/api/review/items?id=' + encodeURIComponent(it.superseded_by), {cache: 'no-store'}).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
        var nx = j && j.items && j.items[0]; if (!nx) return;
        var same = (nx.docs || []).filter(function (d) { return d.path === DOC; })[0] || (nx.docs || [])[0];
        CLOSED = {id: nx.id, label: nx.version || 'the new version', doc: same ? same.path : ''};
        bn.className = 'vbanner old'; bn.hidden = false; bn.textContent = '';
        bn.appendChild(el('b', {text: 'Old version' + (VER || it.version ? ' (' + (VER || it.version) + ')' : '') + '.'}));
        bn.appendChild(document.createTextNode(' ' + CLOSED.label + ' replaced it' + (it.superseded_at ? ' on ' + when(it.superseded_at) : '') +
          '. Notes made here are kept and listed on ' + CLOSED.label + '; new comments go there. '));
        bn.appendChild(currentLink('Open ' + CLOSED.label + ' →'));
        B.classList.add('closed-version');
        var rl = document.getElementById('rail-list'); if (rl) rl.setAttribute('data-note', 'Read-only. These threads continue on ' + CLOSED.label + ', at the matching lines.');
        var vt = document.getElementById('doc-ver'); if (vt) { vt.textContent = (VER || it.version || 'old') + ' · replaced'; vt.classList.add('old'); }
        renderInline(); renderRail();
      }, function () {});
    }
    if (mine && mine.hash && REV && mine.hash !== REV && !mine.frozen) {
      bn.className = 'vbanner stale'; bn.hidden = false; bn.textContent = '';
      bn.appendChild(el('b', {text: 'This copy is out of date.'}));
      bn.appendChild(document.createTextNode(' The document changed after this page loaded. '));
      var rl = el('button', {type: 'button', class: 'btn small', text: 'Reload'}); rl.addEventListener('click', function () { location.reload(); });
      bn.appendChild(rl);
    }
  }
  function poll() {
    fetch('/api/version', {cache: 'no-store'}).then(function (r) { return r.json(); }).then(function (v) {
      var s = String(v.review_mtime);
      if (stamp !== null && s !== stamp && !document.querySelector('.th textarea:focus')) load();
      stamp = s;
    }, function () {}).then(function () { setTimeout(poll, 20000); });
  }
  load().then(fromHash);
  if (LIVE && ITEM) poll();
})();
"""


def page_shell(title, body_html, ctx=None, doc_rel="", toc_html="", tools_html="", extra_css="", body_class="",
               source_note="", foot_hint="Click any paragraph, list item or table row to comment on it.", comments=True):
    """A complete review page: breadcrumb (hub > item > document), tools, contents, the document and the comments rail."""
    ctx = ctx or {}
    E = html.escape
    hub = ctx.get("hub") or "../index.html"
    item_id = ctx.get("item_id") or ""
    item_href = hub + ("#item=" + E(item_id, True) if item_id else "")
    crumbs = ("<nav class='crumbs' aria-label='Breadcrumb'><a href='%s' data-nav='hub' target='_top'><span class='brand'>%s</span>&nbsp;review</a>"
              "<span class='sep'>&rsaquo;</span><a id='crumb-item' class='mid' href='%s' data-nav='item' target='_top' title='Back to the item'>%s</a>"
              "<span class='sep'>&rsaquo;</span><span class='here' title='%s'>%s</span>%s</nav>"
              % (E(hub, True), E(ctx.get("brand") or "Project"), item_href, E(ctx.get("item_title") or ""), E(title, True), E(title),
                 "<span class='ver' id='doc-ver' title='The version of this document'>%s</span>" % E(ctx["version"]) if ctx.get("version") else ""))
    tools = ""
    if comments:
        tools += ("<button type='button' class='tbtn' id='rail-toggle' aria-pressed='true' title='Show or hide the comments'>"
                  "Comments <span class='n zero' id='c-count'>0</span></button>")
    tools += ("<a class='tbtn only-framed' id='open-tab' target='_blank' rel='noopener'>Open in new tab</a>"
              "<button type='button' class='tbtn only-framed' data-nav='item' title='Close (Esc)'>&times; Close</button>")
    rail = ("<aside class='rail' id='rail' aria-label='Comments'><div class='rail-head'><h2><span>Comments</span><span class='grow'></span></h2>"
            "<div class='seg' role='group' aria-label='Show'><button type='button' data-rail='open' aria-pressed='true'>Open <b>0</b></button>"
            "<button type='button' data-rail='resolved' aria-pressed='false'>Resolved <b>0</b></button>"
            "<button type='button' data-rail='all' aria-pressed='false'>All <b>0</b></button></div></div>"
            "<div class='rail-list' id='rail-list'></div><div class='rail-help'>Click a line to comment. <span class='kbd'>j</span> "
            "<span class='kbd'>k</span> next / previous thread. Replies from the orchestrator appear in the thread.</div></aside>") if comments else ""
    foot = ("<p class='docfoot'>Rendered from <code>%s</code>%s.%s</p>" % (E(doc_rel), (" &middot; " + E(source_note)) if source_note else "",
            (" " + E(foot_hint)) if comments else "")) if doc_rel else ""
    layout = "layout" + ("" if toc_html else " no-toc")
    return ("<!doctype html>\n<html lang='en'>\n<head>\n<meta charset='utf-8'>\n"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>\n<title>%s</title>\n"
            "<style>%s%s%s</style>\n</head>\n<body class='%s%s' data-item='%s' data-doc='%s' data-rev='%s' data-version='%s'>\n"
            "<header class='top'><div class='row1'>%s<div class='tools'>%s</div></div>%s</header>\n"
            "<div id='vbanner' class='vbanner' hidden></div><div class='%s'>%s<article class='doc'>%s%s</article>%s</div>\n<script>%s</script>\n</body>\n</html>\n"
            % (E(title), PAGE_CSS, COMMENT_CSS, extra_css, E(body_class, True), "" if comments else " rail-off",
               E(item_id, True), E(doc_rel, True), E(ctx.get("rev") or "", True), E(ctx.get("version") or "", True), crumbs, tools,
               "<div class='subbar'>%s</div>" % tools_html if tools_html else "", layout, toc_html, body_html, foot, rail, DOC_JS.replace("/*@@LINE_JS@@*/", LINE_JS)))


def render_page(text, title=None, resolve=None, ctx=None, doc_rel="", source_note="", comments=True):
    """A complete HTML page for a Markdown document: contents, comment threads per block and the comments rail."""
    body, toc, found = render(text, resolve)
    title = title or found or "Document"
    toc_html = ""
    if len(toc) >= 2:
        toc_html = "<nav class='toc'><h2>Contents</h2>%s</nav>" % "".join(
            "<a class='l%d' href='#%s'>%s</a>" % (lvl, hid, label) for lvl, hid, label in toc)
    return page_shell(title, _with_lines(body) if comments else body, ctx=ctx, doc_rel=doc_rel, toc_html=toc_html,
                      source_note=source_note, comments=comments, body_class="md")


def _with_lines(body):
    """Number paragraphs, list items, headings and table rows so comments can anchor to them (data-line = block number)."""
    n = [0]

    def tag(m):
        n[0] += 1
        return "<%s data-line='%d'" % (m.group(1), n[0])
    return re.sub(r"<(p|li|h2|h3|h4|tr)(?=[ >])", tag, body)


# ---- anchoring comments by content -------------------------------------------------------------------------
# A line comment records the line number AND the line's content (excerpt, fingerprint, speaker). When the document
# is rewritten, a note finds its line again: the same text elsewhere (moved), a close match (changed), or nothing
# (removed). review.py stores the index of every rendered document (docs[].lines: [line, fingerprint, speaker,
# start of the text]); the pages and the hub run the same matching in LINE_JS below.
SKIP_CLASSES = ("th", "marker", "sp-chips", "chip", "anchor")
VOID_TAGS = ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr")


def norm_text(s):
    """What a line's identity is made of: lowercase letters and digits only (punctuation, spacing and markup go)."""
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def line_fp(s):
    """An 8-hex fingerprint of a line's text (FNV-1a over the first 80 normalized characters); '' for an empty line."""
    t = norm_text(s)[:80]
    if not t:
        return ""
    h = 0x811c9dc5
    for ch in t:
        h = ((h ^ ord(ch)) * 0x01000193) & 0xffffffff
    return "%08x" % h


def similarity(a, b):
    """Dice coefficient of the character pairs of two normalized texts (0..1)."""
    a, b = norm_text(a)[:160], norm_text(b)[:160]
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    pa, pb = {}, {}
    for s_, d in ((a, pa), (b, pb)):
        for i in range(len(s_) - 1):
            d[s_[i:i + 2]] = d.get(s_[i:i + 2], 0) + 1
    common = sum(min(n, pb.get(k, 0)) for k, n in pa.items())
    total = max(1, len(a) - 1) + max(1, len(b) - 1)
    return 2.0 * common / total


def relocate(anchor, lines):
    """Where a line comment belongs in the current version of its document.
    lines: [[line, fp, who, text48], ...]. Returns {"status": same|moved|changed|removed, "line": N} or None (unknown:
    no excerpt or no index)."""
    if not lines or not isinstance(anchor, dict):
        return None
    fp = anchor.get("fp") or line_fp(anchor.get("excerpt"))
    if not fp:
        return None
    try:
        n = int(anchor.get("line"))
    except (TypeError, ValueError):
        n = 0
    for row in lines:
        if row[0] == n and row[1] == fp:
            return {"status": "same", "line": n}
    exact = [row for row in lines if row[1] == fp]
    if exact:
        return {"status": "moved", "line": min(exact, key=lambda r: abs(r[0] - n))[0]}
    base = norm_text(anchor.get("excerpt"))[:48]
    best, score = None, 0.0
    for row in lines:
        sc = similarity(base, row[3]) - abs(row[0] - n) * 0.0005
        if sc > score:
            best, score = row, sc
    if best and score >= 0.55:
        return {"status": "changed", "line": best[0]}
    return {"status": "removed", "line": n}


def line_index(page):
    """[[line, fp, who, text48]] for every commentable element of a rendered page, the innermost per line (as the page
    picks it): the text a comment quotes (data-excerpt, else the visible text without chips and buttons)."""
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack, self.found = [], {}

        def handle_starttag(self, tag, attrs):
            if tag in VOID_TAGS:
                return
            a = dict(attrs)
            cls = (a.get("class") or "").split()
            skip = tag in ("audio", "video", "button", "script", "style") or any(c in SKIP_CLASSES for c in cls)
            self.stack.append({"line": a.get("data-line"), "excerpt": a.get("data-excerpt"), "who": a.get("data-who") or "",
                               "skip": skip, "text": []})

        def handle_endtag(self, tag):
            if tag in VOID_TAGS or not self.stack:
                return
            el = self.stack.pop()
            text = el["excerpt"] if el["excerpt"] is not None else re.sub(r"\s+", " ", "".join(el["text"])).strip()[:140]
            if self.stack and not el["skip"]:
                self.stack[-1]["text"].append("".join(el["text"]))
            if el["line"] and el["line"].isdigit():
                n = int(el["line"])
                if n not in self.found or self.found[n][1] < len(self.stack):     # deeper wins
                    self.found[n] = ((n, line_fp(text), el["who"], norm_text(text)[:48]), len(self.stack))

        def handle_data(self, data):
            if self.stack and not any(e["skip"] for e in self.stack):
                self.stack[-1]["text"].append(data)

    parser = P()
    body = page.split("<body", 1)[-1].split("<script>", 1)[0]
    parser.feed("<body" + body)
    return [list(v[0]) for _, v in sorted(parser.found.items())]


LINE_JS = r"""
  /* LINE-MATCH:START (the same code runs in the hub and the document pages; md_render.py LINE_JS is the source) */
  function normText(s) { return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ''); }
  function lineFp(s) {
    var t = normText(s).slice(0, 80); if (!t) return '';
    var h = 0x811c9dc5;
    for (var i = 0; i < t.length; i++) { h ^= t.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
    return ('0000000' + h.toString(16)).slice(-8);
  }
  function similarity(a, b) {
    a = normText(a).slice(0, 160); b = normText(b).slice(0, 160);
    if (!a || !b) return 0; if (a === b) return 1;
    var pa = {}, pb = {}, common = 0, k;
    for (var i = 0; i < a.length - 1; i++) { k = a.slice(i, i + 2); pa[k] = (pa[k] || 0) + 1; }
    for (var j = 0; j < b.length - 1; j++) { k = b.slice(j, j + 2); pb[k] = (pb[k] || 0) + 1; }
    Object.keys(pa).forEach(function (x) { common += Math.min(pa[x], pb[x] || 0); });
    return 2 * common / (Math.max(1, a.length - 1) + Math.max(1, b.length - 1));
  }
  /* a document path without the version number in its file name (review.py doc_stem): ep1_v12.screen.md -> ep1.screen.md */
  function stem(path) {
    var parts = String(path || '').split('/'), name = parts.pop(), i = name.indexOf('.');
    return parts.concat([(i < 0 ? name : name.slice(0, i)).replace(/[_.\- ]?v\d+(?=[_.\- ]|$)/ig, '') + (i < 0 ? '' : name.slice(i))]).join('/');
  }
  /* where a line comment belongs now: {status: same|moved|changed|removed, line} or null (unknown) */
  function relocate(an, lines) {
    if (!lines || !lines.length || !an) return null;
    var fp = an.fp || lineFp(an.excerpt); if (!fp) return null;
    var n = Number(an.line) || 0, i, row;
    for (i = 0; i < lines.length; i++) if (lines[i][0] === n && lines[i][1] === fp) return {status: 'same', line: n};
    var exact = lines.filter(function (r) { return r[1] === fp; });
    if (exact.length) { exact.sort(function (x, y) { return Math.abs(x[0] - n) - Math.abs(y[0] - n); }); return {status: 'moved', line: exact[0][0]}; }
    var base = normText(an.excerpt).slice(0, 48), best = null, score = 0;
    for (i = 0; i < lines.length; i++) { row = lines[i]; var sc = similarity(base, row[3]) - Math.abs(row[0] - n) * 0.0005; if (sc > score) { best = row; score = sc; } }
    if (best && score >= 0.55) return {status: 'changed', line: best[0]};
    return {status: 'removed', line: n};
  }
  /* LINE-MATCH:END */
"""


if __name__ == "__main__":
    import sys
    src = open(sys.argv[1], encoding="utf-8").read()
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(render_page(src, doc_rel=sys.argv[1]))
