"""Build a demo review hub (made-up project "Harbor Lights", an animated short) for screenshots.

  python -I build_demo.py SKILL_DIR DEMO_DIR PORT

Copies the skill's scripts into DEMO_DIR/tools/buildlog, makes media with ffmpeg, adds items in two rounds (the second
round is "new since your last visit"), starts the server on PORT and posts answers through it like the page does.
Writes DEMO_DIR/demo.json with the time between the rounds (for the localStorage baseline) and the server pid.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL, DEMO, PORT = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), int(sys.argv[3])
ROOT = DEMO / "harbor-lights"
TOOLS = ROOT / "tools" / "buildlog"
STATE = DEMO / "state"
PY = sys.executable


def sh(*args, cwd=ROOT, check=True):
    r = subprocess.run([str(a) for a in args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode:
        raise SystemExit("failed: %s\n%s%s" % (args, r.stdout, r.stderr))
    return r


def review(*args, check=True):
    r = sh(PY, "-I", TOOLS / "review" / "review.py", "--state-dir", STATE, *args, check=check)
    if r.stderr.strip():
        print("   ", r.stderr.strip().replace("\n", "\n    "))
    return r.stdout.strip()


def ffmpeg(*args):
    sh("ffmpeg", "-y", "-loglevel", "error", *args)


if DEMO.exists():
    import stat
    shutil.rmtree(DEMO, onerror=lambda f, path, _: (os.chmod(path, stat.S_IWRITE), f(path)))
(TOOLS / "review").mkdir(parents=True)
for f in ("buildlog_tasks.py", "buildlog_server.py"):
    shutil.copy2(SKILL / "scripts" / f, TOOLS / f)
for f in ("review.py", "md_render.py", "screenplay.py", "screenmark.py"):
    shutil.copy2(SKILL / "scripts" / "review" / f, TOOLS / "review" / f)
shutil.copy2(SKILL / "templates" / "review-hub.html", TOOLS / "review" / "hub_template.html")
(TOOLS / "buildlog.json").write_text(json.dumps({
    "name": "harbor-lights-demo", "title": "Harbor Lights", "root": "../..", "port": PORT, "page": "docs/buildlog.html",
    "tasks": "docs/buildlog-tasks.json", "review_hub": True,
    "review": {"brand": "Harbor Lights", "strict": False, "workstreams": [
        {"name": "Script", "tags": ["script"]}, {"name": "Look", "tags": ["look"]},
        {"name": "Sound", "tags": ["sound"]}, {"name": "Production", "tags": ["production"]}]}}, indent=2), encoding="utf-8")
for d in ("docs/film", "docs/look", "docs/sound", "media"):
    (ROOT / d).mkdir(parents=True, exist_ok=True)
(ROOT / "docs" / "buildlog-tasks.json").write_text('{"tasks": []}\n', encoding="utf-8")
(ROOT / "docs" / "buildlog.html").write_text("<!doctype html><title>Build log</title><p>Build log</p>\n", encoding="utf-8")
STATE.mkdir(parents=True)
(DEMO / "webhook.json").write_text(json.dumps({"delivery": "local"}), encoding="utf-8")

# ---- media -----------------------------------------------------------------------------------------------
M = ROOT / "media"
night = "gradients=s=1600x900:c0=0x0b1a33:c1=0x31507a:c2=0xf2a65a:nb_colors=3:x0=0:y0=0:x1=1600:y1=900:seed=4"
dawn = "gradients=s=1600x900:c0=0x26365c:c1=0xd9826b:c2=0xffd59e:nb_colors=3:x0=0:y0=0:x1=1600:y1=900:seed=4"
tower = ("drawbox=x=1040:y=260:w=110:h=520:color=0xe8e2d4:t=fill,drawbox=x=1040:y=380:w=110:h=40:color=0xb3403a:t=fill,"
         "drawbox=x=1040:y=560:w=110:h=40:color=0xb3403a:t=fill,drawbox=x=1015:y=200:w=160:h=70:color=0x1c2333:t=fill,"
         "drawbox=x=1050:y=212:w=90:h=46:color=0xffe28a:t=fill,drawbox=x=0:y=760:w=1600:h=140:color=0x0d2236:t=fill")
ffmpeg("-f", "lavfi", "-i", night, "-vf", tower, "-frames:v", "1", M / "style_frame_night_a.png")
ffmpeg("-f", "lavfi", "-i", night, "-vf", tower.replace("0xffe28a", "0xfff3c2") + ",eq=saturation=1.35:brightness=0.04", "-frames:v", "1", M / "style_frame_night_b.png")
ffmpeg("-f", "lavfi", "-i", dawn, "-vf", tower, "-frames:v", "1", M / "style_frame_dawn.png")
ffmpeg("-f", "lavfi", "-i", "gradients=s=1280x720:c0=0x0b1a33:c1=0x31507a:c2=0xf2a65a:nb_colors=3:speed=0.03:seed=9:d=8",
       "-f", "lavfi", "-i", "aevalsrc=0.25*sin(2*PI*(220+30*sin(2*PI*0.5*t))*t)*(0.6+0.4*sin(2*PI*0.25*t)):d=8",
       "-vf", tower.replace("1600", "1280").replace("x=1040", "x=840").replace("x=1015", "x=815").replace("x=1050", "x=850")
       + ",scale=1280:720", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", M / "animatic_sc2.mp4")
for name, expr in (("wick_take_a", "0.5*sin(2*PI*(170+60*sin(2*PI*4*t))*t)*(0.5+0.5*sin(2*PI*1.7*t))"),
                   ("wick_take_b", "0.5*sin(2*PI*(230+40*sin(2*PI*6*t))*t)*(0.45+0.55*abs(sin(2*PI*1.1*t)))"),
                   ("wick_take_c", "0.45*sin(2*PI*(140+25*sin(2*PI*3*t))*t)*(0.3+0.7*abs(sin(2*PI*0.8*t)))"),
                   ("foghorn_theme", "0.4*sin(2*PI*110*t)*(0.4+0.6*abs(sin(2*PI*0.2*t)))+0.2*sin(2*PI*165*t)")):
    ffmpeg("-f", "lavfi", "-i", "aevalsrc=%s:d=%d" % (expr, 9 if name == "foghorn_theme" else 4), M / (name + ".mp3"))

# ---- documents -------------------------------------------------------------------------------------------
SCRIPT = """---
title: Harbor Lights
credit: written by
author: The Story Team
---

