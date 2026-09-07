# Activation context footprint

This measurement answers one narrow question: how much Baton-authored context is
loaded after a fresh install is activated and its conventional difficulty routes
are configured, but before the user supplies the first coding goal?

## Boundary

The standard activation number includes, in the order an orchestrator is told to
load them:

1. the exact activation instructions in `prompts/use-framework.md`;
2. the installed `.baton/orchestrator.md` copied by `baton init` into a disposable
   Git project; and
3. stdout from `.baton/baton orchestrator brief --phase start` in that fresh
   project after `hard`, `medium`, and `easy` have been configured.

The configured route descriptions intentionally loaded before the goal are
already in the installed orchestrator manual. They are counted there, once. The
fully configured start brief does not repeat onboarding; it prints only safe
settings and the reminder that they can be changed at any time.
`config.toml` itself is machine configuration rather than agent context, so it
is not added as another context component.

The number excludes the host harness's base system prompt and tool schemas,
unrelated user profile or saved harness memory, `summary.md`, Baton source code,
`worker.md`, task specifications, worker prompts, and later Critical Context
Capsules. It also excludes provider message framing that is not present in these
artifacts. Characters are Unicode code points, bytes are UTF-8 bytes, and lines
are Python `str.splitlines()` entries. The total is the raw bytes of the three
artifacts concatenated in the listed order, with no measurement labels or
separators inserted.

## Reproduce it

From the repository root, run:

```bash
python3 tools/measure_context.py
python3 tools/measure_context.py --json
```

The standard-library-only script creates a new temporary Git project, runs the
current checkout's `framework/baton init`, writes a complete disposable routing
configuration, invokes the installed start brief, measures the resulting bytes,
and removes the project. It reports a standard-library offline estimate by
default and does not access the network. To inspect exactly what was counted,
use `--keep-artifacts DIR`; the directory receives each component and their raw
concatenation. `tools/context-provider-differential.json` is explicitly retired
historical evidence for the preceding payload and is rejected if supplied. New
evidence may be passed with `--provider-evidence FILE` only after a genuine live
bracketing measurement of the exact current bytes.

Run the command twice and compare the total and per-artifact SHA-256 values. The
focused automated check does this too:

```bash
python3 tests/test_context_footprint.py
```

## Result for this revision

Two independent fresh temporary installs produced identical metrics and hashes:

| Artifact | Characters | Bytes | Lines | SHA-256 |
| --- | ---: | ---: | ---: | --- |
| activation instructions | 1,407 | 1,407 | 21 | `27d1a5e8a87d806db9fd8abfcb93c7f81a146773a64197da0125e3c79715abcf` |
| installed orchestrator manual | 11,965 | 11,967 | 198 | `56016cbd12fb9ff43ab53a5fa91338494214542178cef11590937f5a1868b5b6` |
| generated configured start brief | 601 | 601 | 13 | `58e9d61ddab7da8916e9737067f4de095e7d53eb66b4a8e356b72b160ea0bbe6` |
| **Total** | **13,973** | **13,975** | **232** | `42c487228f319661bcddc6d297f5eef1d6d87cb76c6203b6617c5116ce57211b` |

These values are revision-specific. Re-run the script whenever the activation
prompt, installed manual, or start brief changes rather than carrying this table
forward as an estimate.

Compared with baseline revision `a65c4d5` (18,629 bytes; 4,658 estimated tokens
under the same bytes/4 heuristic), this revision is 4,654 UTF-8 bytes smaller.
Its estimate is 3,494 tokens, a reduction of 1,164 estimated tokens. These are
byte and heuristic-estimate deltas, not provider-reported token savings, quality
gains, or end-to-end performance measurements.

## Model-aware token result

No live provider differential was collected for the exact 13,975-byte payload
above. The preceding payload's genuine provider evidence remains in
`tools/context-provider-differential.json` with `status: retired`; the default
result does not load it, and explicit loading rejects it. This avoids fabricating
new precision by adding an estimated delta to old token counts.

The current result is explicitly labeled `ESTIMATE`: `ceil(bytes / 4)` gives
3,494 estimated tokens, with a deliberately broad conservative range from
`ceil(bytes / 6)` through `ceil(bytes / 2)`, or 2,330–6,988 tokens, for either
named model path. Matching fallback values do not imply matching tokenization;
the heuristic only measures the same UTF-8 bytes. A similarly named tokenizer,
generic GPT encoding, or third-party Claude approximation is not authoritative
for these provider paths. Publishing a new provider figure requires identical
baseline requests before and after the exact payload request and genuine usage
accounting from each tested harness/model path.

## Context tokens, cache, and billing

This footprint is framework-attributable input context, not a prediction of an
invoice. Prompt caching can make repeated manual or activation prefixes cheaper
to bill, but cached tokens are still present in the model's context and still
consume context-window capacity. The current offline estimate uses only UTF-8
bytes and has no cache accounting. Any new live differential for this boundary
must sum uncached, cache-read, and cache-write input (cache creation) so cache
placement does not erase logical input. Providers may price those categories
differently and may separately report output and reasoning tokens; neither is
part of this activation input measurement.

Conversely, a billed input count can be larger because it may include the host
system prompt, tool definitions, conversation framing, and unrelated messages
excluded by this boundary. It can also differ because provider APIs serialize
roles or attachments in ways that cannot be reconstructed from plain artifact
bytes. Compare Baton runs and direct runs using the same model, harness, cache
state, and provider usage fields; do not subtract this measurement from a mixed
billing total as though both used the same boundary.

## Break-even implication

Activation is overhead. A coding goal likely to consume fewer tokens than this
activation footprint is usually better executed directly rather than delegated
through Baton. Without current live provider evidence, compare the likely direct
goal against the 3,494-token offline estimate while keeping its 2,330–6,988 range
visible. Baton is most defensible when task decomposition, fresh-worker focus,
parallelism, and review are expected to save enough context to exceed that
overhead or to provide quality and risk-control benefits worth the cost.