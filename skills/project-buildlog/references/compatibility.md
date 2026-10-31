# Compatibility and evidence

Documentation checked October 25, 2026 by three independent research agents. This is a skills-only plugin with optional local hook examples, not an MCP server. T3 tools come from the host. Repository docs and live schemas can differ: the installed tool schema is the executable contract.

## Codex

Install the skill in a documented discovery location. For a local clone:

```bash
mkdir -p ~/.agents/skills
ln -s /absolute/path/project-buildlog-plugin/skills/project-buildlog ~/.agents/skills/project-buildlog
```

Choose a different destination if it already exists; do not overwrite another skill. For project scope, use `.agents/skills/project-buildlog`. Invoke `$project-buildlog`. Root `plugin.json` declares the portable plugin; it takes effect only when Codex installs this repository as a plugin through a marketplace, not through the symlink above. `.claude-plugin/plugin.json` remains the Claude manifest, and Claude ignores the root file. A Claude marketplace installation does not install the skill into Codex automatically.

Codex reads `AGENTS.md`; Claude reads `CLAUDE.md`. Init writes a small managed pointer to each and keeps full operating rules in a shared, selectively read document. Existing user text survives reruns. `AGENTS.override.md` or child-directory instructions may override root instructions; do not silently edit those. Restart the T3 agent session after installing skills/instructions if discovery has not refreshed.

Sources: [Codex skill discovery](https://learn.chatgpt.com/docs/build-skills), [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md), [portable plugin packaging](https://developers.openai.com/plugins/build/plugins).

## Claude Code

Keep `.claude-plugin/plugin.json` and the standard `skills/` directory. Use the existing marketplace install in the root README, or `claude --plugin-dir /absolute/path/to/this/repo` for local testing outside T3. Invoke `/project-buildlog:project-buildlog`. T3 discovers enabled Claude skills through that provider; restart its agent session when changing installations. A plugin-root CLAUDE.md is not automatically project memory.

Source: [plugin reference](https://code.claude.com/docs/en/plugins-reference), [skills](https://code.claude.com/docs/en/skills), [memory](https://code.claude.com/docs/en/memory).

## Optional startup hooks

The core manifest does not register hooks. Examples are under root `integrations/`, outside the auto-discovered `hooks/hooks.json` location. Hooks are optional enrichment; direct CLI calls work everywhere this plugin's Python requirements are met (Linux, macOS or Windows).

- **Claude plugin:** copy `integrations/claude-hooks.json` to `hooks/hooks.json` in a private/local distribution. It uses `${CLAUDE_PLUGIN_ROOT}` and runs on startup, resume, `/clear` and compaction. For project/user settings, merge the `hooks` field and replace the command with the quoted absolute script path; the plugin environment variable is not guaranteed outside plugin hooks.
- **Codex plugin:** in a private/manual desktop distribution, copy `integrations/codex-hooks.json` to `hooks/hooks.json`. It uses `${PLUGIN_ROOT}`. Follow the runtime's hook review/trust flow. A standalone skill symlink does not register plugin hooks.
- **T3:** the official docs do not establish that all T3 Codex/Claude provider sessions run these hooks. Test one SessionStart in your installed provider; use explicit `brief` at turn start if no context appears. A browser Send action does not fire SessionStart or start an agent turn.

The adapter accepts JSON stdin (`hook_event_name`, `cwd`) and returns JSON `hookSpecificOutput.additionalContext` only for SessionStart. It is quiet in uninitialized projects, never reads private webhook config, and never installs, commits, launches agents or acknowledges messages. No LLM call is involved; injected text still costs context. Existing projects with custom tool paths need a literal `--settings /absolute/path/buildlog.json` in their hook command.

Codex non-managed hooks require trust, and changed definitions may require renewed trust. Plugin-bundled hooks are documented for manual desktop distribution; they exclude a package from the public plugin directory. Keep a hook-free package for public submission. Hook execution in T3 has not been verified by this change.

Sources: [Codex hooks](https://learn.chatgpt.com/docs/hooks), [Codex distribution limits](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks), [Claude hook protocol](https://code.claude.com/docs/en/hooks#hook-input-and-output), [Claude hook lifecycle](https://code.claude.com/docs/en/hooks-guide).

## T3 Code

Use the same scripts and state for either provider. Discover `orchestrator_capabilities`, `delegate_task`, scheduler and preview tools by their live names, including any harness prefix. Keep actual task IDs and target options. Child work uses delegation; top-level threads are separate conversations. Prefer completion notifications; avoid recurring scans of all threads. See [agent-workflow.md](agent-workflow.md).

Signed webhook setup requires T3 Connect remote access for the public URL. Use a structured schedule object, `bindToCurrentThread:true`, a stable creation `clientRequestId` where supported, event filter and replay key. Get a private one-use `secretRef` through `request_secret`; never load a signing key into the prompt. Keep the returned scheduler ID. Report timer cadence/next run time; for a webhook report that it is configured, while keeping this plugin's capability URL private. A dispatched run is not proof the task completed.

Use `preview_status`, then `preview_open`, snapshots and focused interactions to inspect the actual page. `preview_snapshot(save:true)` captures a screenshot. `html_render` publishes HTML and is not a screenshot tool. Prefer screenshots when visual changes need review; text-only work does not require them.

Evidence: this session's live orchestration, task, scheduler and preview schemas; official T3 repository at commit `7510e5e43c6cb40d454530c9aaa9a8fbb43ab1bf`:

- [Claude skills and usage limits](https://github.com/pingdotgg/t3code/blob/7510e5e43c6cb40d454530c9aaa9a8fbb43ab1bf/docs/user/providers-claude.md)
- [Orchestration](https://github.com/pingdotgg/t3code/blob/7510e5e43c6cb40d454530c9aaa9a8fbb43ab1bf/docs/orchestration-v2/orchestrator-mcp-server.md)
- [Portable handoffs](https://github.com/pingdotgg/t3code/blob/7510e5e43c6cb40d454530c9aaa9a8fbb43ab1bf/docs/user/portable-handoffs.md)
- [Composer discovery](https://github.com/pingdotgg/t3code/blob/7510e5e43c6cb40d454530c9aaa9a8fbb43ab1bf/docs/user/composer.md)

## Token and UI efficiency

The implementation uses progressive disclosure, deterministic scripts, versioned JSON state, minified JSON or bounded line-oriented text, row limits and revision checks. These reduce repeated input/output; they do not establish a provider-specific token saving. Measure actual context with Claude `/context` or your host's usage display under the same workload.

Themes, high contrast, a color-blind palette and cards/compact views use one prebuilt page and localStorage. Their controls do not call an agent. Status is also written in text and partial progress uses stripes. These features are not a claim of full accessibility certification.

Sources: [Claude cost guidance](https://code.claude.com/docs/en/costs#offload-processing-to-hooks-and-skills), [Claude context behavior](https://code.claude.com/docs/en/context-window), [OpenAI reusable skill resources](https://developers.openai.com/plugins/build/skills).
