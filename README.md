# agent relay

## What is Agent Relay?

Agent Relay is a small delegation framework. It can improve code quality and
reduce token use.

You work with one orchestrator agent. It breaks your goal into tasks, sends each
task to a separate worker, and reviews the result before accepting it.

Workers can use Hermes Agent, Claude Code, Codex, OpenCode, or another
non-interactive CLI agent.

## Why can this improve quality and reduce token use?

### Quality

In one long session, context grows quickly. As it grows, models can miss earlier
facts, break old constraints, or invent details. This often appears around
100,000 tokens, but there is no fixed threshold; it depends on the model and
task. Read more:
[Attention Decay](https://jpawchan.substack.com/p/attention-decay).

Each worker receives one task and only the context it needs. A smaller context
can lead to better implementation.

### Token use

As work continues, completed tasks remain in the conversation. Most of that
history is no longer useful, but it may be processed or billed again with later
requests. Relay keeps worker sessions separate, so the orchestrator reviews a
short task report and relevant Git diff instead of the worker's full
conversation.

### Why not just summarize?

Summarization compresses the full history and may lose useful details. Relay
avoids giving that history to each worker in the first place.

The orchestrator keeps the high-level view and delegates low-level work. It
reviews each task's report and diff instead of loading the worker's full
session.

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
git clone https://github.com/jpawchan/agent-relay
```

Install Relay into your project:

```bash
agent-relay/framework/relay init /path/to/project
```

## How to use it

1. Install Relay in your project.
2. Ask your main agent to read `.agent-relay/orchestrator.md`.
3. Describe your goal.

## What is in this repository?

| Path | Contents |
| --- | --- |
| `framework/` | Ready-to-use Relay CLI, config, and agent instructions. |
| `prompts/create-framework.md` | Prompt for building Relay from the specification. |
| `prompts/improve-framework.md` | Prompt for testing and fixing an implementation. |
| `prompts/use-framework.md` | Prompt for using an installed Relay framework. |
| `skill/` | Agent-skill metadata. |
| `tests/` | End-to-end tests. |
| `SPEC.md` | Exact behavior and safety rules. |
| `summary.md` | Code-verified project guide. |

## License

MIT. See [LICENSE](LICENSE).
