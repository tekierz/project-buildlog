#!/usr/bin/env python3
"""Set up (and maintain) the build log + "Your tasks" + webhook feedback system in a project.

  setup.py init --project DIR [options]   copy the scripts, write the page, seed tasks, make the
                                          private key, install the user service, print the T3 steps
  setup.py set-url --project DIR          store the T3 webhook URL (read from stdin, never argv)
  setup.py t3-args --project DIR          rewrite the private schedule_task argument files
  setup.py rotate-secret --project DIR    new HMAC key + the update_scheduled_task arguments
  setup.py delivery --project DIR local|webhook
  setup.py change-port --project DIR --port N
  setup.py upgrade-page --project DIR     bring the page's TASKS section and panel CSS up to the
                                          skill's template, keeping every other section
  setup.py status --project DIR
  setup.py remove --project DIR [--purge] [--files]

Every step prints "ok" or "FAIL" with the reason. Nothing here prints the
webhook secret or URL: they live only in ~/.config/<name>/ (mode 600).
Re-running init is safe: scripts and the unit are refreshed, while the page,
the task file and the secret are kept. Python standard library only.
"""
import argparse
import datetime
import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TEMPLATES = SKILL / "templates"
COPY_SCRIPTS = ("buildlog_server.py", "buildlog_tasks.py", "buildlog_progress.py", "buildlog_agent.py")
COPY_TESTS = ("test_buildlog_server.py", "test_buildlog_progress.py", "test_buildlog_agent.py")
SECTIONS = ("current", "goals", "cards", "modules", "timeline")
EVENT = "buildlog_message"
GUARD_START = "# project-buildlog: private and local-only files (never commit these)"
GUARD_END = "# end project-buildlog"
PLACEHOLDER_RE = re.compile(r"@@[A-Z_]+@@")
FAILED = []


# ---- output -----------------------------------------------------------------

def ok(msg):
    print("  ok    %s" % msg)


def warn(msg):
    print("  note  %s" % msg)


def fail(msg, fatal=True):
    FAILED.append(msg)
    print("  FAIL  %s" % msg)
    if fatal:
        raise SystemExit(1)


def step(title):
    print("\n== %s" % title)


# ---- helpers ----------------------------------------------------------------

def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "project"


def write_atomic(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def render(text, values, what):
    for k, v in values.items():
        text = text.replace("@@%s@@" % k, str(v))
    left = sorted(set(PLACEHOLDER_RE.findall(text)))
    if left:
        fail("%s: unfilled placeholders %s" % (what, ", ".join(left)))
    return text


def keep_sections(text, wanted):
    for name in SECTIONS:
        pat = re.compile(r"@@BEGIN:%s@@\n(.*?)@@END:%s@@\n" % (name, name), re.S)
        if not pat.search(text):
            fail("template has no %s block" % name)
        text = pat.sub(lambda m: m.group(1) if name in wanted else "", text)
    return text


def run(cmd, cwd=None, check=False, env=None, timeout=120):
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          check=check, env=env, timeout=timeout)


def git(project, *args):
    return run(["git", "-C", str(project)] + list(args))


def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def port_owner(port):
    r = run(["ss", "-ltnpH", "sport = :%d" % port])
    return r.stdout.strip() if r.returncode == 0 else ""


def pick_port(name):
    base = 33650 + int(hashlib.sha256(name.encode()).hexdigest(), 16) % 300
    for i in range(300):
        p = 33650 + (base - 33650 + i) % 300
        if port_free(p):
            return p
    raise SystemExit("no free port in 33650-33949; pass --port")


