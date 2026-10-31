# project-buildlog skill

Shared Codex and Claude Code workflow for a live project buildlog, user decisions, agent work records and bounded handoffs.

- [SKILL.md](SKILL.md): short entry point and reference routing.
- [Setup](references/setup.md): install, webhook, service and maintenance.
- [Agent workflow](references/agent-workflow.md): compact context, work records, rendering and delegation.
- [Review hub](references/review-hub.md): the optional page for approvals, choices, auditions and document comments.
- [Compatibility](references/compatibility.md): Codex, Claude Code, T3 Code, optional hooks and sources.
- [Reference](references/reference.md): API, storage and evidence rules.
- [Troubleshooting](references/troubleshooting.md): known failures and fixes.

Scripts use Python 3.10+ and the standard library. Run them with `python3 -I`; they do not need to be read into model context. See the repository [README](../../README.md) for installation and tests.
