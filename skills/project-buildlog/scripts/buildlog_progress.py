#!/usr/bin/env python3
"""Measured progress bars for the status cards on the build log page.

Each card in the CARDS section links one or more markdown status files with
an anchor of class "status-src" (href relative to the page). For every linked
file this reads the checklist in its "## Status" section and counts, per line:

  shown  a line marked with a check mark (U+2713) that cites a file under one
         of the evidence folders (default docs/evidence/) which is committed
         in git (HEAD). A line that also says "partly shown" is not "shown".
  partly a line that says "partly shown", or is ticked [~].
  total  every checklist line (- [ ], - [x], - [~]).

Lines ticked [x] without a check mark only count in the total: a tick is a
claim, the mark plus a committed evidence file is the proof.

  python3 -I buildlog_progress.py           print the numbers as JSON
  python3 -I buildlog_progress.py --write   also rewrite the bars in the CARDS section

--write edits only the text between the CARDS START and END markers. It takes
the same directory lock as buildlog_tasks.py, re-reads the page right before
writing, and replaces the file atomically. Running it again with unchanged
status files changes nothing. Python standard library only.
"""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote

try:
    import fcntl
except ImportError:                          # Windows
    fcntl = None
    import msvcrt

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = "buildlog.json"
START = "<!-- CARDS:START -->"
END = "<!-- CARDS:END -->"

ITEM_RE = re.compile(r"^[-*+]\s+\[([ xX~])\](?:\s|$)")
OTHER_LIST_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s")
STATUS_HEAD_RE = re.compile(r"^##\s+Status\b")
ANY_HEAD_RE = re.compile(r"^#{1,2}\s")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
LINK_RE = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
PARTLY_RE = re.compile(r"\bpartly\s+shown\b", re.I)
MARK_RE = re.compile("[✓✔]")
SRC_RE = re.compile(r'<a\b[^>]*\bclass="status-src"[^>]*>')
HREF_RE = re.compile(r'\bhref="([^"]+)"')
CARD_RE = re.compile(r'^[ \t]*<div class="card">[ \t]*$', re.M)
PROG_LINE_RE = re.compile(r'^[ \t]*<div class="prog"[^\n]*</div>[ \t]*\n', re.M)
NOTE_LINE_RE = re.compile(r'^[ \t]*<p class="muted prog-note"[^\n]*</p>[ \t]*\n', re.M)
TAGS_LINE_RE = re.compile(r'^[ \t]*<div><span class="tag">[^\n]*</div>[ \t]*$', re.M)
H3_LINE_RE = re.compile(r"^([ \t]*)<h3>(.*?)</h3>[ \t]*$", re.M)


def load_settings(path=None):
    path = Path(path) if path else HERE / SETTINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    s = {"root": "..", "page": "docs/buildlog.html", "evidence_dirs": ["docs/evidence"]}
    s.update(data if isinstance(data, dict) else {})
    s["root"] = str((path.parent / s["root"]).resolve())
    return s


# ---- reading the status lists ----------------------------------------------

def status_items(text):
    """Return the checklist lines of the Status section, one string per line (continuations joined)."""
    items, cur = [], None
    in_status = in_fence = False
    prev_blank = False
    for line in text.split("\n"):
        if not in_status:
            in_status = bool(STATUS_HEAD_RE.match(line))
            continue
        if not in_fence and ANY_HEAD_RE.match(line):
            break
        if FENCE_RE.match(line):
            in_fence = not in_fence
            if cur is not None:
                cur.append(line)
            prev_blank = False
            continue
        if in_fence:
            if cur is not None:
                cur.append(line)
            continue
        if not line.strip():
            prev_blank = True
            continue
        indented = line[0] in " \t"
        if ITEM_RE.match(line):
            cur = [line]
            items.append(cur)
        elif OTHER_LIST_RE.match(line) or (prev_blank and not indented):
            cur = None                       # a bullet without a box, or a new paragraph
        elif cur is not None:
            cur.append(line)                 # continuation or nested bullet
        prev_blank = False
    return ["\n".join(i) for i in items]


