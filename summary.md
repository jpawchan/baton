# Baton project guide

## What and why

Baton is a standard-library Python CLI for delegating scoped coding tasks to separate agent processes. One orchestrator creates tasks, runs dependency-ready workers in parallel waves, and reviews each report and Git diff.

A generated Critical Context Capsule appears at both edges of every worker prompt; action-time briefs gate `task finish` and `task accept` with one-use tokens; orchestrator sessions receive phase briefs and a bounded state handoff; and optional Claude Code hooks restore current state after compaction. The design is grounded, with explicit limits, in `docs/research-synthesis.md` and `docs/context-placement.md`; its revision-specific activation cost and direct-execution break-even are measured in `docs/context-footprint.md`.

Baton coordinates external worker CLIs. It is not an agent model, package manager, patch queue, or security sandbox.

## Current state

- The hardening release is commit `eeb6894` (`feat: harden Baton lifecycle and archive durability`) on `main` in `https://github.com/jpawchan/baton`; its GitHub CI passed on Ubuntu and macOS with Python 3.11 and 3.13.
- The current CLI includes generated dual-edge capsules, phase receipts and one-use gates, strict difficulty tiers, read-only statistics, compaction-aware Claude Code hooks, identity-based bounded handoffs, immutable review evidence, and crash-recoverable archival.
- The hardening release serializes ID allocation and accept/archive transitions, publishes retry context before re-queueing, binds results to launch/exit history with SHA-256, tracks handoff identities with a version-3 cursor, reserves handoff consumption space, and uses a durable archive journal plus native atomic no-replace moves.
- Malformed-input, concurrency, interrupted-publication, result-integrity, and archive crash/race behavior have regression coverage. The performance changes and measured limits are recorded in `docs/performance.md`.
- Run the end-to-end suite rather than relying on a point-in-time test count. The release passed the full local and independent suites, focused adversarial regressions, context tests, benchmarks, and the four-job GitHub matrix. The expected `[T001-lease-guard] stale finalizer ignored` diagnostic is not a failure. A framework-owned `baton orchestrate` process remains deliberately out of scope.
- The repository's live, Git-ignored `.baton/` directory is dogfooding state and audit history, not project source; do not edit or delete it casually.

## Run and verify

Requirements: Python 3.11+, Git on `PATH`, macOS or Linux. No dependency install, build step, server, or database exists.

```bash
cd <repo-root>
python3 framework/baton --help
python3 -m py_compile framework/baton tests/test_baton.py tools/measure_context.py tests/test_context_footprint.py
python3 tests/test_baton.py
python3 tests/test_context_footprint.py
python3 tools/benchmark_performance.py --repo . --source framework/baton --samples 3 --skip-suite --output /tmp/baton-performance.json
git diff --check
```

Expected: the help usage line includes `stats` and `tiers`; py_compile is silent; both unittest summaries end in `OK`; the benchmark writes JSON with `context`, `benchmarks`, and `hot_paths` results for comparison with `docs/performance.md`; `git diff --check` is silent. The expected `[T001-lease-guard] stale finalizer ignored` probe diagnostic may follow the primary suite (temp Git repos and stub workers, no network or live agent calls).

The executable-shebang tests invoke `python3` through `#!/usr/bin/env`. If the host's default `python3` is older than 3.11, create a temporary `python3` shim pointing at the interpreter under test and prepend it to `PATH`; do not edit the tracked shebang merely to accommodate the test host.

If you run the suite from inside a Baton-leased worker process, unset the inherited worker env first or fixtures will reject orchestrator commands:

```bash
env -u BATON_TASK_ID -u BATON_ATTEMPT -u BATON_LEASE -u BATON_DIR -u BATON_ROOT python3 tests/test_baton.py
```

Disposable end-to-end smoke (verified this session). The `cd "$tmp"` matters:
runtime discovery is `BATON_DIR` env first, else a walk UP from the current
directory — running a temp project's `baton` from inside this repo would
silently target this repo's own runtime instead.

```bash
tmp=$(mktemp -d) && git -C "$tmp" init -q && git -C "$tmp" config user.name T && git -C "$tmp" config user.email t@example.invalid
echo seed > "$tmp/seed.txt" && git -C "$tmp" add -A && git -C "$tmp" commit -qm seed
./framework/baton init "$tmp"
(cd "$tmp" && .baton/baton orchestrator brief --phase start && .baton/baton validate)
rm -rf "$tmp"
```

