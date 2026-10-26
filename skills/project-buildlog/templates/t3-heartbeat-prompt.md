Hourly heartbeat for @@PROJECT@@ (project root @@ROOT@@). Reply with one short status line unless something needs action. Do the checks in this order:

1. Agents. List this project's threads and delegated tasks (t3_thread_list, task_status). For anything stuck, failed, or waiting for input for over an hour: nudge it, restart it, or report it in one line.
2. Build log. Compare the commit in the UPDATED line of @@PAGE_ABS@@ with `git -C @@ROOT@@ log -1 --format=%h`. If there are new commits since then, and no update round is already running, delegate ONE build log update round to a cheap, fast model (a Haiku-class or Sonnet-class subagent) with this brief:
   - Update @@PAGE_ABS@@ from `git -C @@ROOT@@ log --oneline <hash in the UPDATED line>..HEAD`.
   - Follow the editing-rules comment at the top of the page. Change only the UPDATED line, CURRENT, GOALS, the cards, the module table and the timeline (newest first, one plain-words line per notable change). Never touch the TASKS section.
   - Run `python3 -I @@PROGRESS_ABS@@ --write`.
   - Commit only the page, through a private index, with no trailers: `python3 -I @@TASKS_CLI_ABS@@ commit -m "buildlog: update" @@PAGE_REL@@`
   - Report the new UPDATED line.
   When it is done, show the changed part with a screenshot (html_render).
3. Messages. Run `python3 -I @@TASKS_CLI_ABS@@ inbox`. A message still queued after an hour means the webhook path is broken: check `systemctl --user status @@SERVICE@@` and `journalctl --user -u @@SERVICE@@ -n 50`, then handle the message as if it had arrived (see the build log message task) and run `inbox --mark-read`.
4. Service. `curl -fsS http://127.0.0.1:@@PORT@@/api/version` answers with JSON. If not, `systemctl --user restart @@SERVICE@@` and check again.
5. Resources. Disk (`df -h @@ROOT@@`), memory (`free -h`), load (`uptime`). Warn when the disk is over 90% full, free memory is under 2 GB, or load stays above the CPU count.
