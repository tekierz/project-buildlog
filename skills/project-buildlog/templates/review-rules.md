
## Review hub

Hub: @@REVIEW_URL@@. Everything the user has to look at, decide or do goes there as an item; the user answers on the page. Run `@@PY@@ -I "@@REVIEW_CLI_ABS@@"` (REVIEW below) by absolute path in the served checkout.

- Add: `REVIEW add --id ID --kind approve|choice|audition|action|info --title T --summary "one plain line" [--priority urgent|high|normal|low] [--tag WORKSTREAM] --commit`. Attach work with `--doc PATH` (`.md` and `.screen.md` are rendered; the user comments on lines), `--media PATH[::caption]` (clips, sounds, images), `--compare BEFORE::AFTER`, `--choices "A|B"` or `--candidate ID::label::MEDIA` with `--recommend ID`. `--default` says what happens with no answer; `--on-answer` (agent-only) says what to run. Write Markdown and JSON, never HTML.
- Importance: urgent only when it blocks work now; high for this session; normal by default; low for whenever. Tag items by workstream so the user can filter.
- Answers: `REVIEW inbox` (or `--json`) lists unhandled answers, line and time comments, snoozes and resolved threads. Act, then `REVIEW reply ID TEXT` (`--line N [--doc PATH]`, `--at S --clip PATH` or `--to MESSAGE_ID` answers inside a comment thread), `REVIEW resolve ID --to MESSAGE_ID` when a thread is handled, `REVIEW close ID --reply TEXT` when the item is done, then `inbox --mark-read`. Each with `--commit`.
- Snoozes: the user can snooze an item until a date or for later. `REVIEW list` shows snoozed items and lists the ones that came due first; do not chase a snoozed item before then. `REVIEW snooze ID --for 3d|--until DATE|--later --reply WHY` parks an item yourself.
- Deep links: `index.html#item=ID`; a document line `#item=ID&doc=PATH&line=N`; a clip moment `#item=ID&media=PATH&t=SECONDS`.
