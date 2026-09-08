# Paired coding pilot: 7–8 September 2026

## Bottom line

**This pilot does not demonstrate higher code quality or consistent end-to-end
logical-token savings from Baton.** Every generated solution passed the frozen
functional tests. Both mandatory-delegation runs left review/acceptance unfinished;
the two small-case runs chose direct execution and had mixed token results.

These are **real agent executions on controlled synthetic coding tasks**, not
invented measurements, production-repository benchmarks, or statistically
representative evidence.

## Results

Logical tokens include uncached input, cache-read/write input, and output from
all recorded solver and worker responses, including review and retry work.
A dagger marks **observed, incomplete accounting**, not a complete task total.

| Case / repetition | Direct tokens | Baton tokens | Checks passed, each arm | Baton outcome |
| --- | ---: | ---: | --- | --- |
| Retry-After / 1 | 156,631 | 135,927 | 14/14 | Complete; 13.2% fewer tokens; no workers |
| Retry-After / 2 | 104,812 | 129,597 | 14/14 | Complete; 23.6% more tokens; no workers |
| Ledger / 1 | 253,365 | 1,091,458† | 21/21 | Provider quota; two tasks awaiting review |
| Ledger / 2 | 582,829 | 1,447,110† | 21/21 | 25-minute timeout; retried task awaiting review |

All four direct runs completed. The original quota/timeout runs were preserved,
not replaced with successful rerolls. The quota stopped the batch on September 7;
the remaining frozen schedule resumed on September 8 after operator continuation.
The timeout did not change the remaining run budgets.

The unchanged paired evaluator returns:

- **0 `strict_win`**: no measured quality increase on any pair.
- **1 `noninferior_saving`**: equal measured quality, fewer tokens.
- **1 `no_win`**: equal measured quality, more tokens.
- **2 `inconclusive`**: incomplete Baton usage/workflow, never scored as savings.

### Small case: selective direct execution

Across both repetitions, direct used **261,443** logical tokens and Baton used
**265,524**: **Baton used 1.6% more**, not fewer. Both Baton solvers decided that
worker delegation was unnecessary. These trials therefore do not demonstrate a
worker-delegation benefit.

Observed elapsed time summed to **1,147.3 seconds direct** versus **874.7 seconds
Baton**, 23.8% less for Baton. That is a descriptive observation over two
repetitions, not a significance, throughput, or billing claim. Tokens, output
length, cache behavior, and provider latency are different measurements.

### Multi-module case: mandatory delegation

Direct used **836,194** tokens across both repetitions. Baton had already recorded
**2,538,568** before its incomplete endings—**3.04× the direct total in observed
usage alone**. The evaluator correctly leaves complete Baton totals and deltas
unknown; interrupted/unreported consumption may be additional.

Of those observed Baton tokens, **521,750** were in the orchestrators and
**2,016,818** in workers, including a retry. The excess cannot honestly be labeled
solely "orchestration prompt overhead."

The first run used one easy worker for parsing/reconciliation and one medium
worker for the atomic CLI/docs. Both submitted reports before the orchestrator
hit its provider quota. The second used one cohesive easy task, then returned it
once on a concrete broken-pipe stdout regression. Both attempts submitted reports;
the orchestrator exhausted the wall-time budget before acceptance. That retry
was evidence-based, not a blind tier promotion. Frozen tests do not exhaust every
possible I/O failure; their perfect scores do not negate the extra review finding.

Quota exhaustion is an external confounder, not evidence that a task needed a
harder model. Neither partial run can establish an end-to-end latency advantage.
Mandatory delegation was explicit for this case, so these results also do not
show what unrestricted selection would have done instead.

## Execution and measurement

- **8 fresh solver invocations**, 4 pairs, 2 cases × 2 repetitions × 2 arms.
- Direct and Baton solvers: **gpt-6-astra / max**, through Pi.
- Configured workers: **gpt-6-astra / xhigh** (hard), **gpt-5.6-sol / high**
  (medium), **gpt-5.6-luna / max** (easy), through Pi. Routes were unchanged.
