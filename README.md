# Baton

Introduction article: https://jpawchan.substack.com/p/baton-agentic-orchestration-framework

## What is Baton?

Baton coordinates coding agents. Each worker handles a focused task, and one orchestrator checks the result.

The orchestrator divides a goal into scoped tasks. It runs non-overlapping work in parallel and reviews each report and Git diff. Each worker receives a short capsule explaining what to do, what counts as done, what not to change, and how to verify the work. Baton repeats it at the beginning and end of the prompt, where models tend to use information more reliably. This makes important instructions less likely to be missed.

Baton works with coding agents such as Hermes Agent, Claude Code, Codex, and OpenCode, using them as orchestrators or workers.

## Choose direct work or delegation

Baton is selective rather than mandatory. Use direct execution for a small,
bounded, verifiable goal when fresh-worker context, parallelism, independent
review, or extra residual reasoning is not expected to help. Honor an explicit
request for workers, but account for the handoff overhead. Delegate when one of
those benefits is useful, and judge the complete end-to-end work: activation,
orchestration, workers, review, retries, input, and output.

Choose each delegated task's tier from its residual complexity after the task is
specified, not from the size of the parent request:

- **easy:** a settled local edit, such as a documented API rename with a
deterministic focused test;
- **medium:** bounded investigation or integration, such as tracing a
configuration handoff and wiring an already-defined interface;
- **hard:** unresolved architecture or high-risk uncertainty, such as a
concurrency, security, or data-loss change with weak verification.

Line count, file count, and a missing specification alone do not justify a harder
tier. Batch cohesive mechanical work instead of creating microtasks, and
isolate genuine uncertainty. There are no tier quotas, automatic downgrades, or
assumed model-price/capability claims. Baton uses only configured executable
routes and does not rewrite configuration to hit a target.

## Quality and token use

### Quality

As a session grows, useful instructions compete with irrelevant context from finished tasks. Important details become easier to miss.

Baton gives each worker one focused task in a fresh context. The worker rechecks its instructions before editing, testing, and reporting. The orchestrator reviews the report and Git diff before accepting the work.

### Token use

Without delegation, details from finished tasks stay in the conversation and are sent with later requests. They take up context and may be billed again. Baton gives each worker a fresh context, then returns only its report and Git diff to the orchestrator. That fresh context can avoid carrying finished-task details into later worker requests, but delegation also adds activation, orchestration, review, retry, and worker input/output work. No fixed token or quality saving follows from the design.

### Limitations

The footprint measurement covers only Baton-authored activation artifacts. It
excludes the host system prompt, tool schemas, provider framing, unrelated
messages, workers, and later task capsules. Its bytes/4 value is an offline
estimate, not provider-reported usage; it does not measure produced-code quality,
latency, or end-to-end performance. Workers use external CLIs in a shared
worktree, so Baton is not a sandbox.

### Why not just summarize?

Summaries compress relevant and irrelevant history together. Small but critical details can disappear, such as "do not" or an exact requirement. Each rewrite creates another chance to lose information.

Instead of compressing the whole conversation, Baton builds each worker's context from the task spec and selected memory. Its handoff carries only current task state into the next orchestrator session.

### Research

Studies show that models often use information at the beginning and end of long prompts more reliably than information in the middle. Keeping related facts close together and reading the same instructions twice can also improve understanding. Baton therefore keeps task instructions short, together, and repeated at both ends of the prompt. The copies are identical so exact constraints cannot drift between the beginning and end.

See [Long-context research synthesis](docs/research-synthesis.md) and [Context placement rationale](docs/context-placement.md) for the evidence and limitations.

### Sources

