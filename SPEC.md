# Baton specification

This file defines Baton. The reference code in `framework/` and the
standalone creation prompt must implement the same behavior.

## Requirements

- Python 3.11 or newer
- Git
- macOS or Linux
- Python standard library only
- A Git worktree without tracked submodules

Baton is one executable Python file plus Markdown and TOML templates. It
has no daemon, database, server, UI, plugin system, or package dependency.

## Runtime

`framework/baton init <project>` requires the Git worktree root and creates:

```text
.baton/
  baton
  orchestrator.md
  worker.md
  memory.md
  config.toml
  tasks/                 active task specs and JSON state
  work/<task-id>/        prompt, log, briefs/token, report, result, and diffs
  archive/               completed task state and work
  .locks/                local state locks
```

Initialization adds `.baton/` to the project’s `.gitignore` once. It does
not replace existing files unless `--force` is used. `--force` refreshes the
CLI and manuals but preserves `config.toml`, `memory.md`, tasks, and work.
Initialization and normal commands reject symlinks anywhere in managed runtime
files or directories. Successful initialization tells the user only to have the
coding agent read `.baton/orchestrator.md`; it does not expose or ask the user to
run the internal start command.

The runtime is local and disposable, but deleting it also deletes task state,
reports, and memory.

## Roles

The orchestrator talks to the user. It creates tasks, runs workers, reviews
reports and diffs, and approves or returns work.

A worker handles one task attempt. It may submit a result only for the task id,
attempt, and lease in its `BATON_TASK_ID`, `BATON_ATTEMPT`, and `BATON_LEASE`
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
  "tier": "hard",
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

1. `task create` requires an explicit validated `--tier` and creates a queued
   task. It never silently selects `default` or another difficulty.
2. `run` claims the task as running before starting a worker.
3. `task finish --brief TOKEN` writes an attempt result but leaves the task
   running. By default, the token must come from a fresh report-phase brief.
4. After the worker exits, Baton writes the diff and validates any submitted
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
temporary file followed by atomic replacement. Two Baton processes must not
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

Baton normalizes scopes before saving them and compares paths with Unicode
case-folding so case variants cannot collide on case-insensitive filesystems.
It decides possible overlap from the fixed path segments before the first
wildcard. It may serialize tasks that could have run together, but it must not
run scopes together when their fixed prefixes can address the same path.

`run` selects queued tasks when:

- every dependency is done, including archived dependencies;
- the scope does not overlap a running or selected task;
- the wave has not reached `max_parallel`.

`run --dry-run` lists selected tasks and explains every skipped requested task.
Every selected identity includes task id, title, explicit difficulty, and the
bounded safe worker label resolved from that validated tier. Real launch output
repeats the same identity before starting the worker.

Workers in a wave share the project working tree. Baton takes a Git tree
snapshot before launch and another after all workers exit. Each attempt diff is
the change between those trees, limited to that task’s scope. Existing dirty
work and earlier accepted attempts are therefore excluded from the new diff.
Binary files, modes, additions, deletions, and unborn repositories are handled
through Git trees. The real Git index is not used as task state.

Git-ignored files are not added to snapshots or diffs and are outside Baton’s
scope guarantee. Workers must not modify them. Capturing them would require
reading ignored dependencies, build outputs, and possible secrets. Tracked Git
submodules in either `HEAD` or the index are rejected rather than silently
omitted. Generated snapshots are also checked for gitlinks.

Any changed Git-visible path outside the union of the wave’s scopes is recorded
on the wave tasks and blocks acceptance. Baton writes those changes to a
separate `attempt-N.violations.diff`. The paths must be restored to the
pre-wave tree before the task can be returned. In a shared parallel working
tree, every worker must declare each exact changed path with repeated
`--changed PATH` arguments to `task finish`. Baton compares each declaration
with that task's observed scoped diff. This detects distinct cross-scope writes
and prevents silent attribution, while the orchestrator still compares each
report with its diff before approval. Shared-tree workers are cooperative, not
hostile-process sandboxes; concurrent writes to the same claimed file cannot be
attributed cryptographically.

Approval records review state only. Worker edits are already in the shared
working tree; `accept`, `return`, and `cancel` do not apply or revert patches.

## Orchestrator phases and handoff

