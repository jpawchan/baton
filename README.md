# attention relay

## What is Attention Relay?

Attention Relay is a small delegation framework. It can improve code quality and
reduce token use.

You work with one orchestrator agent. It breaks your goal into tasks, sends each
task to a separate worker, and reviews the result before accepting it.

Workers can use Hermes Agent, Claude Code, Codex, OpenCode, or another
non-interactive CLI agent.

## Why put critical context at the edges?

Models can use context near the beginning and end more reliably than context in
the middle. As a session grows, instructions in the middle can fade or be
missed. The effect varies by model and task; read more in
[Attention Decay](https://jpawchan.substack.com/p/attention-decay).

Attention Relay puts the same Critical Context Capsule at both ends of every
worker prompt. The capsule carries the task's objective, acceptance criteria,
restrictions, verification commands, and summaries of worker-visible memory ids
referenced in the task's Context section. This placement does not guarantee
quality, but it can make critical context easier to recover.

See [Context placement rationale](docs/context-placement.md) for the research,
tradeoffs, and limits behind this design.

Workers re-read the capsule before editing, verification, and reporting. The
CLI stores bounded attempt-local command receipts for those phase briefs; an
optional default-off gate can enforce edit → verify → report order. The
report brief issues a fresh token bound to the current attempt and lease before
`task finish`; the review brief binds its token to the current attempt and a
SHA-256 manifest of the capsule and artifacts it displayed. `task accept`
recomputes that manifest and refuses changed review evidence without consuming
the token, so the reviewer can inspect the change and issue a fresh brief.

At session close, the orchestrator writes a bounded handoff from current state.
The next start brief prints and consumes that handoff. Output from `status`,
`task show`, and each completed real `run` also ends with a short, state-derived
`Next actions` capsule.

Orchestrators can run `.attention-relay/relay stats` for a read-only aggregate
over active and archived status, attempts, reason codes, capsule sizes, phase
receipt coverage, and post-submission warnings. Receipt coverage is command-use
evidence, not proof of attention.

Task tiers are explicit rather than fallback labels. `default` uses the global
worker command and limits; each other tier must be configured and may override
the command, worker timeout, and capsule budget independently. Run
`.attention-relay/relay tiers` to inspect effective settings without printing
worker command flags. Until all three optional conventional tiers (`hard`,
`medium`, and `easy`) are configured, the start brief asks the orchestrator to
ask you which models they should use and prints missing-only TOML to copy. Relay
does not register, select, or write these tiers automatically.

## How is this different from Agent Relay?

- **Edge placement:** a deterministic task capsule appears at both ends of each
  worker prompt.
- **Freshness gates:** finish can require a brief token for the current lease and
  attempt; accept can additionally bind its token to the exact displayed review
  evidence.
- **Bounded evidence:** phase receipts and read-only stats expose command-use
  coverage without storing prompts, logs, or tokens in receipt records.
- **Handoff:** close and start briefs carry current state between orchestrator
  sessions.
- **Claude Code hooks:** optional hooks inject the start brief and bounded next
  actions.
- **Memory-clean defaults:** workers skip saved harness memory and startup offers
  memory-clean choices without applying them.

Attention Relay uses [agent-relay](https://github.com/jpawchan/agent-relay) as
its base.

## Requirements

### Generate Relay from a prompt

To generate Relay, give its prompt to a coding agent. The generated framework
requires Python 3.11+, Git, and macOS or Linux.

### Run the ready-to-use framework

Requirements: Git, Python 3.11+, macOS or Linux, and a command-line coding
agent.

## Install

### Build from the prompt

Give `prompts/create-framework.md` to a coding agent. Then give
`prompts/improve-framework.md` to a fresh agent to test and fix the result. This
costs tokens once, but future models can rebuild Relay from the same
specification and may produce a better implementation.

### Install the ready-to-use version

Clone the repository:

```bash
git clone https://github.com/jpawchan/attention-relay
```

Install Relay into your project:

```bash
attention-relay/framework/relay init /path/to/project
```

This creates a local `.attention-relay/` directory in the project.

## How to use it

1. Install Relay in your project.
2. Ask your main agent to read `.attention-relay/orchestrator.md`.
3. The agent runs the start brief and offers its memory-clean choices before
   planning. If the optional `hard`, `medium`, or `easy` tiers are missing, it
   also asks which model (and optionally provider) each should use:

   ```bash
   .attention-relay/relay orchestrator brief --phase start
   ```

4. Describe your goal.

## Optional Claude Code hooks

Print the Claude Code settings fragment, or merge it into the project's current
settings:

```bash
.attention-relay/relay hooks claude-code
.attention-relay/relay hooks claude-code --write
```

The matcher-free `SessionStart` hook injects the start-phase orchestrator brief
at startup and re-injects it after automatic or manual compaction, with an
explicit context-compacted notice. Post-compaction re-injection omits the
user-facing Difficulty levels ask. The `UserPromptSubmit` hook injects a bounded,
state-derived `Next actions` capsule before Claude handles each prompt. Repeated
setup is idempotent and does not replace existing hook arrays.

The adapters cap output and fail open with no output if Relay state is missing
or broken. Do not launch Claude with `--bare` when using this integration,
because `--bare` disables hooks.

## What is in this repository?

| Path | Contents |
| --- | --- |
| `framework/` | Ready-to-use Relay CLI, config example, orchestrator and worker manuals, and memory template. |
| `prompts/create-framework.md` | Prompt for building Relay from the specification. |
| `prompts/improve-framework.md` | Prompt for testing and fixing an implementation. |
| `prompts/use-framework.md` | Prompt for using an installed Relay framework. |
| `skill/` | Agent-skill metadata and usage guidance. |
| `tests/test_relay.py` | End-to-end test suite. |
| `SPEC.md` | Exact behavior and safety rules. |
| `summary.md` | Code-verified project guide. |
| `LICENSE` | MIT license text. |

## License

MIT. See [LICENSE](LICENSE).