Expected: init directs the coding agent to the orchestrator manual; the start
brief prints the orchestrator role, a `Worker routing:` section whose fresh
install requires onboarding, and current task state; validate prints
`ok: 0 active task(s)` even though no worker route is predefined.
Do not smoke-test a real worker unless the configured worker CLI and its credentials work locally.

## Stack

| Layer | Verified implementation |
| --- | --- |
| Language | Python 3.11+; the entire production CLI is the single file `framework/baton`. |
| Dependencies | Python standard library only; no manifest or lockfile exists. |
| CLI | `argparse` subcommands built in `build_parser()`. |
| Concurrency | `ThreadPoolExecutor` launches one wave of worker subprocesses; POSIX `fcntl.flock` locks; `secrets.token_hex` for gate tokens. |
| Processes | `subprocess.Popen(..., start_new_session=True)`; process-group signalling on timeout/interrupt. |
| Configuration | TOML via `tomllib`; runtime state is JSON records plus Markdown specs/reports/briefs/handoff. |
| Version control | Git CLI snapshots with a temporary `GIT_INDEX_FILE`; no Git library. |
| Tests | `unittest` end-to-end cases in `tests/test_baton.py` with temp repos and embedded stub workers. |
| CI | `.github/workflows/ci.yml`: push+PR, Ubuntu/macOS × Python 3.11/3.13, `checkout@v7`, `setup-python@v6`, 10-minute timeout. |
| License | MIT (`LICENSE`). |

Baton itself makes no HTTP requests. Explicit project-local tier commands are
the only connections to agent CLIs.

## Repository map

| Path | Role |
| --- | --- |
| `framework/baton` | Entire production CLI: paths, config, capsule compiler, tasks, briefs/tokens, scopes, Git snapshots, runner, handoff, hooks, validation, archive, memory, parser. |
| `framework/orchestrator.md` | Orchestrator manual: phase briefs, task creation, waves, token-gated review, handoff, failure handling, memory. |
| `framework/worker.md` | Worker contract: capsule re-reads, phase briefs, scope rules, report shape, token-gated finish. |
| `framework/config.example.toml` | Unconfigured worker-routing template plus limits and gates; copied to runtime `config.toml` on init. |
| `framework/memory.md` | Empty indexed-memory template copied on first initialization. |
| `tests/test_baton.py` | Canonical end-to-end suite and stub workers for lifecycle, concurrency, retry publication, immutable evidence, handoff recovery, and transactional archive behavior. |
| `SPEC.md` | Normative behavioral contract; embedded byte-identically in `prompts/create-framework.md`. |
| `prompts/create-framework.md` | Standalone generation prompt with the embedded exact SPEC copy (BEGIN SPEC / END SPEC markers). |
| `prompts/improve-framework.md` | Review prompt naming required v1 safety and v2 capsule/token/handoff/hook checks. |
| `prompts/use-framework.md` | Short instruction that activates an installed orchestrator (read manual → start brief → memory choices). |
| `skill/SKILL.md` | Portable skill metadata, install command, invariants. |
| `docs/context-placement.md` | Research rationale, linked sources, rejected alternatives, limits, and experiment requirements for capsule edge placement. |
| `docs/research-synthesis.md` | Primary-source-grounded long-context synthesis, claim mapping, and limits. |
| `docs/context-footprint.md` | Reproducible activation footprint, provider differentials, and break-even guidance. |
| `docs/performance.md` | Profiling method, benchmark evidence, and rejected optimizations. |
| `docs/github-description.txt` | Short public repository description. |
| `tools/` | Context-measurement scripts and the 500-active + 500-archived-task performance fixture, including valid finalized review evidence and 100 Git-visible changes. |
| `tests/test_context_footprint.py` | Activation-footprint reproducibility checks. |
| `README.md` | Public explanation, evidence, requirements, install, usage, and repository map. |
| `summary.md` | This guide. |
| `.github/workflows/ci.yml` | Only CI workflow. |

### Code regions in `framework/baton` (by function, top to bottom)