`.baton/baton orchestrator brief --phase start|plan|run|review|close [ID]` is available
only outside a leased worker. The orchestrator runs the matching brief before
task creation, run, review/accept, and session close. Close is invoked as
`.baton/baton orchestrator brief --phase close --goal TEXT [--note TEXT]...
[--avoid TEXT]...`.

- After reading `orchestrator.md`, the coding agent runs `start` internally and
  silently. The user is never asked to install, start, validate, inspect state,
  or run a Baton command. Start remains the single parser for current task state,
  unresolved decisions/reviews, and the latest handoff.

- `start` prints a short role summary and exactly one `Worker routing:` section.
  It validates the project-local Git-ignored configuration and verifies each
  conventional route's executable without running it. Its current safe summary
  uses only validated display metadata or `unlabeled worker`; it never prints
  commands, paths, flags, provider internals, or credentials. When all of `hard`,
  `medium`, and `easy` are valid, it does not ask a question: it says to continue
  with the current settings and remind the user they can change them at any time.

  If any conventional route is missing, incomplete, invalid, or not executable,
  the manual's initial question is exactly: `Which model and reasoning level
  should Baton use for hard, medium, and easy tasks? You can specify each one or
  ask me to derive the settings from the current orchestrator.` Immediately
  around it the manual says exactly: `Ask this as a persistent plain-text question
  that remains visible until answered. Never use a transient form; expiration or
  dismissal is not an answer and must not be treated as selecting any option.`

  The initial question is asked only once. The orchestrator derives settings when
  the user is unsure, asks Baton to choose, or continues without settings. It
  discovers the current harness, model, and reasoning from reliable
  harness-provided context, then uses read-only local config or CLI capability
  checks as needed. It never infers from labels, naming conventions, display
  metadata, or unsupported assumptions. If it cannot verify an executable route,
  it explains what is unknown and requests explicit settings instead of inventing
  one.

  Derived routes use the orchestrator's current model: `hard` uses current
  reasoning, `medium` uses the next lower available level, and `easy` uses the
  lowest. With only two levels, medium and easy share the lower level. If current
  reasoning is already minimum, all three use it. The orchestrator tells the user
  the selected routes and that they can change them at any time.

  Whenever lowering is possible, the orchestrator asks persistent plain-text
  permission before lowering. Omission is not approval. If the user continues
  without permission, all three routes use current reasoning; the orchestrator
  says Baton avoided an unapproved downgrade and reminds the user settings can
  change. Expiration or dismissal of a transient UI is never consent.

  Configuration may be written only after an explicit choice or the defined
  derive/fallback path. Executable commands, profiles, or wrappers must implement
  the stated model, reasoning, and fallback; display metadata alone is
  insufficient. Baton itself remains noninteractive and never invents, selects,
  or writes routes. Start also prints the current handoff when present, task
  counts, unresolved
  decision/review ids, and one recommended next command. Directly beneath the
  ids-only decision line it prints at most two available worker questions,
  flattened to one line, stripped of ANSI and C0/C1 controls, bounded to 160
  characters, and labeled `worker question:`. A decision recommendation always
  names a real task id, never an overflow marker. Under a dedicated handoff leaf
  lock, start atomically marks the handoff consumed without deleting it.
- `plan` first requires choosing and announcing one explicit configured
  difficulty, then prints the remaining task-spec quality checklist and a bounded
  queued/blocked dependency graph.
- `run` uses the read-only wave selection logic to print what would run and
  cautions for overlapping scopes or unmet dependencies. Every selected task is
  shown with id, title, difficulty, and safe worker label.
- `review ID` is valid only for a `needs_review` task. Under the task lock it
  freshly compiles the task capsule but displays the stored launch capsule when
  `attempt-N.brief.md` exists. If the fresh and stored capsules differ, it prints
  one bounded warning that spec or memory inputs drifted and that the launch
  capsule is shown. If fresh compilation fails while a stored launch capsule
  exists, review continues with that stored capsule and one bounded warning that
  includes the compile error; without a stored brief, compilation failure still
  fails the command. The report, result, and attempt diff must each be a regular,
  non-symlink file; an empty diff is valid. The brief prints their paths with the
  first 12 hexadecimal characters of each SHA-256 digest, plus declared and
  observed changed paths. It then prints current-attempt phase-brief command-use
  counts as `Phase briefs: edit=N verify=N report=N`, using zero for an unrecorded
  phase, or `Phase briefs: none recorded` when no valid receipt file exists.
  Receipt coverage is evidence that the command ran, not proof of attention.
  After those artifact lines it streams the diff to print
  an aggregate added/removed stat and at most 10 per-file add/delete/modify,
  binary, mode, or conservative `~` lines using the manifest's observed paths as
  the authoritative file list; an empty diff prints `Diff stat: no changes`.
  Retried tasks also list existing report and diff paths for at most the three
  most recent prior attempts, newest first, with an older-attempt overflow marker.
  The review-only `--include-log-tail` flag appends an explicitly labeled
  `Untrusted worker log tail (opt-in):` block. Baton seeks within only the final
  64 KiB of the current attempt log, sanitizes ANSI/OSC and C0/C1 controls except
  tab, redacts environment/credential values, and emits at most the final 15
  lines, 240 characters per line, and 1500 characters for the whole block. A
  missing or unreadable log says `log tail unavailable`; without the flag no log
  content is printed. The flag is rejected for every non-review phase. A
  post-submission exit warning still names the current attempt log path. The
  brief then prints an accept/return/decide checklist.
  Under the same lock, it builds an evidence manifest containing task id,
  attempt, the displayed capsule SHA-256, report SHA-256, result SHA-256,
  attempt-diff SHA-256, and sorted declared and observed changed-path lists. It
  atomically stores that manifest beside a new `Review token: <value>`; issuing
  another review brief replaces both token and manifest.
- `close` requires an explicit nonblank `--goal TEXT`; it never inherits a goal
  from the preceding handoff. `--avoid TEXT` is optional and repeatable at most
  five times. The goal and each avoid note are flattened with whitespace
  collapsed, ANSI and C0/C1 controls removed, and output bounded to 200
  characters. More than five avoid notes is an error that tells the caller to
  consolidate them. Close-only `--note TEXT` is repeatable at most three times;
  a fourth value is rejected before any handoff lock. Each value is flattened by
  `flatten_bounded_text(value, 160)`, sanitized blanks are omitted, and exact
  sanitized duplicates are deduplicated in first-occurrence order. `--goal`,
  `--avoid`, and `--note` are rejected for every non-close phase, and a missing
  or sanitized-to-blank goal is rejected with an error that says to add
  `--goal TEXT`.

  After all flag validation and task loading, but before the handoff lock, close
  runs the direct non-shell command `git -C ROOT status --porcelain=v1
  --untracked-files=normal --ignore-submodules=none` with the inherited
  environment except `GIT_OPTIONAL_LOCKS=0` and a five-second timeout. A zero
  exit with no output is clean, a zero exit with any output is dirty, and a
  nonzero exit, timeout, decode error, or launch error is unavailable. Dirty
  emits exactly `warning: uncommitted Git-visible changes at close`; unavailable
  emits exactly `warning: working-tree check unavailable at close`; clean emits
  no warning. Git output and paths are never copied into the handoff.

  Active and archived task snapshots and their acceptance/decision candidate
  tuples are also gathered before the dedicated `orchestrator-handoff` leaf
  lock. The lock covers only the previous-handoff read, boundary/id dedupe,
  rendering, and atomic write; no task or scheduler lock is acquired beneath it.
  A `done` entry is included only for a matched `accepted` history event at or
  after (`>=`) the preceding `generated_at`, excluding ids already in the
  preceding `done` section so same-second closes emit each id once. Its value
  keeps the `ID: title` prefix and appends ` — outcome: TEXT` only when that exact
  event's `note` flattens nonblank via `flatten_bounded_text(note, 120)`. No other
  history event, report, or log is an outcome source. The title budget is
  allocated before concatenation so the complete done value remains at most 240
  characters.

  The handoff field order is exactly the header, `generated_at`, `consumed_at`,
  `goal`, the optional warning, `done`, `decisions`, `next`, `unresolved`, the
  optional `notes`, and `avoid`. `notes:` is present only for nonempty sanitized
  notes and never has a placeholder. With no nonblank avoid notes, `avoid:` has
  one `(fill in)` line. List sections initially show at most eight values plus an
  accurate `+N more` line.

  The complete serialized handoff, including its final newline, is at most 4000
  characters. If the initial rendering is larger, close re-renders with every
  outcome suffix omitted. If still larger, it repeatedly lowers the displayed
  done cap and then the decisions cap by one per pass, maintaining accurate
  `+N more` counts, until the artifact fits or both caps reach zero. Only then it
  applies the same per-pass reduction to next and then unresolved. Metadata,
  goal, warning, notes, and avoid are never naively truncated or dropped. Close
  atomically writes `.baton/orchestrator-handoff.md`, prints it, and
  then prints one deterministic runtime-wide worker-usage sentence and reminds
  the user to start a fresh coding-agent session and tell it to read
  `.baton/orchestrator.md`. It does not expose or ask the user to run the internal
  start command. The sentence counts every recorded `launched` history event in
  active and archived tasks, so retries count as separate worker processes. It
  states the total, hard/medium/easy counts with
  correct singular/plural grammar, and an other-level count when any default,
  custom, missing, or malformed tier value was used. Non-object history entries,
  entries whose event is not exactly `launched`, and non-list histories are
  ignored. The sentence says `for this Baton runtime so far` because state does
  not prove that all launches belong to one user request. It is continuity and
  audit evidence only and must never be presented as a request-scoped count. The
  8000-character handoff reader and hook-output cap are unchanged.

With the default accept gate enabled, `task accept --brief TOKEN` requires the
stored token for that task's current attempt. Under the task lock and before any
state change, it recomputes the manifest using the capsule the review brief would
display and requires full equality with the stored manifest. A mismatch is
rejected with `review evidence changed; run a fresh review brief` without
consuming the token. Equality proceeds to acceptance and one-use token
consumption. Missing, wrong, replaced, replayed, or stale-attempt tokens are
rejected. Return, decide, and cancel remove any review token and manifest. With
the gate disabled, accept bypasses the manifest exactly as it bypasses `--brief`.

`.baton/baton task create`, list/show output, `.baton/baton status`, dry-run
selection, and real launch output all expose task id, title where appropriate,
explicit difficulty, and a safe effective worker label. JSON list/show enriches
the displayed copy with `difficulty`, `worker_label`, and a safe `routing` string;
these derived fields are never stored in task state. `.baton/baton status`,
`.baton/baton task show`, and each real `.baton/baton run` end with a
deterministic `Next actions` block whose heading is followed by at most five
content lines globally across reviews, decisions, and overflow markers. Reviews
come first, followed by one line per displayed decision; overflow markers remain
within the same budget. Each available decision question is flattened to one
line, stripped of ANSI and C0/C1 controls, bounded to 160 characters, and
explicitly labeled `worker question:`; a missing or non-text question leaves an
id-only decision line. The block otherwise derives report paths or create/run
commands from current task state and contains no generic advice.

`.baton/baton stats` is orchestrator-only and read-only. Without `--task`, it is
an aggregate over active and archived task state and work. With no task state it
prints exactly `no task data`. Otherwise it prints deterministic bounded
sections for status counts, a histogram of tasks' current attempt numbers,
failed/blocked reason-code counts,
launched-capsule character sizes (minimum, lower-middle median for an even count,
and maximum), per-phase receipt coverage, and a post-submission warning count.
Status and count entries are sorted; variable count sections show at most 12
entries and aggregate overflow as `other`.

Failure/blocked reasons come from corresponding `worker_exited` history notes,
falling back to current `last_note` when no such history exists. Only complete
notes matching `^[a-z_]+(_\d+)?$` are printed as codes; every other value counts
as `other`, so stats never emits worker free text, logs, tokens, or secrets.
Capsule sizes come only from integer `capsule_chars` values on `launched` entries.
Receipt coverage counts attempts represented by a launch or a valid receipt and
is labeled `command-use evidence, not proof of attention`. Stats reads archived
receipts from the work directories moved by `archive`; it acquires no
write-capable lock and creates or changes no runtime file.

One or more repeatable `.baton/baton stats --task ID` values switch the command
to request-scoped worker accounting. Before printing anything, Baton validates
each id, deduplicates repeated ids in first-seen order, resolves tasks across
active and archived state, and rejects an unknown id. It then prints exactly one
copy-ready sentence: `I used N worker(s) for this request: H on hard, M on
medium, and E on easy.`, adding `and O on other levels` only when needed. Every
recorded `launched` event counts, so retries are separate workers. Default,
custom, missing, non-text, and otherwise non-conventional tiers count as other;
commands, paths, flags, provider internals, history free text, and credentials
are never printed. No-filter aggregate output is byte-unchanged. The
orchestrator passes every unique task id created for the completed user request
and copies the sentence into its final response. When a request created no Baton
task, it states the explicit zero sentence instead of inventing an id. The
runtime-wide close sentence is never substituted for this request boundary.

`.baton/baton tiers` is orchestrator-only and read-only. It prints one deterministic
block for each explicitly configured tier in sorted name order. Each
block shows the difficulty and bounded safe worker label, then only the command
executable (`argv[0]`, never its flags or remaining arguments), whether the
command comes from the global command or the tier, the effective worker timeout in minutes,
and the effective capsule budget in characters. If any conventional `hard`,
`medium`, or `easy` route is missing or invalid,
it appends exactly one line `Conventional levels missing: <comma-separated
names>` in hard, medium, easy order. The line is omitted when all three are
valid; every tier block is otherwise byte-unchanged. With no configured tiers it
prints `No worker tiers configured.` before the missing-level line.

## Optional Claude Code integration

`.baton/baton hooks claude-code [--write]` is an orchestrator-only, opt-in setup
command. Without `--write` it prints the exact JSON hook fragment and one-line
merge instructions. With `--write` it atomically creates or merges
`.claude/settings.json`, preserving the order and contents of existing hook
arrays. It appends only missing Baton entries, identified by their
command strings, so repeated setup is idempotent. Invalid JSON is rejected
without changing the file.

The fragment registers exactly one command hook under each of `SessionStart`
and `UserPromptSubmit`, using Claude Code's matcher-free `[{"hooks": [...]}]`
shape. Commands invoke the project-local adapter through
`"$CLAUDE_PROJECT_DIR"/.baton/baton hook-event ...`.

The matcher-free `SessionStart` entry fires at startup and after automatic or
manual compaction; post-compaction stdin carries `"source": "compact"`. Claude
adds SessionStart stdout back to session context. `.baton/baton hook-event
session-start` emits the same state and route-validity result as the start-phase
orchestrator brief. Valid routes therefore remain silent apart from their safe
reminder, while missing or invalid routes require onboarding. For a compact
source it prefixes the brief with the single line
`Baton: context was compacted; state re-injected below.` so the new
context is explicitly re-grounded. PreCompact stdout does not reach Claude's
summarizer or the resulting context, so this integration intentionally has no
PreCompact hook and re-injects state through the post-compaction SessionStart.

`.baton/baton hook-event user-prompt-submit` emits `hookSpecificOutput` JSON whose
`hookEventName` is `UserPromptSubmit` and whose `additionalContext` is a
`Baton state:` line followed by the deterministic `Next actions`
block. Both emitted outputs are always at most 9000 characters. Truncation
retains the first and last lines and places `(truncated)` immediately before the
last line; if both edge lines cannot fit, the adapter emits nothing and fails
open.

Hook events tolerate empty or malformed stdin, never write Baton state, and
fail open: a missing or broken runtime exits successfully with no stdout or
stderr, so Baton cannot break the host session. The adapter and setup commands
are unavailable to leased workers. Claude's `--bare` mode disables hooks and
therefore conflicts with this integration.

## Worker process

The command in `config.toml` is parsed into arguments and launched without a
shell. It must contain exactly one complete `{prompt}` or `{prompt_file}`
argument. Shell operators are not supported; users can call a wrapper script
when needed.

The worker's standard input is `/dev/null`. Workers are non-interactive and must never block on input.

Baton exports:

```text
BATON_TASK_ID
BATON_ATTEMPT
BATON_LEASE
BATON_DIR
BATON_ROOT
```

The launch prompt explicitly states the validated task difficulty and safe worker
label without including the worker command or flags. Workers reprint the current attempt's launch capsule at each moment of action
with `.baton/baton task brief ID --phase edit|verify|report`. The capsule is followed by
a short phase-specific checklist. The command is available only to the matching
leased worker while its task is running: task id, attempt, and lease must all
match the worker environment.

Under the existing task lock, every successful phase brief atomically records
bounded command-use evidence in `work/<id>/attempt-N.briefs.json`. The record
contains only task id, attempt, lease, the stored capsule's `sha256:` digest, and
at most one `edit`, `verify`, and `report` entry. Each phase entry has exactly
`first_at`, `last_at`, and `count`; repetition preserves `first_at` and updates
only `last_at` and `count`, so no receipt file accumulates entries, text, logs,
tokens, or reusable secrets. A missing, malformed, stale-lease, or otherwise
invalid receipt is treated as empty and replaced on the next successful brief,
never allowed to crash that brief.

The report phase also prints `Brief token: <value>` and atomically stores the
token under `work/<id>/`, bound to the current task id, attempt, and lease. A
second report brief replaces it. Edit and verify briefs do not issue tokens.
With the default finish gate enabled, `task finish` requires the current token
as `--brief TOKEN` and consumes it after successfully writing the result. A
missing, wrong, replaced, replayed, different-attempt, or different-lease token
is rejected with instructions to run a fresh report brief.

The optional `phase_sequence_requires_briefs` gate defaults to false. Recording
always occurs, and while the gate is off edit, verify, and report retain their
existing non-blocking behavior. When true, a verify brief requires a
current-attempt edit receipt, and a report brief requires current-attempt edit
and verify receipts; rejection names the exact missing `.baton/baton task brief ID
--phase PHASE` remediation. A new edit receipt after a report receipt removes an
outstanding finish token, requiring report to be briefed again. This is the only
sequence-gate change to finish-token one-use semantics.

For `needs_review`, a separate default-on report gate reads the report as UTF-8
after finish-token identity checks and before writing the result or consuming the
token. It requires the exact level-2 headings `Result`, `Changes`,
`Verification`, and `Decisions and risks` outside fences opened by up to three
leading spaces and three or more backticks or tildes. A fence closes only with up
to three leading spaces and at least the opening run's length of the same
character. Result, Changes, and Verification bodies must be nonblank, and Result's
first nonblank body line must exactly equal the submitted status. Missing,
non-regular, undecodable, or malformed reports are rejected with a precise
problem and instructions to correct the report and refinish with the same token.
`needs_decision`, `blocked`, and `failed` reports are never structure-checked.

Each worker runs in a separate process group. Baton captures combined output and
enforces the task tier's effective `worker_timeout_minutes`, so workers in one
wave may have different timeouts. It terminates process groups on timeout or
`SIGINT`, `SIGTERM`, or `SIGHUP` interruption. It signals every active group
before one shared grace interval. Interrupted tasks become `failed` instead of
remaining stale. Timeout, interruption, and runner/launch errors override any
submitted result. For an ordinary nonzero exit, Baton first applies every normal
result check, including report, scope, and changed-path validation. Invalid
submissions retain their normal failure, while a fully valid submission retains
its worker status with a structured `worker_exit_N_after_submission` warning in
task state, status output, history, and the review brief.

Before launch, Baton compiles a deterministic Critical Context Capsule from the
task state, the existing `Objective`, `Acceptance criteria`, `Not allowed`,
`Verification`, and retry sections, and the memory index at compile time. The
same task state, spec text, and memory index produce byte-identical capsule text.
The task spec remains the only hand-edited task source; there is no capsule
section in the task template. Empty objectives or acceptance criteria, and
either section retaining its template placeholder line, are actionable launch
errors. `validate` reports the same errors for queued tasks.

Baton scans only the spec's `Context` section for ordered, deduplicated
`M\d{3,}` references. When any exist, the capsule includes `Referenced memory`
after `Verification` and before `Retry delta`. It contains one instruction to
load full entries with
`python3 .baton/baton memory show ID` and one
`- M###: summary` line per reference. It never includes full memory bodies.
With no references this section is absent and the capsule format is unchanged.
More than six distinct references is an error that directs the orchestrator to
split the task or remove references. A missing id or an orchestrator-only `[O]`
reference is also an error; worker capsules accept only `[W]` and `[B]` entries.
Compilation, launch, prospective preview, and queued-task validation report
these errors without dropping or truncating references.

