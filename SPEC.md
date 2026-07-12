# Attention Relay specification

This file defines Attention Relay. The reference code in `framework/` and the
standalone creation prompt must implement the same behavior.

## Requirements

- Python 3.11 or newer
- Git
- macOS or Linux
- Python standard library only
- A Git worktree without tracked submodules

Attention Relay is one executable Python file plus Markdown and TOML templates. It
has no daemon, database, server, UI, plugin system, or package dependency.

## Runtime

`relay init <project>` requires the Git worktree root and creates:

```text
.attention-relay/
  relay
  orchestrator.md
  worker.md
  memory.md
  config.toml
  tasks/                 active task specs and JSON state
  work/<task-id>/        prompt, log, briefs/token, report, result, and diffs
  archive/               completed task state and work
  .locks/                local state locks
```

Initialization adds `.attention-relay/` to the project’s `.gitignore` once. It does
not replace existing files unless `--force` is used. `--force` refreshes the
CLI and manuals but preserves `config.toml`, `memory.md`, tasks, and work.
Initialization and normal commands reject symlinks anywhere in managed runtime
files or directories.

The runtime is local and disposable, but deleting it also deletes task state,
reports, and memory.

## Roles

The orchestrator talks to the user. It creates tasks, runs workers, reviews
reports and diffs, and approves or returns work.

A worker handles one task attempt. It may submit a result only for the task id,
attempt, and lease in its `RELAY_TASK_ID`, `RELAY_ATTEMPT`, and `RELAY_LEASE`
environment variables.
Worker processes cannot use normal orchestrator commands while those variables
are present.

These checks prevent accidental role violations. They are not a security
sandbox because workers run as the same operating-system user and can edit the
same files.

## Tasks

Task ids use `T###-short-slug`. The CLI assigns them monotonically; callers
cannot supply ids, and numbers are not reused after archiving.

Each active task has:

- `tasks/<id>.md`: objective, acceptance criteria, context, limits,
  verification, decisions, and review feedback;
- `tasks/<id>.json`: state used by the CLI.

Required JSON fields:

```json
{
  "id": "T001-add-email-validation",
  "title": "Add email validation",
  "status": "queued",
  "attempt": 1,
  "tier": "default",
  "scope": ["src/auth/**"],
  "depends_on": [],
  "created_at": "...",
  "updated_at": "...",
  "history": []
}
```

A running task also has `runner` with its process id, start time, and run lease. A task
blocked by scope changes has `scope_violations` and the Git tree id in
`scope_baseline`.

Statuses are:

```text
queued
running
needs_review
needs_decision
blocked
failed
done
cancelled
```

Lifecycle rules:

1. `task create` creates a queued task.
2. `run` claims the task as running before starting a worker.
3. `task finish --brief TOKEN` writes an attempt result but leaves the task
   running. By default, the token must come from a fresh report-phase brief.
4. After the worker exits, Relay writes the diff and validates any submitted
   result before applying its worker status. Timeout, interruption, and
   runner/launch errors unconditionally become `failed`. An ordinary nonzero
   exit with no result becomes `failed` with `worker_exit_N`; with a result,
   validation takes precedence. A fully valid result preserves its submitted
   status and records `worker_exit_N_after_submission` as a warning.
5. Workers may submit only `needs_review`, `needs_decision`, `blocked`, or
   `failed`. `needs_review` requires a non-empty regular report file. Result
   status, note, timestamp, lease, and exact changed-path list must have the
   expected types.
6. `task accept --brief TOKEN` changes only `needs_review` to `done`. It refuses
   live workers and unresolved scope violations and, by default, requires a
   fresh review-phase orchestrator brief token bound to the current attempt.
7. `task return --reason` queues another attempt and appends feedback to the
   task spec. Scope-violating paths must first match their pre-wave state.
8. `task decide --answer` answers a worker question and queues another attempt.
9. `task cancel` refuses running and done tasks.
10. A missing or invalid worker result becomes `failed` with
    `invalid_worker_output`.
11. Declared changed paths must match the observed paths in that task's scope;
    a mismatch becomes `failed` with `changed_paths_mismatch`.

Task state changes use file locks. JSON and generated Markdown writes use a
temporary file followed by atomic replacement. Two Relay processes must not
claim the same task. A dedicated execution lock serializes separate real `run`
processes from their first snapshot through finalization; one process may still
run a parallel wave. `run --dry-run` does not take the execution lock.

Every run has a unique lease. A finalizer updates state only when task id,
attempt, running status, and lease still match, so a stale process cannot clear
or overwrite a newer runner.

