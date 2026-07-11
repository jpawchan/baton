# Agent Relay specification

This file defines Agent Relay. The reference code in `framework/` and the
standalone creation prompt must implement the same behavior.

## Requirements

- Python 3.11 or newer
- Git
- macOS or Linux
- Python standard library only
- A Git worktree without tracked submodules

Agent Relay is one executable Python file plus Markdown and TOML templates. It
has no daemon, database, server, UI, plugin system, or package dependency.

## Runtime

`relay init <project>` requires the Git worktree root and creates:

```text
.agent-relay/
  relay
  orchestrator.md
  worker.md
  memory.md
  config.toml
  tasks/                 active task specs and JSON state
  work/<task-id>/        prompt, log, report, result, and diffs per attempt
  archive/               completed task state and work
  .locks/                local state locks
```

Initialization adds `.agent-relay/` to the project’s `.gitignore` once. It does
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
3. `task finish` writes an attempt result but leaves the task running.
4. After the worker exits, Relay writes the diff and changes the task to the
   submitted worker status.
5. Workers may submit only `needs_review`, `needs_decision`, `blocked`, or
   `failed`. `needs_review` requires a non-empty regular report file. Result
   status, note, timestamp, lease, and exact changed-path list must have the
   expected types.
6. `task accept` changes only `needs_review` to `done`. It refuses live workers
   and unresolved scope violations.
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

Each worker runs in a separate process group. Relay captures combined output,
enforces `worker_timeout_minutes`, and terminates process groups on timeout or
`SIGINT`, `SIGTERM`, or `SIGHUP` interruption. It signals every active group
before one shared grace interval. Interrupted tasks become `failed` instead of
remaining stale.

The generated prompt identifies the task, attempt, root, scope, task spec,
worker contract, report path, and finish command. It points to files instead of
copying their contents.

## Config

`config.toml` contains:

```toml
[commands]
worker = "hermes chat -Q -t terminal,file --source tool -q {prompt}"

[limits]
max_parallel = 3
worker_timeout_minutes = 60
```

`max_parallel` is a positive integer. The timeout is a non-negative number in
minutes; zero disables it.

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

`W` is for workers, `O` for the orchestrator, and `B` for both. Full entries use
`### M001 ...` headings. Agents read the index and load only relevant entries.

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
- reports, worker results, lifecycle guards, return, decide, and accept;
- attempt-local Git diffs in clean, dirty, and unborn worktrees;
- direct command execution without a shell;
- process-group timeout, batched `SIGINT`/`SIGTERM` cleanup, and non-UTF-8
  output handling;
- memory, all-or-nothing archive preflight, and signal-safe archive completion;
- exact equality between this specification and the contract embedded in the
  standalone creation prompt.