On attempts after the first, the capsule also contains a `Retry delta` with only
the newest entry from `Review feedback` and/or `Decisions`. The previous-attempt
report remains a file pointer in the middle of the prompt. Baton places the
byte-identical capsule at the exact beginning and end of the launch prompt,
around the task metadata, file pointers, and finish mechanics. It also writes
that capsule to `work/<id>/attempt-N.brief.md` with its SHA-256 content digest.
This immutable launch snapshot is the audit record; worker phase briefs reread
it without consulting mutable spec or memory text.

Prepared worker items retain that capsule digest and Unicode character count.
Only after every preparation succeeds, the existing task-claim block records
the integer count as `capsule_chars` on the `launched` history entry. Failed
preparation therefore records no launch.

Capsules are never truncated. The budget resolves from each task's tier. If one
exceeds the tier's effective `capsule_max_chars`, launch and validation fail
with the measured size and overflow.

`.baton/baton task capsule ID [--raw]` is a read-only, orchestrator-only preview. For a
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
# worker = "agent-cli run {prompt_file}"

[limits]
max_parallel = 3
capsule_max_chars = 4000
worker_timeout_minutes = 60

# [tiers.category]
# command = "agent-cli run {prompt_file}"
# capsule_max_chars = 6000
# worker_timeout_minutes = 90
# [tiers.category.display]
# model = "<selected model>"
# harness = "<local CLI>"
# effort = "<selected reasoning level>"

