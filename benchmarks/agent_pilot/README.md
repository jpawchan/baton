# Live paired-agent pilot

An **opt-in experiment**, not a CI benchmark or proof that delegation wins.
Python 3.11+, Git, POSIX process management, and an authenticated Pi CLI are required.
Only the explicit `run` command invokes models and can consume paid usage.

## Frozen design

- Two synthetic standard-library coding projects, two repetitions, both arms:
  **8 fresh solver runs / 4 paired comparisons**.
- `retry_after`: bounded single-module repair. Baton may work directly.
- `ledger`: CSV/normalization/reconciliation/atomic-output CLI. Baton must use at
  least one worker, then complete its normal review and acceptance workflow.
- Same task requirements, starting source, immutable smoke test, allowed changes,
  tools, solver model/effort, and per-run timeout in each pair. Only Baton receives
  the activation prompt/runtime. No hand-selected favorable outputs.
- Pi resources and ambient context files are disabled for both arms. Provider
  credentials/model registrations remain available. No outside-workspace reading,
  network/dependency installation, other agents in the direct arm, or background
  commands are allowed by the task contract. This is **cooperative isolation**, not
  an OS security sandbox. The withheld grader and answer keys are not copied into
  either solver workspace.
- Case/repetition blocks are seeded and shuffled. Each case alternates its first
  arm across repetitions. Runs are serial to avoid concurrency interference.
- Setup freezes source/prompt/config hashes before invocation. Existing run
  directories and interrupted runs cannot silently be overwritten. Provider or
  grader failures stop the batch for inspection, not automatic replacement trials.
- Default top-level timeout: 1,500 seconds. Worker timeouts/retry policy and all
  Baton safety gates come from the supplied config, without tier changes. The
  experiment records incomplete runs rather than pretending they saved tokens.

The initial pilot uses the initiating session's **Astra/max** for both direct and
Baton solvers. Worker routes are the user's configuration: **Astra/xhigh** (hard),
**Sol/high** (medium), **Luna/max** (easy), all through Pi. These are different
reasoning budgets; do not describe the experiment as same-model-only delegation.

## Quality and accounting

`holdout.py` is an arm-blind, external acceptance-test grader. The score is the
fraction of equal-weight test groups passed. Groups include edge cases; all
assertions in a group must pass. Critical groups are marked in the test names.
Both critical functional checks and protocol/workflow completion must pass the
published evaluator's critical gate. Exact passed/failed group names are retained.
Offline tests check that starting code fails and independent answer keys pass.

This is a **functional correctness proxy**, not an assessment of all code quality,
maintainability, security, or production fitness. Test-ceiling ties cannot show
higher quality. Two synthetic cases and two repetitions do not justify population
estimates or significance claims. Mandatory delegation in `ledger` tests that
workflow specifically, not whether unrestricted selective delegation always wins.

The measured boundary starts immediately before the fresh solver process and ends
on its exit/termination. Its usage includes every recorded solver/worker API call,
internal review, retries, and separately recorded compaction/branch-summary usage.
Fixture preparation, deterministic external grading, and the initiating session's
benchmark construction/reporting are **outside both arms**, not free task-solving
work or evidence of request-wide savings. No model grader is invoked outside this
boundary. Wall time includes startup, tools, orchestration, provider latency and
backoff; it is not normalized throughput.

`usage.py` reconciles saved session IDs and assistant-call counts against terminal
JSON streams and Baton launch records. It counts each API response once, not the
repeated `message_end`/`turn_end`/`agent_end` copies. It checks:

```text
input + output + cacheRead + cacheWrite == totalTokens
logical_input = input + cacheRead + cacheWrite
logical_total = logical_input + output
```

Provider caching is not controllable; cache partitions are retained and cached
input counts fully. Billing, prices, provider-internal retries and unreported
internal reasoning cannot be independently audited from Pi logs. Missing usage,
unfinished/error responses, duplicate identities or unmatched sessions make
accounting incomplete. Observed unmatched-session usage is still included. A
transcript-free per-call ledger preserves provider/model/effort, partitions, and
hashes of the private source evidence. Zero-price provider metadata is not a
cost estimate. Partial accounting must not become an efficiency win.

Use `tools/evaluate_delegation.py` unchanged: strict wins require **higher quality
and fewer tokens**; ties with fewer tokens are `noninferior_saving`. Per-case and
aggregate totals must be read together; opposite case results can cancel out.

## Reproduce

Use a native temporary filesystem, not a DrvFS checkout, for solver projects.
Preparation is offline and does not call a provider:

```bash
python3.11 tests/test_agent_benchmarks.py
python3.11 tools/benchmark_agents.py prepare /tmp/baton-agent-pilot \
  --config .baton/config.toml --repeats 2 --seed 20260907 \
  --provider YOUR_PROVIDER --model YOUR_ASTRA_MODEL_ID --effort max

# LIVE: up to eight solver runs plus any delegated workers; consumes quota.
python3.11 tools/benchmark_agents.py run /tmp/baton-agent-pilot

# Public output excludes raw prompts/tool results/session transcripts.
python3.11 tools/benchmark_agents.py summarize /tmp/baton-agent-pilot \
  --publish benchmarks/results/agent-pilot-1
python3.11 tools/evaluate_delegation.py \
  benchmarks/results/agent-pilot-1/measurements.json --json
```

`run --limit 1` executes the next pending run. No automatic retry is performed after
an interrupted runner; inspect its preserved `started.json` and raw evidence.
Keep the private experiment directory for auditing: solver/worker JSON streams,
Pi session files, grading stdout/stderr, source snapshots, Git patches and hashes.
Public solutions are outputs, **not additional benchmark cases**. The source
fixtures/task prompts live in `cases.py`; never point solver agents at this directory.
