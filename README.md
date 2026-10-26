# project-buildlog

A Claude Code plugin that gives any project a live build log page with a "Your tasks" panel.

The page shows where the work stands. The panel lists what only you can do or decide. You tick a task, answer a decision or write a message, and press Send. A small local server signs the message (HMAC-SHA256) and posts it to a T3 Code webhook task in the orchestrator's thread. The orchestrator answers on the page.

![The Your tasks panel](skills/project-buildlog/assets/panel-screenshot.png)

## What you get

- **A page** (`docs/buildlog.html`) with sections for the latest update, your tasks, current work, goals, status cards with measured progress bars, modules and a timeline. It refreshes itself without wiping what you type.
- **A local server** on 127.0.0.1, run as a systemd user service. It serves the repo read-only, guards every POST against cross-site requests, and queues and retries messages that fail to send.
- **A task CLI** to add, close, reopen and reply to tasks, read the inbox, and commit through a private git index.
- **A private key and webhook URL** in `~/.config/<name>/webhook.json` (mode 600), outside git.
- **Two T3 Code tasks**: the signed webhook into the orchestrator's thread, and an optional hourly heartbeat.
- **Checks**: unit tests, plus `verify.py`, which runs end to end against a fake T3 webhook (or the real one).

Everything is Python standard library. Nothing to install with pip.

## Install

This plugin is listed in the [Dalt's Bazar](https://github.com/tekierz/dalts-bazar) marketplace. In Claude Code:

```bash
/plugin marketplace add tekierz/dalts-bazar
/plugin install project-buildlog@dalts-bazar
/reload-plugins
```

Then ask Claude in your project, for example: "set up a build log for this repo". The skill walks through the steps. You can also call it directly with `/project-buildlog:project-buildlog`.

## Requirements

- **Python 3.10 or newer.**
- **Linux with systemd user services** (`systemctl --user`) to keep the server running. Run `loginctl enable-linger` if it should keep running after you log out. Without systemd, pass `--no-service` to `setup.py init` and start the server yourself.
- **git**, for commits and the evidence checks behind the progress bars.
- **T3 Code with remote access turned on**, for the webhook step. It gives the webhook task its public URL.

### Without T3 Code

Everything except the webhook works without T3. Switch to local delivery:

```bash
python3 -I <skill dir>/scripts/setup.py delivery --project /path/to/repo local
```

Messages from the page then wait in a local inbox. Agents read them with `buildlog_tasks.py inbox` and mark them handled with `inbox --mark-read`. Poll it from a heartbeat, from `/loop 30m`, or at the start of each turn.

## Setup in brief

The skill (`skills/project-buildlog/SKILL.md`) has the full steps. In short:

1. `setup.py init` copies the scripts into the project, writes the page and task list, makes the secret, and installs the service.
2. In the orchestrator's thread, create the T3 webhook task from the arguments file that init writes.
3. Store the returned webhook URL with `setup.py set-url` (read from stdin, never from the command line).
4. Run `verify.py`, then `verify.py --real`.
5. Optional: add the hourly heartbeat task.

Maintenance commands: `status`, `rotate-secret`, `change-port`, `upgrade-page`, `delivery`, `t3-args` and `remove`.

## Security

- The server listens on 127.0.0.1 only and refuses foreign Host and Origin headers.
- The secret and the webhook URL stay in `~/.config/<name>/webhook.json` (mode 600). They never go into git, logs, the page or the chat.
- Every message to the webhook is signed. T3 checks the signature, an event filter and a replay key.

## Tests

```bash
cd skills/project-buildlog
python3 -I scripts/tests/test_setup.py
python3 -I scripts/tests/test_buildlog_server.py
python3 -I scripts/tests/test_buildlog_progress.py
```

## License

Apache-2.0. See [LICENSE](LICENSE).