Completed dependencies remain valid after their task files are archived.
`validate` reports missing dependencies, self-dependencies, and dependency
cycles.

## Scopes and scheduling

A task scope is a project-relative path pattern. It supports `*`, `?`, and `**`.
`**` must be a complete path segment. Absolute paths, `..`, backslashes, and
character classes are rejected. `.` and an omitted scope mean the whole
project.

Relay normalizes scopes before saving them and compares paths with Unicode
case-folding so case variants cannot collide on case-insensitive filesystems.
It decides possible overlap from the fixed path segments before the first
wildcard. It may serialize tasks that could have run together, but it must not
run scopes together when their fixed prefixes can address the same path.

`run` selects queued tasks when:

- every dependency is done, including archived dependencies;
- the scope does not overlap a running or selected task;
- the wave has not reached `max_parallel`.

`run --dry-run` lists selected tasks and explains every skipped requested task.

Workers in a wave share the project working tree. Relay takes a Git tree
snapshot before launch and another after all workers exit. Each attempt diff is
the change between those trees, limited to that task’s scope. Existing dirty
work and earlier accepted attempts are therefore excluded from the new diff.
Binary files, modes, additions, deletions, and unborn repositories are handled
through Git trees. The real Git index is not used as task state.

Git-ignored files are not added to snapshots or diffs and are outside Relay’s
scope guarantee. Workers must not modify them. Capturing them would require
reading ignored dependencies, build outputs, and possible secrets. Tracked Git
submodules in either `HEAD` or the index are rejected rather than silently
omitted. Generated snapshots are also checked for gitlinks.

Any changed Git-visible path outside the union of the wave’s scopes is recorded
on the wave tasks and blocks acceptance. Relay writes those changes to a
separate `attempt-N.violations.diff`. The paths must be restored to the
pre-wave tree before the task can be returned. In a shared parallel working
tree, every worker must declare each exact changed path with repeated
`--changed PATH` arguments to `task finish`. Relay compares each declaration
with that task's observed scoped diff. This detects distinct cross-scope writes
and prevents silent attribution, while the orchestrator still compares each
report with its diff before approval. Shared-tree workers are cooperative, not
hostile-process sandboxes; concurrent writes to the same claimed file cannot be
attributed cryptographically.

Approval records review state only. Worker edits are already in the shared
working tree; `accept`, `return`, and `cancel` do not apply or revert patches.

## Orchestrator phases and handoff

`relay orchestrator brief --phase start|plan|run|review|close [ID]` is available
only outside a leased worker. The orchestrator runs the matching brief before
task creation, run, review/accept, and session close.

- `start` prints a short role summary and a `Harness memory` notice of at most 12
  lines. The notice offers Claude Code's `"autoMemoryEnabled": false`,
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `/memory`, and `"claudeMdExcludes"`
  controls, including the managed-policy exclusion limit and the warning that
  `claude --bare` disables hooks. It offers Hermes `--ignore-rules`, warns that
  `--safe-mode` drops user config and `hermes memory reset` is destructive, and
  says framework workers are already clean by default. The orchestrator relays
  these choices in its first response and never auto-applies one. Start also
  prints the current handoff when present, task counts, unresolved
  decision/review ids, and one recommended next command. Under a dedicated
  handoff leaf lock, it atomically marks the handoff consumed without deleting
  it.
- `plan` prints the task-spec quality checklist and a bounded queued/blocked
  dependency graph.
- `run` uses the read-only wave selection logic to print what would run and
  cautions for overlapping scopes or unmet dependencies.
- `review ID` is valid only for a `needs_review` task. Under the task lock it
  freshly compiles the task capsule but displays the stored launch capsule when
  `attempt-N.brief.md` exists. If the fresh and stored capsules differ, it prints
  one bounded warning that spec or memory inputs drifted and that the launch
  capsule is shown. With no stored brief it displays the fresh capsule. It also
  prints current report and diff paths, declared and observed changed paths, and
  an accept/return/decide checklist. It atomically stores and prints a new
  `Review token: <value>` bound to task id and attempt; issuing another review
  brief replaces the token.
- `close` uses the same dedicated handoff leaf lock to atomically write the
  bounded `.attention-relay/orchestrator-handoff.md`, prints it, and reminds the
  orchestrator to start a fresh session. The template carries the newest goal,
  tasks accepted at or after the preceding handoff boundary except task ids
  already in its `done` section, recent decision answers, queued and review
  work, unresolved decisions, and an avoid placeholder.