| Concern | Start here |
| --- | --- |
| Runtime discovery, safety | `find_baton_dir`, `runtime_paths_are_safe`, `require_baton_dir`; `BATON_DIRNAME = ".baton"`. |
| Locks and atomic state | `file_lock`, `task_lock`, `atomic_write`, `atomic_json`, directory fsync, and `lock_path`; writer order is scheduler before task/handoff. |
| Config | `load_config`, `cfg_get`, the `configured_*` readers (including `configured_tier` and the default-off phase-sequence gate), `validate_worker_template`, `validate_worker_executable`, `command_template`, `worker_argv`; route onboarding via `conventional_level_names` + `worker_routing_lines`. |
| Paths and review evidence | `report_path`, `result_path`, `diff_path`, `sha256_regular_file`, `review_result_schema_problems`, `review_lifecycle_details`, `review_evidence_details`, `build_review_evidence_manifest`, streaming `attempt_diff_summary`, bounded log sanitization, one-use token paths, and phase receipts. |
| Capsule | `CAPSULE_SECTIONS`, `task_spec_sections`, `memory_index_entries`, `context_capsule_components` + `compile_context_capsule` (deterministic, budgeted, placeholder- and memory-reference-validating), `stored_context_capsule_components` (launch-snapshot parsing), `report_section_problems` (report gate parser). |
| Task lifecycle commands | `cmd_task_create` (scheduler-serialized ID allocation), `cmd_task_list/show`, `cmd_task_capsule`, `cmd_task_accept` (manifest gate under scheduler + task locks), `publish_retry_context` + `cmd_task_return/decide`, `cmd_task_cancel`, `cmd_task_finish`, `cmd_task_brief`, `cmd_task_unlock`. |
| Next-actions capsule | `flatten_bounded_text`, `decision_question`, `render_next_actions`, `say_next_actions` (tails `status`, `task show`, real `run`; globally budgets five review/decision/overflow lines). |
| Orchestrator briefs | `read_handoff_cursor`, `handoff_structure`, `consume_handoff_content`, `render_handoff`, `orchestrator_start_brief`, `orchestrator_review_brief`, and `orchestrator_close_brief`; version-3 cursor state is canonical and new handoffs reserve timestamp-growth space. |
| Claude Code hooks | `claude_code_hook_fragment`, `cmd_hooks_claude_code` (print/merge, idempotent), `cap_hook_output` (hard 9000-char cap, fail-open), `claude_user_prompt_output`, `cmd_hook_event`. |
| Git snapshots and scopes | `git_snapshot`, `git_changed_paths`, `git_tree_diff`, `normalize_scope`, `scopes_overlap`, `ScopeOverlapIndex`, and `path_in_scopes`. |
| Worker launch and waves | `WORKER_PROMPT`, `build_prompt`, `prepare_worker`, `run_one_worker`, indexed `pick_wave`, `finalize_task` (result digest + lifecycle record), `cmd_run`, `run_wave`. |
| Validation and transactional archive | `task_problems`, `archive_layout_problems`, strict journal builders/readers/topology checks, `atomic_archive_rename_no_replace`, transaction completion/recovery/rollback, `cmd_validate`, and `cmd_archive`. |
| Tiers, stats, memory, CLI | Read-only `cmd_tiers`/`cmd_stats`, `cmd_memory_*`, `cmd_init`, `build_parser`, `main`. |

## How it works

Runtime layout after `.baton/baton init <git-root>` (all Git-ignored):

```text
<git-root>/.baton/
├── baton, orchestrator.md, worker.md, memory.md, config.toml
├── orchestrator-handoff.md      written by close brief, consumed by start brief
├── orchestrator-handoff-cursor.json  canonical handoff + reported acceptance IDs
├── archive-transaction.json     present only during/recovery of archive transaction
├── tasks/<id>.json + <id>.md    state records and hand-edited specs
├── work/<id>/attempt-N.{prompt.md,brief.md,briefs.json,log,report.md,result.json,diff}
│   └── {finish,review}-brief-token.json   one-use gate tokens
├── archive/                     done/cancelled tasks
└── .locks/                      scheduler, execution, memory, per-task, orchestrator-handoff
```

End-to-end flow with the v2 edge mechanisms marked:

```text
orchestrator brief --phase start      <- beginning edge: role + route validity/onboarding + handoff + task state
   | task create -> edit spec (Objective/Acceptance criteria/... are the capsule source)
   v
run: pick_wave -> prepare_worker compiles capsule
   |   launch prompt = CAPSULE + mechanics + CAPSULE   <- both worker edges
   v
worker: task brief --phase edit|verify|report          <- bounded receipts; report issues token
   |    task finish --brief TOKEN                      <- token + needs_review report-shape gates (default on)
   v
finalize: hash result, bind it to launch/exit history, diff wave snapshot, scope check
   v
orchestrator brief --phase review ID -> diff stat/history + token/evidence manifest <- decision edge
   |    task accept --brief TOKEN verifies evidence               <- gate (default on)
   v
status/show/run output ends with "Next actions:"       <- recency edge, any harness
stats --task ID [--task ID]... -> request-scoped worker count <- final response edge
orchestrator brief --phase close --goal TEXT [--note TEXT]... [--avoid TEXT]... -> handoff + runtime count <- next session edge
```

Statuses: `queued → running → needs_review → done`, or `needs_decision`/`blocked`/`failed → queued` (after decide/repair/return). Workers can submit only the four `WORKER_FINAL` statuses; only `task accept` records `done`.
Scope enforcement, temp-index Git snapshots, leases, and conservative overlap semantics remain inherited from v1: every changed path outside the wave's scopes blocks the wave; declared `--changed` paths must equal the observed scoped diff case-insensitively. Archive semantics are now strict, journaled, durable, and crash-recoverable.

Claude Code integration (opt-in): `.baton/baton hooks claude-code [--write]` prints or merges two matcher-free hooks into the project's `.claude/settings.json` — SessionStart runs `hook-event session-start` (start brief as stdout → session context, including explicit state re-injection after automatic or manual compaction with the same route-validity behavior) and UserPromptSubmit runs `hook-event user-prompt-submit` (JSON `additionalContext` with the Next-actions capsule). Both cap output at 9000 chars and emit nothing (exit 0) on any error.

## Debugging baseline from the hardening release

Read this before opening a concurrency, durability, handoff, retry, or review-integrity investigation. It records what commit `eeb6894` already diagnosed and fixed; rerun the named regressions before assuming the same symptom has a new cause.

| Prior failure mode | Root cause and completed fix | Start here when debugging |
| --- | --- | --- |
| Concurrent task creation could reuse an ID or observe inconsistent active/archive snapshots. | `cmd_task_create` now holds `scheduler.lock`, loads active and archived state once, allocates from that snapshot, and writes state/spec before release. | `cmd_task_create`, `next_task_id`; concurrent-create tests and `test_task_create_loads_active_and_archive_once`. |
| A returned or decided task could become runnable before feedback/answer publication; Markdown fences or mixed line endings could misroute or duplicate text. | `publish_retry_context` writes a section-bound SHA-256 proof and verifies it before state becomes `queued`; its scanner handles CommonMark fences plus LF, CRLF, and bare CR without rewriting unrelated bytes. | `task_spec_unfenced_lines`, `task_spec_has_retry_publication`, `publish_retry_context`; search tests for `retry_publication`, `return_*publication`, and `decide_*publication`. |
| Review acceptance trusted mutable or forged result data after worker finalization. | Finalization hashes the regular result file and records `result_digest`; review binds exact schema, lease, attempt, timestamps, status, note, paths, launch, and worker exit. Review tokens include report/result/diff/capsule digests and reject evidence drift. | `review_result_schema_problems`, `review_lifecycle_details`, `review_evidence_details`, `build_review_evidence_manifest`, `finalize_task`; search tests for `review_result`, `review_evidence`, `post_finalization`, and `validate_*review`. |
| `task accept` and `archive` could race between the done-state write and review-token removal. | Both transitions serialize under `scheduler.lock`; accept then takes the task lock, verifies the fresh manifest, writes `done`, and removes the token before archive can snapshot eligibility. | `cmd_task_accept`, `cmd_archive`, `test_accept_serializes_done_write_and_token_consumption_with_archive`. |
| Timestamp-only handoff tracking could duplicate or omit acceptances under same-second events, clock rollback, bounded done lists, or interrupted two-file publication. | Close uses a version-3 cursor with the full `reported_ids` set and canonical handoff, writes cursor first, recovers presentation drift, and migrates strict v1/v2 cursors. | `read_handoff_cursor`, `handoff_presentations_match`, `orchestrator_close_brief`; search tests for `handoff_same_second`, `handoff_cursor`, `clock_rollback`, `crash_recovery`, and `consumption`. |
| A valid 3,990-character close handoff grew beyond the 4,000-character reader budget when `consumed_at` replaced `(not consumed)`. | New handoffs stop at 3,989 characters, reserving worst-case timestamp growth. `consume_handoff_content` deterministically compacts legacy near-limit prose while preserving structure and every task ID. | `handoff_structure`, `consume_handoff_content`, `render_handoff`, `test_handoff_consumption_reserves_and_compacts_every_legacy_boundary`. |
| Archive crashes could leave split state; rollback was incomplete; plain `os.rename` could silently replace a destination created after preflight. | Archive writes/fsyncs a strict journal, masks termination signals, completes or rolls back durable boundaries, recovers on the next call, and uses Linux `renameat2(RENAME_NOREPLACE)` or macOS `renamex_np(RENAME_EXCL)` for forward and rollback moves. | `archive_layout_problems`, transaction build/complete/recover/rollback helpers, `atomic_archive_rename_no_replace`; search tests for `archive_recovery`, `archive_rollback`, `archive_move_boundary`, and `atomic_archive_rename`. |
| Large task sets made scope scheduling and repeated state reads unnecessarily expensive. | `ScopeOverlapIndex` indexes literal prefixes conservatively, task creation reuses one active/archive load, and the benchmark fixture includes valid archived evidence so `validate` measures real work. | `ScopeOverlapIndex`, `pick_wave`, `cmd_task_create`, `tools/benchmark_performance.py`; `test_scope_overlap_index_matches_conservative_overlap_rules`, `test_task_create_loads_active_and_archive_once`, and `test_performance_fixture_has_valid_finalized_review_evidence`. |

