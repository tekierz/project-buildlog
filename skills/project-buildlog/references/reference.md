# Reference

## Files in a set-up project

| Path | In git | Notes |
|---|---|---|
| `tools/buildlog/buildlog.json` | yes | `generator`, `name`, `title`, `slug`, `root` (relative to this file), `port`, `page`, `tasks`, `tools_dir`, `service`, `signature_header`, `evidence_dirs`, `media_dir`, `sections`. No secrets. |
| `tools/buildlog/buildlog_server.py`, `buildlog_tasks.py`, `buildlog_progress.py` | yes | Copies of `SKILL_DIR/scripts/`. They find `buildlog.json` next to themselves. Refresh them by re-running `setup.py init`. |
| `tools/buildlog/tests/test_buildlog_server.py`, `test_buildlog_progress.py` | yes | Run with `python3 -I`. They also check the project's own page and task file. |
| `tools/buildlog/<service>.service` | yes | A copy of the installed unit. It holds absolute paths for this machine. |
| `docs/buildlog.html` | yes | The page. |
| `docs/buildlog-tasks.json` | yes | Tasks and replies. Only the CLI writes it. |
| `docs/buildlog-media/` | no (ignored) | Screenshots. Reference them relatively from the page. |
| `docs/evidence/<date>/<topic>/` | yes | Small text evidence cited by status lines. |
| `~/.config/<name>/webhook.json` | never | `url`, `secret` (64 hex), `header`, `prefix`, `delivery` (`webhook` or `local`). Mode 600, folder 700. |
| `~/.config/<name>/t3-*.json` | never | schedule_task and update arguments. The webhook ones hold the secret; set-url deletes the creation file. |
| `~/.local/state/<name>/state.json` | never | Per task: done, done_updated, note (unsent draft), last_send. |
| `~/.local/state/<name>/inbox.jsonl` | never | Append-only: `{"type":"message","payload":...}` and `{"type":"delivered","id","at","http","via"}`. |
| `~/.local/state/<name>/read.json` | never | Ids handled from the local inbox (`inbox --mark-read`). |

## HTTP API (127.0.0.1 only)

Every request must carry `Host: 127.0.0.1:PORT` or `localhost:PORT`; anything else gets 403. Every POST also needs:
- `Content-Type: application/json`
- `X-Buildlog: 1`
- an `Origin` that is missing (curl) or one of `http://127.0.0.1:PORT` and `http://localhost:PORT`.

OPTIONS, PUT, DELETE and PATCH get 405 without CORS headers. Responses carry `Cache-Control: no-store`. Dotfiles and symlinks that lead outside the root are never served.

| Request | Answer |
|---|---|
| `GET /api/tasks` | `{"tasks": [task + done, note, updated, last_send, replies, last_note], "pending": N, "delivery": "webhook" or "local"}`. `replies` is `[{at, text}]`, oldest first, malformed ones skipped. `last_note` is `{id, at, text, status}`, the user's latest non-empty note for the task, or null. |
| `POST /api/tasks/<id>` `{"done": bool?, "note": str?}` | Saves the tick or the draft note. Returns `{"ok": true, "task": ...}`. |
| `POST /api/send` `{"kind", "task_id"?, "done"?, "note", "id"?}` | Logs the message, then delivers it. Returns `{"ok", "id", "delivered", "status", "http_status", "error", "pending", "sent_at"}`. Sending the same `id` again only reports; it never sends twice. |
| `GET /api/messages` | `{"messages": [{"type": "user" or "orchestrator", "at", "text", "id"?, "status"?}], "total": N}`. The general conversation: free messages (kind `message`, not tests or task notes) and `general_replies`, oldest first, the last 50. |
| `GET /api/version` | `{"mtime": page mtime, "tasks_mtime", "name"}`. The page polls it to reload itself. |
| `GET /` | 302 to the page, unless an index.html exists. |

Message `kind` values:
- `task_update` needs `task_id` and `done`.
- `message` needs a non-empty note.
- `test` is for checks.

## The signed webhook request

```
POST <webhook url>
Content-Type: application/json
User-Agent: <name>/1
<signature_header>: sha256=<hex HMAC-SHA256(key = secret text as UTF-8, message = exact body bytes)>

{"done":"true","event":"buildlog_message","id":"<32 hex>","kind":"task_update","note":"...","sent_at":"2026-10-25T19:14:38Z","task_id":"...","task_title":"..."}
```

- The body is compact JSON with sorted keys. A retry sends the same bytes.
- `done` is the text `"true"`, `"false"`, or `""` (not a task update).
- Redirects are not followed, and the timeout is 5 s.
- The T3 task matches on the `body.event` filter. Its replay key is `{{body.id}}`, kept for 7 days.

Delivery states (`status`):

| State | Meaning |
|---|---|
| `queued` | Not delivered yet. It is retried every 3 minutes and on every send, oldest first. |
| `sent` | The webhook answered 2xx. |
| `local` | Local delivery; it waits in the inbox. |
| `read` | An agent handled it from the inbox, so it is never forwarded. |

## Task file

```json
{
  "tasks": [
    {"id": "nightly-builds", "kind": "decision", "title": "...", "details": "...", "how": "commands",
     "added": "2026-10-25T19:00:00.000Z", "status": "open", "done_at": null, "updated": "2026-10-25T19:00:00.000Z",
     "replies": [{"at": "2026-10-25T19:20:00.123Z", "text": "..."}]}
  ],
  "general_replies": [{"at": "...", "text": "..."}]
}
```

- `status` is `open` or `done`. `updated` changes only with status changes (add, done, reopen).
- The page merges the user's tick with the file: the newer of the tick time and `updated` wins. So an agent's done or reopen overrides an older tick, and a newer tick overrides the file.
- Replies never touch `updated`.
- `replies` and `general_replies` are optional. A reply is at most 8000 characters.

Inside the page, the TASKS-SNAPSHOT block holds `{"tasks": [{id, kind, title, details, how, status, replies}], "general_replies": [...]}`. Every CLI change rewrites the block, and the page shows it when opened without the server. `<` is escaped as `\u003c`.

## Page sections and markers

| Marker pair | Content | Who edits it |
|---|---|---|
| `UPDATED` | "Updated DATE TIME TZ · commit HASH" | the build log helper |
| `TASKS` | The panel: HTML, snapshot, script | nobody by hand. Use `upgrade-page` to refresh it from the template. |
| `TASKS-SNAPSHOT` (inside TASKS) | Static copy of the tasks | the CLI |
| `CURRENT`, `GOALS`, `MODULES`, `TIMELINE` | Free content | the helper |
| `CARDS` | Status cards (`<div class="card">`, an `<h3>` line, a tags line, a `class="status-src"` link to a markdown file with a `## Status` checklist) | the helper. The `.prog` bar lines come from `buildlog_progress.py --write`. |
| CSS `/* TASKS-CSS:START ... */` to `/* TASKS-CSS:END */` | Panel CSS | nobody by hand |

## Progress bars

For each card, every `status-src` link is read. The `## Status` section's checklist lines (`- [ ]`, `- [x]`, `- [~]`) are counted:

| Count | Rule |
|---|---|
| shown | The line has a ✓ and cites a file under an evidence folder that is committed at HEAD. Links resolve relative to the status file; plain `docs/evidence/...` paths resolve from the root. |
| partly | The line says "partly shown", or is `[~]`. |
| total | All checklist lines. |

The bar's green part is `shown` and its striped amber part is `partly`. A `[x]` without a ✓ is only counted in the total.
