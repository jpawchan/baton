# Paired delegation evaluation

`tools/evaluate_delegation.py` is a small, offline evaluator for measurements
that an operator has already collected. It does not run agents, inspect logs,
verify quality, or independently verify provider accounting. It only validates
the input and applies transparent rules to each direct/Baton pair.

## Live paired-agent pilot

The separate [opt-in Pi pilot harness](../benchmarks/agent_pilot/README.md) launches
fresh agents, applies frozen withheld tests, and reconciles recorded session/worker
usage before producing this evaluator's input. Its [September 2026 results](../benchmarks/results/2026-09-07-agent-pilot/README.md)
contain eight runs: no strict wins, one equal-quality saving, one no-win, and two
inconclusive pairs. All code passed the functional rubric; quota and timeout left
both mandatory-delegation workflows incomplete. These small controlled tasks do
not establish general superiority. Live runs consume provider quota; CI tests
only the offline harness and grading/accounting fixtures.

## Command

```text
python3 tools/evaluate_delegation.py MEASUREMENTS.json
python3 tools/evaluate_delegation.py MEASUREMENTS.json --json
```

A valid analysis exits 0 even when it finds no wins. Invalid JSON or invalid
measurement data exits 2 and reports a concise field path and reason without a
traceback. The `--json` result is deterministic and has this shape:

```json
{
  "schema_version": 1,
  "pairs": [
    {
      "case_id": "...",
      "rubric_id": "...",
      "verdict": "...",
      "direct_total_tokens": 0,
      "baton_total_tokens": 0,
      "token_delta": 0,
      "quality_delta": 0.0
    }
  ],
  "summary": {
    "pairs": 1,
    "verdicts": {
      "inconclusive": 0,
      "quality_gate_failed": 0,
      "quality_regression": 0,
      "strict_win": 0,
      "noninferior_saving": 0,
      "quality_gain_only": 0,
      "no_win": 1
    }
  }
}
```

## Input schema

The top-level object must contain exactly these keys:

- `schema_version`: integer `1` (a Boolean is not an integer here).
- `pairs`: a nonempty list.

Each pair must contain exactly `case_id`, `rubric_id`, `direct`, and `baton`.
The two IDs are nonblank, single-line strings of at most 160 characters.
`case_id` is unique across the input. Both run objects must contain exactly:

| Key | Type and meaning |
| --- | --- |
| `quality` | finite real number in the inclusive range 0 through 1 |
| `critical_checks_passed` | Boolean result of the independent critical checks |
| `usage_complete` | Boolean; true only when both token measurements are complete |
| `logical_input_tokens` | nonnegative integer, or `null` when unavailable |
| `output_tokens` | nonnegative integer, or `null` when unavailable |
| `elapsed_seconds` | finite nonnegative real number, or `null`; evidence only |

Booleans are rejected where numbers are expected. Negative and nonfinite
numbers are rejected, including JSON `NaN`, `Infinity`, and exponent values
that overflow to infinity. Required keys and unexpected keys are rejected at
every object level so misspellings cannot silently change an analysis.

A run's `total_tokens` is computed as `logical_input_tokens + output_tokens`
only when `usage_complete` is true and both fields are present. Otherwise its
total is unknown. The evaluator never imputes missing usage. Consequently, a
pair with an unknown total on either side is `inconclusive`, and its
`token_delta` is `null`. `quality_delta` is always Baton quality minus direct
quality; `token_delta` is Baton total minus direct total when both totals are
known. `elapsed_seconds` is reported as accepted evidence in the input but
never affects a verdict or a token calculation.

## Verdicts

For each pair, evaluation proceeds in this order:

1. `inconclusive` — either total token measurement is unknown.
2. `quality_gate_failed` — either run's critical checks failed.
3. `quality_regression` — Baton quality is lower.
4. `strict_win` — Baton quality is higher *and* Baton uses fewer tokens.
5. `noninferior_saving` — quality is equal and Baton uses fewer tokens. This is
   useful evidence for a noninferior goal, but it is not the strict goal.
6. `quality_gain_only` — quality is higher but there is no token saving.
7. `no_win` — none of the preceding conditions holds.

The summary counts all seven verdict names. The tool does not calculate pooled
averages across heterogeneous rubrics or infer confidence, significance,
prices, or costs.

## Experiment boundaries

A real comparison should use the same starting commit, goal, test/quality
rubric, and verification budget for both conditions. Run each condition in a
fresh isolated worktree. Compare a direct single-agent run with Baton using the
intended configured routes; record model, harness, effort level, and cache
conditions externally. Repeat trials, randomize condition order, and have an
independent, preferably blind, quality review rather than treating the tool's
quality field as an independent check.

Count **all** activation, orchestration, planning, worker, review, and retry
calls, including failed attempts. Do not sum both usage message deltas and
final events for the same call. For provider-reported accounting, count
logical input as uncached, cache-read, and cache-write input exactly once each
according to that provider's accounting. Count output tokens, including
reasoning, when the provider reports reasoning there; never double count it.
If a provider field is missing, leave it unknown: do not turn bytes into a
measured token count with an expression such as `bytes / 4`. Complete failed
runs still count their usage. Counts, prices, and latency are different
measurements; elapsed time is optional here and this evaluator makes no cost
claim.

State whether the comparison is about activation cost alone or a full
end-to-end baseline. Activation is a sunk cost only when that boundary is
explicitly the question; it must not be silently omitted from an end-to-end
comparison. Do not introduce an additional “quota” target in place of the
configured accounting boundary.

## SYNTHETIC example (illustrative only; not measured evidence)

The following values are **SYNTHETIC** fixtures showing the schema and rules.
They do not represent a real experiment and do not prove that Baton
outperforms a direct run:

```json
{
  "schema_version": 1,
  "pairs": [
    {
      "case_id": "synthetic-case-1",
      "rubric_id": "synthetic-rubric",
      "direct": {
        "quality": 0.80,
        "critical_checks_passed": true,
        "usage_complete": true,
        "logical_input_tokens": 1000,
        "output_tokens": 200,
        "elapsed_seconds": 12.0
      },
      "baton": {
        "quality": 0.90,
        "critical_checks_passed": true,
        "usage_complete": true,
        "logical_input_tokens": 850,
        "output_tokens": 150,
        "elapsed_seconds": 13.0
      }
    }
  ]
}
```

This synthetic pair would be labeled `strict_win` by the arithmetic rules
(quality delta `0.10`, token delta `-200`) only as an illustration. It is not a
claim of real savings or quality superiority; real conclusions require the
independent checks and experiment boundaries above.