A fresh 2026-07-17 WSL2/Python 3.11.4 run of the current three-sample fixture measured medians of 0.0183 s for 4,000 distinct scheduler scopes, 0.0630 s for one 500-active + 500-archived state pass, 0.0815 s for task creation, and 0.5880 s for large validation. These are hardware-sensitive baselines, not hard thresholds. The older table in `docs/performance.md` predates the final realistic archived-evidence fixture; compare workload and JSON shape before calling a timing change a regression.

The final independent review initially blocked the release on exactly two reproduced defects: handoff consumption overflow and archive destination replacement at the move boundary. Their fixes are the 3,989-character unconsumed budget/legacy compaction path and native no-replace archive primitive above. Do not revert either to a preflight-only check.

Known conclusions that should not be rediscovered as new bugs:

- Archive intentionally fails closed when the OS, C library, or filesystem cannot provide atomic no-replace. Some WSL/DrvFS mounts reject `renameat2` with `EINVAL`; never fall back to plain `os.rename`, which recreates the confirmed data-loss race. Run archive tests on a native Linux filesystem when needed.
- Version-1/version-2 handoff cursor migration and legacy finalized no-change review evidence are deliberate compatibility paths. Tighten them only with migration regressions.
- The stale-finalizer diagnostic named in Current state is expected proof that an old lease did not overwrite newer task state.
- `.baton/` dogfood task records are Git-ignored runtime data, not the portable source of truth. The tracked tests and implementation functions named here are the durable debugging record.

Efficient future-debugging sequence:

1. Reproduce in a temporary initialized Git project; preserve task JSON, result/report/diff bytes, handoff/cursor or archive journal, and source/destination existence before changing code.
2. Run the narrow unittest by name or `-k` substring. For archive crash/race work, use `archive_transaction_boundary` and `archive_atomic_rename_boundary` instead of timing sleeps.
3. Check transition and lock ordering before adding validation: scheduler before task/handoff; retry publication before re-queue; handoff cursor before Markdown; fsynced archive journal before moves.
4. After a focused fix, run compile, the full primary/context suites, `git diff --check`, and the real benchmark. Cross-platform archive changes require Linux and macOS CI.

## Configuration

`.baton/config.toml` (user-managed; source default is `framework/config.example.toml`):

