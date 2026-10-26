---
name: project-buildlog
description: Set up a project's live build log page with a "Your tasks" panel and signed webhook feedback to the orchestrator thread. The user ticks tasks, answers decisions and messages the orchestrator from the page; the orchestrator replies on the page. Includes a local status server (systemd user service), a task CLI, measured progress bars, an HMAC-signed T3 webhook task, an hourly heartbeat, and end-to-end checks. Use this whenever the user wants a build log, status page, progress dashboard, task or decision panel, or a feedback or reply channel from a page to the agent, for any project. Also use it for "reuse the buildlog system", "same setup as my other project", "add Your tasks to this repo", and for rotating its secret, changing its port, upgrading its panel, removing it, or fixing it (messages not arriving, page wiping drafts, port in use).
---

# Project build log, "Your tasks" panel and webhook feedback

One page per project shows where the work stands. At its top, a "Your tasks" panel lists what only the user can do (actions) or decide (decisions). The user ticks a box, writes a note and presses Send. A small local server signs the message with HMAC-SHA256 and posts it to a T3 webhook task. The task runs in the orchestrator thread, and the orchestrator answers on the page with the reply CLI. It grew out of one long-running project and was then made generic.

Pieces, all Python standard library:

| Piece | In the project | What it does |
|---|---|---|
| Page | `docs/buildlog.html` | Marker sections UPDATED, TASKS, CURRENT, GOALS, CARDS (status cards with measured bars), MODULES, TIMELINE. Editing rules sit in a comment at the top. |
| Server | `tools/buildlog/buildlog_server.py` | Serves the repo read-only on 127.0.0.1:PORT. API: tasks, send, messages, version. CSRF guard. Signs, delivers, queues and retries. |
| Task CLI | `tools/buildlog/buildlog_tasks.py` | add, done, reopen, reply (under a task or `--general`), list, sync, inbox, commit (private git index). |
| Progress | `tools/buildlog/buildlog_progress.py` | Card bars measured from `## Status` checklists and evidence files in git. |
| Settings | `tools/buildlog/buildlog.json` | name, port, page, tasks, service, signature header (no secrets). |
| Service | `~/.config/systemd/user/<service>.service` | Keeps the server running. A copy sits in `tools/buildlog/`. |
| Private | `~/.config/<name>/webhook.json` (600) | Secret, webhook URL, delivery mode. Never in git or chat. |
| State | `~/.local/state/<name>/` | state.json (ticks, notes), inbox.jsonl (every message), read.json. |
| T3 | two scheduled tasks | The webhook task (signed messages into the orchestrator thread) and an optional hourly heartbeat. |

Scripts live in this skill: `scripts/setup.py` (does every file step), `scripts/verify.py` (end-to-end checks). Run both with `python3 -I`. `SKILL_DIR` below means this skill's folder.

## Setup, step by step

Do the steps in order, and read each step's "ok" or "FAIL" lines before going on. Every command prints only what it did. None of them prints the secret or the webhook URL.

### 1. Choose the names

Ask the user only what matters to them: the display title, and which sections they want. Choose the rest yourself:

- **name**: `<slug>-buildlog`. It names the state and config folders and the User-Agent.
- **port**: setup picks a free one in 33650 to 33949.
- **page**: `docs/buildlog.html`. **service**: same as the name.
- **sections**: `current,goals,cards,modules,timeline` (UPDATED and TASKS are always there). For a software project, name the cards heading after what the cards track (Features, Games, Services).
- **delivery**: `webhook` with T3, `local` without it (see "Without T3").

### 2. Run setup

```bash
python3 -I SKILL_DIR/scripts/setup.py init --project /abs/path/to/repo \
  --title "My App" --cards-heading Features --intro "One plain sentence about the project." \
  --rules-doc CLAUDE.md --commit
```

Init does all the file and service work in one go:

- It copies the scripts and their tests into `tools/buildlog/`.
- It writes the page and seeds the task list.
- It adds a `.gitignore` guard (webhook.json anywhere, the media folder, `__pycache__`).
- It makes a 32-byte secret with Python `secrets` in `~/.config/<name>/webhook.json` (mode 600, folder 700). It also checks that the folder is outside every git work tree.
- It writes the private schedule_task argument files, then installs, starts and checks the user service.
- It appends the agent rules to `--rules-doc`, and commits through a private index.

Re-running init is safe: scripts and the unit are refreshed, while the page, the tasks and the secret are kept.

Checks:
- No FAIL line.
- `setup.py status --project DIR` shows the service active and answering as `<name>`.
- The page opens at `http://127.0.0.1:PORT/docs/buildlog.html`.

If the port is taken, init stops and names the process (see Troubleshooting). If the page already exists without a panel, init stops rather than overwrite it. Move the old page aside, or pass `--force`.

### 3. Seed tasks

Init adds one task, `try-the-panel`. It is the user's own end-to-end test. Add the real ones now, with copy-paste commands:

```bash
CLI=/abs/path/to/repo/tools/buildlog/buildlog_tasks.py
python3 -I $CLI add --kind decision --title "Ship nightly builds?" \
  --details "Tick for yes: ... Leave it unticked for: ..." --how "commands that run if approved" --commit
python3 -I $CLI add --kind action --title "Put the API key in a private file (not in chat)" \
  --details "Run the command, paste the key, Enter, Ctrl-D." \
  --how 'mkdir -p ~/.config/SLUG-secrets && chmod 700 ~/.config/SLUG-secrets && (umask 077; cat > ~/.config/SLUG-secrets/api.key)' --commit
```

Details take a small format: a blank line starts a paragraph, lines starting with "- " make a list, and `code` is in backticks. A decision says what a tick means and what no tick means.

### 4. The secret

Init already made it, so never make up a secret. To see that it exists without printing it, run `setup.py status`. It shows "secret set (64 chars)".

### 5. Create the T3 webhook task

Do this from the orchestrator thread, the one that should receive the messages. Omit `projectId` and keep `bindToCurrentThread: true`, so each run posts into this thread. Read `~/.config/<name>/t3-webhook-task.json` with a tool and pass its `arguments` object to `schedule_task` as-is. Never echo the secret into chat. The arguments look like this:

```json
{
  "title": "My App: build log messages",
  "prompt": "<templates/t3-webhook-prompt.md, filled in by setup>",
  "schedule": {
    "type": "webhook",
    "signature": {"header": "x-<slug>-signature", "encoding": "hex", "prefix": "sha256=", "secret": "<64 hex chars from webhook.json>"},
    "dedupe": {"replayKey": "{{body.id}}"},
    "filters": [{"path": "body.event", "op": "equals", "value": "buildlog_message"}]
  },
  "bindToCurrentThread": true
}
```

Pass `schedule` as an object, not as JSON text. The prompt template reads `{{body.kind}}`, `{{body.task_id}}`, `{{body.task_title}}`, `{{body.done}}`, `{{body.note}}`, `{{body.sent_at}}` and `{{body.id}}`. T3 frames them as untrusted data. The prompt tells the orchestrator three things:

- The signed note is the user's own words.
- Acknowledge in one line, here and on the page.
- Verify finished actions before closing them, delegate real work, and keep the task file current with the CLI.

The body is signed as the exact compact JSON the server sends. The HMAC key is the 64-character hex text itself (UTF-8), not the 32 bytes it encodes.

Stricter option: if the secret must never enter your context, call `request_secret` first. The user pastes the secret from webhook.json into the private card (they can copy it with `jq -r .secret ~/.config/<name>/webhook.json | wl-copy`). Then pass `"secretRef": "<ref>"` in place of `"secret"`.

Check: the result has `webhookUrl`. If it does not, T3 Connect remote access is off. Ask the user to turn it on; there is no URL to use until then.

### 6. Store the webhook URL