[gates]
phase_sequence_requires_briefs = false
finish_requires_brief = true
report_requires_sections = true
accept_requires_brief = true
```

The installed file has no active worker command, tier, model, provider,
reasoning, or fallback. Commented examples explain only generic syntax and do
not prescribe named models or Baton routes. Worker commands are project-local;
any desired harness-memory isolation must be implemented by that command or its
wrapper rather than assumed by Baton.

`max_parallel` and `capsule_max_chars` are positive integers. The capsule limit
defaults to 4000 characters. The timeout is a finite non-negative number in
minutes; zero disables it.

All gate values must be booleans. `phase_sequence_requires_briefs` defaults to
`false` when absent; the other three gates default to `true`.
`finish_requires_brief = false` lets `task finish` work without a token;
`report_requires_sections = false` restores free-form non-empty review reports;
`accept_requires_brief = false` lets `task accept` work without a review token.
Each disabled token gate ignores its corresponding `--brief` argument.

Every task tier must have a matching `[tiers.<name>]` table. Unknown and blank
tier names are errors at creation, validation, preview, and launch rather than
falling back to another command. `default` is not a task route, and
`task create --tier default` fails. A literal `[tiers.default]` table is reserved
and invalid.

Each tier table may set `command`, `worker_timeout_minutes`, `capsule_max_chars`,
and a `[tiers.<name>.display]` table. Display supports only `model`, `harness`,
`effort`, `engineering_role`, and `fallback`. Every configured value is nonblank
text of at most 80 characters, contains no C0/C1 controls or command flags, and is
credential-redacted before rendering. Unknown fields and malformed display
tables are errors. The assembled label is single-line and at most 240 characters;
missing metadata deterministically renders `unlabeled worker`. Unset routing keys inherit their global values, so a limits-only
tier inherits `[commands].worker` only when that command is explicitly configured.
Tier commands obey the same one-placeholder rule. Tier limits obey the same type and range rules as
their global counterparts. `validate` checks every configured tier, including
unused tiers. `.baton/baton tiers` displays all effective settings without exposing
command flags. Display metadata never changes routing: worker argv, timeout, and
capsule budget still come from the task's strictly validated tier, and Baton never
infers display values by parsing command strings. `hard`, `medium`, and `easy` have no special validation or
scheduling semantics: they are ordinary strict opt-in tiers whose names are
used only by the onboarding section and missing-level hint.

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
.baton/baton memory index [--for worker|orchestrator]
.baton/baton memory show M001
.baton/baton memory add --for worker|orchestrator|both "summary" "body"
```

