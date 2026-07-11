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
restrictions, and verification commands. This placement does not guarantee
quality, but it can make critical context easier to recover.

Workers re-read the capsule before editing, verification, and reporting. The
report brief issues a fresh token bound to the current attempt and lease before
`task finish`; the review brief does the same for `task accept` and the current
attempt.

At session close, the orchestrator writes a bounded handoff from current state.
The next start brief prints and consumes that handoff. Output from `status`,
`task show`, and each completed real `run` also ends with a short, state-derived
`Next actions` capsule.

## How is this different from Agent Relay?

- **Edge placement:** a deterministic task capsule appears at both ends of each
  worker prompt.
- **Freshness gates:** finish and accept can require brief tokens bound to the
  current action and attempt.
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
   planning:

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

The `SessionStart` hook injects the start-phase orchestrator brief. The
`UserPromptSubmit` hook injects a bounded, state-derived `Next actions` capsule
before Claude handles each prompt. Repeated setup is idempotent and does not
replace existing hook arrays.

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
