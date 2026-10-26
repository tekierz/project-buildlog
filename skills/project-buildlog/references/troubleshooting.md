# Troubleshooting

These are the problems we actually hit while building and running the system. Each one has its sign, its cause and the fix. `setup.py status` and `verify.py` find most of them.

## The service won't start: "Address already in use"

**Sign.** `systemctl --user status <service>` shows `OSError: [Errno 98] Address already in use`, and the page still loads, but `/api/tasks` returns 404.

**Cause.** An old ad-hoc server (`python3 -m http.server PORT`, started by hand before the service existed) still holds the port. It serves files but has no API.

**Fix.**
1. Find the process: `ss -ltnp 'sport = :PORT'`. Check it is the stray one (`ps -o pid,cmd -p PID`).
2. Stop it: `kill PID`.
3. Restart the service: `systemctl --user restart <service>`.

`setup.py init` refuses a port that something else holds and names the process. `verify.py` checks that the port belongs to the service's MainPID and that `/api/version` answers with the project's name.

## The page reloads and wipes what the user typed

**Sign.** A half-written note disappears every few minutes.

**Cause.** A `<meta http-equiv="refresh" content="300">` tag in the head. The first version of the page had one.

**Fix.** Remove the tag. The panel script already polls `/api/version` every 30 s. It reloads only when the page file changed and nobody is typing. Otherwise it shows "This page has a newer version" with a Reload button. Drafts are also saved in localStorage and on the server. The project tests and `verify.py` fail if a meta refresh comes back.

## Text from the editing rules shows on the page

**Sign.** Part of the "Editing rules" comment appears as plain text above the page.

**Cause.** The rules comment contained comment syntax, for example a marker written out as an example. The first closing sequence ends the whole comment, so the rest becomes page text.

**Fix.**
- Name markers in words inside the rules ("the TASKS START/END markers").
- Never write the opening or closing comment sequence there.
- Avoid double hyphens too (command flags): write "run with its write flag".

`setup.py` refuses to write a page whose head has more than one comment. The project tests and `verify.py` check the same thing.

## Messages stay queued with "HTTP 403" from the relay

**Sign.** The panel says "Queued: not delivered yet (HTTP 403)", and `journalctl --user -u <service>` shows `queued: HTTP 403`.

**Cause.** The T3 relay (behind a bot filter) refuses requests that carry urllib's default `User-Agent: Python-urllib/3.x`.

**Fix.** The server sends `User-Agent: <name>/1`. Keep that line in any copy or rewrite. `verify.py` checks the header the fake webhook received.

## 401 from T3

**Cause.** The secret stored in the T3 task differs from `webhook.json`. Common reasons:
- init made a new config after the task was created.
- Someone edited the file.
- A rotation was only half done.

**Fix.** Run `setup.py rotate-secret`, then `update_scheduled_task` with the file it writes. Or delete the task and create it again from `setup.py t3-args`. Queued messages retry within 3 minutes.

## 404 from the webhook URL

The task was deleted, or the URL was mistyped. Run `list_scheduled_tasks` to find it. Then store the right URL with `set-url`, or create the task again.

## No webhookUrl in the schedule_task result

T3 Connect remote access is off on this machine. Ask the user to turn it on; there is no public URL until then. Meanwhile, use local delivery (`setup.py delivery local`) and poll the inbox.

## Commits carry "Co-Authored-By: Claude ..." trailers

**Cause.** The Claude Code harness asks for attribution trailers. Sessions whose config dir has no `"attribution": {"commit": ""}` setting get that instruction. The user's own rule (no AI attribution) wins.

**Fix.**
- The CLI's `--commit` and `commit` never add trailers.
- For raw git, write the message yourself, then check with `git log -1 --format=%B`. If a trailer slipped in, run `git commit --amend -m "<message>"`. This is local only, and never pushed.
- To stop the request at the source, the user can add `{"attribution": {"commit": "", "pr": ""}}` to that config dir's settings.json.

## A commit reverted other work: the stale shared index

**Sign.** A plain `git commit` in the main checkout produced a huge diff that undid other threads' commits. Or `git status` there shows many `MM` and `AD` entries.

**Cause.** The shared `.git/index` of the main checkout still reflected an old commit. Other agents committed through worktrees or private indexes, which moved HEAD but not this index. Anything committed from that index writes its old tree back.

**Fix.**
- Never use plain `git add` or `git commit` in such a checkout.
- Commit through a private index. Use the CLI (`--commit`, or `buildlog_tasks.py commit -m MSG PATH...`), or the exported form:
  `( export GIT_INDEX_FILE=$(mktemp -u); git read-tree HEAD && git add PATHS && git commit -m "msg"; rm -f "$GIT_INDEX_FILE" )`
- The prefix form `GIT_INDEX_FILE=$(mktemp -u) git read-tree HEAD; git add ...` is wrong. It sets the variable for the first command only, so `git add` and `git commit` use the shared index after all.
- Resetting the shared index (`git reset -q`, after backing up `.git/index`) needs the user's approval. Put it in the panel as a decision.

The CLI moves the shared index to the new commit only for files whose shared entry matched the old HEAD. Entries that were already stale stay as they were.

## The page shows "read-only copy"

The page was opened as a file (file://), or the server is down. The static snapshot inside the TASKS section is shown instead. Open `http://127.0.0.1:PORT/<page>`, or start the service.

## Ticks or replies don't show

- **A task the user ticked shows as open again.** An agent changed the task in the file after the tick (done, then reopen). The newest change wins by design. Replies never change a task's status stamp, so they cannot undo a tick.
- **A reply doesn't show.** It was written by a CLI in a worktree, not in the main checkout the server serves. Run the CLI by absolute path in the main checkout.
- **`/api/messages` returns 404.** The service still runs an old server. Re-run `setup.py init` to refresh the scripts, then `upgrade-page`.
