# Review hub screenshots

Made-up demo data: "Harbor Lights", an invented animated short. Nothing here comes from a real project.

Versions and ordering (1.3.0):

| File | What it shows |
|---|---|
| `v01-needs-you-grouped.png` | Default view: "new since your last visit" bar, "Needs you now", then one group per workstream |
| `v02-current-version-carried-notes.png` | The current script (v3): notes carried over from v2 with where each line is now, the script's decisions inside its card, then its own verdict, earlier versions folded |
| `v03-earlier-versions.png` | Earlier versions opened: superseded banners, read-only threads |
| `v04-old-version-page.png` | The old v2 script page: sticky "old version" banner, comments read-only |
| `v05-decisions-in-their-document.png` | Answered view: the script in short form, shown because of its answered decision |
| `v06-bible-notes-carried.png` | A document rewritten in place (story bible v2) with the note left on v1 |
| `v07-notes-follow-their-lines.png` | The v3 script page: v2's line comments re-attached to the matching v3 lines |
| `v08-new-version-while-writing.png` | v4 is published while a note is being typed on v3 |

The rest of the hub (1.2.0): `f01` kind filter, `f02` snooze menu, `f03` Snoozed view, `f04` clip and sound
players with timed comments, `f05` audition with A/B, `f06` before/after slider, `f07` image viewer, `f08`
script review page, `f09` Done view, `f10` phone width.

Regenerate: `python3 -I tools/build_demo.py SKILL_DIR DEMO_DIR PORT` builds the demo project, starts its server
and prints the time between the two rounds of items. Then, in a folder with `puppeteer-core` installed:
`CHROME=/path/to/chrome node tools/shoot.js http://127.0.0.1:PORT SINCE OUT_DIR v01,v02,v03,v04,v05,v06,v07,f`,
and last `DEMO_ROOT=DEMO_DIR/harbor-lights DEMO_STATE=DEMO_DIR/state node tools/shoot.js ... v08`, which
publishes a v4.