With the default accept gate enabled, `task accept --brief TOKEN` requires the
stored token for that task's current attempt and consumes it only on successful
accept. Missing, wrong, replaced, replayed, or stale-attempt tokens are rejected.
Return, decide, and cancel remove any review token. With the gate disabled,
accept retains its earlier behavior and ignores `--brief`.

`relay status`, `relay task show`, and each real `relay run` end with a
deterministic `Next actions` block of at most about six lines. It derives review
report paths, decision ids, or create/run commands from current task state and
contains no generic advice.

## Optional Claude Code integration

`relay hooks claude-code [--write]` is an orchestrator-only, opt-in setup
command. Without `--write` it prints the exact JSON hook fragment and one-line
merge instructions. With `--write` it atomically creates or merges
`.claude/settings.json`, preserving the order and contents of existing hook
arrays. It appends only missing Attention Relay entries, identified by their
command strings, so repeated setup is idempotent. Invalid JSON is rejected
without changing the file.

The fragment registers exactly one command hook under each of `SessionStart`
and `UserPromptSubmit`, using Claude Code's matcher-free `[{"hooks": [...]}]`
shape. Commands invoke the project-local adapter through
`"$CLAUDE_PROJECT_DIR"/.attention-relay/relay hook-event ...`.

`relay hook-event session-start` emits the same plain stdout as the start-phase
orchestrator brief; Claude adds that stdout to session context.
`relay hook-event user-prompt-submit` emits `hookSpecificOutput` JSON whose
`hookEventName` is `UserPromptSubmit` and whose `additionalContext` is an
`attention-relay state:` line followed by the deterministic `Next actions`
block. Both emitted outputs are always at most 9000 characters. Truncation
retains the first and last lines and places `(truncated)` immediately before the
last line; if both edge lines cannot fit, the adapter emits nothing and fails
open.

Hook events tolerate empty or malformed stdin, never write Relay state, and
fail open: a missing or broken runtime exits successfully with no stdout or
stderr, so Relay cannot break the host session. The adapter and setup commands
are unavailable to leased workers. Claude's `--bare` mode disables hooks and
therefore conflicts with this integration.

## Worker process

The command in `config.toml` is parsed into arguments and launched without a
shell. It must contain exactly one complete `{prompt}` or `{prompt_file}`
argument. Shell operators are not supported; users can call a wrapper script
when needed.

Relay exports:

```text
RELAY_TASK_ID
RELAY_ATTEMPT
RELAY_LEASE
RELAY_DIR
RELAY_ROOT
```

Workers reprint the current attempt's launch capsule at each moment of action
with `relay task brief ID --phase edit|verify|report`. The capsule is followed by
a short phase-specific checklist. The command is available only to the matching
leased worker while its task is running: task id, attempt, and lease must all
match the worker environment.

The report phase also prints `Brief token: <value>` and atomically stores the
token under `work/<id>/`, bound to the current task id, attempt, and lease. A
second report brief replaces it. Edit and verify briefs do not issue tokens.
With the default finish gate enabled, `task finish` requires the current token
as `--brief TOKEN` and consumes it after successfully writing the result. A
missing, wrong, replaced, replayed, different-attempt, or different-lease token
is rejected with instructions to run a fresh report brief.

Each worker runs in a separate process group. Relay captures combined output,
enforces `worker_timeout_minutes`, and terminates process groups on timeout or
`SIGINT`, `SIGTERM`, or `SIGHUP` interruption. It signals every active group
before one shared grace interval. Interrupted tasks become `failed` instead of
remaining stale. Timeout, interruption, and runner/launch errors override any
submitted result. For an ordinary nonzero exit, Relay first applies every normal
result check, including report, scope, and changed-path validation. Invalid
submissions retain their normal failure, while a fully valid submission retains
its worker status with a structured `worker_exit_N_after_submission` warning in
task state, status output, history, and the review brief.

Before launch, Relay compiles a deterministic Critical Context Capsule from the
task state, the existing `Objective`, `Acceptance criteria`, `Not allowed`,
`Verification`, and retry sections, and the memory index at compile time. The
same task state, spec text, and memory index produce byte-identical capsule text.
The task spec remains the only hand-edited task source; there is no capsule
section in the task template. Empty objectives or acceptance criteria, and
either section retaining its template placeholder line, are actionable launch
errors. `validate` reports the same errors for queued tasks.

