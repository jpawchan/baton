# Baton

## What is Baton?

Baton coordinates coding agents. Each worker handles a focused task, and one orchestrator checks the result.

The orchestrator divides a goal into scoped tasks. It runs non-overlapping work in parallel and reviews each report and Git diff. Each worker receives a short capsule explaining what to do, what counts as done, what not to change, and how to verify the work. Baton repeats it at the beginning and end of the prompt, where models tend to use information more reliably. This makes important instructions less likely to be missed.

Baton works with coding agents such as Hermes Agent, Claude Code, Codex, and OpenCode, using them as orchestrators or workers.

## Quality and token use

### Quality

As a session grows, useful instructions compete with irrelevant context from finished tasks. Important details become easier to miss.

Baton gives each worker one focused task in a fresh context. The worker rechecks its instructions before editing, testing, and reporting. The orchestrator reviews the report and Git diff before accepting the work.

### Token use

Without delegation, details from finished tasks stay in the conversation and are sent with later requests. They take up context and may be billed again. Baton gives each worker a fresh context, then returns only its report and Git diff to the orchestrator.

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

### Framework token usage

Baton currently adds about 4,658 tokens before the first task. The estimate covers its activation prompt, orchestrator manual, and configured start brief.

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

## Soon

OKF;

Benchmarks with different harnesses, models and reasoning efforts;

More orchestration strategies to increase quality and reduce token consumption;

Improved context management techniques;

Smarter task difficulty evaluation.

## License

[MIT License](LICENSE).