| Key | Purpose |
| --- | --- |
| `commands.worker` | Optional shared worker argv template with exactly one `{prompt}` or `{prompt_file}` argument; absent in a fresh install. |
| `tiers.<name>.command` | Optional per-tier command override; every task tier must be explicitly configured, and limits-only tiers require an explicit shared command. |
| `tiers.<name>.worker_timeout_minutes` | Optional per-tier worker timeout override; unset inherits the global timeout. |
| `tiers.<name>.capsule_max_chars` | Optional per-tier capsule budget override; unset inherits the global budget. |
| `tiers.<name>.display` | Optional bounded safe `model`, `harness`, `effort`, `engineering_role`, and `fallback` declarations; missing metadata displays `unlabeled worker` and metadata never changes routing. |
| `limits.max_parallel` | Wave size (default 3). |
| `limits.worker_timeout_minutes` | Worker timeout; 0 disables. |
| `limits.capsule_max_chars` | Capsule budget (default 4000); overflow is a launch/validate error, never truncation. |
| `gates.finish_requires_brief` | Default true: `task finish` needs a fresh report-phase brief token. |
| `gates.report_requires_sections` | Default true: `needs_review` reports need the exact worker.md sections, nonblank core bodies, and matching Result status. |
| `gates.accept_requires_brief` | Default true: `task accept` needs a fresh review-phase brief token. |
| `gates.phase_sequence_requires_briefs` | Default false: optionally require edit → verify → report receipts; a new edit after report invalidates the finish token. |

Environment variables (all read/written in `framework/baton`): `BATON_DIR` (runtime override in, worker export out), `BATON_TASK_ID`, `BATON_ATTEMPT`, `BATON_LEASE`, `BATON_ROOT` (worker exports; their presence marks a process as a leased worker and blocks orchestrator commands). There is no `.env`; worker credentials belong to the external agent CLI.

A fresh start asks the exact persistent plain-text model/reasoning question once
because no conventional routes are predefined. The derive path discovers actual
local capabilities, asks permission before lowering, and uses current reasoning
for all three when permission is omitted. Only explicit choice or that path may
write project-local routing, and executable commands or wrappers must match
display metadata. Once all three routes are valid, later starts and compaction
recovery only state safe settings and remind the user they can change them.
Every task creation requires an explicit configured tier; `default` is rejected.

## Landmines

