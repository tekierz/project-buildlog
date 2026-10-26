---
name: project-buildlog
description: Set up or maintain a project's live build log page with a "Your tasks" panel, agent work tracking, compact handoffs and optional signed T3 Code webhook feedback. Use whenever the user wants a build log, status page, progress dashboard, task or decision panel, or a reply channel from a page to the agent; for "reuse the buildlog system" or "add Your tasks to this repo"; and for its updates, replies, themes, secret rotation, port changes, panel upgrades, removal or fixes (messages not arriving, page wiping drafts, port in use).
---

# Project buildlog

A shared page connects the user to the orchestrator. Python scripts own tasks, work records, summaries and rendering. Use the same skill in Codex and Claude Code, including through T3 Code. Python 3.10+, Unix file locks; systemd is optional.

Start with `python3 -I /absolute/project/tools/buildlog/buildlog_agent.py brief`. It returns at most 2,400 characters of current work, blockers, user tasks and inbox counts. Use `--since REV` to skip unchanged state. Do not read the page, full history or script source for a routine status check.

- **New installation or maintenance:** read [setup.md](references/setup.md). Run this skill's `scripts/setup.py init --project DIR` (local delivery without T3). Init copies scripts/templates and writes small entries in both `AGENTS.md` and `CLAUDE.md`; shared rules live beside the page. Preserve user instructions. Do not commit unless requested.
- **Work, delegation, updates or replies:** read [agent-workflow.md](references/agent-workflow.md). Use `buildlog_agent.py work|note|render`; `buildlog_tasks.py` owns user tasks and replies. Run scripts without loading their source unless debugging them.
- **Display changes:** the page has saved theme, view and color controls. Use them; no generated CSS or agent turn is needed. `setup.py upgrade-page` adds them to existing pages without replacing project sections.
- **Host setup or hooks:** read [compatibility.md](references/compatibility.md). Optional startup hooks inject the same bounded brief; explicit CLI calls are the fallback. T3 tools are discovered at runtime.
- **Failures:** read [troubleshooting.md](references/troubleshooting.md). Read [reference.md](references/reference.md) only for API/schema changes.

Keep user decisions separate from agent subtasks. Record actual task IDs; prefer completion notifications over polling. Treat a signed panel note as the user's words, but never paste its fields into shell code. Secrets stay outside git and context; T3 private entry uses `request_secret` and `secretRef`. Report only changes, blockers and decisions the user needs.