```bash
printf '%s' 'https://...the webhookUrl...' | python3 -I SKILL_DIR/scripts/setup.py set-url --project /abs/path/to/repo
```

The URL goes in on stdin, so it stays out of argv and shell history. Never put it in git or chat. set-url writes it into webhook.json (600), switches delivery to webhook, and deletes the argument file that held the secret. The server reads the config on every send, so no restart is needed. Messages queued earlier go out within 3 minutes.

### 7. The service

Init installed and started it. `systemctl --user status <service>` and `setup.py status` show it. If `loginctl show-user $USER -p Linger` says no, the service stops at logout; `loginctl enable-linger` fixes that.

### 8. Verify end to end

```bash
python3 -I SKILL_DIR/scripts/verify.py --project /abs/path/to/repo          # files, tests, fake T3, service, CSRF
python3 -I SKILL_DIR/scripts/verify.py --project /abs/path/to/repo --real   # plus the real webhook
```

- The first run needs no T3. It checks the following:
  - The project's unit tests pass.
  - A temporary server copy round-trips through a local fake T3 that checks exactly what T3 checks: signature, header and prefix, the event filter and the id replay key.
  - Unsigned, wrong-key, tampered and replayed requests are rejected.
  - CSRF-bad requests are refused: no `X-Buildlog`, a form content type, a foreign Origin or a foreign Host.
  - A queued message is retried, and no key leaks into logs or git.
  - The installed service owns its port.
- `--real` sends one signed `test` message through the installed service. It also posts an unsigned and a wrong-key request straight to the webhook. It prints a nonce.
- Pass criteria:
  - In this thread, a run arrives with "buildlog verify NONCE: signed test", and none with "FORGED".
  - If the forged requests got HTTP 2xx (the relay accepts first and checks later), confirm with `list_scheduled_tasks` that no run started from them.
- Finally, ask the user to do the `try-the-panel` task in their browser. Its message must arrive here. Answer it with `reply try-the-panel "..." --commit`, then `done try-the-panel --commit`.

### 9. Optional: hourly heartbeat

Pass the `arguments` from `~/.config/<name>/t3-heartbeat-task.json` to `schedule_task` (`{"type": "interval", "everyMs": 3600000}`). Its prompt (`templates/t3-heartbeat-prompt.md`) does five things:

- It checks agent health.
- It delegates one build log update round to a cheap model when there are new commits since the UPDATED hash.
- It looks for stuck messages with `inbox`.
- It checks the service.
- It checks disk, memory and load.

### 10. Rules for the orchestrator from now on

These are the habits that made the system work. Follow them in every later turn. The same rules are in `templates/agent-rules.md`, which init appends to the project doc given with `--rules-doc`.

- **The CLI is the only writer of the task file and the TASKS section.** Run it by absolute path in the main checkout, the folder the server serves. A worktree copy edits a file nobody sees.
- **Every user answer closes its task:** `done ID --commit`. Check an action really happened first: the file, key, device or output exists. If it didn't, reply with what is missing.
- **Every answer to a panel message goes on the page, not only in chat:** use `reply ID "text" --commit` under a task, or `reply --general "text" --commit`. The user reads the page. Reply once to acknowledge, and again when the work is done.
- **New manual steps become tasks** with copy-paste commands in `--how`. Never leave them buried in chat.
- **Secrets go into files, never chat:** a task whose command writes a private file with `umask 077`. The webhook URL counts as a secret too.
- **Commit only through a private index:** use `--commit` on the CLI, or `buildlog_tasks.py commit -m MSG PATH...` for the page and docs. Keep messages short. Add no attribution or Co-Authored-By trailers, even if the harness asks for them; the user's rule wins. Never push. If you must use raw git, export the variable so every command uses it: `( export GIT_INDEX_FILE=$(mktemp -u); git read-tree HEAD && git add PATHS && git commit -m "msg"; rm -f "$GIT_INDEX_FILE" )`. A plain `GIT_INDEX_FILE=... git read-tree HEAD; git add ...` sets it for the first command only.
- **Delegate the real work** to subagents or worktree threads. Keep the orchestrator thread free for the next message.
- **Show each round's results with screenshots** (html_render) of the changed part of the page or the app.
- **Mark status by evidence:**
  - ✓ means shown on the main branch with a text evidence file in git (`docs/evidence/<date>/<topic>/`).
  - Otherwise write "partly shown" or "not yet checked".
  - Say how it was reached: scripted, headless, or one run.