- `SPEC.md` is normative and embedded byte-identically in `prompts/create-framework.md` between `BEGIN SPEC`/`END SPEC`; a test fails on drift. Change SPEC → regenerate the embedded copy in the same change.
- The capsule is always GENERATED from the spec's existing sections (`Objective`, `Acceptance criteria`, `Not allowed`, `Verification`, latest feedback/decision) plus summaries for up to six worker-visible memory ids referenced only in `Context`. Never add a hand-edited capsule section, copy full memory bodies, or duplicate criteria; the stored launch capsule is the immutable audit snapshot and review warns on input drift.
- Template-placeholder specs refuse to launch and fail `validate`. Test fixtures must write real Objective/Acceptance criteria before `run`.
- The finish, report-structure, and accept gates default ON; the phase-sequence gate defaults OFF. Phase receipts are always recorded. Stub workers that submit `needs_review` must write the exact worker.md report shape, call `task brief --phase report`, and pass the token to `finish`; orchestrator fixtures need `orchestrator brief --phase review ID` tokens for `accept` (or set the relevant gate key false in the fixture's config.toml).
- Gate tokens are one-use and bound to (task, attempt, lease)/(task, attempt, review-evidence manifest). Review issuance and acceptance verification happen under the task lock; acceptance additionally holds the scheduler lock through done-state publication and token removal. Replay or evidence drift must fail without consuming a valid token.
- Lock order is global `scheduler.lock` before per-task or `orchestrator-handoff.lock`; `execution.lock` is independent. Start holds only the handoff lock for consumption. Close snapshots Git first, then loads one active/archive state snapshot under scheduler and nests the handoff lock only for previous/cursor read, render, and cursor-first publication. Never acquire scheduler while holding task/handoff.
- Handoff `done` dedupe is identity-based: the version-3 cursor retains every reported accepted task ID, including entries omitted by display limits. Do not simplify it to timestamps or only the visible Markdown; same-second events, clock rollback, and interrupted publication are covered regressions.
- Every close brief requires a fresh explicit nonblank `--goal`; never restore goal inheritance. Goal and up to five `--avoid` values use `flatten_bounded_text(..., 200)`; up to three trusted `--note` values use 160 characters; done outcomes use 120. New handoffs stop at 3989 characters so consumption can grow metadata to at most 4000. Legacy compaction may shorten prose but must preserve metadata, headings, list syntax, and every task ID.
- Retry safety is publication-before-state: `return` and `decide` publish and re-read an exact section-bound unfenced proof before incrementing the attempt and re-queueing. If publication fails, the task must remain non-runnable.
- Review result/report/diff evidence is immutable after finalization. A changed result digest, forged lifecycle, changed manifest, symlink, malformed schema, or declared/observed path mismatch must block validate/review/accept without consuming a valid review token.
- At request completion, pass every unique task id created for that request to repeatable `stats --task ID`; copy its single sentence into the final response. It counts retries and classifies legacy default/custom/malformed tier state as `other levels`. If no task was created, state the explicit zero breakdown. Close still reports all runtime launches for continuity, but that fallback may span requests and must never be relabeled as request-scoped.
- `cap_hook_output` returning `""` (and adapters emitting nothing, exit 0) is deliberate fail-open, spec'd behavior — don't "fix" silence into errors, and keep every emission ≤ 9000 chars including edge lines.
- `hooks claude-code --write` must merge idempotently (detected by exact command string) and never drop existing entries; refusal on invalid JSON is intentional (no partial writes).

- `claude --bare` conflicts with the hook integration (it disables hooks); the start brief and README state this — keep the warning when editing either.
- Worker-facing command examples must use `python3 .baton/baton ...` or `.baton/baton ...`; bare `baton` is not on PATH in installed projects.
- Runtime discovery is `BATON_DIR` env first, else a walk up from the CURRENT directory — never the invoked binary's location. Invoking another project's `baton` from inside this repo targets this repo's runtime; `cd` into the intended project first (see the smoke).
- Run the suite with `BATON_*` unset if inside a leased worker (see Run and verify).
- v1 invariants still apply: stdlib only; no Windows (`fcntl`, process groups); init target must be `git rev-parse --show-toplevel`; no submodules/Gitlinks; keep temp-index snapshots (not `git diff HEAD`); scopes case-fold; workers share one tree (no isolation); `accept` records review, `return` never reverts; don't hand-edit task JSON; task numbers never reuse across archive.
- Archive is now a durable transaction, not best-effort renames: keep strict journal/topology validation, signal masking, forward and rollback directory fsync, recovery, and journal removal under scheduler. Every move must be atomic no-replace at the syscall boundary; a preflight `lexists()` check alone is insufficient.
- `worker_timeout_minutes` default is 60: long-thinking workers on hard tasks can hit it; prefer smaller tasks or a deliberate per-tier override over raising it globally.
- An external provider failure (for example, HTTP 429 quota) after a fully valid submission preserves the submitted status and records a `worker_exit_N_after_submission` warning. Before accepting, reviewers must inspect the prominent review-brief warning and linked attempt log; failures before submission still surface as `failed` with `worker_exit_N` and require a return/retry.

## Guide self-test routes

| Plausible task | Guide-only starting route |
| --- | --- |
| Fix a capsule validation message | `compile_context_capsule` in `framework/baton`; capsule tests in `tests/test_baton.py`; SPEC.md + embedded copy only if wording is normative. |
| Add a new orchestrator brief phase | `orchestrator_*_brief` functions + `cmd_orchestrator_brief` + `build_parser` in `framework/baton`; `framework/orchestrator.md`; SPEC.md + embedded copy; new tests. |
| Change the default capsule budget | `configured_capsule_max_chars` in `framework/baton`; `framework/config.example.toml`; SPEC.md + embedded copy; budget tests in `tests/test_baton.py`. |
| Add a field to `.baton/baton stats` output | `cmd_stats` + `stats_count_lines` in `framework/baton` (receipts via `read_phase_receipts`); SPEC.md stats sentences + embedded copy; stats fixture tests in `tests/test_baton.py`. |
| Debug an archive crash/collision | `read_archive_transaction` → topology validation → `complete_archive_transaction`/rollback → `atomic_archive_rename_no_replace`; use deterministic boundary probes and search tests for `archive_recovery`, `archive_rollback`, `archive_move_boundary`, and `atomic_archive_rename`. |
| Debug duplicate/missing handoff completions | Treat `orchestrator-handoff-cursor.json` as canonical; inspect `read_handoff_cursor` and `orchestrator_close_brief`; run same-second, clock-rollback, cursor-drift, overflow, and consumption tests. |

Last updated 2026-07-17 from commit `eeb6894` using `/mnt/e/dashboard/skills/summary-create-update.md`; refreshed against implementation, tests, CI, release debugging, and performance tooling.
