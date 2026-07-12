# Attention Relay project guide

## What and why

Attention Relay is a Python CLI for delegating scoped coding tasks to separate agent processes.
One orchestrator agent creates tasks, runs dependency-ready workers in parallel waves, and reviews each report and Git diff.
It is the v2 evolution of [agent-relay](https://github.com/jpawchan/agent-relay): everything v1 did, plus deliberate placement of critical context at the beginning and end of agent context windows (the "Lost in the Middle" / attention-decay response).
Concretely: a generated Critical Context Capsule sandwiches every worker prompt, action-time re-briefs gate `task finish` and `task accept` behind one-use tokens, orchestrator sessions get phase briefs and a durable handoff, and optional Claude Code hooks inject state at the host session's edges.
It coordinates worker CLIs; it is not an agent model, package manager, patch queue, or security sandbox.

## Current state

- Published at `https://github.com/jpawchan/attention-relay`, branch `main`, tag `v2.0.0` (2026-07-11).
- The implementation is complete and CI is green (4 matrix jobs passed on the publish commit).
- Local verification on 2026-07-12: all 74 end-to-end tests pass; `needs_review` finish submissions enforce the structured report gate, review tokens are bound to SHA-256 evidence manifests, review briefs include streaming diff stats and retry pointers with an opt-in sanitized log tail, and decision questions appear as sanitized, bounded worker-authored data in status, start briefs, and next actions. A full live orchestration loop (init → start brief → gated worker finish → gated accept → close/start handoff → validate → archive) was exercised manually.
- An independent audit found and fixed four defects before release: an unlocked handoff read-modify-write race, a soft hook-output cap, a same-second handoff boundary loss, and non-executable command forms in `worker.md`. All have regression tests.
- One single-test error was observed once in seven local suite runs under heavy parallel load and never reproduced (locally or in CI). If a test flakes in CI, suspect timing-sensitive lock/interleaving tests first.
- No known unfinished feature path. A `relay orchestrate` launcher (framework-owned orchestrator process) was deliberately deferred, not forgotten.
- The upstream working copy on the author's machine contains a live, Git-ignored `.agent-relay/` (v1) runtime that was used to orchestrate this build. It is dev tooling, not part of the project; v2 installs create `.attention-relay/`.

## Run and verify

Requirements: Python 3.11+, Git on `PATH`, macOS or Linux. No dependency install, build step, server, or database exists.

```bash
cd <repo-root>
python3 framework/relay --help
python3 -m py_compile framework/relay tests/test_relay.py
python3 tests/test_relay.py
```

Expected: the help usage line lists `{init,task,run,status,validate,archive,orchestrator,hooks,hook-event,memory}`; py_compile is silent; the suite ends with `Ran 74 tests` and `OK` (temp Git repos and stub workers, no network or live agent calls).

If you run the suite from inside a Relay-leased worker process, unset the inherited worker env first or fixtures will reject orchestrator commands:

```bash
env -u RELAY_TASK_ID -u RELAY_ATTEMPT -u RELAY_LEASE -u RELAY_DIR -u RELAY_ROOT python3 tests/test_relay.py
```

Disposable end-to-end smoke (verified this session):

```bash
tmp=$(mktemp -d) && git -C "$tmp" init -q && git -C "$tmp" config user.name T && git -C "$tmp" config user.email t@example.invalid
echo seed > "$tmp/seed.txt" && git -C "$tmp" add . && git -C "$tmp" commit -qm seed
./framework/relay init "$tmp"
"$tmp/.attention-relay/relay" orchestrator brief --phase start
"$tmp/.attention-relay/relay" validate && rm -rf "$tmp"
```

Expected: init ends with `next: have your agent read .attention-relay/orchestrator.md and run .attention-relay/relay orchestrator brief --phase start`; the start brief prints the orchestrator role, a `Harness memory:` section, and `Next actions:`; validate prints `ok: 0 active task(s)`.
Do not smoke-test a real worker unless the configured worker CLI and its credentials work locally.

## Stack

| Layer | Verified implementation |
| --- | --- |
| Language | Python 3.11+; the entire production CLI is the single file `framework/relay`. |
| Dependencies | Python standard library only; no manifest or lockfile exists. |
| CLI | `argparse` subcommands built in `build_parser()`. |
| Concurrency | `ThreadPoolExecutor` launches one wave of worker subprocesses; POSIX `fcntl.flock` locks; `secrets.token_hex` for gate tokens. |
| Processes | `subprocess.Popen(..., start_new_session=True)`; process-group signalling on timeout/interrupt. |
| Configuration | TOML via `tomllib`; runtime state is JSON records plus Markdown specs/reports/briefs/handoff. |
| Version control | Git CLI snapshots with a temporary `GIT_INDEX_FILE`; no Git library. |
| Tests | `unittest`, 74 end-to-end cases in `tests/test_relay.py` with temp repos and embedded stub workers. |
| CI | `.github/workflows/ci.yml`: push+PR, Ubuntu/macOS × Python 3.11/3.13, `checkout@v7`, `setup-python@v6`, 10-minute timeout. |
| License | MIT (`LICENSE`). |

Relay itself makes no HTTP requests. The configured worker command (default: Hermes with `--ignore-rules`) is the only connection to an agent CLI.

## Repository map

| Path | Role |
| --- | --- |
| `framework/relay` | Entire production CLI: paths, config, capsule compiler, tasks, briefs/tokens, scopes, Git snapshots, runner, handoff, hooks, validation, archive, memory, parser. |
| `framework/orchestrator.md` | Orchestrator manual: phase briefs, task creation, waves, token-gated review, handoff, failure handling, memory. |
| `framework/worker.md` | Worker contract: capsule re-reads, phase briefs, scope rules, report shape, token-gated finish. |
| `framework/config.example.toml` | Default worker command (memory-clean Hermes), tiers, limits, gates; copied to runtime `config.toml` on init. |
| `framework/memory.md` | Empty indexed-memory template copied on first initialization. |
| `tests/test_relay.py` | Canonical 71-test end-to-end suite and all stub worker fixtures. |
| `SPEC.md` | Normative behavioral contract; embedded byte-identically in `prompts/create-framework.md`. |
| `prompts/create-framework.md` | Standalone generation prompt with the embedded exact SPEC copy (BEGIN SPEC / END SPEC markers). |
| `prompts/improve-framework.md` | Review prompt naming required v1 safety and v2 capsule/token/handoff/hook checks. |
| `prompts/use-framework.md` | Short instruction that activates an installed orchestrator (read manual → start brief → memory choices). |
| `skill/SKILL.md` | Portable skill metadata, install command, invariants. |
| `README.md` | Public page in the predecessor's question-led style. |
| `summary.md` | This guide. |
| `.github/workflows/ci.yml` | Only CI workflow. |

### Code regions in `framework/relay` (by function, top to bottom)

| Concern | Start here |
| --- | --- |
| Runtime discovery, safety | `find_relay_dir`, `runtime_paths_are_safe`, `require_relay_dir`; `RELAY_DIRNAME = ".attention-relay"`. |
| Locks and atomic state | `file_lock`, `task_lock`, `atomic_write`, `atomic_json`, `lock_path`. |
| Config | `load_config`, `cfg_get`, `configured_limits`, `configured_capsule_max_chars`, `configured_finish_requires_brief`, `configured_report_requires_sections`, `configured_accept_requires_brief`, `command_template`, `worker_argv`. |
| Paths and review evidence | `report_path`, `result_path`, `diff_path`, `sha256_regular_file`, `build_review_evidence_manifest`, streaming `attempt_diff_summary`, bounded `bounded_log_tail`, `brief_token_path` (finish-brief-token.json), and `review_token_path` (review-brief-token.json). |
| Capsule | `CAPSULE_SECTIONS`, `task_spec_sections`, `compile_context_capsule` (deterministic, budgeted, placeholder- and memory-reference-validating). |
| Task lifecycle commands | `cmd_task_create`, `cmd_task_list/show`, `cmd_task_accept` (review-token gate), `cmd_task_return/decide/cancel` (invalidate review token), `cmd_task_finish` (finish-token gate), `cmd_task_brief` (worker phases + report token), `cmd_task_unlock`. |
| Next-actions capsule | `flatten_bounded_text`, `decision_question`, `render_next_actions`, `say_next_actions` (tails `status`, `task show`, real `run`; globally budgets five review/decision/overflow lines). |
| Orchestrator briefs | `orchestrator_start_brief` (consumes handoff under the `orchestrator-handoff` lock), `orchestrator_plan_brief`, `orchestrator_review_brief` (issues review token), `orchestrator_run_brief`, `orchestrator_close_brief` (writes handoff under the same lock), `cmd_orchestrator_brief`. |
| Claude Code hooks | `claude_code_hook_fragment`, `cmd_hooks_claude_code` (print/merge, idempotent), `cap_hook_output` (hard 9000-char cap, fail-open), `claude_user_prompt_output`, `cmd_hook_event`. |
| Git snapshots and scopes | `git_snapshot`, `git_changed_paths`, `git_tree_diff`, `normalize_scope`, `scopes_overlap`, `path_in_scopes`. |
| Worker launch and waves | `WORKER_PROMPT`, `build_prompt` (capsule sandwich), `prepare_worker` (writes attempt-N.prompt.md + attempt-N.brief.md with sha256 digest), `run_one_worker`, `pick_wave`, `finalize_task`, `cmd_run`, `run_wave`. |
| Validation, archive, memory, CLI | `task_problems` (includes queued-capsule checks), `cmd_validate`, `cmd_archive`, `cmd_memory_*`, `cmd_init`, `build_parser`, `main`. |

## How it works

Runtime layout after `relay init <git-root>` (all Git-ignored):

```text
<git-root>/.attention-relay/
├── relay, orchestrator.md, worker.md, memory.md, config.toml
├── orchestrator-handoff.md      written by close brief, consumed by start brief
├── tasks/<id>.json + <id>.md    state records and hand-edited specs
├── work/<id>/attempt-N.{prompt.md,brief.md,log,report.md,result.json,diff}
│   └── {finish,review}-brief-token.json   one-use gate tokens
├── archive/                     done/cancelled tasks
└── .locks/                      scheduler, execution, memory, per-task, orchestrator-handoff
```

End-to-end flow with the v2 edge mechanisms marked:

```text
orchestrator brief --phase start      <- beginning edge: role + handoff + Harness memory + next actions
   | task create -> edit spec (Objective/Acceptance criteria/... are the capsule source)
   v
run: pick_wave -> prepare_worker compiles capsule
   |   launch prompt = CAPSULE + mechanics + CAPSULE   <- both worker edges
   v
worker: task brief --phase edit|verify|report          <- action-time re-briefs (report issues token)
   |    task finish --brief TOKEN                      <- token + needs_review report-shape gates (default on)
   v
finalize: attempt diff vs wave snapshot, scope check
   v
orchestrator brief --phase review ID -> diff stat/history + token/evidence manifest <- decision edge
   |    task accept --brief TOKEN verifies evidence               <- gate (default on)
   v
status/show/run output ends with "Next actions:"       <- recency edge, any harness
orchestrator brief --phase close -> handoff written    <- next session's beginning edge
```

Statuses: `queued → running → needs_review → done`, or `needs_decision`/`blocked`/`failed → queued` (after decide/repair/return). Workers can submit only the four `WORKER_FINAL` statuses; only `task accept` records `done`.
Scope enforcement, temp-index Git snapshots, leases, and archive semantics are inherited from v1 unchanged: every changed path outside the wave's scopes blocks the wave; declared `--changed` paths must equal the observed scoped diff case-insensitively.

Claude Code integration (opt-in): `relay hooks claude-code [--write]` prints or merges two hooks into the project's `.claude/settings.json` — SessionStart runs `hook-event session-start` (start brief as stdout → session context) and UserPromptSubmit runs `hook-event user-prompt-submit` (JSON `additionalContext` with the Next-actions capsule). Both cap output at 9000 chars and emit nothing (exit 0) on any error.

## Configuration

`.attention-relay/config.toml` (user-managed; source default is `framework/config.example.toml`):

| Key | Purpose |
| --- | --- |
| `commands.worker` | Worker argv template with exactly one `{prompt}` or `{prompt_file}` argument; default is Hermes with `--ignore-rules` (memory-clean). |
| `tiers.<name>.command` | Optional per-tier command override selected by a task's `tier`. |
| `limits.max_parallel` | Wave size (default 3). |
| `limits.worker_timeout_minutes` | Worker timeout; 0 disables. |
| `limits.capsule_max_chars` | Capsule budget (default 4000); overflow is a launch/validate error, never truncation. |
| `gates.finish_requires_brief` | Default true: `task finish` needs a fresh report-phase brief token. |
| `gates.report_requires_sections` | Default true: `needs_review` reports need the exact worker.md sections, nonblank core bodies, and matching Result status. |
| `gates.accept_requires_brief` | Default true: `task accept` needs a fresh review-phase brief token. |

Environment variables (all read/written in `framework/relay`): `RELAY_DIR` (runtime override in, worker export out), `RELAY_TASK_ID`, `RELAY_ATTEMPT`, `RELAY_LEASE`, `RELAY_ROOT` (worker exports; their presence marks a process as a leased worker and blocks orchestrator commands). There is no `.env`; worker credentials belong to the external agent CLI.

## Landmines

- `SPEC.md` is normative and embedded byte-identically in `prompts/create-framework.md` between `BEGIN SPEC`/`END SPEC`; a test fails on drift. Change SPEC → regenerate the embedded copy in the same change.
- The capsule is always GENERATED from the spec's existing sections (`Objective`, `Acceptance criteria`, `Not allowed`, `Verification`, latest feedback/decision) plus summaries for up to six worker-visible memory ids referenced only in `Context`. Never add a hand-edited capsule section, copy full memory bodies, or duplicate criteria; the stored launch capsule is the immutable audit snapshot and review warns on input drift.
- Template-placeholder specs refuse to launch and fail `validate`. Test fixtures must write real Objective/Acceptance criteria before `run`.
- All three gates default ON. Stub workers that submit `needs_review` must write the exact worker.md report shape, call `task brief --phase report`, and pass the token to `finish`; orchestrator fixtures need `orchestrator brief --phase review ID` tokens for `accept` (or set the relevant gate key false in the fixture's config.toml).
- Gate tokens are one-use and bound to (task, attempt, lease)/(task, attempt, review-evidence manifest); issuance, evidence verification, and consumption happen under the task lock. Do not weaken bindings — replay across attempts/leases must fail, and evidence mismatches must not consume review tokens.
- The `orchestrator-handoff` lock is a leaf: both start-consume and close-generate hold it for their whole read-derive-write; never acquire task/scheduler locks while holding it.
- Handoff `done` entries dedupe by task id against the previous handoff (same-second boundary). Don't simplify to a pure timestamp comparison; whole-second `now()` makes `>` and `>=` both wrong alone.
- `cap_hook_output` returning `""` (and adapters emitting nothing, exit 0) is deliberate fail-open, spec'd behavior — don't "fix" silence into errors, and keep every emission ≤ 9000 chars including edge lines.
- `hooks claude-code --write` must merge idempotently (detected by exact command string) and never drop existing entries; refusal on invalid JSON is intentional (no partial writes).
- `--ignore-rules` in the default Hermes worker command is deliberate memory hygiene (keeps model config). Do not swap in `--safe-mode` (drops user config, loses the model) or `hermes memory reset` (destructive).
- `claude --bare` conflicts with the hook integration (it disables hooks); the start brief and README state this — keep the warning when editing either.
- Worker-facing command examples must use `python3 .attention-relay/relay ...` or `.attention-relay/relay ...`; bare `relay` is not on PATH in installed projects.
- Run the suite with `RELAY_*` unset if inside a leased worker (see Run and verify).
- v1 invariants still apply: stdlib only; no Windows (`fcntl`, process groups); init target must be `git rev-parse --show-toplevel`; no submodules/Gitlinks; keep temp-index snapshots (not `git diff HEAD`); scopes case-fold; workers share one tree (no isolation); `accept` records review, `return` never reverts; don't hand-edit task JSON; archive preflight+rollback and signal masking stay; task numbers never reuse across archive.
- `worker_timeout_minutes` default is 60: long-thinking workers on hard tasks can hit it; prefer smaller tasks over raising it globally.
- An external provider failure (for example, HTTP 429 quota) after a fully valid submission preserves the submitted status and records a `worker_exit_N_after_submission` warning. Before accepting, reviewers must inspect the prominent review-brief warning and linked attempt log; failures before submission still surface as `failed` with `worker_exit_N` and require a return/retry.

## Guide self-test routes

| Plausible task | Guide-only starting route |
| --- | --- |
| Fix a capsule validation message | `compile_context_capsule` in `framework/relay`; capsule tests in `tests/test_relay.py`; SPEC.md + embedded copy only if wording is normative. |
| Add a new orchestrator brief phase | `orchestrator_*_brief` functions + `cmd_orchestrator_brief` + `build_parser` in `framework/relay`; `framework/orchestrator.md`; SPEC.md + embedded copy; new tests. |
| Change the default capsule budget | `configured_capsule_max_chars` in `framework/relay`; `framework/config.example.toml`; SPEC.md + embedded copy; budget tests in `tests/test_relay.py`. |

Last updated 2026-07-12 — Sanitized inline decision questions added to status, start briefs, next actions, and hook capsules (74 tests).
