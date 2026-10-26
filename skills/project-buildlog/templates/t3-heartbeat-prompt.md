Optional hourly buildlog check for @@PROJECT@@ at @@ROOT@@. AGENT means `python3 -I "@@AGENT_CLI_ABS@@"`; TASKS means `python3 -I "@@TASKS_CLI_ABS@@"`.

1. Run `AGENT brief` and end your status line with its rev. If the rev matches the last heartbeat line in this thread and no child completion needs handling, reply with that one line and stop. Do not read the HTML, the full inbox, all threads or machine reports.
2. If inbox is nonzero, run `TASKS inbox --limit 5`. Handle each message as the build log message task would (reply on the page with `--commit`), then mark only handled IDs read with `TASKS inbox --id ID --mark-read`. Never paste notes into shell code. A `queued` message (not `local`) older than an hour means webhook delivery is broken: check `systemctl --user status @@SERVICE@@` and `journalctl --user -u @@SERVICE@@ -n 50`.
3. `curl -fsS http://127.0.0.1:@@PORT@@/api/version` must answer with JSON. If not, run `systemctl --user restart @@SERVICE@@` and check again.
4. For changed work, use retained task IDs and completion notifications; query task_status only for a specific unresolved result. A wait or usage pause is not a failure, so do not restart work for waiting. If commits or work records changed since the last render, update records and notes, then run `AGENT render --commit`. Refresh card bars with `python3 -I "@@PROGRESS_ABS@@" --write` when evidence changed. Never invent progress.
5. Delegate substantive work with delegate_task (live catalog, required options) and record the returned taskId with `AGENT work`. Do not create top-level threads for child work, or another recurring check.

Report only changes, blockers and decisions. Never push.