class Evidence:
    """Is a path an evidence file that is in git? Falls back to the disk when there is no git."""

    def __init__(self, repo, roots=("docs/evidence",), use_git=True):
        self.repo = Path(repo)
        self.roots = tuple(r.strip("/") for r in roots)
        self.files = None
        self.mode = "disk"
        if use_git:
            try:
                out = subprocess.run(["git", "-C", str(self.repo), "ls-tree", "-r", "-z", "--name-only", "HEAD",
                                      "--"] + list(self.roots), capture_output=True, check=True, timeout=60).stdout
                self.files = {p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p}
                self.dirs = {d for f in self.files for d in Evidence._parents(f)}
                self.mode = "git HEAD"
            except (OSError, subprocess.SubprocessError):
                print("note: git not usable in %s; checking evidence files on disk" % self.repo, file=sys.stderr)

    @staticmethod
    def _parents(rel):
        parts = rel.split("/")
        return {"/".join(parts[:i]) for i in range(1, len(parts))}

    def path_re(self):
        alts = "|".join(re.escape(r) for r in self.roots)
        return re.compile(r"(?<![\w/.-])((?:\.\./)*(?:%s)/[^\s)\]`'\"<>,;]+)" % alts)

    def ok(self, rel):
        if not any(rel == r or rel.startswith(r + "/") for r in self.roots):
            return False
        if self.files is not None:
            return rel in self.files or rel in self.dirs
        p = self.repo / rel
        return p.is_file() or (p.is_dir() and any(q.is_file() for q in p.rglob("*")))


def cited_evidence(item, src, repo, evidence):
    """Evidence paths named by this line (markdown links or plain evidence paths) that exist."""
    found = []
    for target in LINK_RE.findall(item) + evidence.path_re().findall(item):
        t = unquote(target.split("#")[0].split("?")[0]).rstrip(".")
        if not t or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", t):
            continue
        rooted = t.startswith("/") or any(t.startswith(r + "/") for r in evidence.roots)
        base = repo if rooted else src.parent
        p = Path(os.path.normpath(base / t.lstrip("/")))
        try:
            rel = p.relative_to(os.path.normpath(repo)).as_posix()
        except ValueError:
            continue
        if evidence.ok(rel):
            found.append(rel)
    return found


def classify(item, src, repo, evidence):
    """Return 'partly', 'shown', 'marked-no-evidence', 'ticked' or 'open' for one checklist line."""
    box = ITEM_RE.match(item).group(1)
    if PARTLY_RE.search(item) or box == "~":
        return "partly"
    if MARK_RE.search(item):
        return "shown" if cited_evidence(item, src, repo, evidence) else "marked-no-evidence"
    return "ticked" if box in "xX" else "open"


def count_file(src, repo, evidence):
    items = status_items(src.read_text(encoding="utf-8"))
    kinds = [(classify(i, src, repo, evidence), i) for i in items]
    n = lambda k: sum(1 for kind, _ in kinds if kind == k)
    return {
        "shown": n("shown"), "partly": n("partly"), "total": len(items),
        "ticked_only": n("ticked"),
        "marked_without_evidence": [re.sub(r"\s+", " ", i)[:100] for k, i in kinds if k == "marked-no-evidence"],
    }


# ---- the page --------------------------------------------------------------

def page_cards(section_html):
    """Yield (start, end, title, [hrefs]) for each card in the CARDS section text."""
    starts = [m.start() for m in CARD_RE.finditer(section_html)]
    for n, s in enumerate(starts):
        e = starts[n + 1] if n + 1 < len(starts) else len(section_html)
        region = section_html[s:e]
        m = H3_LINE_RE.search(region)
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))) if m else "(untitled)"
        hrefs = []
        for tag in SRC_RE.findall(region):
            h = HREF_RE.search(tag)
            if h and html.unescape(h.group(1)) not in hrefs:
                hrefs.append(html.unescape(h.group(1)))
        yield s, e, title, hrefs


