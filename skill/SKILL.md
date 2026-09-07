---
name: baton
description: "Coordinate fresh, scoped coding agents with generated context capsules and evidence-bound diff review."
version: 0.3.0
author: JPawchan
license: MIT
metadata:
  hermes:
    tags: [coding-agents, orchestration, delegation, code-review, token-efficiency]
    related_skills: [hermes-agent, codex, opencode]
---

# Baton

Use Baton when fresh context, parallel execution, isolation, or independent
review has an expected quality/context benefit. Unless the user expressly asks
for workers, execute a small, bounded, verifiable goal directly when delegation
adds none.

The activation overhead is revision- and harness-specific. `docs/context-footprint.md`
has a reproducible byte measurement and offline estimate; no live provider count
or measured quality/token gain is claimed. Optimize total end-to-end work,
including activation, orchestration, workers, review, retries, and input/output,
not launch count or capsule bytes alone. Already-paid activation is sunk for a
marginal decision.

## Select and route economically

Direct mode creates no task, claims no worker review or acceptance, does not edit
paths covered by live task scopes, and still runs the smallest relevant checks.
Its completion sentence is exactly:
`I used 0 workers for this request: 0 on hard, 0 on medium, and 0 on easy.`

For delegation, assess each child's residual complexity easy-first and announce
one line with tier, reason, and a concrete check before launch:

- **easy** when method/interfaces are settled and checks are strong;
- **medium** when bounded investigation or integration remains;
- **hard** for genuinely unresolved architecture, high uncertainty/coupling,
  irreversible, security, or concurrency risk, or weak verification.

Line count, multiple files, parent complexity, and missing specifications alone
do not justify harder reasoning. Many deterministic renames can be easy; a small
authentication or concurrency patch can be hard. Batch coherent cheap work,
avoid microtask handoffs, and isolate genuine uncertainty. A hard design may
leave easy mechanical implementation once interfaces settle; add independent
strong review only where risk warrants it. Use no tier quotas and assume no model
price/capability not locally verified. Change only to configured executable
routes, never configuration chosen to hit a target.

## Install

```bash
git clone https://github.com/jpawchan/baton
cd baton
framework/baton init /path/to/project
```

Requirements: Git, Python 3.11+, macOS or Linux, and a worktree without tracked
submodules.

Then tell the main coding agent to read `.baton/orchestrator.md`; it runs the
start brief internally and silently. A fresh project asks once for explicit
project-local hard, medium, and easy model/reasoning routes. Valid later sessions
recover those routes without asking again and state that settings can change at
any time. Missing or invalid routing uses the manual's persistent plain-text
question, capability discovery, consent-before-lowering, and no-consent fallback.
A copy-ready instruction is in `prompts/use-framework.md`.

Hermes Agent, Claude Code, Codex, OpenCode, and other noninteractive CLI agents
can be workers when their locally verified command accepts one prompt or prompt
file argument. Optional host hooks are specific to Claude Code.

To generate the same framework instead of copying it, use
`prompts/create-framework.md`, then review the result with
`prompts/improve-framework.md`.

## Preserve these rules

- Tasks have explicit scopes and dependencies.
- Only non-overlapping tasks run together.
- Workers submit results and exact changed paths; Baton checks declarations
  against scoped diffs before the orchestrator approves them.
- Changes to Git-visible files outside a wave’s scopes block approval; workers
  never modify Git-ignored files.
- Memory contains durable project facts, not task history.
- Fresh config contains no worker command or tier; every task tier is explicit,
  configured, and project-local. There is no `default` route.
- Executable commands or wrappers must implement displayed model/reasoning
  settings; metadata alone is never routing.
- Startup and compaction use the same route-validity rule and never ask a
  harness-memory or fresh-session question.
- Request completion uses `stats --task ID` for every task created for that
  request; the final response copies its hard/medium/easy worker breakdown.
- `task return ID --reason TEXT [--tier NAME]` preserves the route when omitted
  and validates configured reroutes before mutation; retry from failed evidence,
  never blind promotion. `stats --routing [--task ID]...` is read-only routing
  evidence, not a token/quality inference or quota target.
- Verification starts with a falsifiable regression and smallest relevant
  checks; broader tests run once at integration unless risk requires otherwise.
- Close briefs retain a separately labeled runtime-wide count for continuity,
  never as a substitute for the request-scoped sentence.
- The runtime remains local and Git-ignored, and Baton adds no third-party
  Python packages.
