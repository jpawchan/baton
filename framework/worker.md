# Worker contract

You are a worker for one Baton task. Other workers may be active in the
same repository.

A fresh worker process does not imply a clean harness. The task's explicit
project-local tier selects its worker command. Follow only the task spec, capsule,
and this contract; do not enable additional memory injection unless the task spec
says so.

## Work loop

1. Read the task spec named in your launch prompt. The prompt begins and ends
   with the same generated Critical Context Capsule; re-read the closing copy
   before finishing so its requirements are fresh.
2. The capsule includes summaries for memory ids referenced in the spec's
   Context section. A summary is not the full entry; load each needed entry with
   `.baton/baton memory show M001`.
3. Immediately before the first write, run
   `python3 .baton/baton task brief ID --phase edit`, then make the
   smallest change that meets the acceptance criteria.
4. Immediately before verification, run
   `python3 .baton/baton task brief ID --phase verify`. Exercise a falsifiable
   regression for the changed behavior, then run the smallest relevant checks
   from the spec; leave broad suites to integration unless the spec or risk
   requires them.
5. Immediately before writing the report, run
   `python3 .baton/baton task brief ID --phase report`. Write the
   report to the path in the launch prompt and list every exact changed
   project-relative path. Declare only the net Git-visible paths changed in the
   CURRENT attempt, excluding prior attempts and pre-existing dirty work; do not
   copy `git status` or `git diff HEAD` as the changed-path list.
6. Submit the result with the
   `python3 .baton/baton task finish --brief TOKEN` command in the
   prompt, using the token from the report-phase brief and repeating
   `--changed PATH` for every path in the report. Omit `--changed` only for
   no-change results.

Baton always records one bounded, attempt-local receipt for every phase brief.
The normal sequence remains edit, verify, then report. Configuration may make
that order a strict gate; by default it is guidance only and no brief is blocked.
When the gate is enabled, running a new edit brief after report invalidates the
finish token, so run the report brief again before finishing.

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
- Report failed checks and risks. On retry, use their evidence to correct the
  failed assumption, implementation, or check rather than repeating blindly.
  Never include secrets.

Baton keeps the task running until your process exits. Calling `task finish`
submits your result; it does not approve the task. If the process then exits
nonzero, Baton preserves the submitted status only when the result passes every
normal validation check, and records a post-submission warning for the reviewer.
Timeout, interruption, and runner or launch errors still fail the attempt.

## Report

Keep the report short. For `needs_review`, Baton requires all four exact level-2
headings below, nonblank Result, Changes, and Verification bodies, and a Result
body whose first nonblank line exactly matches the submitted status:

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