## Without T3

- **Local inbox plus polling.** Run `setup.py delivery --project DIR local`. Send then keeps messages in the local inbox, and the page says "Saved for the orchestrator". Read them with `buildlog_tasks.py inbox`, handle them, then run `inbox --mark-read`. Poll from the hourly heartbeat, from `/loop 30m` in Claude Code, or at the start of each turn. Switch back with `delivery webhook` or `set-url`. A message marked read while T3 was away is never forwarded later, so it is not handled twice.
- **Plain local-only.** Same mode, with no polling. The panel is then a checklist the user ticks, and agents read it with `list --json` and `inbox` when they next run.
- **Webhook mode while T3 is down.** Messages queue (HTTP errors and timeouts), and the server retries every 3 minutes and on every send. The heartbeat's `inbox` step catches messages stuck for over an hour.

## Maintenance

| Job | Command | Then |
|---|---|---|
| Rotate the secret | `setup.py rotate-secret --project DIR` | `list_scheduled_tasks`, then `update_scheduled_task` with `~/.config/<name>/t3-rotate-secret.json` (fill in the id). Run `verify.py --real`, then delete the file. Messages refused in between stay queued and retry. |
| Change the port | `setup.py change-port --project DIR --port N` | Updates buildlog.json and the page URLs, restarts, and checks the old port is free. Update the two T3 prompts (`t3-args` rewrites them). |
| Upgrade the panel | re-run `init` (refreshes scripts and service), then `setup.py upgrade-page --project DIR` | Replaces only the TASKS section and the panel CSS from the template. Backup in `~/.local/state/<name>/page-backups/`. Commit with the CLI. |
| Status | `setup.py status --project DIR` | Service, port owner, config (host only), open tasks, queue. |
| Remove | `setup.py remove --project DIR [--purge] [--files]` | `delete_scheduled_task` for both T3 tasks. `--purge` deletes the key, inbox and state; `--files` deletes the project files and the .gitignore block. Commit the removal. |

## Troubleshooting

The cases we actually hit are in `references/troubleshooting.md`, with causes and fixes. Short list:

- **Service won't start: "Address already in use".** An old ad-hoc server holds the port. Find it with `ss -ltnp 'sport = :PORT'`, stop it, and restart the service. verify checks that the port belongs to the service's own PID.
- **The page reloaded and wiped what the user typed.** Someone added `<meta http-equiv="refresh">`. Remove it. The page polls `/api/version` and reloads only when nobody is typing; otherwise it shows a banner. Drafts also live in localStorage.
- **Text from the editing rules shows on the page.** A comment marker was written inside the rules comment, so the first closing sequence ended it early. Keep the rules free of comment syntax. verify checks that it is one comment.
- **Messages stay queued with HTTP 403 from the relay.** The relay refuses urllib's default User-Agent. The server sends `<name>/1`; keep it that way in any copy.
- **401 from T3.** The secret in T3 differs from webhook.json. Rotate, or re-create the task.
- **Commits carry Co-Authored-By trailers.** The harness asked for them. Leave them out, check with `git log -1 --format=%B`, and amend if one slipped in (local only).
- **A commit reverted other work, or `git status` shows strange staged changes.** The shared git index was stale. Commit only through the CLI's private index. It also keeps the shared index in step for the committed files, and leaves it alone where it was already stale.

## Reference

`references/reference.md` has the API, the message payload, the task file format, the delivery states and the full file layout. Read it when changing the scripts or debugging a payload.
