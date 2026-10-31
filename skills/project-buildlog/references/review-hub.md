# Review hub

An optional second page for everything the user has to look at, decide or do: approvals, choices, auditions (pick one of several takes), actions and FYIs, with the work itself inline. Clips, sounds, images, before/after compares, Markdown documents and ScreenMark screenplays render on the page; the user answers with one click, comments on a line of a document or a moment of a clip, and can snooze an item until later. Answers reach the agent through the same inbox and signed webhook as the build log page.

`REVIEW` below is `python3 -I /absolute/project/tools/buildlog/review/review.py` (`python` on Windows). Run it by absolute path in the served checkout.

## Install

```bash
python3 -I "$SKILL_DIR/scripts/setup.py" init --project /absolute/project --review-hub
```

On an existing setup the same command adds the hub; plain reruns keep it and refresh its scripts and page template. Init copies `scripts/review/` and `templates/review-hub.html` to `tools/buildlog/review/`, `test_review.py` to `tools/buildlog/tests/`, adds a Review hub section to the shared agent rules and writes an empty hub at `docs/review/index.html` (`http://127.0.0.1:PORT/docs/review/index.html`). Init restarts the installed service so it serves the review API; with `--no-service`, restart the server yourself.

Optional settings in `tools/buildlog/buildlog.json`:

```json
"review_items": "docs/review/items.json",
"review_page": "docs/review/index.html",
"review": {
  "brand": "My App",
  "pages": [{"src": "docs/CHANGELOG.md", "out": "changelog.html", "title": "Changelog", "nav": "Changelog"}],
  "screenplay": {"words_per_second": 2.5, "shot_tags": ["cam", "dur"], "caption_characters": {"ROBOT": "The robot beeps; the caption reads:"},
                 "character_colors": {"HOST": "#0f8a7a"}, "grammar": "path/to/another/screenmark.py"}
}
```

`pages` are Markdown files rendered next to the hub and linked from its header (`REVIEW sync`). The screenplay profile sets the runtime estimate, which note tags mark a picture-only shot, characters whose lines are on-screen captions, cast colours and, if a project keeps its own copy of the ScreenMark grammar, its path.

## Items

```bash
REVIEW add --id logo-v2 --kind approve --title "New logo" --summary "Two weights; the bold one reads at 16 px." \
  --priority high --tag brand --media docs/media/logo-v2.png::"16 px and 512 px" --compare docs/media/logo-v1.png::docs/media/logo-v2.png
REVIEW add --id ep1-script --kind approve --title "Episode 1 script" --summary "Read it; comment on any line." \
  --doc docs/scripts/ep1.screen.md::"The script" --doc docs/notes/ep1.md --default "It records on Friday as written."
REVIEW add --id voice --kind audition --title "Pick the narrator" --summary "Three takes of the same line." \
  --candidate A::Warm::docs/media/a.mp3 --candidate B::Dry::docs/media/b.mp3 --candidate C::Fast::docs/media/c.mp4 --recommend B
REVIEW add --json item.json        # the same fields as one JSON object; for long bodies and many options
REVIEW update ID [flags]           # a list flag replaces that list
```

