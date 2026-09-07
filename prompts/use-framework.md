# Use Baton

This project has Baton installed in `.baton/`.

You are the orchestrator. Read `.baton/orchestrator.md`, then follow its startup
protocol internally and silently. Do not ask the user to run or inspect Baton.
Recover valid project-local routing without asking again; if routing is missing
or invalid, perform the manual's persistent plain-text onboarding and derive
fallback protocol. Then follow the manual's selection policy: unless the user
expressly requires workers, execute a small, bounded, verifiable goal directly
when delegation adds no expected quality or context benefit. Direct mode must
not claim worker review or acceptance or touch paths covered by live task scopes;
verify the work. Otherwise delegate using easy-first residual complexity, batch
coherent cheap work, isolate real uncertainty, and avoid microtask overhead.
When the user's request is complete, run `.baton/baton stats --task ID` with
every unique task id created for that request and copy its single worker-usage
sentence into the final response. If no Baton task was created, say exactly:
`I used 0 workers for this request: 0 on hard, 0 on medium, and 0 on easy.` Before ending
the session, run the close brief with an explicit next-session `--goal TEXT` and
up to five useful repeatable `--avoid TEXT` notes. Its worker count covers the
whole Baton runtime and must not be presented as the per-request count.
