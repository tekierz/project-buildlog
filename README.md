# project-buildlog

A shared buildlog for Codex and Claude Code, including through T3 Code. The page shows progress, agent work, and the actions or decisions that need you. You can send a message and read the orchestrator's reply there.

![The Your tasks panel](skills/project-buildlog/assets/panel-screenshot.png)

## What it provides

- A live page with user tasks, replies, work status, evidence-based progress bars and a timeline. Drafts survive updates.
- Agent work records with owners, parent tasks, blockers and actual T3 task IDs. Python renders updates from data.
- A status brief capped at 2,400 characters by default. Revision checks skip unchanged state; details load on demand.
- Saved dark, light, system and high contrast themes, cards/compact views, and a color-blind palette. Display changes need no agent call.
- Optional startup hook adapters and signed T3 webhook feedback. Local inbox delivery works without T3.
- A loopback-only server, retry queue, private credential storage and optional systemd user service.

Python standard library only. No package dependencies or model API calls.

## Install

In Claude Code, from [Dalt's Bazar](https://github.com/tekierz/dalts-bazar):

```bash
/plugin marketplace add tekierz/dalts-bazar
/plugin install project-buildlog@dalts-bazar
/reload-plugins
```

Invoke `/project-buildlog:project-buildlog`. For local development, use `claude --plugin-dir /absolute/path/to/this/repo`.

For Codex, symlink the skill from a local clone into a discovery directory:

```bash
mkdir -p ~/.agents/skills
ln -s /absolute/path/project-buildlog-plugin/skills/project-buildlog ~/.agents/skills/project-buildlog
```

Use `.agents/skills/project-buildlog` for project scope. Keep any existing installation; choose another destination if occupied. Invoke `$project-buildlog`. This repository also includes a portable root plugin manifest. A Claude installation does not automatically install the Codex skill.

In T3 Code, restart the agent session after changing provider skill/plugin installations. Both providers use the same scripts and project data. See [compatibility and source evidence](skills/project-buildlog/references/compatibility.md) for host limits and optional hook setup.

## Set up a project

Ask the agent to “set up a buildlog for this repo,” or run:

```bash
python3 -I skills/project-buildlog/scripts/setup.py init --project /path/to/repo --title "My App"
```

Init writes compact entries to both `AGENTS.md` and `CLAUDE.md`, with shared rules beside the page. Repeat `--rules-doc PATH` to choose entry points. It preserves existing instruction text and does not commit unless `--commit` is supplied.

Requirements: Python 3.10+, Unix file locks, and git for commits/evidence. Linux systemd user services keep the server running; otherwise use `--no-service` and start the copied server yourself. T3 webhook delivery requires remote access. Use `--delivery local` for a local inbox without T3.

The [setup guide](skills/project-buildlog/references/setup.md) covers private webhook entry, verification, upgrades and removal. Recurring heartbeats are optional and created only when requested. Hooks supplement delivery; they cannot wake an idle agent, and hook execution in T3 still needs a live provider check.

## Routine work

```bash
python3 -I /path/to/repo/tools/buildlog/buildlog_agent.py brief
python3 -I /path/to/repo/tools/buildlog/buildlog_agent.py work parser --title "Implement parser" --status running --owner child
python3 -I /path/to/repo/tools/buildlog/buildlog_agent.py note "Parser passes the input checks."
python3 -I /path/to/repo/tools/buildlog/buildlog_agent.py render --commit
```

The [agent workflow](skills/project-buildlog/references/agent-workflow.md) covers bounded JSON/text, revision checks, delegation and message handling. Human tasks and replies use `buildlog_tasks.py`; agent subtasks use `buildlog_agent.py`. `--commit` commits only buildlog files through a private index; nothing is pushed. Full records stay in JSON, outside routine model context.

Rendering replaces CURRENT, TIMELINE and UPDATED; migrate handwritten content there before using it on an older page. Other sections remain intact. Run `setup.py upgrade-page --project DIR` to add display controls to an older page with a backup.

## Security and verification

The server binds to 127.0.0.1, guards POST requests against cross-site use, and signs webhook messages. The signing key and webhook URL stay outside git in private files. A signed note is treated as the user's words, never as shell code. Use T3 private secret entry and a one-use `secretRef` for scheduling.

```bash
python3 -I skills/project-buildlog/scripts/tests/test_setup.py
python3 -I skills/project-buildlog/scripts/tests/test_buildlog_server.py
python3 -I skills/project-buildlog/scripts/tests/test_buildlog_progress.py
python3 -I skills/project-buildlog/scripts/tests/test_buildlog_agent.py
claude plugin validate .
```

`verify.py --project DIR` checks a temporary fake T3 webhook end to end; `--real` additionally tests the configured live webhook. Actual token savings depend on the provider and workload; character budgets are not token measurements.

Apache-2.0. See [LICENSE](LICENSE).
