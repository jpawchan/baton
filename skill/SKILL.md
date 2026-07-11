---
name: agent-relay
description: "Delegate coding to fresh, scoped agents with parallel scheduling and diff review."
version: 0.3.0
author: JPawchan
license: MIT
metadata:
  hermes:
    tags: [coding-agents, orchestration, delegation, code-review, token-efficiency]
    related_skills: [hermes-agent, codex, opencode]
---

# Agent Relay

Use Agent Relay when a coding goal should be split across fresh workers without
losing central review. Do not use it for a single small edit where delegation
costs more than it saves.

## Install

```bash
git clone https://github.com/jpawchan/agent-relay
agent-relay/framework/relay init /path/to/project
```

Requirements: Git, Python 3.11+, macOS or Linux, and a worktree without tracked
submodules.

Then tell the main coding agent to read `.agent-relay/orchestrator.md`. A
copy-ready instruction is in `prompts/use-framework.md`.

To generate the same framework instead of copying it, use
`prompts/create-framework.md`, then review the result with
`prompts/improve-framework.md`.

## Preserve these rules

- Tasks have explicit scopes and dependencies.
- Only non-overlapping tasks run together.
- Workers submit results and exact changed paths; Relay checks declarations
  against scoped diffs before the orchestrator approves them.
- Changes to Git-visible files outside a wave’s scopes block approval; workers
  never modify Git-ignored files.
- Memory contains durable project facts, not task history.
- The runtime remains local and Git-ignored, and Relay adds no third-party
  Python packages.