def http_get(url, timeout=3):
    req = urllib.request.Request(url, headers={"User-Agent": "buildlog-setup/1"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return r.status, dict(r.headers), r.read()


def wait_for_server(port, name, seconds=10):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            status, headers, body = http_get("http://127.0.0.1:%d/api/version" % port)
            data = json.loads(body)
            if status == 200 and data.get("name") == name:
                return True, headers.get("Server", "")
            last = "answers, but as %r (another server?)" % data.get("name")
        except (OSError, ValueError, urllib.error.URLError) as e:
            last = type(e).__name__
        time.sleep(0.3)
    return False, last


def systemctl(*args):
    return run(["systemctl", "--user"] + list(args))


def py_path():
    return "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


def unit_quote(p):
    p = str(p).replace("%", "%%")
    return '"%s"' % p if " " in p else p


# ---- project context ----------------------------------------------------------

class Ctx:
    """Everything derived from the project and its buildlog.json."""

    def __init__(self, project, settings, homes):
        self.root = Path(project).resolve()
        self.s = settings
        self.name = settings["name"]
        self.tools_rel = settings["tools_dir"]
        self.tools = self.root / self.tools_rel
        self.page_rel = settings["page"]
        self.tasks_rel = settings["tasks"]
        self.page = self.root / self.page_rel
        self.tasks = self.root / self.tasks_rel
        self.port = int(settings["port"])
        self.service = settings["service"]
        self.config_home = Path(homes.get("config_home") or Path.home() / ".config")
        self.state_home = Path(homes.get("state_home") or Path.home() / ".local/state")
        self.systemd_dir = Path(homes.get("systemd_dir") or self.config_home / "systemd/user")
        self.config_dir = self.config_home / self.name
        self.config = self.config_dir / "webhook.json"
        self.state = self.state_home / self.name
        self.unit = self.systemd_dir / ("%s.service" % self.service)
        self.unit_in_repo = self.tools / ("%s.service" % self.service)
        self.live_url = "http://127.0.0.1:%d/%s" % (self.port, self.page_rel)

    @property
    def cli(self):
        return self.tools / "buildlog_tasks.py"

    @property
    def progress(self):
        return self.tools / "buildlog_progress.py"

    @property
    def server(self):
        return self.tools / "buildlog_server.py"

    def values(self):
        s = self.s
        return {
            "PROJECT": html.escape(s["title"]), "NAME": self.name, "SLUG": s["slug"], "PORT": self.port,
            "LIVE_URL": self.live_url, "SERVICE": self.service, "ROOT": str(self.root),
            "TASKS_FILE": self.tasks_rel, "TASKS_CLI": "%s/buildlog_tasks.py" % self.tools_rel,
            "TASKS_CLI_REL": "%s/buildlog_tasks.py" % self.tools_rel, "TASKS_CLI_ABS": str(self.cli),
            "SERVER": "%s/buildlog_server.py" % self.tools_rel, "SERVER_REL": "%s/buildlog_server.py" % self.tools_rel,
            "SERVER_ABS": str(self.server), "PROGRESS": "%s/buildlog_progress.py" % self.tools_rel,
            "PROGRESS_ABS": str(self.progress), "PAGE_ABS": str(self.page), "PAGE_REL": self.page_rel,
            "AGENT_CLI_ABS": str(self.tools / "buildlog_agent.py"),
            "AGENT_CLI_REL": "%s/buildlog_agent.py" % self.tools_rel,
            "STATE_DIR": "~/.local/state/%s/" % self.name, "MEDIA_DIR": s["media_dir"].rstrip("/") + "/",
            "EVIDENCE_DIR": s["evidence_dirs"][0], "NS": self.name,
        }


def settings_path(project, tools_dir):
    return Path(project).resolve() / tools_dir / "buildlog.json"


def find_settings(project):
    """Locate buildlog.json in a set-up project (default tools dir first, then a search)."""
    root = Path(project).resolve()
    p = root / "tools/buildlog/buildlog.json"
    if p.is_file():
        return p
    for cand in sorted(root.glob("**/buildlog.json")):
        if ".git" in cand.parts or "node_modules" in cand.parts:
            continue
        try:
            data = json.loads(cand.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("generator") == "project-buildlog":
            return cand
    raise SystemExit("no buildlog.json found under %s (run setup.py init first)" % root)


def load_ctx(args):
    p = find_settings(args.project)
    s = json.loads(p.read_text())
    homes = s.pop("_homes", {}) or {}
    for k in ("config_home", "state_home", "systemd_dir"):
        if getattr(args, k, None):
            homes[k] = getattr(args, k)
    return Ctx(args.project, s, homes), p


def read_config(ctx):
    try:
        data = json.loads(ctx.config.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_config(ctx, data):
    ctx.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ctx.config_dir, 0o700)
    write_atomic(ctx.config, json.dumps(data, indent=1) + "\n", mode=0o600)


# ---- T3 arguments ---------------------------------------------------------------

def webhook_schedule(ctx, secret):
    return {"type": "webhook",
            "signature": {"header": ctx.s["signature_header"], "encoding": "hex", "prefix": "sha256=",
                          "secret": secret},
            "dedupe": {"replayKey": "{{body.id}}"},
            "filters": [{"path": "body.event", "op": "equals", "value": EVENT}]}


def t3_arguments(ctx):
    """The webhook and heartbeat schedule_task records, built in memory."""
    cfg = read_config(ctx)
    secret = cfg.get("secret")
    if not secret:
        fail("no secret in %s" % ctx.config)
    vals = ctx.values()
    vals["PROJECT"] = ctx.s["title"]
    prompt = render((TEMPLATES / "t3-webhook-prompt.md").read_text(), vals, "webhook prompt")
    hb = render((TEMPLATES / "t3-heartbeat-prompt.md").read_text(), vals, "heartbeat prompt")
    webhook = {"tool": "schedule_task",
               "note": "Private key-bearing storage. Do not load into model context; use t3-args --redacted and request_secret.",
               "arguments": {"title": "%s: build log messages" % ctx.s["title"], "prompt": prompt,
                             "schedule": webhook_schedule(ctx, secret), "bindToCurrentThread": True}}
    heartbeat = {"tool": "schedule_task",
                 "arguments": {"title": "%s: hourly heartbeat" % ctx.s["title"], "prompt": hb,
                               "schedule": {"type": "interval", "everyMs": 3600000}, "bindToCurrentThread": True}}
    return webhook, heartbeat


def write_t3_args(ctx, quiet=False, webhook_too=True):
    """Write the private schedule_task argument files. Returns their paths."""
    webhook, heartbeat = t3_arguments(ctx)
    a = ctx.config_dir / "t3-webhook-task.json"
    b = ctx.config_dir / "t3-heartbeat-task.json"
    if webhook_too:
        write_atomic(a, json.dumps(webhook, indent=1, ensure_ascii=False) + "\n", mode=0o600)
    write_atomic(b, json.dumps(heartbeat, indent=1, ensure_ascii=False) + "\n", mode=0o600)
    if not quiet:
        if webhook_too:
            ok("schedule_task arguments: %s (private, has the secret)" % a)
        else:
            ok("webhook URL already stored: no new schedule_task file (t3-args writes one if needed)")
        ok("heartbeat arguments: %s" % b)
    return a, b


# ---- init -------------------------------------------------------------------------

def seed_tasks(ctx, how):
    if how == "none":
        return []
    if how != "default":
        data = json.loads(Path(how).read_text())
        return data["tasks"] if isinstance(data, dict) else data
    return [{"id": "try-the-panel", "kind": "action",
             "title": "Try the panel: tick this, write a note and press Send",
             "details": ("This checks the whole path from this page to the orchestrator. Tick Done, write any short "
                         "note (for example \"hello\") and press Send. The status should say Sent, and the "
                         "orchestrator should answer in its thread within a minute."),
             "how": "xdg-open %s" % ctx.live_url}]


def cmd_init(args):
    project = Path(args.project).resolve()
    step("1. Check the project and choose names")
    if not project.is_dir():
        fail("project folder %s does not exist" % project)
    in_git = git(project, "rev-parse", "--show-toplevel").returncode == 0
    if in_git:
        ok("git repository: %s" % git(project, "rev-parse", "--show-toplevel").stdout.strip())
    else:
        warn("%s is not a git repository: --commit and evidence checks need git" % project)
        if args.commit:
            fail("--commit needs a git repository")
    if sys.version_info < (3, 10):
        fail("Python 3.10 or newer is needed")
    old = {}
    requested_delivery = args.delivery
    sp = settings_path(project, args.tools_dir or "tools/buildlog")
    if args.tools_dir is None and not sp.is_file():
        try:
            sp = find_settings(project)
        except SystemExit:
            pass
    if sp.is_file():
        old = json.loads(sp.read_text())
        ok("existing setup found (%s): refreshing it" % sp.relative_to(project))
    # Omitted options keep installed values. An upgrade must not switch the page,
    # task file, private credential identity or custom tools directory to defaults.
    defaults = {"tools_dir": "tools/buildlog", "page": "docs/buildlog.html", "tasks": None,
                "media_dir": "docs/buildlog-media", "rules_doc": ["AGENTS.md", "CLAUDE.md"]}
    for key, default in defaults.items():
        if getattr(args, key) is None:
            setattr(args, key, old.get("rules_docs" if key == "rules_doc" else key, default))
    args.sections = args.sections if args.sections is not None else ",".join(old.get("sections", SECTIONS))
    args.evidence_dir = args.evidence_dir or old.get("evidence_dirs", ["docs/evidence"])[0]
    args.delivery = args.delivery or "webhook"
    title = args.title or old.get("title") or project.name
    slug = slugify(args.slug or old.get("slug") or project.name)
    name = args.name or old.get("name") or "%s-buildlog" % slug
    if not re.match(r"^[a-z0-9][a-z0-9_.-]{0,62}$", name):
        fail("--name must be lowercase letters, digits, dot, dash or underscore")
    service = args.service or old.get("service") or name
    port = args.port or old.get("port") or pick_port(name)
    homes = {key: getattr(args, key) or (old.get("_homes") or {}).get(key)
             for key in ("config_home", "state_home", "systemd_dir")}
    sections = [x.strip() for x in args.sections.split(",") if x.strip()]
    bad = [x for x in sections if x not in SECTIONS]
    if bad:
        fail("unknown sections: %s (choose from %s)" % (", ".join(bad), ", ".join(SECTIONS)))
    settings = {
        "generator": "project-buildlog", "name": name, "title": title, "slug": slug,
        "root": os.path.relpath(project, project / args.tools_dir), "port": int(port),
        "page": args.page, "tasks": args.tasks or str(Path(args.page).with_name("buildlog-tasks.json")),
        "tools_dir": args.tools_dir, "service": service,
        "signature_header": args.header or (old or {}).get("signature_header") or "x-%s-signature" % slug,
        "evidence_dirs": [args.evidence_dir], "media_dir": args.media_dir, "sections": sections,
        "agent_state": old.get("agent_state", str(Path(args.page).with_name("buildlog-state.json"))),
        "rules_docs": args.rules_doc,
    }
    if any(homes.values()):
        settings["_homes"] = {k: v for k, v in homes.items() if v}
    ctx = Ctx(project, settings, homes)
    ok("name %s, service %s, port %d, header %s" % (name, service, ctx.port, settings["signature_header"]))
    ours = False
    if not port_free(ctx.port):
        found, _ = wait_for_server(ctx.port, name, seconds=1)
        if found:
            ours = True
            ok("port %d is held by this build log's own server (will restart it)" % ctx.port)
        else:
            fail("port %d is in use by something else: %s\n        Find it with: ss -ltnp 'sport = :%d'. "
                 "An old ad-hoc server (python3 -m http.server) is the usual cause: stop it, or pick another --port."
                 % (ctx.port, port_owner(ctx.port) or "(unknown process)", ctx.port))
    else:
        ok("port %d is free" % ctx.port)

    step("2. Copy the scripts and tests into %s" % args.tools_dir)
    (ctx.tools / "tests").mkdir(parents=True, exist_ok=True)
    for f in COPY_SCRIPTS:
        shutil.copy2(SCRIPTS / f, ctx.tools / f)
        os.chmod(ctx.tools / f, 0o755)
    for f in COPY_TESTS:
        shutil.copy2(SCRIPTS / "tests" / f, ctx.tools / "tests" / f)
    stored = {k: v for k, v in settings.items()}
    write_atomic(sp, json.dumps(stored, indent=2) + "\n")
    ok("copied %s and tests/; wrote %s" % (", ".join(COPY_SCRIPTS), sp.relative_to(project)))

    step("3. Write the page")
    if ctx.page.exists() and not args.force:
        text = ctx.page.read_text(encoding="utf-8")
        missing = [m for m in ("<!-- TASKS:START -->", "<!-- TASKS:END -->", "<!-- TASKS-SNAPSHOT:START -->",
                               "<!-- TASKS-SNAPSHOT:END -->") if m not in text]
        if missing:
            fail("%s exists but has no Your tasks panel (missing %s). Move it aside or pass --force to replace it."
                 % (args.page, ", ".join(missing)))
        ok("kept the existing page %s (pass --force to replace it)" % args.page)
    else:
        now = datetime.datetime.now().astimezone()
        head = git(project, "log", "-1", "--format=%h").stdout.strip() if in_git else ""
        status_src = Path(args.status_src)
        status_abs = status_src if status_src.is_absolute() else project / status_src
        if not status_abs.is_file():
            warn("%s does not exist yet: the example card links it; add a '## Status' checklist there" % args.status_src)
        vals = ctx.values()
        vals.update({
            "UPDATED": now.strftime("%Y-%m-%d %H:%M %Z") + (" · commit <code>%s</code>" % head if head else ""),
            "TODAY_SHORT": now.strftime("%m-%d %H:%M"),
            "INTRO": html.escape(args.intro),
            "STATUS_SRC": html.escape(os.path.relpath(status_abs, ctx.page.parent)),
            "CARDS_HEADING": html.escape(args.cards_heading), "MODULES_HEADING": html.escape(args.modules_heading),
        })
        text = keep_sections((TEMPLATES / "buildlog.html").read_text(encoding="utf-8"), sections)
        text = render(text, vals, "page")
        check_page(text, sections)
        write_atomic(ctx.page, text)
        ok("wrote %s with sections: updated, tasks, %s" % (args.page, ", ".join(sections)))

    step("4. Seed the task list")
    if ctx.tasks.exists():
        ok("kept the existing task file %s" % ctx.tasks_rel)
    else:
        for t in seed_tasks(ctx, args.seed):
            cmd = [sys.executable, "-I", str(ctx.cli), "add", "--kind", t["kind"], "--title", t["title"],
                   "--details", t.get("details", ""), "--how", t.get("how", "")]
            if t.get("id"):
                cmd += ["--id", t["id"]]
            r = run(cmd)
            if r.returncode != 0:
                fail("task CLI failed: %s" % r.stderr.strip())
        if not ctx.tasks.exists():
            write_atomic(ctx.tasks, '{\n  "tasks": []\n}\n')
        ok("task file %s" % ctx.tasks_rel)
    r = run([sys.executable, "-I", str(ctx.cli), "sync"])
    if r.returncode != 0:
        fail("snapshot sync failed: %s" % r.stderr.strip())
    r = run([sys.executable, "-I", str(ctx.cli), "list", "--json"])
    ok("page snapshot in sync; %d open task(s)" % len(json.loads(r.stdout or "[]")))
    if "cards" in sections:
        r = run([sys.executable, "-I", str(ctx.progress), "--write"] + ([] if in_git else ["--no-git"]))
        if r.returncode != 0:
            fail("progress script failed: %s" % r.stderr.strip())
        ok("card progress bars written")

    step("5. Guard private files in .gitignore")
    guard_gitignore(ctx)

    step("6. Make the private key and config (outside git)")
    cfg = read_config(ctx)
    if cfg.get("secret"):
        ok("kept the existing secret in %s" % ctx.config)
    else:
        cfg = {"url": "", "secret": secrets.token_hex(32), "header": settings["signature_header"], "prefix": "sha256=",
               "delivery": args.delivery,
               "note": "HMAC-SHA256 hex of the raw JSON body; the key is this secret text. Never commit this file."}
        write_config(ctx, cfg)
        ok("new 32-byte secret (64 hex characters) in %s" % ctx.config)
    if requested_delivery is not None and cfg.get("delivery") != requested_delivery:
        cfg["delivery"] = requested_delivery
        write_config(ctx, cfg)
    mode = ctx.config.stat().st_mode & 0o777
    if mode != 0o600:
        fail("%s has mode %o, expected 600" % (ctx.config, mode))
    ok("mode 600, folder mode %o, delivery %s" % (ctx.config_dir.stat().st_mode & 0o777, cfg.get("delivery", "webhook")))
    inside = run(["git", "-C", str(ctx.config_dir), "rev-parse", "--show-toplevel"])
    if inside.returncode == 0:
        warn("%s is inside a git work tree (%s): make sure webhook.json is ignored there"
             % (ctx.config_dir, inside.stdout.strip()))
    else:
        ok("the config folder is not inside any git work tree")
    write_t3_args(ctx, webhook_too=not cfg.get("url"))

    step("7. Install and start the user service")
    unit = render((TEMPLATES / "buildlog.service").read_text(), dict(ctx.values(), **{
        "PROJECT": title, "UNIT_IN_REPO": "%s/%s.service" % (args.tools_dir, service),
        "DOC_URL": "file://" + urllib.parse.quote(str(ctx.page)), "PYTHON": py_path(),
        "ROOT": str(ctx.root).replace("%", "%%"), "SERVER_ABS": unit_quote(ctx.server),
        "CONFIG_ABS": unit_quote(ctx.config), "STATE_ABS": unit_quote(ctx.state)}), "unit")
    write_atomic(ctx.unit_in_repo, unit)
    ok("unit file %s" % ctx.unit_in_repo.relative_to(project))
    if args.no_service:
        warn("--no-service: not installed. Start it by hand with: python3 -I %s" % shlex.quote(str(ctx.server)))
    else:
        install_service(ctx, unit, restart=ours)

    step("8. Write shared agent rules and compact entry points")
    rules_rel = write_rules(ctx)
    rules_docs = settings["rules_docs"]

    if args.commit:
        step("9. Commit the new files (private index)")
        paths = [args.tools_dir, args.page, settings["tasks"], rules_rel, ".gitignore"] + rules_docs
        r = run([sys.executable, "-I", str(ctx.cli), "commit", "-m", "buildlog: build log and Your tasks panel"] + paths)
        if r.returncode != 0:
            fail("commit failed: %s" % r.stderr.strip(), fatal=False)
        else:
            ok(r.stdout.strip() or "nothing new to commit")

    print_next_steps(ctx, cfg)
    return 1 if FAILED else 0


def check_page(text, sections):
    head = text[:text.index("<style>")]
    if head.count("<!--") != 1 or head.count("-->") != 1:
        fail("editing-rules comment is not a single comment")
    body = head[head.index("<!--") + 4:head.index("-->")]
    if "--" in body:
        fail("editing-rules comment contains a double hyphen")
    wanted = ["UPDATED", "TASKS", "TASKS-SNAPSHOT"] + [s.upper() for s in sections]
    for m in wanted:
        for edge in ("START", "END"):
            marker = "<!-- %s:%s -->" % (m, edge)
            if text.count(marker) != 1:
                fail("page has %d copies of %s" % (text.count(marker), marker))
    if 'http-equiv="refresh"' in text:
        fail("page has a meta refresh tag")


def guard_gitignore(ctx):
    gi = ctx.root / ".gitignore"
    text = gi.read_text() if gi.exists() else ""
    lines = [GUARD_START, "webhook.json", "*.webhook.json", ctx.s["media_dir"].rstrip("/") + "/",
             "%s/**/__pycache__/" % ctx.tools_rel, GUARD_END]
    block = "\n".join(lines) + "\n"
    if GUARD_START in text:
        text = re.sub(re.escape(GUARD_START) + r".*?" + re.escape(GUARD_END) + r"\n?", lambda m: block, text, flags=re.S)
    else:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    write_atomic(gi, text)
    if git(ctx.root, "rev-parse").returncode != 0:
        ok(".gitignore written (no git here to check it)")
        return
    probe = [ctx.root / "webhook.json", ctx.root / "docs" / "webhook.json", ctx.root / ctx.s["media_dir"] / "x.png"]
    bad = [p for p in probe if git(ctx.root, "check-ignore", "-q", "--no-index", str(p)).returncode != 0]
    if bad:
        fail(".gitignore guard does not cover %s" % ", ".join(str(p.relative_to(ctx.root)) for p in bad), fatal=False)
    else:
        ok(".gitignore ignores webhook.json anywhere in the repo and %s/" % ctx.s["media_dir"].rstrip("/"))


def install_service(ctx, unit, restart=False):
    if not shutil.which("systemctl"):
        fail("systemctl not found: start the server by hand (python3 -I %s)" % ctx.server, fatal=False)
        return
    ctx.systemd_dir.mkdir(parents=True, exist_ok=True)
    write_atomic(ctx.unit, unit)
    r = systemctl("daemon-reload")
    if r.returncode != 0:
        fail("systemctl --user daemon-reload: %s" % r.stderr.strip())
    for verb in ("enable", "restart"):            # restart also picks up refreshed scripts on a re-run
        r = systemctl(verb, "%s.service" % ctx.service)
        if r.returncode != 0:
            fail("systemctl --user %s: %s" % (verb, r.stderr.strip()))
    good, why = wait_for_server(ctx.port, ctx.name)
    if not good:
        log = run(["journalctl", "--user", "-u", "%s.service" % ctx.service, "-n", "15", "--no-pager"]).stdout
        fail("the service does not answer on port %d (%s). Last log lines:\n%s\n        Port owner: %s"
             % (ctx.port, why, log, port_owner(ctx.port) or "none"))
    pid = systemctl("show", "-p", "MainPID", "--value", "%s.service" % ctx.service).stdout.strip()
    owner = port_owner(ctx.port)
    if pid and pid != "0" and ("pid=%s," % pid) not in owner:
        fail("port %d is answered by another process (%s), not the service (pid %s)" % (ctx.port, owner, pid))
    ok("%s.service active (pid %s), serving %s" % (ctx.service, pid, ctx.live_url))
    linger = run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"]).stdout.strip()
    if linger != "yes":
        warn("user lingering is off: the service stops when you log out. Turn it on with: loginctl enable-linger")


RULES_START, RULES_END = "<!-- project-buildlog:START -->", "<!-- project-buildlog:END -->"
LEGACY_RULES = '## Build log and "Your tasks"'


def shared_rules_rel(ctx):
    return str(Path(ctx.page_rel).with_name("buildlog-agent.md"))


def write_rules(ctx):
    """Write the shared rules beside the page and a small managed block in each entry point."""
    rel = shared_rules_rel(ctx)
    rules = render((TEMPLATES / "agent-rules.md").read_text(), dict(ctx.values(), PROJECT=ctx.s["title"]), "rules")
    write_atomic(ctx.root / rel, rules)
    snippet = ('%s\nBuild log: %s. Read `%s` when handling buildlog work.\n'
               'Start with `python3 -I "%s" brief`; details only as needed.\n%s\n' %
               (RULES_START, ctx.live_url, rel, ctx.tools / "buildlog_agent.py", RULES_END))
    for rules_doc in ctx.s.get("rules_docs", []):
        doc = ctx.root / rules_doc
        prev = doc.read_text() if doc.exists() else ""
        if RULES_START in prev:
            if prev.count(RULES_START) != 1 or prev.count(RULES_END) != 1:
                fail("%s has malformed buildlog markers" % rules_doc)
            updated = re.sub(re.escape(RULES_START) + r".*?" + re.escape(RULES_END) + r"\n?",
                             lambda match: snippet, prev, flags=re.S)
        elif LEGACY_RULES in prev:
            warn("%s has the older full rules; kept them. Replace that section with a pointer to %s when convenient"
                 % (rules_doc, rel))
            continue
        else:
            updated = (prev.rstrip("\n") + "\n\n" if prev.strip() else "") + snippet
        if updated != prev:
            write_atomic(doc, updated)
        ok("compact buildlog instructions in %s" % rules_doc)
    return rel


def print_next_steps(ctx, cfg):
    if cfg.get("delivery") == "local":
        print("\nLocal inbox ready. Read: python3 -I %s inbox --limit 5\nPage: %s" % (shlex.quote(str(ctx.cli)), ctx.live_url))
        return
    a = ctx.config_dir / "t3-webhook-task.json"
    b = ctx.config_dir / "t3-heartbeat-task.json"
    if cfg.get("url"):
        print("\n== Already wired to a webhook (host %s). Check it with:\n     python3 -I %s --project %s --real"
              % (urllib.parse.urlsplit(cfg["url"]).hostname, shlex.quote(str(SCRIPTS / "verify.py")),
                 shlex.quote(str(ctx.root))))
        return
    print("""
== Remaining steps (see references/setup.md)

A. In the orchestrator thread (the thread that should receive messages), create the
   webhook task with schedule_task. Obtain safe arguments with:
     python3 -I %(setup)s t3-args --project %(root)s --redacted
   Call request_secret for private key entry, then add its one-use secretRef to signature.
   The user copies the key on their own machine, never into chat:
     python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["secret"], end="")' %(config)s | wl-copy
   (xclip -selection clipboard or pbcopy instead of wl-copy elsewhere.)
   Do not read the private key-bearing argument file into model context. The schedule is:
     type webhook, signature {header %(header)s, encoding hex, prefix sha256=, secretRef},
     dedupe {replayKey {{body.id}}}, filters [body.event equals %(event)s]

B. Store the returned webhookUrl (stdin, so it stays out of argv and shell history):
     printf '%%s' 'WEBHOOK_URL' | python3 -I %(setup)s set-url --project %(root)s
   No webhookUrl in the result means T3 Connect remote access is off: ask the user to turn it on.

C. Check the whole path:
     python3 -I %(verify)s --project %(root)s            (tests, fake webhook, service, CSRF)
     python3 -I %(verify)s --project %(root)s --real     (signed test to T3 + forged ones)
   Then confirm the test run with its nonce arrived in the orchestrator thread.

D. Only if the user requests an hourly heartbeat: schedule_task with the arguments in
     %(b)s

Page: %(url)s   Service: %(service)s   Delivery: %(delivery)s""" % {
        "a": a, "b": b, "header": ctx.s["signature_header"], "event": EVENT, "setup": shlex.quote(str(SCRIPTS / "setup.py")),
        "config": shlex.quote(str(ctx.config)),
        "verify": shlex.quote(str(SCRIPTS / "verify.py")), "root": shlex.quote(str(ctx.root)), "url": ctx.live_url, "service": ctx.service,
        "delivery": cfg.get("delivery", "webhook")})


# ---- maintenance ----------------------------------------------------------------------

def cmd_set_url(args):
    ctx, _ = load_ctx(args)
    step("Store the webhook URL")
    url = (sys.stdin.read() if not args.url_file else Path(args.url_file).read_text()).strip()
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        parts = None
    local = parts and parts.hostname in ("127.0.0.1", "localhost")
    if not parts or not parts.netloc or not (parts.scheme == "https" or (parts.scheme == "http" and local)):
        fail("expected an https URL on stdin (got %d characters)" % len(url))
    cfg = read_config(ctx)
    if not cfg.get("secret"):
        fail("no secret yet: run init first")
    cfg["url"] = url
    cfg["delivery"] = "webhook"
    write_config(ctx, cfg)
    ok("stored in %s (host %s, mode 600)" % (ctx.config, parts.hostname))
    args_file = ctx.config_dir / "t3-webhook-task.json"
    if args_file.exists() and not args.keep_args:
        args_file.unlink()
        ok("removed %s (its secret is no longer needed there; t3-args rewrites it)" % args_file.name)
    print("Next: python3 -I %s --project %s --real" % (shlex.quote(str(SCRIPTS / "verify.py")), shlex.quote(str(ctx.root))))
    return 0


def cmd_t3_args(args):
    ctx, _ = load_ctx(args)
    if args.redacted:
        # Built in memory: printing safe arguments must not recreate the key-bearing file set-url removed.
        data = t3_arguments(ctx)[0]["arguments"]
        del data["schedule"]["signature"]["secret"]
        print(json.dumps(data, separators=(",", ":")))
        return 0
    step("Write the schedule_task arguments")
    write_t3_args(ctx)
    return 0


def cmd_rotate(args):
    ctx, _ = load_ctx(args)
    step("Rotate the HMAC secret")
    cfg = read_config(ctx)
    cfg["secret"] = secrets.token_hex(32)
    write_config(ctx, cfg)
    ok("new secret in %s" % ctx.config)
    update = {"tool": "update_scheduled_task",
              "note": "Private key-bearing storage. Use t3-args --redacted and request_secret to update the retained scheduler ID.",
              "arguments": {"scheduledTaskId": "FROM list_scheduled_tasks",
                            "schedule": webhook_schedule(ctx, cfg["secret"])}}
    path = ctx.config_dir / "t3-rotate-secret.json"
    write_atomic(path, json.dumps(update, indent=1) + "\n", mode=0o600)
    ok("update_scheduled_task arguments: %s (private fallback; do not read it into context)" % path)
    print("""
Now, in the orchestrator thread:
  1. Use the retained scheduler ID (or list_scheduled_tasks: "%(title)s: build log messages").
  2. Call request_secret. The user copies the new key on their own machine, never into chat:
       python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["secret"], end="")' %(config)s | wl-copy
  3. Get the schedule from: python3 -I %(setup)s t3-args --project %(root)s --redacted
     Add the returned secretRef to schedule.signature and call update_scheduled_task with that schedule.
Messages sent in between are refused and stay queued; the server retries them every 3 minutes.
Then run verify.py --real and delete %(path)s.""" % {
        "title": ctx.s["title"], "config": shlex.quote(str(ctx.config)), "root": shlex.quote(str(ctx.root)),
        "setup": shlex.quote(str(SCRIPTS / "setup.py")), "path": shlex.quote(str(path))})
    return 0


def cmd_delivery(args):
    ctx, _ = load_ctx(args)
    step("Switch delivery to %s" % args.mode)
    cfg = read_config(ctx)
    if not cfg.get("secret"):
        fail("no config yet: run init first")
    cfg["delivery"] = args.mode
    write_config(ctx, cfg)
    ok("delivery is now %s (the server reads this on every send; no restart needed)" % args.mode)
    if args.mode == "webhook" and not cfg.get("url"):
        warn("no webhook URL stored yet: messages will queue until set-url")
    return 0


def cmd_change_port(args):
    ctx, sp = load_ctx(args)
    step("Change the port from %d to %d" % (ctx.port, args.port))
    if not port_free(args.port):
        fail("port %d is in use: %s" % (args.port, port_owner(args.port)))
    old = ctx.port
    s = json.loads(sp.read_text())
    s["port"] = args.port
    write_atomic(sp, json.dumps(s, indent=2) + "\n")
    ok("buildlog.json port %d" % args.port)
    if ctx.page.exists():
        text = ctx.page.read_text(encoding="utf-8")
        new = text.replace("http://127.0.0.1:%d/" % old, "http://127.0.0.1:%d/" % args.port)
        write_atomic(ctx.page, new)
        ok("page URLs updated (%d replaced)" % text.count("http://127.0.0.1:%d/" % old))
    ctx2, _ = load_ctx(args)
    if ctx2.unit.exists():
        systemctl("restart", "%s.service" % ctx2.service)
        good, why = wait_for_server(args.port, ctx2.name)
        if not good:
            fail("service does not answer on %d (%s)" % (args.port, why))
        ok("service restarted on %d; port %d %s" % (args.port, old, "free" if port_free(old) else "STILL IN USE"))
    write_rules(ctx2)
    write_t3_args(ctx2, quiet=True, webhook_too=not read_config(ctx2).get("url"))
    print("Also: update_scheduled_task on both T3 tasks with the new prompts (t3-args --redacted; the URL in them\n"
          "changed), and grep other project docs for :%d." % old)
    return 0


CSS_START = "/* TASKS-CSS:START"
CSS_END = "/* TASKS-CSS:END */"
TASKS_RE = re.compile(r"<!-- TASKS:START -->.*?<!-- TASKS:END -->", re.S)


def css_block(text):
    a = text.find(CSS_START)
    b = text.find(CSS_END)
    if a < 0 or b < a:
        return None
    a = text.rfind("\n", 0, a) + 1
    return a, b + len(CSS_END)


def cmd_upgrade_page(args):
    ctx, _ = load_ctx(args)
    step("Upgrade the Your tasks panel in %s" % ctx.page_rel)
    if not ctx.page.exists():
        fail("no page at %s" % ctx.page)
    vals = ctx.values()
    vals.update({"UPDATED": "", "TODAY_SHORT": "", "INTRO": "", "STATUS_SRC": "", "CARDS_HEADING": "",
                 "MODULES_HEADING": ""})
    tpl = render(keep_sections((TEMPLATES / "buildlog.html").read_text(encoding="utf-8"), []), vals, "page")
    new_tasks = TASKS_RE.search(tpl).group(0)
    nb = css_block(tpl)
    new_css = tpl[nb[0]:nb[1]]
    lock = os.open(str(ctx.page.parent), os.O_RDONLY)          # the same lock as the task CLI
    try:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX)
        text = ctx.page.read_text(encoding="utf-8")
        if len(TASKS_RE.findall(text)) != 1:
            fail("the page needs exactly one TASKS START/END pair")
        snap = re.search(r"<!-- TASKS-SNAPSHOT:START -->.*?<!-- TASKS-SNAPSHOT:END -->", text, re.S)
        if snap:                                  # keep the page's own snapshot, so a re-run changes nothing
            new_tasks = re.sub(r"<!-- TASKS-SNAPSHOT:START -->.*?<!-- TASKS-SNAPSHOT:END -->",
                               lambda m: snap.group(0), new_tasks, count=1, flags=re.S)
        out = TASKS_RE.sub(lambda m: new_tasks, text, count=1)
        ob = css_block(out)
        if ob:
            out = out[:ob[0]] + new_css + out[ob[1]:]
            how = "replaced the marked panel CSS"
        else:
            at = out.index("</style>")
            out = out[:at] + new_css + "\n" + out[at:]
            how = "added the panel CSS before </style> (the page had no TASKS-CSS markers; old panel rules stay, the new ones win)"
        for pattern, insertion in (
            (r"/\* DISPLAY-CSS:START \*/.*?/\* DISPLAY-CSS:END \*/", "</style>"),
            (r"<!-- DISPLAY:START -->.*?<!-- DISPLAY:END -->", "<!-- TASKS:START -->"),
        ):
            block = re.search(pattern, tpl, re.S).group(0)
            if re.search(pattern, out, re.S):
                out = re.sub(pattern, lambda match: block, out, count=1, flags=re.S)
            else:
                out = out.replace(insertion, block + "\n" + insertion, 1)
        if out == text:
            ok("already up to date")
            return 0
        bdir = ctx.state / "page-backups"          # outside the repo, so the server never serves it
        bdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = bdir / ("%s.%s" % (ctx.page.name, time.strftime("%Y%m%d-%H%M%S")))
        shutil.copy2(ctx.page, backup)
        write_atomic(ctx.page, out, mode=ctx.page.stat().st_mode & 0o777)
    finally:
        os.close(lock)
    ok("replaced the TASKS section; %s" % how)
    r = run([sys.executable, "-I", str(ctx.cli), "sync"])
    if r.returncode != 0:
        fail("snapshot sync failed: %s" % r.stderr.strip())
    check_page(ctx.page.read_text(encoding="utf-8"), [])
    ok("snapshot refreshed and markers checked; the old page is in %s" % backup)
    return 0


def cmd_status(args):
    ctx, _ = load_ctx(args)
    step("Status of %s" % ctx.name)
    active = systemctl("is-active", "%s.service" % ctx.service).stdout.strip()
    pid = systemctl("show", "-p", "MainPID", "--value", "%s.service" % ctx.service).stdout.strip()
    print("  service   %s.service: %s (pid %s)" % (ctx.service, active or "unknown", pid or "-"))
    print("  port      %d: %s" % (ctx.port, port_owner(ctx.port) or "nothing listening"))
    good, why = wait_for_server(ctx.port, ctx.name, seconds=1)
    print("  answers   %s" % ("yes, as %s" % ctx.name if good else "no (%s)" % why))
    cfg = read_config(ctx)
    host = urllib.parse.urlsplit(cfg.get("url", "")).hostname if cfg.get("url") else None
    print("  config    %s: secret %s, url %s, delivery %s" % (
        ctx.config, "set (%d chars)" % len(cfg["secret"]) if cfg.get("secret") else "MISSING",
        "set (host %s)" % host if host else "not set", cfg.get("delivery", "webhook")))
    if good:
        try:
            _, _, body = http_get("http://127.0.0.1:%d/api/tasks" % ctx.port)
            d = json.loads(body)
            print("  tasks     %d open, %d message(s) waiting for delivery" % (
                len([t for t in d["tasks"] if not t["done"]]), d.get("pending", 0)))
        except (OSError, ValueError, KeyError, urllib.error.URLError):
            pass
    return 0


def cmd_remove(args):
    ctx, sp = load_ctx(args)
    step("Remove the build log system for %s" % ctx.name)
    if shutil.which("systemctl"):
        systemctl("disable", "--now", "%s.service" % ctx.service)
        if ctx.unit.exists():
            ctx.unit.unlink()
        systemctl("daemon-reload")
        systemctl("reset-failed", "%s.service" % ctx.service)
        ok("service %s stopped, disabled and removed" % ctx.service)
    if not port_free(ctx.port):
        warn("port %d is still in use: %s" % (ctx.port, port_owner(ctx.port)))
    if args.purge:
        for d in (ctx.config_dir, ctx.state):
            if d.exists():
                shutil.rmtree(d)
                ok("deleted %s" % d)
    else:
        warn("kept %s and %s (pass --purge to delete the key, inbox and state)" % (ctx.config_dir, ctx.state))
    if args.files:
        for rules_doc in ctx.s.get("rules_docs", []):
            doc = ctx.root / rules_doc
            if doc.is_file():
                text = doc.read_text()
                if text.count(RULES_START) == text.count(RULES_END) == 1:
                    text = re.sub(r"\n*" + re.escape(RULES_START) + r".*?" + re.escape(RULES_END) + r"\n?", "\n",
                                  text, flags=re.S).strip("\n")
                    if text:
                        write_atomic(doc, text + "\n")
                    else:
                        doc.unlink()                # init created it for the block alone
                    ok("removed the buildlog block from %s" % rules_doc)
        for p in (ctx.tools, ctx.page, ctx.tasks, ctx.root / shared_rules_rel(ctx),
                  ctx.root / ctx.s.get("agent_state", str(Path(ctx.page_rel).with_name("buildlog-state.json")))):
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        gi = ctx.root / ".gitignore"
        if gi.exists() and GUARD_START in gi.read_text():
            text = re.sub(r"\n*" + re.escape(GUARD_START) + r".*?" + re.escape(GUARD_END) + r"\n?", "\n", gi.read_text(), flags=re.S)
            write_atomic(gi, text.lstrip("\n"))
        ok("removed %s, %s, %s and the .gitignore block (commit the removal yourself)" % (ctx.tools_rel, ctx.page_rel, ctx.tasks_rel))
    print("\nIn T3: list_scheduled_tasks, then delete_scheduled_task for \"%s: build log messages\" and "
          "\"%s: hourly heartbeat\"." % (ctx.s["title"], ctx.s["title"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--project", default=".", help="project root (default: the current folder)")
        p.add_argument("--config-home", help=argparse.SUPPRESS)
        p.add_argument("--state-home", help=argparse.SUPPRESS)
        p.add_argument("--systemd-dir", help=argparse.SUPPRESS)
        return p

    p = common(sub.add_parser("init", help="set up the system in a project"))
    p.add_argument("--title", help="display name (default: the folder name)")
    p.add_argument("--slug", help="short id for headers and secret folders (default: from the folder name)")
    p.add_argument("--name", help="state/config/User-Agent name (default: <slug>-buildlog)")
    p.add_argument("--port", type=int, help="port on 127.0.0.1 (default: a free one in 33650-33949)")
    p.add_argument("--service", help="systemd user service name (default: the name)")
    p.add_argument("--page", help="default: existing page or docs/buildlog.html")
    p.add_argument("--tasks", help="task file (default: buildlog-tasks.json next to the page)")
    p.add_argument("--tools-dir", help="default: existing tools directory or tools/buildlog")
    p.add_argument("--sections", help="optional sections, comma separated: " + ", ".join(SECTIONS))
    p.add_argument("--cards-heading", default="Status")
    p.add_argument("--modules-heading", default="Modules")
    p.add_argument("--intro", default="What this project is, in one or two plain sentences.")
    p.add_argument("--status-src", default="README.md", help="status list the example card links (relative to the root)")
    p.add_argument("--header", help="signature header (default: x-<slug>-signature)")
    p.add_argument("--delivery", choices=("webhook", "local"), help="default: keep existing mode, or webhook")
    p.add_argument("--seed", default="default", help="default | none | a JSON file with a list of tasks")
    p.add_argument("--evidence-dir", help="default: existing directory or docs/evidence")
    p.add_argument("--media-dir", help="default: existing directory or docs/buildlog-media")
    p.add_argument("--rules-doc", action="append", help="instruction entry point; repeat for several (default: AGENTS.md and CLAUDE.md)")
    p.add_argument("--no-service", action="store_true", help="do not install the systemd user service")
    p.add_argument("--commit", action="store_true", help="commit the new files through a private index")
    p.add_argument("--force", action="store_true", help="replace an existing page")
    p = common(sub.add_parser("set-url", help="store the webhook URL, read from stdin"))
    p.add_argument("--url-file", help="read the URL from this file instead of stdin")
    p.add_argument("--keep-args", action="store_true", help="keep the schedule_task argument file")
    p = common(sub.add_parser("t3-args", help="rewrite the schedule_task argument files"))
    p.add_argument("--redacted", action="store_true", help="print only webhook arguments with the signing secret removed; add a T3 secretRef before use")
    common(sub.add_parser("rotate-secret", help="new HMAC secret"))
    p = common(sub.add_parser("delivery", help="switch between webhook and local-only delivery"))
    p.add_argument("mode", choices=("webhook", "local"))
    p = common(sub.add_parser("change-port", help="move the server to another port"))
    p.add_argument("--port", type=int, required=True)
    common(sub.add_parser("status", help="show the service, port and config state"))
    common(sub.add_parser("upgrade-page", help="refresh the TASKS section and panel CSS from the template"))
    p = common(sub.add_parser("remove", help="stop and remove the service"))
    p.add_argument("--purge", action="store_true", help="also delete ~/.config/<name> and ~/.local/state/<name>")
    p.add_argument("--files", action="store_true", help="also delete the project files (tools dir, page, task file)")
    args = ap.parse_args(argv)
    return {"init": cmd_init, "set-url": cmd_set_url, "t3-args": cmd_t3_args, "rotate-secret": cmd_rotate,
            "delivery": cmd_delivery, "change-port": cmd_change_port, "status": cmd_status,
            "upgrade-page": cmd_upgrade_page,
            "remove": cmd_remove}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