<!-- @id: harbor_lights @set: lighthouse -->
<!-- Draft {v}. Seven minutes, two characters, one storm. -->

# Cold open

## EXT. LIGHTHOUSE GALLERY - NIGHT #1#

Wind. The lamp turns. MARA (60s, oilskin coat) wipes salt off the glass.

<!-- @cam: wide_slow_push @sfx: wind_gust -->
**MARA**
_(to herself)_
> Forty years and the glass still fogs on the inside.

**WICK**
_(cheerful, too loud)_
> I have polished it four hundred and twelve times!

**MARA**
> {line_mara}

# The storm

## INT. LAMP ROOM - NIGHT #2#

The lamp stutters. A single bulb flickers out.

<!-- @cam: close_bulb @sfx: glass_tick -->
**WICK**
_(quiet)_
> {line_wick}

**MARA**
_(not looking up)_
> Then we light it by hand. Like before you.

<!-- @cam: graphic @graphic: title_card @text: HARBOR LIGHTS @dur: 3 -->
Title card over the beam.

# Morning

## EXT. ROCKS BELOW THE LIGHTHOUSE - DAWN #3#

{ending}

**WICK**
> {last_line}
"""
v1 = dict(v="v1", line_mara="Four hundred and eleven. I counted.", line_wick="The bulb is tired. So am I.",
          ending="The sea is flat. A fishing boat rounds the point, safe.", last_line="Do we do it again tonight?")
v2 = dict(v1, v="v2", line_wick="The bulb is tired. I can carry the light up the stairs if you hold the door.")
v3 = dict(v2, v="v3", line_mara="Four hundred and eleven. The last one doesn't count, you sneezed on it.",
          line_wick="I can carry the light. You hold the door. That is the deal.",
          ending="Fog lifts off the water. A fishing boat rounds the point, its crew waving at the tower.",
          last_line="Same time tomorrow?")
for ver, d in (("v1", v1), ("v2", v2)):
    (ROOT / "docs" / "film" / ("harbor_lights_%s.screen.md" % ver)).write_text(SCRIPT.format(**d), encoding="utf-8")
BIBLE_V1 = """# Harbor Lights: story bible

## The world
A lighthouse on a cold northern coast, the last one still kept by hand. The town below has stopped looking up.

## Mara
Sixty-one, keeper for forty years. Dry, patient, allergic to fuss. Talks to the lamp, never to the town.

## Wick
A maintenance robot from the harbor authority, sent to automate the light. Loud, eager, counts everything.