- **4 delegated worker launches: 0 hard, 1 medium, 3 easy**; includes the retry.
- **201 response records; 3,901,729 observed logical tokens** across
  the eight solver trees. This is not a price estimate or complete request-wide
  consumption. Construction, external grading, and publication are outside both
  measured arms; the two incomplete runs may have unreported usage.
- Linux/WSL, native `/tmp` Git workspaces, shared host/provider, serial solver
  runs. This was cooperative isolation, not an OS sandbox or exclusive host.
- Same starting commit and task requirements within each pair; immutable smoke
  tests; additional arm-blind, withheld acceptance tests frozen before execution.
- Seed `20260907`; case blocks shuffled, first arm counterbalanced by repetition.
  Protocol and harness frozen in commit **`4ddb31f`**, before the first model run.
- Baton framework revision **`a7d198f654839e4aaa9090f39b6d6a0c3516aab3`**.
- Per-solver deadline 1,500 seconds; the timeout includes a three-second shutdown
  grace. Worker configuration: parallel cap 3, capsule cap 4,000 characters,
  worker timeout 60 minutes (the outer deadline dominates). Finish, report-section,
  and acceptance gates were enabled; phase-sequence gating retained the user's
  existing disabled setting. No gate or model setting was changed by a solver.

## Evidence and reproduction

- [Frozen protocol and input hashes](protocol.json)
- [Per-run quality, completion, route counts, token partitions, per-call ledgers,
  and private-evidence hashes](results.json)
- [Strict evaluator input](measurements.json) and [output](evaluation.json)
- [Generated Python solution snapshots](solutions/)
- [Harness, task contracts, frozen grader, limitations and live-run commands](../../agent_pilot/README.md)
- [Separate publication-audit accounting and evidence hashes](publication-audit.json)

A read-only **Sol/high** evidence auditor independently reproduced all 201 records,
12 frozen source hashes, 46 solution-file hashes, eight snapshot grades, verdicts
and launch counts, with no release blockers. This was not a new model code-quality
grade. Its **22 responses / 1,324,916 additional logical tokens** are outside both
measured arms and recorded separately—not hidden inside a claim of request-wide
savings. It adds one medium worker beyond the four benchmark worker launches.

```bash
python3.11 tests/test_agent_benchmarks.py  # offline; no provider calls
python3.11 tools/evaluate_delegation.py \
  benchmarks/results/2026-09-07-agent-pilot/measurements.json --json
```

A post-run CI check exposed a macOS temporary-path alias issue in the frozen
atomic-publication check. [Canonicalizing `TMPDIR`](../../README.md#temporary-directory-portability)
fixes setup without changing frozen source hashes, grading rules or the measured
Linux results; a symlink-root reproduction confirmed the issue and remedy.

Raw session/worker transcripts, full project trees, Git patches, and grader
stdout/stderr are retained privately by the operator. Public ledgers contain no
prompt/tool-result text or credentials. Public Python snapshots reproduce the
functional checks with the frozen source fixtures; they are not complete project
or transcript archives. Hashes support a private audit but do not let a public
reader independently recover or authenticate unavailable provider records.

The quality score is an equal-weight acceptance-test-group pass fraction, **not
all code quality**. No maintainability, security, general superiority, monetary
saving, or significance claim follows. Provider-internal retries, unreported
reasoning, global quota, caching and shared-host conditions are not independently
controlled. This pilot has no realistic long-running multi-request context, one
of the settings where fresh-worker context might help.

## Next experiment

Keep direct execution available for similar bounded work. Before recommending
workers for token efficiency, test broader repository tasks with more repetitions,
complete accounting and sufficient quota, recording all failures. Investigate
worker output/context growth and the review/retry path without weakening safety
checks. Any changed routing, budget, rubric or retry policy needs a **new frozen
experiment**, not edits to these results.
