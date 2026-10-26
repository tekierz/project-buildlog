## Build log and "Your tasks"

The build log is served at @@LIVE_URL@@ by the user service `@@SERVICE@@` (`@@SERVER_REL@@`). Its "Your tasks" panel lists what only the user can do or decide, and lets them tick items, add notes and message the orchestrator. Replies reach the orchestrator through a signed webhook. Its URL and key are in `~/.config/@@NAME@@/webhook.json` and never go into the repo or the chat.

Rules for agents:

- Tasks change only through the CLI, run by absolute path in the main checkout: `python3 -I @@TASKS_CLI_ABS@@ list|add|done|reopen ... --commit`. Never edit `@@TASKS_FILE@@` or the TASKS section of the page by hand.
- Every answer from the user closes its task with that CLI (`done ID --commit`). Check that an action really happened before closing it.
- Every answer to a panel message goes on the page with the reply CLI, not only in chat: `reply ID "text" --commit` under a task, `reply --general "text" --commit` for the conversation. The user reads the page.
- A new manual step for the user becomes a task, with copy-paste commands in `--how`. Decisions say what a tick means and what no tick means.
- Secrets go into files, never into chat: the task's command writes them to a private file (`umask 077`).
- Commit through a private index (`@@TASKS_CLI_REL@@ commit -m MSG PATH...` or `--commit`). Short messages, no attribution or Co-Authored-By trailers. Never push.
- The page is updated in one write, following the editing-rules comment at its top. Card bars come from `python3 -I @@PROGRESS_ABS@@ --write`.
- Show each round's results with screenshots.

How we mark status:

- Evidence lives in git: small text files in `@@EVIDENCE_DIR@@/<YYYY-MM-DD>/<topic>/` (logs under about 200 KB, summaries, test output, command lists). Cite that path. Media stays local.
- ✓ means shown on the main branch, and the evidence file is in git.
- "partly shown" means something was seen, but less than the sentence says (one scripted run, a shortcut, an older build, or evidence outside git).
- "not yet checked" means no run, or no record of one.
- Say how it was reached ("scripted", "headless", "one run"). A number needs a log line.
- Status checklists (`## Status` in the files the cards link): tick `[x]` only when the sentence as written was seen.