def split_section(page):
    if page.count(START) != 1 or page.count(END) != 1 or page.index(START) > page.index(END):
        raise SystemExit("page needs exactly one %s ... %s pair" % (START, END))
    a = page.index(START) + len(START)
    return a, page.index(END)


def measure(page_text, page_path, repo, evidence):
    """Numbers for every status file linked from a card, and for each card."""
    a, b = split_section(page_text)
    files, cards = {}, []
    repo_n = os.path.normpath(repo)
    for _, _, title, hrefs in page_cards(page_text[a:b]):
        have = []
        for href in hrefs:
            target = unquote(href.split("#")[0].split("?")[0])
            if not target or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
                continue
            p = Path(os.path.normpath(Path(page_path).parent / target))
            try:
                rel = p.relative_to(repo_n).as_posix()
            except ValueError:
                continue
            if rel not in files and p.is_file():
                files[rel] = count_file(p, Path(repo), evidence)
            if rel in files and rel not in have:
                have.append(rel)
        if not have:
            print("note: card %r has no readable status-src link; no bar" % title, file=sys.stderr)
            cards.append({"title": title, "sources": [], "shown": None, "partly": None, "total": None})
            continue
        cards.append({"title": title, "sources": have,
                      "shown": sum(files[f]["shown"] for f in have),
                      "partly": sum(files[f]["partly"] for f in have),
                      "total": sum(files[f]["total"] for f in have)})
    measure_text = ("shown = checklist lines marked with a check mark that cite a file under %s committed in git; "
                    "partly = lines that say 'partly shown'; total = all checklist lines" % ", ".join(evidence.roots))
    return {"measure": measure_text, "evidence_check": evidence.mode, "cards": cards, "files": files}


def pct(x, total):
    return ("%.2f" % (100.0 * x / total)).rstrip("0").rstrip(".") if total else "0"


def prog_html(card, index, indent):
    s, p, t = card["shown"], card["partly"], card["total"]
    files = " and ".join(card["sources"])
    if t:
        used = pct(s + p, t)
        segs = ""
        if s:
            segs += '<i class="s" style="flex:%d"></i>' % s
        if p:
            segs += '<i class="p" style="flex:%d"></i>' % p
        fill = '<span class="prog-fill" style="--w:%s%%">%s</span>' % (used, segs)
        label = ('<b class="n-s">%d</b> of %d checks shown, <b class="n-p">%d</b> partly' % (s, t, p))
        tip = ("From %s, %d checklist lines. Shown: %d marked ✓ with a cited file in git. "
               "Partly: %d say &quot;partly shown&quot;. The empty part is the rest, including lines ticked without a ✓."
               % (html.escape(files), t, s, p))
    else:
        fill, label, tip = "", "no status checklist found", "No checklist lines found in %s." % html.escape(files)
    return ('%s<div class="prog" style="--i:%d"><span class="prog-bar" aria-hidden="true">%s</span>'
            '<span class="prog-label">%s</span>'
            '<a class="prog-how" href="#prog-how" data-tip="%s">how measured</a></div>\n'
            % (indent, index, fill, label, tip))


def note_html(roots):
    where = ", ".join("<code>%s/</code>" % html.escape(r) for r in roots)
    return ('<p class="muted prog-note" id="prog-how">How the bars are measured: from each card\'s status checklist '
            '(the <code>## Status</code> list in the linked file). The whole bar is every checklist line. '
            'Solid progress is the lines marked ✓ that cite a file under %s that is in git. '
            'Striped progress is the lines that say &quot;partly shown&quot;. The empty part is everything else, '
            'including lines ticked <code>[x]</code> without a ✓. '
            'Made by <code>buildlog_progress.py --write</code>.</p>\n' % where)


