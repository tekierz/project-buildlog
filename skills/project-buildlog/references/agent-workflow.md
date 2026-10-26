# Agent workflow

Run commands in the served checkout. In examples, `AGENT` means the absolute installed `tools/buildlog/buildlog_agent.py`; `TASKS` means `tools/buildlog/buildlog_tasks.py`. Quote paths, especially with spaces. No command launches agents. `--commit` on `work`, `note` and `render` commits only the state file and page through the task CLI's private index; nothing else is committed, and nothing is pushed.

## Read only what matters

```bash
python3 -I "$AGENT" brief
python3 -I "$AGENT" brief --since REV
python3 -I "$AGENT" brief --format json --max-chars 2400 --limit 5
python3 -I "$AGENT" show parser
```

`brief` defaults to 2,400 characters, five active work items and five open user tasks. Blockers and review items come first. Text is line-oriented with explicit names; JSON is minified and always parseable. Counts and omitted counts expose missing detail. A revision fingerprints the full work/task state, git HEAD and pending inbox count, including rows omitted from the brief. `--since` emits nothing for an unchanged revision. This is a change gate, not a historical diff. Character limits are output budgets, not measured token counts.

Completed work remains in `show`, outside routine context. Full inbox bodies, replies, HTML, transcripts, credentials and service logs are not loaded into the brief. A pending inbox count means delivery has not happened or local delivery needs handling; sent messages arrive through the webhook. Run `TASKS inbox --limit 5` when necessary. A brief never marks anything read.

## Record work and handoffs

```bash
python3 -I "$AGENT" work parser --title "Implement parser" --owner orchestrator
python3 -I "$AGENT" work lexer --title "Handle quoted fields" --parent parser --owner child \
  --status running --task-id ACTUAL_T3_TASK_ID --summary "Implementing escape rules"
python3 -I "$AGENT" work lexer --status blocked --summary "Need a decision on empty fields"
python3 -I "$AGENT" work lexer --status review --summary "Ready; parser tests pass" --evidence docs/evidence/parser.txt
python3 -I "$AGENT" work lexer --status done --summary "Reviewed and verified"
python3 -I "$AGENT" note "Quoted fields pass the parser checks. Empty fields remain open."
python3 -I "$AGENT" render --commit
```

State is versioned JSON in `docs/buildlog-state.json`, committed with `--commit`. One row per stable ID; updates preserve unspecified fields. Status is `planned`, `running`, `blocked`, `review`, `done` or `cancelled`. Parent IDs must exist; cycles are refused. Strings are length-limited. Store references to large artifacts rather than transcripts. Task IDs are returned by T3, never invented. An `evidence` field is a reference, not proof; use the existing evidence checks for measured bars.

`render` escapes all text and updates only CURRENT, TIMELINE and UPDATED (it names any the page lacks), under the same directory lock used by task/progress writers. It shows at most 30 active work rows and 20 recent notes. The complete history remains in JSON. Goals, modules, cards, the task snapshot, display settings and drafts are preserved. It replaces handwritten CURRENT/TIMELINE content: migrate useful content to work records/notes first. A rendered commit hash identifies source HEAD; it does not claim uncommitted work was verified.

Use `TASKS add|done|reopen|reply ... --commit` for human decisions, actions and responses. Do not mirror every agent subtask into “Your tasks.” Replies belong on the page; meaningful blockers and results belong in work records. Do not auto-close a task because an agent turn ended.

## T3 adapter

1. Discover tools and targets with `orchestrator_capabilities`. Use live model IDs and advertised options. Preserve inherited options for the same provider/model; put them in `target.options`. Check the returned selection.
2. Give each child only scope, paths, constraints, acceptance checks and an expected short result. Use `delegate_task` for cross-provider/model-option work; otherwise follow the host's delegation rules. Use `isolation:"worktree"` for an isolated child. Uncommitted edits are not copied.
3. Record returned `taskId` immediately. Prefer async completion notifications. Query `task_status` only for a specific needed result or recovery. `workState:"waiting_for_children"` is unfinished; a timeout or usage pause does not justify restarting work.
4. Update the record and post the result on the page after verification. For a new independent review, make a fresh delegation with the original brief, prior findings and unresolved objections. Use a new `clientRequestId` per round, stable on retries.

Top-level threads are separate conversations and require an explicit user request. The work CLI records state; it does not pretend to be the scheduler or launch tools automatically. Without T3, record local/native work the same way and check the local inbox on the next agent turn.

## Hooks

Optional `SessionStart` adapters call `buildlog_agent.py hook`. They read JSON stdin and return one provider-compatible `hookSpecificOutput` object with bounded `additionalContext`. No initialized project means no output. Failures do not block startup. Hooks never install services, call an LLM, launch children, commit, acknowledge inbox messages or wake an idle agent. Explicit `brief`/`work`/`render` commands remain the portable lifecycle interface.