## Tone
Quiet comedy. No villains: the weather is the only antagonist.
"""
BIBLE_V2 = BIBLE_V1.replace("Sent to automate the light.", "Sent to automate the light, and quietly hoping he never finishes.") \
    .replace("A maintenance robot from the harbor authority, sent to automate the light.",
             "A maintenance robot from the harbor authority, sent to automate the light, and quietly hoping it never finishes.") \
    + "\n## What Wick wants\nTo be needed. Every count he keeps is a way of saying he was there.\n"
(ROOT / "docs" / "look" / "bible.md").write_text(BIBLE_V1, encoding="utf-8")
(ROOT / "docs" / "sound" / "music_brief.md").write_text(
    "# Music brief\n\nOne theme, the foghorn's two notes, played by a small string group.\n\n- Cold open: horn only\n"
    "- Storm: strings join, no percussion\n- Morning: full theme, slower\n", encoding="utf-8")

sh("git", "init", "-q", "-b", "main")
sh("git", "-c", "user.name=Demo", "-c", "user.email=demo@example.com", "add", "-A")
sh("git", "-c", "user.name=Demo", "-c", "user.email=demo@example.com", "commit", "-qm", "Harbor Lights: first drafts")


def line_of(path, needle):
    for i, l in enumerate((ROOT / path).read_text(encoding="utf-8").splitlines(), 1):
        if needle in l:
            return i
    raise SystemExit("no %r in %s" % (needle, path))


# ---- round 1 -----------------------------------------------------------------------------------------------
S1, S2, S3 = "docs/film/harbor_lights_v1.screen.md", "docs/film/harbor_lights_v2.screen.md", "docs/film/harbor_lights_v3.screen.md"
review("add", "--id", "script-v1", "--kind", "approve", "--title", "Script: Harbor Lights", "--summary",
       "The first full draft, seven minutes.", "--doc", S1 + "::Harbor Lights script", "--tag", "script", "--version", "v1")
review("add", "--id", "script-v2", "--kind", "approve", "--title", "Script: Harbor Lights", "--summary",
       "Second draft: Wick offers to carry the light. Read it and comment on any line.",
       "--doc", S2 + "::Harbor Lights script", "--tag", "script", "--supersedes", "script-v1")
review("add", "--id", "foghorn-gag", "--kind", "choice", "--title", "Keep the foghorn gag in the cold open?",
       "--summary", "Wick sets off the foghorn while polishing. It is the only slapstick beat in the film.",
       "--choice", "keep::Keep it::the one big laugh before the storm", "--choice", "cut::Cut it::keeps the opening quiet",
       "--choice", "move::Move it to the morning::a release after the storm", "--default", "Keep it", "--parent", "script-v2", "--tag", "script")
review("add", "--id", "title-font", "--kind", "choice", "--title", "Title card lettering",
       "--summary", "The title card over the beam: which lettering?", "--choices", "Hand-painted|Stencil|Lighthouse-plaque serif",
       "--parent", "script-v2", "--tag", "script")
review("add", "--id", "bible-v1", "--kind", "approve", "--title", "Story bible", "--summary",
       "Who Mara and Wick are, the world and the tone. Everything else follows from this.",
       "--doc", "docs/look/bible.md::Story bible", "--tag", "look")
review("add", "--id", "style-frames", "--kind", "approve", "--title", "Style frames: the lighthouse at night",
       "--summary", "Two lamp treatments for the night scenes. Drag the divider to compare; B is warmer and brighter.",
       "--compare", "media/style_frame_night_a.png::media/style_frame_night_b.png::Lamp A (left) vs lamp B",
       "--media", "media/style_frame_dawn.png::Dawn frame for the last scene", "--tag", "look", "--priority", "high")
review("add", "--id", "wick-voice", "--kind", "audition", "--title", "Wick's voice", "--summary",
       "Three takes of Wick's first line. Play A against B at the same moment, then pick one.",
       "--candidate", "A::Bright::media/wick_take_a.mp3::eager, a little too loud::synth voice 2::current",
       "--candidate", "B::Warm::media/wick_take_b.mp3::softer, rounder::synth voice 5::current",
       "--candidate", "C::Low::media/wick_take_c.mp3::slow and heavy::synth voice 1::current", "--recommend", "B,A", "--tag", "sound")
review("add", "--id", "animatic-sc2", "--kind", "approve", "--title", "Animatic: the storm (scene 2)", "--summary",
       "Timing pass for scene 2 with the temp foghorn theme. Comment at any moment of the clip.",
       "--media", "media/animatic_sc2.mp4::Scene 2 animatic", "--media", "media/foghorn_theme.mp3::Temp theme",
       "--doc", "docs/sound/music_brief.md::Music brief", "--tag", "sound")
review("add", "--id", "render-budget", "--kind", "choice", "--title", "Render budget for the final pass", "--summary",
       "The farm needs an answer by tomorrow noon to hold the slot.", "--priority", "urgent",
       "--choice", "full::Full quality, 4 days::the slot we booked", "--choice", "half::Half resolution, 2 days::upscale after",
       "--default", "We lose the slot and wait two weeks", "--tag", "production")
review("add", "--id", "farm-move", "--kind", "info", "--title", "The render farm moves on Monday", "--summary",
       "Nothing to do: renders pause for two hours on Monday morning.", "--priority", "low", "--tag", "production")
review("add", "--id", "festival-form", "--kind", "action", "--title", "Sign the festival submission form", "--summary",
       "The festival needs the director's signature on the entry form.", "--how", "Open the form, sign page 2, send it back.",
       "--tag", "production")
review("snooze", "festival-form", "--for", "3d", "--reply", "The form opens on Thursday; back then.")
review("add", "--id", "logo-lockup", "--kind", "approve", "--title", "Logo lockup", "--summary", "The title logo for posters.",
       "--media", "media/style_frame_dawn.png::Poster draft", "--tag", "look")
review("close", "logo-lockup", "--reply", "Approved on the call; locked.")
review("add", "--id", "color-script", "--kind", "approve", "--title", "Color script", "--summary", "Night to dawn in five frames.",
       "--media", "media/style_frame_night_a.png::Night", "--tag", "look")
review("close", "color-script", "--reply", "Done; the frames went to the layout team.")

# the earlier rounds happened over the last two days
items_path = ROOT / "docs" / "review" / "items.json"
data = json.loads(items_path.read_text(encoding="utf-8"))
now = datetime.now(timezone.utc)
ages = {"script-v1": 50, "script-v2": 30, "foghorn-gag": 29, "title-font": 29, "bible-v1": 46, "style-frames": 20,
        "wick-voice": 8, "animatic-sc2": 6, "render-budget": 3, "farm-move": 26, "festival-form": 22, "logo-lockup": 70, "color-script": 60}
for it in data["items"]:
    t = (now - timedelta(hours=ages.get(it["id"], 1))).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    it["added"] = it["updated"] = t
    if it.get("superseded_at"):
        it["superseded_at"] = (now - timedelta(hours=30)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
items_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
review("sync")

# ---- the server, and the person's answers -----------------------------------------------------------------
log = open(DEMO / "server.log", "w")
srv = subprocess.Popen([PY, "-I", str(TOOLS / "buildlog_server.py"), "--state-dir", str(STATE), "--config", str(DEMO / "webhook.json")],
                       cwd=str(ROOT), stdout=log, stderr=log)
for _ in range(50):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/api/version" % PORT, timeout=1)
        break
    except OSError:
        time.sleep(0.2)


def api_items():
    return json.load(urllib.request.urlopen("http://127.0.0.1:%d/api/review/items" % PORT))["items"]


def answer(body):
    req = urllib.request.Request("http://127.0.0.1:%d/api/review/answer" % PORT, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Buildlog": "1"})
    return json.load(urllib.request.urlopen(req))["answer"]


def doc_hash(item_id, path):
    it = next(i for i in api_items() if i["id"] == item_id)
    return next(d["hash"] for d in it["docs"] if d["path"] == path)


def line_note(item_id, path, needle, text, who="", decision="comment"):
    n = line_of(path, needle)
    src = (ROOT / path).read_text(encoding="utf-8").splitlines()[n - 1].lstrip("> ")
    return answer({"item_id": item_id, "decision": decision, "comment": text, "seen": {"doc_hash": doc_hash(item_id, path)},
                   "anchor": {"type": "line", "line": n, "doc": path, "excerpt": (who + ": " if who else "") + src}})


a1 = line_note("script-v2", S2, "Four hundred and eleven", "Funny, but it needs a second beat. Let her top him.", "MARA")
a2 = line_note("script-v2", S2, "I can carry the light up", "Too long for a robot that just lost a bulb. Shorter, more stubborn.", "WICK")
a3 = line_note("script-v2", S2, "The sea is flat", "The ending is too calm. I want to see the boat make it.")
a4 = answer({"item_id": "script-v2", "decision": "needs_changes", "comment": "Close. Fix the ending and Wick's lamp-room line, then send v3.",
             "seen": {"rev": next(i for i in api_items() if i["id"] == "script-v2")["rev"]}})
answer({"item_id": "title-font", "decision": "choice", "choice": "hand-painted"})
answer({"item_id": "animatic-sc2", "decision": "comment", "comment": "Hold on the dark lamp a beat longer before the bulb dies.",
        "anchor": {"type": "time", "time_s": 2.4, "media": "media/animatic_sc2.mp4", "excerpt": "Scene 2 animatic @ 0:02.40"}})
answer({"item_id": "animatic-sc2", "decision": "comment", "comment": "The horn is great here.",
        "anchor": {"type": "time", "time_s": 5.1, "media": "media/foghorn_theme.mp3", "excerpt": "Temp theme @ 0:05.10"}})
answer({"item_id": "bible-v1", "decision": "comment", "comment": "Wick needs a want, not just a job.",
        "anchor": {"type": "line", "line": 7, "doc": "docs/look/bible.md", "excerpt": "A maintenance robot from the harbor authority"},
        "seen": {"doc_hash": doc_hash("bible-v1", "docs/look/bible.md")}})
answer({"item_id": "style-frames", "decision": "approve", "comment": "B. Warmer lamp, keep the dark sea."})
# the agent answers in a thread and marks one note handled
msgs = [json.loads(l)["payload"] for l in (STATE / "inbox.jsonl").read_text(encoding="utf-8").splitlines() if json.loads(l)["type"] == "message"]
review("reply", "script-v2", "Agreed: Mara tops him in v3.", "--to", a1["id"])
review("resolve", "script-v2", "--to", a1["id"])
review("reply", "animatic-sc2", "Held 12 frames longer in the next pass.", "--to", next(m["id"] for m in msgs if m.get("anchor") and m["anchor"].get("time_s") == 2.4))
round2 = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
time.sleep(1.2)

# ---- round 2: new since the person's last visit ------------------------------------------------------------
(ROOT / S3).write_text(SCRIPT.format(**v3), encoding="utf-8")
review("add", "--id", "script-v3", "--kind", "approve", "--title", "Script: Harbor Lights", "--priority", "high",
       "--summary", "Third draft: a stronger ending, Mara tops Wick in the cold open, Wick's lamp-room line is shorter.",
       "--body", "**What changed since v2**\n\n- Cold open: Mara gets the last word on the count\n- Lamp room: Wick's offer is one line\n"
       "- Morning: the boat makes it, the crew waves\n\nYour 3 notes on v2 are listed on this card.",
       "--doc", S3 + "::Harbor Lights script", "--tag", "script", "--supersedes", "script-v2")
review("add", "--id", "ending-choice", "--kind", "choice", "--title", "Last line: question or promise?", "--summary",
       "Wick's last line closes the film. v3 has the question; the promise is warmer.",
       "--choice", "question::Same time tomorrow?::light, open", "--choice", "promise::I'll be here tomorrow.::warmer, final",
       "--parent", "script-v3", "--tag", "script")
(ROOT / "docs" / "look" / "bible.md").write_text(BIBLE_V2, encoding="utf-8")
review("add", "--id", "bible-v2", "--kind", "approve", "--title", "Story bible", "--summary",
       "Wick now wants something: to be needed. Read the new section at the end.", "--doc", "docs/look/bible.md::Story bible",
       "--tag", "look", "--supersedes", "bible-v1")
b1 = line_note("script-v3", S3, "Same time tomorrow", "Love this. Can Wick wave back at the boat?", "WICK")
review("reply", "script-v3", "Yes: a small wave is in the layout notes for shot 3C.", "--to", b1["id"])
review("reply", "wick-voice", "Take B is the director's favourite so far; A is the brightest.")
# an unmerged branch with a newer music brief (for `review.py check`)
sh("git", "-c", "user.name=Demo", "-c", "user.email=demo@example.com", "add", "-A")
sh("git", "-c", "user.name=Demo", "-c", "user.email=demo@example.com", "commit", "-qm", "round 2")
sh("git", "checkout", "-qb", "music-rewrite")
(ROOT / "docs" / "sound" / "music_brief.md").write_text("# Music brief (rewrite)\n\nStrings from the first frame.\n", encoding="utf-8")
sh("git", "-c", "user.name=Demo", "-c", "user.email=demo@example.com", "commit", "-qam", "Music brief: strings from the first frame")
sh("git", "checkout", "-q", "main")

(DEMO / "demo.json").write_text(json.dumps({"since": round2, "pid": srv.pid, "port": PORT,
                                            "hub": "http://127.0.0.1:%d/docs/review/index.html" % PORT}), encoding="utf-8")
print(json.dumps({"since": round2, "pid": srv.pid}))
