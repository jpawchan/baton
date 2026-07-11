# Worker contract

You are a worker for one Agent Relay task. Other workers may be active in the
same repository.

## Work loop

1. Read the task spec named in your launch prompt.
2. Load only memory ids referenced by the spec:
   `.agent-relay/relay memory show M001`
3. Make the smallest change that meets the acceptance criteria.
4. Run the targeted verification commands from the spec.
5. Write the report to the path in the launch prompt. List every exact changed
   project-relative path.
6. Submit the result with the `task finish` command in the prompt, repeating
   `--changed PATH` for every path in the report. Omit it only for no-change
   results.

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
submits your result; it does not approve the task.

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