def apply_bars(page, result, roots):
    """Return the page with bars regenerated inside the CARDS section only."""
    a, b = split_section(page)
    section = page[a:b]
    out, pos = [], 0
    cards = list(result["cards"])
    for n, (s, e, title, _) in enumerate(page_cards(section)):
        region = PROG_LINE_RE.sub("", section[s:e])
        card = cards[n]
        m = H3_LINE_RE.search(region)
        if card["total"] is not None and m:
            at = m.end()
            t = TAGS_LINE_RE.match(region, at + 1) if region[at:at + 1] == "\n" else None
            if t:
                at = t.end()
            line = prog_html(card, n, m.group(1))
            region = region[:at] + "\n" + line.rstrip("\n") + region[at:]
        out.append(section[pos:s])
        out.append(region)
        pos = e
    out.append(section[pos:])
    section = "".join(out)
    # the footnote goes last in the section, just before the END marker
    section = NOTE_LINE_RE.sub("", section).rstrip("\n") + "\n\n" + note_html(roots)
    return page[:a] + section + page[b:]


def write_atomic(path, text):
    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        for i in range(40):                   # Windows: a reader may briefly hold the target open
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if os.name != "nt" or i == 39:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_page(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def lock_dir(path):
    """The same directory lock as buildlog_tasks.py (Locked). Returns a function that releases it."""
    if fcntl:
        fd = os.open(str(path), os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
    else:                                     # Windows: a lock file named after the folder, in the temp folder
        key = hashlib.sha256(os.path.normcase(os.path.abspath(str(path))).encode("utf-8")).hexdigest()[:20]
        fd = os.open(os.path.join(tempfile.gettempdir(), "buildlog-%s.lock" % key), os.O_RDWR | os.O_CREAT, 0o600)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                break
            except OSError:
                continue

    def release():
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)
    return release


def write_page(page_path, repo, evidence):
    """Read, transform and replace the page in one quick step; retry if someone else wrote meanwhile."""
    release = lock_dir(page_path.parent)     # same lock as buildlog_tasks.py
    try:
        for _ in range(5):
            before = page_path.stat()
            text = read_page(page_path)
            result = measure(text, page_path, repo, evidence)
            new = apply_bars(text, result, evidence.roots)
            after = page_path.stat()
            if (after.st_mtime_ns, after.st_size, after.st_ino) != (before.st_mtime_ns, before.st_size, before.st_ino):
                continue                                  # changed while we worked: start again
            if new == text:
                return result, False
            if read_page(page_path) != text:              # re-read right before writing
                continue
            write_atomic(page_path, new)
            return result, True
        raise SystemExit("%s kept changing; nothing written" % page_path)
    finally:
        release()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--settings", help="settings file (default: buildlog.json next to this script)")
    ap.add_argument("--repo", type=Path, help="project root (default: from the settings)")
    ap.add_argument("--page", type=Path, help="page to read/write (default: from the settings)")
    ap.add_argument("--evidence", action="append", help="evidence folder, relative to the root (repeatable)")
    ap.add_argument("--write", action="store_true", help="rewrite the bars in the CARDS section of the page")
    ap.add_argument("--no-git", action="store_true", help="check evidence files on disk instead of in git HEAD")
    args = ap.parse_args(argv)
    if os.name == "nt":                       # Windows pipes default to the ANSI code page
        for f in (sys.stdout, sys.stderr):
            if f is not None and not f.isatty():
                f.reconfigure(encoding="utf-8", errors="backslashreplace")
    s = load_settings(args.settings)
    repo = (args.repo or Path(s["root"])).resolve()
    page = (args.page or repo / s["page"]).resolve()
    evidence = Evidence(repo, roots=args.evidence or s["evidence_dirs"], use_git=not args.no_git)
    if args.write:
        result, changed = write_page(page, repo, evidence)
        print("%s %s" % ("wrote" if changed else "unchanged:", page), file=sys.stderr)
    else:
        result = measure(read_page(page), page, repo, evidence)
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
