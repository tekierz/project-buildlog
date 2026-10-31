#!/usr/bin/env python3
"""The review hub's CLI: one structured source of truth (docs/review/items.json by default) for everything the
user has to look at or decide, rendered to one HTML page they keep open (docs/review/index.html).

Agents write Markdown and JSON and never hand-write HTML. The CLI validates every referenced path, copies media
from outside the repo into docs/review/media/, renders every referenced document (Markdown -> HTML, ScreenMark
*.screen.md scripts -> screenplay review pages) and rebuilds the hub page. Answers, snoozes and resolved comment
threads come back through the build log server's inbox as structured JSON (`review.py inbox --json`).

  review.py add  --id ID --kind approve|choice|audition|action|info --title T --summary S
                 [--body TEXT|@file.md] [--priority urgent|high|normal|low] [--tag NAME]... [--default TEXT]
                 [--how CMDS] [--media PATH[::caption]]... [--compare BEFORE::AFTER[::caption]]...
                 [--doc PATH[::title]]... [--choices "A|B|C"] [--choice ID::label[::detail]]...
                 [--candidate ID::label::MEDIA[::detail[::model[::round]]]]... [--recommend ID,ID]
                 [--on-answer TEXT] [--source-task TASK_ID] [--commit]
  review.py add --json FILE|-          the same fields as one JSON object (complex items)
  review.py update ID [the same flags; a list flag replaces that list] [--json FILE|-] [--commit]
  review.py close ID [--reply TEXT] [--commit]     the agent handled the answer; the card moves to Done
  review.py reopen ID [--commit]
  review.py reply ID TEXT [--media PATH]... [--line N [--doc PATH] | --at SECONDS --clip PATH | --to MSG_ID]
                                       a note from the agent under the item, or in a comment thread
  review.py resolve ID (--line N [--doc PATH] | --at SECONDS --clip PATH | --to MSG_ID) [--undo]
  review.py snooze ID (--until DATE | --for 3d | --later) [--reply TEXT]     unsnooze ID
  review.py list [--all] [--json]    review.py show ID
  review.py inbox [--all] [--json] [--mark-read]      the user's answers no agent has handled yet
  review.py render [--all | ID...]   re-render the items' documents and the hub
  review.py preview PATH             render one .md / .screen.md to docs/review/docs/ and print its URL
  review.py sync                     rebuild the hub and the extra pages listed in buildlog.json
  review.py import-media PATH [--name NAME]   copy a file from outside the repo into docs/review/media/
  review.py commit -m MSG PATH...    commit only these paths (private index, see buildlog_tasks.py)

Settings come from the build log's buildlog.json (found next to this folder or in ../buildlog/): review_items,
review_page and an optional "review" object (brand, pages, screenplay profile). Run it by absolute path in the
served checkout. Python standard library only.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:              # python -I does not add the script folder
    sys.path.insert(0, str(HERE))
import md_render   # noqa: E402
import screenplay  # noqa: E402

KINDS = ("approve", "choice", "audition", "action", "info")
PRIORITIES = ("urgent", "high", "normal", "low")
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9 _./-]{0,39}$")
# The hub page (review-hub.html, MEDIA_KINDS) uses the same table: test_review.py checks they agree.
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif")
VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v")
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac")
MEDIA_EXT = IMAGE_EXT + VIDEO_EXT + AUDIO_EXT
DOC_EXT = (".md", ".txt", ".html", ".pdf", ".json")
LIST_FIELDS = ("media", "compare", "docs", "choices", "candidates", "tags")
AGENT_ONLY = ("on_answer",)
SNAP_RE = re.compile(r"(<!-- ITEMS-SNAPSHOT:START -->).*?(<!-- ITEMS-SNAPSHOT:END -->)", re.S)
DURATION_RE = re.compile(r"^(\d+)\s*([hdw])$")


def die(msg):
    raise SystemExit("review: " + msg)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def find_settings(explicit=None):
    """buildlog.json: --settings, else next to this folder (tools/buildlog/review/), else ../buildlog/."""
    cands = [Path(explicit)] if explicit else [HERE.parent / "buildlog.json", HERE.parent / "buildlog" / "buildlog.json"]
    for c in cands:
        if c.is_file():
            return c.resolve()
    die("buildlog.json not found (looked at %s)" % ", ".join(str(c) for c in cands))


def configure(settings_path=None):
    """Load the build log settings and derive every path. Called at import and again for --settings."""
    global SETTINGS_PATH, bt, settings, CFG, ROOT, ITEMS_PATH, REVIEW_DIR, HUB_PATH, DOCS_OUT, MEDIA_OUT, TEMPLATE
    global LIVE_URL, BUILDLOG_URL, PORT
    SETTINGS_PATH = find_settings(settings_path)
    bt = _load_module("buildlog_tasks", SETTINGS_PATH.parent / "buildlog_tasks.py")
    settings = bt.load_settings(SETTINGS_PATH)
    CFG = settings.get("review") if isinstance(settings.get("review"), dict) else {}
    ROOT = Path(settings["root"]).resolve()
    ITEMS_PATH = ROOT / settings.get("review_items", "docs/review/items.json")
    REVIEW_DIR = ITEMS_PATH.parent
    HUB_PATH = ROOT / settings.get("review_page", (REVIEW_DIR / "index.html").relative_to(ROOT).as_posix())
    DOCS_OUT = REVIEW_DIR / "docs"
    MEDIA_OUT = REVIEW_DIR / "media"
    TEMPLATE = next((p for p in (HERE / "hub_template.html", HERE.parent / "templates" / "review-hub.html",
                                 HERE.parents[1] / "templates" / "review-hub.html") if p.is_file()),
                    HERE / "hub_template.html")
    PORT = int(settings.get("port", 33650))
    LIVE_URL = "http://127.0.0.1:%d/%s" % (PORT, HUB_PATH.relative_to(ROOT).as_posix())
    BUILDLOG_URL = "http://127.0.0.1:%d/%s" % (PORT, settings.get("page", "docs/buildlog.html"))
    screenplay.configure(CFG.get("screenplay") or {}, ROOT)


def rel(path):
    return Path(path).resolve().relative_to(ROOT).as_posix()


def up(from_dir):
    """The relative prefix from a folder inside the repo back to the repo root ('../../' for docs/review)."""
    depth = len(Path(from_dir).resolve().relative_to(ROOT).parts)
    return "../" * depth


def now_iso():
    return bt.now_iso()


def parse_time(text):
    try:
        t = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()


def iso_utc(t):
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- store -----------------------------------------------------------------------------------------------

def load():
    if not ITEMS_PATH.exists():
        return {"version": 1, "items": []}
    data = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        die("%s: expected {\"items\": [...]}" % ITEMS_PATH)
    return data


def save(data):
    ITEMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    bt.write_atomic(ITEMS_PATH, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def find(data, item_id):
    for it in data["items"]:
        if it["id"] == item_id:
            return it
    die("no item with id %r (try: list --all)" % item_id)


# ---- paths and media ---------------------------------------------------------------------------------------

def repo_path(p, what="path", allow_outside=False):
    """A path as a repo-relative POSIX string. Files outside the repo are copied into the media folder."""
    raw = str(p).strip()
    full = (Path(raw) if os.path.isabs(raw) else ROOT / raw).resolve()
    if not full.is_file():
        die("%s %s: no such file" % (what, raw))
    if ROOT != full and ROOT not in full.parents:
        if not allow_outside:
            die("%s %s: outside the repo. Copy it in first: review.py import-media PATH" % (what, raw))
        return import_media(full)
    return full.relative_to(ROOT).as_posix()


def import_media(src, name=None):
    """Copy a file into <review dir>/media/<date>_<name>_<hash>; returns its repo path."""
    src = Path(src).resolve()
    if not src.is_file():
        die("import-media %s: no such file" % src)
    if src.suffix.lower() not in MEDIA_EXT + DOC_EXT:
        die("import-media %s: unsupported type %s" % (src, src.suffix))
    MEDIA_OUT.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^a-z0-9]+", "-", (name or src.stem).lower()).strip("-")[:60] or "file"
    digest = hashlib.sha1(src.read_bytes()).hexdigest()[:8]
    target = MEDIA_OUT / ("%s_%s_%s%s" % (datetime.date.today().isoformat(), stem, digest, src.suffix.lower()))
    if not target.exists():
        shutil.copy2(str(src), str(target))
    print("copied %s -> %s" % (src, rel(target)), file=sys.stderr)
    return rel(target)


def media_kind(path):
    ext = Path(str(path)).suffix.lower()
    return "video" if ext in VIDEO_EXT else "audio" if ext in AUDIO_EXT else "image" if ext in IMAGE_EXT else "file"


def normalize_media(entries):
    out = []
    for m in entries or []:
        if isinstance(m, str):
            path, _, caption = m.partition("::")
            m = {"path": path, "caption": caption}
        path = repo_path(m.get("path", ""), "--media", allow_outside=True)
        if media_kind(path) == "file":
            die("--media %s: not an image, clip or sound (%s)" % (path, ", ".join(MEDIA_EXT)))
        out.append({"path": path, "caption": (m.get("caption") or "").strip(), "kind": media_kind(path)})
    return out


def normalize_compare(entries):
    out = []
    for c in entries or []:
        if isinstance(c, str):
            parts = c.split("::")
            if len(parts) < 2:
                die("--compare needs BEFORE::AFTER[::caption]")
            c = {"before": parts[0], "after": parts[1], "caption": parts[2] if len(parts) > 2 else ""}
        b = repo_path(c["before"], "--compare before", allow_outside=True)
        a = repo_path(c["after"], "--compare after", allow_outside=True)
        kb, ka = media_kind(b), media_kind(a)
        if kb not in ("image", "video") or ka not in ("image", "video"):
            die("--compare %s::%s: both sides must be images or clips" % (b, a))
        if kb != ka:
            die("--compare %s::%s: one side is an image, the other a clip; compare like with like" % (b, a))
        out.append({"before": b, "after": a, "caption": (c.get("caption") or "").strip(), "kind": ka})
    return out


def doc_kind(path):
    ext = Path(path).suffix.lower()
    return "script" if path.endswith(".screen.md") else "md" if ext == ".md" else ext.lstrip(".")


def normalize_docs(entries):
    out = []
    for d in entries or []:
        if isinstance(d, str):
            path, _, title = d.partition("::")
            d = {"path": path, "title": title}
        path = repo_path(d.get("path", ""), "--doc")
        if Path(path).suffix.lower() not in DOC_EXT:
            die("--doc %s: not a document (%s)" % (path, ", ".join(DOC_EXT)))
        out.append({"path": path, "title": (d.get("title") or "").strip() or Path(path).name, "kind": doc_kind(path), "html": ""})
    return out


def normalize_choices(entries, choices_flag=None):
    out = []
    if choices_flag:
        for i, label in enumerate([s.strip() for s in choices_flag.split("|") if s.strip()]):
            out.append({"id": re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "c%d" % (i + 1),
                        "label": label, "detail": ""})
    for c in entries or []:
        if isinstance(c, str):
            parts = c.split("::")
            c = {"id": parts[0], "label": parts[1] if len(parts) > 1 else parts[0], "detail": parts[2] if len(parts) > 2 else ""}
        if not c.get("id") or not c.get("label"):
            die("a choice needs an id and a label")
        out.append({"id": str(c["id"]), "label": str(c["label"]), "detail": str(c.get("detail", "")),
                    "default": bool(c.get("default"))})
    ids = [c["id"] for c in out]
    if len(ids) != len(set(ids)):
        die("duplicate choice ids: %s" % ids)
    return out


def normalize_candidates(entries, recommend=()):
    out = []
    for c in entries or []:
        if isinstance(c, str):
            parts = c.split("::")
            if len(parts) < 3:
                die("--candidate needs ID::label::MEDIA[::detail[::model[::round]]]")
            c = {"id": parts[0], "label": parts[1], "media": parts[2], "detail": parts[3] if len(parts) > 3 else "",
                 "model": parts[4] if len(parts) > 4 else "", "round": parts[5] if len(parts) > 5 else ""}
        media = c.get("media")
        paths = [repo_path(m, "--candidate media", allow_outside=True) for m in (media if isinstance(media, list) else [media]) if m]
        if not paths:
            die("candidate %s has no media" % c.get("id"))
        for p in paths:
            if media_kind(p) not in ("audio", "video", "image"):
                die("candidate %s: %s is not a sound, clip or image" % (c.get("id"), p))
        rank = c.get("rank") or (list(recommend).index(c["id"]) + 1 if c["id"] in recommend else 0)
        out.append({"id": str(c["id"]), "label": str(c.get("label") or c["id"]), "media": paths,
                    "detail": str(c.get("detail", "")), "model": str(c.get("model", "")),
                    "round": str(c.get("round", "")), "recommended": bool(c.get("recommended")) or c["id"] in recommend,
                    "rank": int(rank), "lines": c.get("lines") or []})
    ids = [c["id"] for c in out]
    if len(ids) != len(set(ids)):
        die("duplicate candidate ids: %s" % ids)
    return out


def normalize_tags(entries):
    out = []
    for t in entries or []:
        for part in str(t).split(","):
            part = part.strip().lower()
            if not part:
                continue
            if not TAG_RE.match(part):
                die("--tag %r: lowercase letters, digits, space, _ . / - (up to 40)" % part)
            if part not in out:
                out.append(part)
    return out


def body_text(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith("@"):
        p = Path(value[1:])
        p = p if p.is_absolute() else ROOT / p
        if not p.is_file():
            die("--body %s: no such file" % value)
        return p.read_text(encoding="utf-8")
    return str(value)


# ---- rendering ---------------------------------------------------------------------------------------------

def doc_slug(repo_rel):
    s = repo_rel
    if s.startswith("docs/"):
        s = s[len("docs/"):]
    s = re.sub(r"\.md$", "", s)
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", s.replace("/", "--")).strip("-")


def doc_html_path(repo_rel):
    return (DOCS_OUT / ("%s.html" % doc_slug(repo_rel))).relative_to(ROOT).as_posix()


def hub_href_from_docs():
    """The hub page as seen from a rendered document."""
    return os.path.relpath(HUB_PATH, DOCS_OUT).replace(os.sep, "/")


def make_resolver(src_rel, item_id, queue, rendered):
    """Links inside a Markdown doc: .md in the repo -> its rendered page (queued), media -> relative file path."""
    src_dir = (ROOT / src_rel).parent
    to_root = up(DOCS_OUT)

    def resolve(url, kind):
        if re.match(r"^[a-z][a-z0-9+.-]*:", url) or url.startswith("#") or url.startswith("/"):
            return url
        target, _, frag = url.partition("#")
        try:
            full = (src_dir / target).resolve()
        except (OSError, RuntimeError):
            return url
        if not full.is_file() or (ROOT != full and ROOT not in full.parents):
            return url
        r = full.relative_to(ROOT).as_posix()
        if r.endswith(".md"):
            if r not in rendered:
                queue.append(r)
            out = Path(doc_html_path(r)).name
            return out + ("?item=%s" % item_id if item_id else "") + ("#" + frag if frag else "")
        return to_root + r + ("#" + frag if frag else "")
    return resolve


def page_context(item=None, doc_title=None):
    """What a rendered document needs for its breadcrumb and comment threads."""
    return {"brand": brand(), "hub": hub_href_from_docs(), "item_id": item["id"] if item else "",
            "item_title": item["title"] if item else "", "doc_title": doc_title or ""}


def render_doc(repo_rel, item=None, title=None, rendered=None, depth=2, source_note=""):
    """Render one repo document to the docs folder; linked .md files are rendered too (depth-limited)."""
    rendered = rendered if rendered is not None else set()
    queue = [repo_rel]
    first = doc_html_path(repo_rel)
    level = {repo_rel: 0}
    while queue:
        r = queue.pop(0)
        if r in rendered:
            continue
        rendered.add(r)
        src = ROOT / r
        out = ROOT / doc_html_path(r)
        out.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8")
        sub_queue = []
        own = r == repo_rel
        ctx = page_context(item if own else None, (title if own else None))
        if r.endswith(".screen.md"):
            page = screenplay.render_page(text, title=title if own else None, ctx=ctx, doc_rel=r,
                                          source_note=source_note if own else "")
        else:
            resolver = make_resolver(r, item["id"] if (own and item) else "", sub_queue, rendered)
            page = md_render.render_page(text, title=title if own else None, resolve=resolver, ctx=ctx, doc_rel=r,
                                         source_note=source_note if own else "")
        bt.write_atomic(out, page)
        if level[r] < depth:
            for s in sub_queue:
                level.setdefault(s, level[r] + 1)
                queue.append(s)
    return first


def render_item_docs(item):
    """Render the item's documents (with its breadcrumb), then anything its body links to.
    One `rendered` set per item, so a document linked from another is not rendered twice."""
    rendered = set()
    for d in item.get("docs") or []:
        if d["kind"] in ("md", "script"):
            d["html"] = render_doc(d["path"], item=item, title=d.get("title") or None, rendered=rendered,
                                   source_note="part of: %s" % item["title"])
        else:
            d["html"] = d["path"]
    body = item.get("body") or ""
    if body:
        queue = []
        html_body, _, _ = md_render.render(body, resolve=body_resolver(item["id"], queue))
        item["body_html"] = html_body
        for r in queue:
            render_doc(r, item=item, rendered=rendered, source_note="linked from: %s" % item["title"])
    else:
        item["body_html"] = ""


def body_resolver(item_id, queue):
    """Links in an item's body are repo paths. A .md becomes its rendered page; media stays a repo path
    (the hub page prefixes the root for file:// and served use); anything else is refused loudly."""
    docs_rel = os.path.relpath(DOCS_OUT, HUB_PATH.parent).replace(os.sep, "/")

    def resolve(url, kind):
        if re.match(r"^[a-z][a-z0-9+.-]*:", url) or url.startswith("#"):
            return url
        target, _, frag = url.partition("#")
        full = (ROOT / target).resolve()
        if not full.is_file() or (ROOT != full and ROOT not in full.parents):
            die("body of %s links to %s, which is not a file in the repo" % (item_id, url))
        r = full.relative_to(ROOT).as_posix()
        if r.endswith(".md"):
            queue.append(r)
            return "%s/%s?item=%s%s" % (docs_rel, Path(doc_html_path(r)).name, item_id, ("#" + frag) if frag else "")
        return "@@ROOT@@" + r + (("#" + frag) if frag else "")
    return resolve


def brand():
    return str(CFG.get("brand") or settings.get("title") or "Project")


def extra_pages():
    """Markdown pages rendered next to the hub and linked from its header (buildlog.json review.pages)."""
    pages = CFG.get("pages")
    return [p for p in pages if isinstance(p, dict) and p.get("src") and p.get("out")] if isinstance(pages, list) else []


def snapshot_items(data):
    """What the hub page embeds: everything except agent-only fields."""
    return [{k: v for k, v in it.items() if k not in AGENT_ONLY} for it in data["items"]]


def write_hub(data):
    if not TEMPLATE.exists():
        die("hub template missing: %s" % TEMPLATE)
    tpl = TEMPLATE.read_text(encoding="utf-8")
    nav = [{"label": "Build log", "href": BUILDLOG_URL,
            "local": os.path.relpath(ROOT / settings.get("page", "docs/buildlog.html"), HUB_PATH.parent).replace(os.sep, "/")}]
    nav += [{"label": p.get("nav") or p.get("title") or p["out"], "href": p["out"]} for p in extra_pages()]
    snap = json.dumps({"items": snapshot_items(data), "generated": now_iso(), "live_url": LIVE_URL,
                       "buildlog_url": BUILDLOG_URL, "root": up(HUB_PATH.parent), "brand": brand(), "nav": nav,
                       "docs_dir": os.path.relpath(DOCS_OUT, HUB_PATH.parent).replace(os.sep, "/"),
                       "hub_dir": HUB_PATH.parent.relative_to(ROOT).as_posix()},
                      ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
    import html as _html
    page = tpl.replace("@@LIVE_URL@@", _html.escape(LIVE_URL, True)).replace("@@BRAND@@", _html.escape(brand()))
    block = ("<!-- ITEMS-SNAPSHOT:START -->\n<script type=\"application/json\" id=\"items-snapshot\">%s</script>\n"
             "<!-- ITEMS-SNAPSHOT:END -->" % snap)
    if "@@SNAPSHOT@@" in page:
        page = page.replace("@@SNAPSHOT@@", block)
    else:
        page = SNAP_RE.sub(lambda m: block, page, count=1)
    HUB_PATH.parent.mkdir(parents=True, exist_ok=True)
    bt.write_atomic(HUB_PATH, page)


def write_extra_pages():
    """review.pages in buildlog.json: [{src: docs/LOG.md, out: log.html, title, nav}] rendered next to the hub."""
    out = []
    for p in extra_pages():
        src = ROOT / p["src"]
        if not src.is_file():
            continue
        target = HUB_PATH.parent / p["out"]
        queue, rendered = [], set()
        resolver = make_resolver(p["src"], "", queue, rendered)
        ctx = {"brand": brand(), "hub": HUB_PATH.name, "item_id": "", "item_title": "", "doc_title": p.get("title") or ""}
        page = md_render.render_page(src.read_text(encoding="utf-8"), title=p.get("title"), resolve=resolver, ctx=ctx,
                                     doc_rel=p["src"], source_note="regenerated by review.py sync", comments=False)
        # this page sits next to the hub, one level above the docs folder: fix the media prefix and doc links
        docs_rel = os.path.relpath(DOCS_OUT, target.parent).replace(os.sep, "/")
        page = page.replace("'" + up(DOCS_OUT), "'" + up(target.parent))
        page = re.sub(r"href='([A-Za-z0-9_.-]+\.html)(\?item=[^']*)?'",
                      lambda m: "href='%s/%s'" % (docs_rel, m.group(1)) if m.group(1) != HUB_PATH.name else m.group(0), page)
        bt.write_atomic(target, page)
        for sub in queue:
            render_doc(sub)
        out.append(rel(target))
    return out


def render_all(data, ids=None):
    for it in data["items"]:
        if ids is None or it["id"] in ids:
            render_item_docs(it)
    write_hub(data)


# ---- items -------------------------------------------------------------------------------------------------

def build_item(args, spec, existing=None):
    """Merge CLI flags and/or a JSON spec into a validated item dict."""
    it = dict(existing or {})
    spec = dict(spec or {})
    for key in ("id", "kind", "title", "summary", "priority", "default", "how", "on_answer", "source_task"):
        val = getattr(args, key, None)
        if val is not None:
            spec[key] = val
    if getattr(args, "body", None) is not None:
        spec["body"] = body_text(args.body)
    elif isinstance(spec.get("body"), str) and spec["body"].startswith("@"):
        spec["body"] = body_text(spec["body"])
    for flag, key in (("media", "media"), ("compare", "compare"), ("doc", "docs"), ("candidate", "candidates"),
                      ("tag", "tags")):
        if getattr(args, flag, None):
            spec[key] = getattr(args, flag)
    if getattr(args, "choices", None) or getattr(args, "choice", None):
        spec["choices"] = normalize_choices(getattr(args, "choice", None), getattr(args, "choices", None))
    rec = getattr(args, "recommend", None) or spec.get("recommend") or []
    recommend = [s.strip() for s in rec.split(",") if s.strip()] if isinstance(rec, str) else list(rec)

    for key in ("id", "kind", "title", "summary", "priority", "default", "how", "on_answer", "source_task", "body"):
        if key in spec:
            it[key] = spec[key]
    if "media" in spec:
        it["media"] = normalize_media(spec["media"])
    if "compare" in spec:
        it["compare"] = normalize_compare(spec["compare"])
    if "docs" in spec:
        it["docs"] = normalize_docs(spec["docs"])
    if "choices" in spec:
        it["choices"] = normalize_choices(spec["choices"])
    if "candidates" in spec:
        it["candidates"] = normalize_candidates(spec["candidates"], recommend)
    if "thread" in spec:
        it["thread"] = spec["thread"]
    if "tags" in spec:
        it["tags"] = normalize_tags(spec["tags"])

    it.setdefault("priority", "normal")
    it.setdefault("status", "open")
    for key in LIST_FIELDS:
        it.setdefault(key, [])
    for key in ("summary", "body", "default", "how", "on_answer", "source_task"):
        it.setdefault(key, "")
    it.setdefault("thread", [])
    if not it.get("id") or not ID_RE.match(it["id"]):
        die("--id is required: lowercase letters, digits and hyphens")
    if it.get("kind") not in KINDS:
        die("--kind must be one of %s" % ", ".join(KINDS))
    if not (it.get("title") or "").strip():
        die("--title is required")
    if not (it.get("summary") or "").strip():
        die("--summary is required: one plain-language line on what this is and why it matters")
    if it["priority"] not in PRIORITIES:
        die("--priority must be one of %s" % ", ".join(PRIORITIES))
    if it["kind"] == "choice" and len(it["choices"]) < 2:
        die("a choice item needs at least two --choices")
    if it["kind"] == "audition" and len(it["candidates"]) < 2:
        die("an audition needs at least two --candidate entries")
    if it["kind"] == "action" and not it["how"].strip() and not it["body"].strip():
        print("note: an action usually carries --how with the commands to run", file=sys.stderr)
    return it


def cmd_add(args, data):
    spec = read_json_arg(args.json) if args.json else {}
    it = build_item(args, spec)
    if any(x["id"] == it["id"] for x in data["items"]):
        die("id %r already exists (use update)" % it["id"])
    stamp = now_iso()
    it["added"], it["updated"], it["closed_at"] = stamp, stamp, None
    render_item_docs(it)
    data["items"].append(it)
    print(it["id"])
    return "review: add %s" % it["id"]


def cmd_update(args, data):
    it = find(data, args.id)
    spec = read_json_arg(args.json) if args.json else {}
    spec.pop("id", None)
    args.id = None
    new = build_item(args, spec, existing=it)
    new["id"] = it["id"]
    new["updated"] = now_iso()
    render_item_docs(new)
    it.clear()
    it.update(new)
    print("updated: %s" % it["id"])
    return "review: update %s" % it["id"]


def cmd_close(args, data):
    it = find(data, args.id)
    stamp = now_iso()
    if args.reply:
        it.setdefault("thread", []).append({"at": stamp, "who": "agent", "text": args.reply.strip()})
    if it.get("status") == "closed":
        print("already closed: %s" % it["id"])
        return "review: reply %s" % it["id"] if args.reply else None
    it["status"], it["closed_at"], it["updated"] = "closed", stamp, stamp
    print("closed: %s" % it["id"])
    return "review: close %s" % it["id"]


def cmd_reopen(args, data):
    it = find(data, args.id)
    if it.get("status") != "closed":
        print("already open: %s" % it["id"])
        return None
    it["status"], it["closed_at"], it["updated"] = "open", None, now_iso()
    print("reopened: %s" % it["id"])
    return "review: reopen %s" % it["id"]


def thread_key(anchor):
    """The comment thread an anchor belongs to; the server (thread_key) and the pages use the same keys."""
    if anchor["type"] == "line":
        return "line:%s#%d" % (anchor.get("doc", ""), int(anchor["line"]))
    return "time:%s@%.1f" % (anchor.get("media", ""), float(anchor["time_s"]))


def anchor_from_args(args, it, state_dir_path=None):
    """--line N [--doc PATH], --at SECONDS --clip PATH or --to MESSAGE_ID -> an anchor dict, or None."""
    if getattr(args, "to", None):
        msg = next((m for m in bt.inbox_messages(state_dir_path) if m.get("id") == args.to), None)
        if not msg or msg.get("item_id") != it["id"] or not msg.get("anchor"):
            die("--to %s: no comment with an anchor on %s in the inbox" % (args.to, it["id"]))
        return dict(msg["anchor"])
    if getattr(args, "line", None) is not None:
        doc = args.doc
        if not doc:
            docs = [d["path"] for d in it.get("docs") or [] if d["kind"] in ("script", "md")]
            if not docs:
                die("--line needs --doc (the item has no rendered document)")
            doc = docs[0]
        else:
            doc = repo_path(doc, "--doc")
        return {"type": "line", "line": int(args.line), "doc": doc, "excerpt": line_excerpt(doc, int(args.line))}
    if getattr(args, "at", None) is not None:
        if not args.clip:
            die("--at needs --clip PATH (the clip or sound the time refers to)")
        clip = repo_path(args.clip, "--clip", allow_outside=False)
        return {"type": "time", "time_s": round(float(args.at), 2), "media": clip, "excerpt": ""}
    return None


def line_excerpt(doc, line):
    """The source text of a 1-based line of a script (for Markdown docs the 'line' is a block number)."""
    if not doc.endswith(".screen.md"):
        return ""
    try:
        lines = (ROOT / doc).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    return lines[line - 1].strip()[:140] if 0 < line <= len(lines) else ""


def cmd_reply(args, data):
    it = find(data, args.id)
    text = args.text
    if text == "-":
        text = sys.stdin.read()
    text = text.strip()
    if not text:
        die("reply text is empty")
    media = normalize_media(args.media or [])
    entry = {"at": now_iso(), "who": "agent", "text": text}
    if media:
        entry["media"] = media
    anchor = anchor_from_args(args, it, state_dir(args))
    if anchor:
        entry["anchor"] = anchor
    it.setdefault("thread", []).append(entry)       # not "updated": a reply never reorders the card
    print("replied: %s%s" % (it["id"], " (thread %s)" % thread_key(anchor) if anchor else ""))
    return "review: reply %s" % it["id"]


def cmd_resolve(args, data):
    it = find(data, args.id)
    anchor = anchor_from_args(args, it, state_dir(args))
    if not anchor:
        die("resolve needs --line N [--doc PATH], --at SECONDS --clip PATH or --to MESSAGE_ID")
    key = thread_key(anchor)
    it.setdefault("resolved", {})[key] = {"resolved": not args.undo, "at": now_iso(), "by": "agent"}
    if args.reply:
        it.setdefault("thread", []).append({"at": now_iso(), "who": "agent", "text": args.reply.strip(), "anchor": anchor})
    print("%s: %s %s" % ("reopened" if args.undo else "resolved", it["id"], key))
    return "review: %s %s" % ("unresolve" if args.undo else "resolve", it["id"])


def parse_until(args):
    if args.later:
        return None, True
    if args.for_:
        m = DURATION_RE.match(args.for_.strip().lower())
        if not m:
            die("--for takes a number and h, d or w (3d, 12h, 2w)")
        n, unit = int(m.group(1)), m.group(2)
        delta = {"h": datetime.timedelta(hours=n), "d": datetime.timedelta(days=n), "w": datetime.timedelta(weeks=n)}[unit]
        return iso_utc(datetime.datetime.now(datetime.timezone.utc) + delta), False
    if args.until:
        t = parse_time(args.until)
        if t is None:
            die("--until %r: use an ISO date or time (2026-11-03 or 2026-11-03T09:00)" % args.until)
        if len(args.until.strip()) == 10:             # a bare date means the start of that day, local time
            t = datetime.datetime.combine(t.date(), datetime.time(8, 0)).astimezone()
        return iso_utc(t), False
    die("snooze needs --until DATE, --for 3d or --later")


def cmd_snooze(args, data):
    it = find(data, args.id)
    until, later = parse_until(args)
    it["snooze"] = {"until": until, "later": later, "at": now_iso(), "by": "agent"}
    if args.reply:
        it.setdefault("thread", []).append({"at": now_iso(), "who": "agent", "text": args.reply.strip()})
    print("snoozed: %s %s" % (it["id"], "until " + until if until else "for later"))
    return "review: snooze %s" % it["id"]


def cmd_unsnooze(args, data):
    it = find(data, args.id)
    it["snooze"] = {"cleared": True, "at": now_iso(), "by": "agent"}
    print("unsnoozed: %s" % it["id"])
    return "review: unsnooze %s" % it["id"]


# ---- reading: list, show, inbox ------------------------------------------------------------------------------

def state_dir(args):
    homes = settings.get("_homes") or {}
    return Path(getattr(args, "state_dir", None) or os.environ.get("BUILDLOG_STATE_DIR") or
                Path(homes.get("state_home") or Path.home() / ".local/state") / settings["name"])


def user_state(args):
    """The server's per-item review state (the person's snoozes, resolved threads and answers), read-only."""
    try:
        data = json.loads((state_dir(args) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    review = data.get("review") if isinstance(data, dict) else None
    return review if isinstance(review, dict) else {}


def _newer(a, b):
    if not isinstance(a, dict):
        return b if isinstance(b, dict) else None
    if not isinstance(b, dict):
        return a
    ta, tb = parse_time(a.get("at")), parse_time(b.get("at"))
    if tb is None:
        return a
    return b if ta is None or tb >= ta else a


def effective_snooze(it, ustate):
    s = _newer(it.get("snooze"), (ustate.get(it["id"]) or {}).get("snooze"))
    return None if not s or s.get("cleared") else s


def bucket(it, ustate, now=None):
    """open | answered | snoozed | due | closed (due: a snooze that ran out; it is back in the main list)."""
    if it.get("status") == "closed":
        return "closed"
    s = effective_snooze(it, ustate)
    if s:
        until = parse_time(s.get("until")) if s.get("until") else None
        if s.get("later") or (until and until > (now or datetime.datetime.now(datetime.timezone.utc))):
            return "snoozed"
        if until:
            return "due"
    answers = (ustate.get(it["id"]) or {}).get("answers") or []
    if any(a.get("decision") not in ("comment", "snooze", "unsnooze", "resolve", "unresolve") for a in answers):
        return "answered"
    return "open"


def sort_key(it):
    """Open before closed, most important first, then newest first (the hub sorts the same way)."""
    pr = PRIORITY_RANK.get(it.get("priority"), PRIORITY_RANK["normal"])
    t = parse_time(it.get("added", ""))
    return (it.get("status") == "closed", pr, -(t.timestamp() if t else 0))


def cmd_list(args, data):
    ustate = user_state(args)
    rows = []
    for it in sorted(data["items"], key=sort_key):
        b = bucket(it, ustate)
        if args.all or b != "closed":
            rows.append((it, b))
    if args.json:
        out = []
        for it, b in rows:
            row = dict(it, bucket=b, snooze=effective_snooze(it, ustate))
            out.append(row)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    groups = [("due", "Back from snooze (due again)"), ("open", "Needs the user"), ("answered", "Answered, waiting on an agent"),
              ("snoozed", "Snoozed"), ("closed", "Done")]
    printed = False
    for b, head in groups:
        part = [it for it, bb in rows if bb == b]
        if not part:
            continue
        print("== %s (%d)" % (head, len(part)))
        for it in part:
            extra = ""
            s = effective_snooze(it, ustate)
            if b in ("snoozed", "due") and s:
                extra = "  [%s by %s]" % ("later" if s.get("later") else "until " + str(s.get("until")), s.get("by", "?"))
            tags = (" #" + " #".join(it["tags"])) if it.get("tags") else ""
            print("  %-8s %-6s %-36s %s%s%s" % (it["kind"], it.get("priority", "normal"), it["id"], it["title"], tags, extra))
        printed = True
    if not printed:
        print("(no open items)")


def cmd_show(args, data):
    it = find(data, args.id)
    ustate = user_state(args)
    print(json.dumps(dict(it, bucket=bucket(it, ustate), snooze=effective_snooze(it, ustate),
                          answers=(ustate.get(it["id"]) or {}).get("answers") or []), indent=2, ensure_ascii=False))


def read_json_arg(value):
    text = sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8")
    spec = json.loads(text)
    if not isinstance(spec, dict):
        die("--json must hold one JSON object")
    return spec


def describe(m):
    d = m.get("decision", "?")
    if d == "snooze":
        return "snooze %s" % ("for later" if m.get("later") else "until %s" % m.get("until"))
    return d


def cmd_inbox(args, data):
    sd = state_dir(args)
    sd.mkdir(parents=True, exist_ok=True, mode=0o700)
    by_id = {it["id"]: it for it in data["items"]}
    with bt.Locked(sd):
        read = bt.read_ids(sd)
        seen = set(read)
        msgs = [m for m in bt.inbox_messages(sd) if m.get("kind") == "review"]
        for m in msgs:
            if m["id"] in seen and m["status"] in ("queued", "local"):
                m["status"] = "read"
            it = by_id.get(m.get("item_id"))
            m["item_title"] = it["title"] if it else "(unknown item)"
            if m.get("anchor"):
                m["thread"] = thread_key(m["anchor"])
            if it and it.get("on_answer"):
                m["on_answer"] = it["on_answer"]
        shown = msgs if args.all else [m for m in msgs if m["status"] in ("queued", "local")]
        if args.json:
            print(json.dumps(shown, indent=2, ensure_ascii=False))
        else:
            for m in shown:
                head = "%s %-6s %-14s %s id=%s" % (m.get("sent_at", "?"), m["status"], describe(m), m.get("item_id"), m["id"])
                if m.get("choice"):
                    head += " choice=%s" % m["choice"]
                anchor = m.get("anchor") or {}
                if anchor.get("type") == "line":
                    head += " line=%s %s" % (anchor.get("line"), anchor.get("doc", ""))
                elif anchor.get("type") == "time":
                    head += " t=%ss %s" % (anchor.get("time_s"), anchor.get("media", ""))
                print(head)
                print("    item: %s" % m["item_title"])
                if (m.get("comment") or "").strip():
                    print("    " + m["comment"].strip().replace("\n", "\n    "))
                if anchor.get("excerpt"):
                    print("    at: %s" % anchor["excerpt"])
                if m.get("on_answer") and m.get("decision") not in ("snooze", "unsnooze", "resolve", "unresolve"):
                    print("    agent: %s" % m["on_answer"])
            if not shown:
                print("(no unhandled review answers)")
        if args.mark_read:
            new = [m["id"] for m in shown if m["id"] not in seen]
            if new:
                bt.write_atomic(sd / "read.json", json.dumps({"read": read + new}, indent=1) + "\n", mode=0o600)
                print("marked read: %d" % len(new), file=sys.stderr)


# ---- main --------------------------------------------------------------------------------------------------

def add_item_flags(p):
    p.add_argument("--json", help="item fields as a JSON file ('-' = stdin); flags override it")
    p.add_argument("--kind", choices=KINDS)
    p.add_argument("--title")
    p.add_argument("--summary", help="one plain-language line: what it is and why it matters")
    p.add_argument("--body", help="Markdown text, or @path/to/file.md")
    p.add_argument("--priority", "--importance", dest="priority", choices=PRIORITIES,
                   help="urgent (blocks work now), high, normal (default) or low")
    p.add_argument("--tag", action="append", metavar="NAME", help="workstream or topic; repeatable, or comma-separated")
    p.add_argument("--default", help="what happens with no answer (decisions)")
    p.add_argument("--how", help="copy-paste commands (actions)")
    p.add_argument("--media", action="append", metavar="PATH[::caption]")
    p.add_argument("--compare", action="append", metavar="BEFORE::AFTER[::caption]")
    p.add_argument("--doc", action="append", metavar="PATH[::title]", help="a .md / .screen.md (rendered) or .pdf/.html")
    p.add_argument("--choices", help='"A|B|C" (choice items)')
    p.add_argument("--choice", action="append", metavar="ID::label[::detail]")
    p.add_argument("--candidate", action="append", metavar="ID::label::MEDIA[::detail[::model[::round]]]")
    p.add_argument("--recommend", help="comma-separated candidate ids the agents recommend, best first (shown as #1, #2, ...)")
    p.add_argument("--on-answer", dest="on_answer", help="agent-only: what to run when the answer arrives")
    p.add_argument("--source-task", dest="source_task", help="the buildlog task this replaces")
    p.add_argument("--commit", action="store_true")


def add_anchor_flags(p):
    p.add_argument("--line", type=int, help="the comment thread on this line (script source line; Markdown block number)")
    p.add_argument("--doc", help="the document of --line (default: the item's first rendered document)")
    p.add_argument("--at", type=float, metavar="SECONDS", help="the comment thread at this time of --clip")
    p.add_argument("--clip", help="the clip or sound of --at")
    p.add_argument("--to", metavar="MESSAGE_ID", help="the thread of this inbox comment")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Manage the review hub's items.")
    ap.add_argument("--state-dir", help="default: ~/.local/state/<name>")
    ap.add_argument("--settings", help="buildlog.json (default: found next to this folder)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add", help="add an item")
    p.add_argument("--id", help="lowercase id; the deep link is index.html#item=ID")
    add_item_flags(p)
    p = sub.add_parser("update", help="change an item (list flags replace that list)")
    p.add_argument("id")
    add_item_flags(p)
    p = sub.add_parser("close")
    p.add_argument("id")
    p.add_argument("--reply", help="a closing note shown under the item")
    p.add_argument("--commit", action="store_true")
    p = sub.add_parser("reopen")
    p.add_argument("id")
    p.add_argument("--commit", action="store_true")
    p = sub.add_parser("reply", help="a note from the agent under an item or in one of its comment threads")
    p.add_argument("id")
    p.add_argument("text", help='"text" or "-" for stdin')
    p.add_argument("--media", action="append", metavar="PATH[::caption]")
    add_anchor_flags(p)
    p.add_argument("--commit", action="store_true")
    p = sub.add_parser("resolve", help="mark a comment thread resolved (--undo reopens it)")
    p.add_argument("id")
    add_anchor_flags(p)
    p.add_argument("--undo", action="store_true")
    p.add_argument("--reply", help="a note in the thread as it is resolved")
    p.add_argument("--commit", action="store_true")
    p = sub.add_parser("snooze", help="hide an item from the user's main list until a time, or for later")
    p.add_argument("id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--until", help="ISO date or time")
    g.add_argument("--for", dest="for_", metavar="DURATION", help="12h, 3d, 2w")
    g.add_argument("--later", action="store_true", help="no date: under Snoozed until someone wakes it")
    p.add_argument("--reply", help="why, shown under the item")
    p.add_argument("--commit", action="store_true")
    p = sub.add_parser("unsnooze")
    p.add_argument("id")
    p.add_argument("--commit", action="store_true")
    p = sub.add_parser("list", help="items by state; snoozed ones that came due are listed first")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show")
    p.add_argument("id")
    p = sub.add_parser("inbox", help="the user's answers no agent has handled yet")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--mark-read", action="store_true")
    p = sub.add_parser("render", help="re-render documents and the hub")
    p.add_argument("ids", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--commit", action="store_true")
    p = sub.add_parser("preview", help="render one document (no item) and print where to open it")
    p.add_argument("path")
    p = sub.add_parser("sync", help="rebuild the hub and the extra pages")
    p.add_argument("--commit", action="store_true")
    p = sub.add_parser("import-media")
    p.add_argument("path")
    p.add_argument("--name")
    p = sub.add_parser("commit")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)
    if args.settings:
        configure(args.settings)
    bt.utf8_stdio()

    if args.cmd == "import-media":
        print(import_media(args.path, args.name))
        return 0
    if args.cmd == "commit":
        paths = [Path(p) if os.path.isabs(p) else ROOT / p for p in args.paths]
        short = bt.commit_paths(paths, args.message, ROOT)
        if short:
            print("committed %s: %s" % (short, args.message))
        return 0
    if args.cmd == "preview":
        r = repo_path(args.path, "preview")
        if doc_kind(r) not in ("md", "script"):
            die("preview renders .md and .screen.md files")
        with bt.Locked(REVIEW_DIR if REVIEW_DIR.exists() else ROOT):
            out = render_doc(r)
        print("http://127.0.0.1:%d/%s" % (PORT, out))
        return 0

    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    with bt.Locked(REVIEW_DIR):
        data = load()
        if args.cmd in ("list", "show", "inbox"):
            {"list": cmd_list, "show": cmd_show, "inbox": cmd_inbox}[args.cmd](args, data)
            return 0
        touched = [ITEMS_PATH, HUB_PATH, DOCS_OUT, MEDIA_OUT]
        if args.cmd == "render":
            ids = None if (args.all or not args.ids) else set(args.ids)
            render_all(data, ids)
            save(data)
            message = "review: render"
        elif args.cmd == "sync":
            write_hub(data)
            touched += [ROOT / p for p in write_extra_pages()]
            message = "review: sync"
        else:
            message = {"add": cmd_add, "update": cmd_update, "close": cmd_close, "reopen": cmd_reopen,
                       "reply": cmd_reply, "resolve": cmd_resolve, "snooze": cmd_snooze,
                       "unsnooze": cmd_unsnooze}[args.cmd](args, data)
            if message:
                save(data)
                write_hub(data)
    if message and getattr(args, "commit", False):
        short = bt.commit_paths([p for p in touched if p.exists()], message, ROOT)
        if short:
            print("committed %s: %s" % (short, message), file=sys.stderr)
    return 0


configure(os.environ.get("REVIEW_SETTINGS") or None)

if __name__ == "__main__":
    sys.exit(main())