- Kinds: `approve` (Approve / Needs changes / Reject), `choice` (`--choices "A|B"` or `--choice ID::label::detail`), `audition` (`--candidate ID::label::MEDIA[::detail[::model[::round]]]`, at least two; a `round` other than `current` files the candidate under an earlier round), `action` (Done / Can't yet; put the steps in `--how`), `info` (Got it).
- Importance (`--priority` or `--importance`): `urgent` blocks work now, `high` matters this session, `normal` is the default, `low` is whenever. The hub sorts and groups by it.
- `--tag NAME` (repeatable or comma-separated): workstreams or topics; the hub filters by them.
- Paths are repo paths. Media from outside the repo is copied to `docs/review/media/` (or use `REVIEW import-media PATH`); documents must be in the repo. `--body` is Markdown or `@file.md`; its links to `.md` files open as rendered pages.
- Media types: clips `.mp4 .webm .mov .m4v`, sounds `.mp3 .wav .m4a .aac .ogg .oga .opus .flac`, images `.png .jpg .jpeg .gif .webp .svg .avif`. Both sides of a compare must be the same type.
- Never put credentials in items. `--on-answer` is agent-only (never shown on the page).

## Answers, threads and snoozes

```bash
REVIEW inbox                       # unhandled answers, comments, snoozes and resolved threads (--json for structured)
REVIEW reply ID "Cut in v2."       # a note under the item
REVIEW reply ID "Tightened." --to MESSAGE_ID     # inside the comment thread that message started
REVIEW reply ID "Done." --line 42 --doc docs/scripts/ep1.screen.md     # or --at 12.5 --clip docs/media/reel.mp4
REVIEW resolve ID --to MESSAGE_ID [--reply TEXT] # mark a thread handled (--undo reopens)
REVIEW close ID --reply "Shipped in 1.4."         # the item moves to Done
REVIEW inbox --mark-read           # only after handling what was listed
```

A comment carries an anchor: `{type: "line", doc, line, excerpt}` or `{type: "time", media, time_s, excerpt}`. For a `.screen.md` script the line is the source line; for Markdown it is the block number (paragraph, list item, heading or table row) on the rendered page. A thread is every comment and reply with the same anchor (`line:DOC#LINE`, `time:MEDIA@SECONDS`). The script page lists threads in a side rail (open, resolved, all), shows replies under the line and marks a thread whose line changed since the comment.

Snooze: the user picks later today, tomorrow, next week, a date, or "someday" (no date). The item leaves the main list and waits under Snoozed; at its time it comes back marked "back from snooze". `REVIEW list` groups items by state (back from snooze first, then needs the user, answered, snoozed) and shows who snoozed what until when. Do not chase a snoozed item. `REVIEW snooze ID --for 3d|--until DATE|--later [--reply WHY]` parks one yourself; `REVIEW unsnooze ID` brings it back. Snoozes and resolves are recorded in the local inbox for agents and never sent to the webhook, so they do not wake a thread.

Every command that changes items takes `--commit` (only the review files, through the private index). `REVIEW render [ID...]` re-renders documents after their sources change; `REVIEW preview PATH` renders one document without an item.

## The page

- Header: a breadcrumb (hub > view and filters > current item > open document), views with counts (Needs you, Answered, Snoozed, Done, All), search (`/`), Kind, Importance and Tag filters with counts. Everything lives in the URL hash, so links, reloads and back/forward work: `#item=ID`, `#view=answered&kind=choice&prio=urgent,high&tag=brand&q=logo`, a document line `#item=ID&doc=PATH&line=42`, a clip moment `#item=ID&media=PATH&t=12.5`. Old `index.html#ID` links still open the item.
- Clips: play, frame back and forward (`,` `.`), speed, loop, mute, full screen, a scrub bar with comment markers, "comment at this moment" and the clip's timestamped comments listed under it (click to jump). Sounds: a waveform to scrub, speed and the same comments. Auditions: an A/B bar switches between two candidates at the same moment (`x`). Images open in a viewer with wheel zoom, drag, 1:1 and fit. Compares: a before/after slider (or side by side; pictures of different shapes start side by side), clips play in sync.
- Documents open over the hub with their own breadcrumb; Esc or Back returns to the item.
- Keys: `j` `k` items, `a` approve/done/got it, `n` needs changes, `1`-`9` pick, `c` comment, `o` open the first document, `s` snooze, `space` play, `?` help.

Opened as a file, the page is a read-only snapshot with a link to the live page.

## Server API

| Request | Answer |
|---|---|
| `GET /api/review/items[?id=ID]` | `{"items": [item + answers, answer, snooze, resolved], "pending", "delivery", "now"}`. `answer` is the latest decision (comments, snoozes and resolves are not decisions). `snooze` is the newer of the item's (agent) and the user's, or null. `resolved` maps thread keys to `{resolved, at, by}`. Agent-only fields are removed. |
| `POST /api/review/answer` `{"item_id", "decision", "choice"?, "comment"?, "anchor"?, "until"?, "later"?, "id"?}` | Decisions: `approve`, `needs_changes`, `reject`, `choice`, `pick`, `done`, `read`, `comment`, and the quiet `snooze` (`until` ISO time or `later: true`), `unsnooze`, `resolve` and `unresolve` (with the thread's `anchor`). Answers are logged and delivered like messages (kind `review`); quiet ones are kept in state and the local inbox only. Same `id` again only reports. |

Static files are served with `Accept-Ranges` and single byte ranges, so players can seek. Documents may be framed by the hub (`X-Frame-Options: SAMEORIGIN`).