## Verification

`python3 tests/test_baton.py` must pass. The suite uses temporary Git projects
and stub workers. It covers:

- Git-only, idempotent initialization, root checks, nested symlink rejection,
  and submodule rejection from `HEAD`, the index, and snapshots;
- monotonic task creation, path validation, dependencies, cycles, and archived
  dependencies;
- scope normalization, case-folded overlap, scope-violation blocking, and
  changed-path attribution;
- parallel workers, serialized run processes, leases, and duplicate-claim
  prevention;
- worker and orchestrator phase briefs, bounded receipts, optional phase order,
  finish/review tokens and gates, structured
  review-report validation (including fences, CRLF, unreadable files, status
  matching, retry with the same token, and gate bypasses), handoff, stdout
  next-action capsules, worker results, lifecycle guards, return, decide, and
  accept;
- attempt-local Git diffs in clean, dirty, and unborn worktrees;
- direct command execution without a shell;
- per-tier process-group timeouts, batched `SIGINT`/`SIGTERM` cleanup, and
  non-UTF-8 output handling;
- strict tier validation and read-only tier settings, aggregate stats, memory,
  receipt-preserving all-or-nothing archive preflight, and signal-safe archive
  completion;
- exact equality between this specification and the contract embedded in the
  standalone creation prompt.