Relay scans only the spec's `Context` section for ordered, deduplicated
`M\d{3,}` references. When any exist, the capsule includes `Referenced memory`
after `Verification` and before `Retry delta`. It contains one instruction to
load full entries with
`python3 .attention-relay/relay memory show ID` and one
`- M###: summary` line per reference. It never includes full memory bodies.
With no references this section is absent and the capsule format is unchanged.
More than six distinct references is an error that directs the orchestrator to
split the task or remove references. A missing id or an orchestrator-only `[O]`
reference is also an error; worker capsules accept only `[W]` and `[B]` entries.
Compilation, launch, prospective preview, and queued-task validation report
these errors without dropping or truncating references.

On attempts after the first, the capsule also contains a `Retry delta` with only
the newest entry from `Review feedback` and/or `Decisions`. The previous-attempt
report remains a file pointer in the middle of the prompt. Relay places the
byte-identical capsule at the exact beginning and end of the launch prompt,
around the task metadata, file pointers, and finish mechanics. It also writes
that capsule to `work/<id>/attempt-N.brief.md` with its SHA-256 content digest.
This immutable launch snapshot is the audit record; worker phase briefs reread
it without consulting mutable spec or memory text.

Capsules are never truncated. If one exceeds `capsule_max_chars`, launch and
validation fail with the measured size and overflow.

`relay task capsule ID [--raw]` is a read-only, orchestrator-only preview. For a
non-running active task it compiles the current spec prospectively. For a
running task it reads the stored `attempt-N.brief.md` launch capsule and never
recompiles mutable spec text. Unknown and archived ids are rejected.

Default output prints the complete capsule followed by its measured size,
headroom or overflow, per-section Unicode character counts, SHA-256 digest, and
source. It is never truncated, and an over-budget preview exits nonzero after
printing all diagnostics. `--raw` prints only the exact capsule bytes; when the
capsule is over budget it prints no stdout and exits nonzero. Empty required
sections and template placeholders retain the same errors used by launch and
validation. Preview creates or changes no file or task state.

## Config

`config.toml` contains:

```toml
[commands]
worker = "hermes chat -Q -t terminal,file --source tool --ignore-rules -q {prompt}"

[limits]
max_parallel = 3
capsule_max_chars = 4000
worker_timeout_minutes = 60

[gates]
finish_requires_brief = true
accept_requires_brief = true
```

The default worker command is harness-memory-clean: `--ignore-rules` suppresses
automatic rules, saved memory, and preloaded skills while preserving the user's
configured model and reasoning.

`max_parallel` and `capsule_max_chars` are positive integers. The capsule limit
defaults to 4000 characters. The timeout is a non-negative number in minutes;
zero disables it.

Both gate values must be booleans and default to `true` when absent.
`finish_requires_brief = false` lets `task finish` work without a token;
`accept_requires_brief = false` lets `task accept` work without a review token.
Each disabled gate ignores its corresponding `--brief` argument.

Optional `[tiers.<name>].command` values override the default worker command for
a task with that tier. An unknown tier uses the default command.

## Memory

`memory.md` stores durable project facts, not task progress or logs. Its index
uses:

```text
- M001 [W] summary
- M002 [O] summary
- M003 [B] summary
```

Ids contain `M` followed by three or more digits. `W` is for workers, `O` for
the orchestrator, and `B` for both. Index lines are parsed strictly in file
order; malformed lines and duplicate ids are errors. Full entries use
`### M001 ...` headings. Agents read the index and load only relevant entries.
Task specs reference useful ids in `Context`; capsules carry their one-line
worker-visible summaries, but agents still load full entries explicitly when
needed.

Commands:

```text
relay memory index [--for worker|orchestrator]
relay memory show M001
relay memory add --for worker|orchestrator|both "summary" "body"
```

## Verification

`python3 tests/test_relay.py` must pass. The suite uses temporary Git projects
and stub workers. It covers:

- Git-only, idempotent initialization, root checks, nested symlink rejection,
  and submodule rejection from `HEAD`, the index, and snapshots;
- monotonic task creation, path validation, dependencies, cycles, and archived
  dependencies;
- scope normalization, case-folded overlap, scope-violation blocking, and
  changed-path attribution;
- parallel workers, serialized run processes, leases, and duplicate-claim
  prevention;
- worker and orchestrator phase briefs, finish/review tokens and gates, handoff,
  stdout next-action capsules, reports, worker results, lifecycle guards,
  return, decide, and accept;
- attempt-local Git diffs in clean, dirty, and unborn worktrees;
- direct command execution without a shell;
- process-group timeout, batched `SIGINT`/`SIGTERM` cleanup, and non-UTF-8
  output handling;
- memory, all-or-nothing archive preflight, and signal-safe archive completion;
- exact equality between this specification and the contract embedded in the
  standalone creation prompt.
