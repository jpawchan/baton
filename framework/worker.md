# Worker contract

You are a worker for one Attention Relay task. Other workers may be active in the
same repository.

A fresh worker process does not imply a clean harness. The default worker command
uses `--ignore-rules` so the task spec and capsule are the whole intended context.
Do not re-enable memory injection unless the task spec says so.

## Work loop

1. Read the task spec named in your launch prompt. The prompt begins and ends
   with the same generated Critical Context Capsule; re-read the closing copy
   before finishing so its requirements are fresh.
2. The capsule includes summaries for memory ids referenced in the spec's
   Context section. A summary is not the full entry; load each needed entry with
   `.attention-relay/relay memory show M001`.
3. Immediately before the first write, run
   `python3 .attention-relay/relay task brief ID --phase edit`, then make the
   smallest change that meets the acceptance criteria.
4. Immediately before verification, run
   `python3 .attention-relay/relay task brief ID --phase verify`, then run the
   targeted verification commands from the spec.
5. Immediately before writing the report, run
   `python3 .attention-relay/relay task brief ID --phase report`. Write the
   report to the path in the launch prompt and list every exact changed
   project-relative path.
6. Submit the result with the
   `python3 .attention-relay/relay task finish --brief TOKEN` command in the
   prompt, using the token from the report-phase brief and repeating
   `--changed PATH` for every path in the report. Omit `--changed` only for
   no-change results.

## Rules

- Change only Git-visible files in the task scope. Do not modify Git-ignored
  files; the only exception is writing the exact report path and using
  `task finish`.
- Do not run repository-wide formatters, migrations, installs, or test suites
  unless the spec requires them.
- Do not add dependencies, use destructive commands, or edit sensitive code
  without permission in the spec.
- Do not spawn agents or ask the user. Submit `needs_decision` with the question.
- Use `blocked` for missing credentials or broken external systems.
- Do not run orchestrator commands such as `accept`, `return`, or `run`.
- Report failed checks and risks. Never include secrets.

Relay keeps the task running until your process exits. Calling `task finish`
submits your result; it does not approve the task. If the process then exits
nonzero, Relay preserves the submitted status only when the result passes every
normal validation check, and records a post-submission warning for the reviewer.
Timeout, interruption, and runner or launch errors still fail the attempt.

## Report

Keep the report short:

```markdown
# <task-id> report

## Result
needs_review

## Changes
- `path`: what changed and why

## Verification
- `command`: pass or fail, with the relevant output

## Decisions and risks
- none
```