- ["Lost in the Middle: How Language Models Use Long Contexts"](https://arxiv.org/abs/2307.03172) — Liu et al., TACL.
- ["Found in the Middle: Calibrating Positional Attention Bias Improves Long Context Utilization"](https://aclanthology.org/2024.findings-acl.890/) — Hsieh et al., Findings of ACL 2024.
- ["Distance between Relevant Information Pieces Causes Bias in Long-Context LLMs"](https://arxiv.org/abs/2410.14641) — Tian et al., Findings of ACL 2025.
- ["Re-Reading Improves Reasoning in Large Language Models"](https://arxiv.org/abs/2309.06275) — Xu et al., EMNLP 2024.
- ["EchoPrompt: Instructing the Model to Rephrase Queries for Improved In-context Learning"](https://aclanthology.org/2024.naacl-short.35/) — Mekala, Razeghi, and Singh, NAACL 2024.
- ["Serial Position Effects of Large Language Models"](https://aclanthology.org/2025.findings-acl.52/) — Guo and Vosoughi, Findings of ACL 2025.
- ["Read Before You Think: Mitigating LLM Comprehension Failures with Step-by-Step Reading"](https://arxiv.org/abs/2504.09402) — Han et al., arXiv preprint.
- ["Prompt engineering for Claude's long context window"](https://www.anthropic.com/news/prompting-long-context) — Anthropic, practitioner guidance.
- ["LLM Position Bias: Primacy and Recency Effects in Prompts"](https://intuitionlabs.ai/articles/llm-position-bias-primacy-recency-effects) — A. Laurent, secondary synthesis.
- ["Lost in the Middle: The Context Crisis of LLMs"](https://davidwsilva.substack.com/p/lost-in-the-middle-the-context-crisis) — David William Silva, secondary article.

## Requirements

### Generate Baton from a prompt

The generated framework requires Python 3.11+, Git, and macOS or Linux.

### Run the ready version

Requirements: Python 3.11+, Git on `PATH`, macOS or Linux, and a Git worktree without tracked submodules. No third-party Python packages are needed.

### Supported agents

Use any agent with file and command access as the orchestrator, and any agent that accepts CLI prompts as a worker.

### Measured activation footprint

For this checkout, the reproducible activation boundary is 13,975 UTF-8 bytes:
the activation instructions, installed orchestrator manual, and configured start
brief before the first coding goal. The standard-library bytes/4 heuristic is
3,494 estimated tokens, with a broad 2,330–6,988 range. These are estimates, not
provider-reported usage or a claim about end-to-end savings. See [Activation
context footprint](docs/context-footprint.md) for the exact artifact hashes,
reproduction command, baseline comparison, and limitations.

## Install

### Build from the prompt

Give [`prompts/create-framework.md`](prompts/create-framework.md) to a coding agent in the directory of your project. After it builds Baton, give [`prompts/improve-framework.md`](prompts/improve-framework.md) to a fresh agent in the same directory to test and repair it.

### Install the ready version

```bash
git clone https://github.com/jpawchan/baton
cd baton
framework/baton init /path/to/project
```

## How to use

1. Tell the main coding agent to read `.baton/orchestrator.md`.
2. Describe your goal.

### Deliberate retries and routing evidence

When review evidence shows that a retry needs a different reasoning route, reroute
it deliberately rather than promoting it automatically:

```bash
.baton/baton task return TASK-ID --reason "specific evidence and next check" --tier medium
```

`--tier` is optional; omitting it preserves the current tier. The selected tier
must already be configured and executable, and invalid routes are rejected
before retry state changes. Use the read-only routing view for launch and outcome
accounting:

```bash
.baton/baton stats --routing
.baton/baton stats --routing --task TASK-ID
```

This view reports recorded launches, retries, and lifecycle outcomes by tier; it
makes no token or quality inference. For paired direct/Baton measurements, use
the [paired delegation evaluator](docs/delegation-evaluation.md). It consumes
operator-supplied usage and independent quality evidence, does not run agents,
and cannot establish a result from incomplete measurements.

## Repository contents

| Path | Contents |
| --- | --- |
| `framework/` | Ready CLI, manuals, configuration, and memory template. |
| `prompts/` | Creation, review, and activation prompts. |
| `docs/` | Research, context placement, token measurement, audits, and performance results. |
| `tests/` | End-to-end and token-footprint tests. |
| `tools/` | Measurement and benchmark scripts. |
| `skill/` | Portable Baton skill and operating guidance. |
| `SPEC.md` | Normative behavior and safety contract. |
| `summary.md` | Code-verified maintainer guide. |


## Tech used

Hermes, claude code, codex, gpt 5.6 sol, fable 5

## Future work

- OKF;
- Comparative measurements across harnesses, models, and reasoning efforts,
  using complete accounting and independent quality checks;
- Additional orchestration strategies, only where reproducible evaluation
  justifies them;
- Further context-management experiments.

No quality, token, or delivery improvement is promised without comparable
external evidence.

## License

[MIT License](LICENSE).
