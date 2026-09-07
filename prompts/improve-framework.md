# Review and improve Baton

You have write access to a Baton implementation. Test it, fix confirmed
bugs, remove unnecessary code, and leave it ready for use. Do not stop at a
review report.

Baton is a Python 3.11+ CLI for Git projects on macOS and Linux. It uses no
third-party Python packages and creates `.baton/` with:

```text
baton
orchestrator.md
worker.md
memory.md
config.toml
tasks/
work/
archive/
.locks/
```

Check these behaviors with temporary Git repositories and stub workers:

1. Initialization is idempotent and preserves config, memory, and task state.
2. Task ids are monotonic; ids, scopes, limits, dependencies, and cycles are
   validated.
3. Separate real `run` processes serialize their snapshot windows. A stale run
   lease cannot overwrite newer task state.
4. Only non-overlapping, dependency-ready tasks run together; case-variant
   scopes conflict conservatively.
5. Workers submit only their assigned task result and declare exact changed
   paths. Declarations must match observed scoped diffs. The task stays
   `running` until the process exits and the attempt diff exists.
6. Diffs start from the wave baseline, not `HEAD`, so earlier dirty work is not
   attributed to the worker.
7. Worker commands run as argument lists without a shell.
8. Scope violations have a separate diff, block acceptance, and must be
   restored before retry.
9. Timeout and orchestrator interruption stop every worker group before one
   shared grace interval, without leaving stale task state.
10. Completed dependencies still work after archive.
11. Memory add, index, and show work with audience filters.
12. Nested runtime symlinks, non-UTF-8 results, and Gitlinks found in `HEAD`, the
    index, or snapshots are rejected safely. Git-ignored files are explicitly
    outside diff guarantees and forbidden by the worker contract.
13. Archive preflights all destinations and defers termination signals until
    every move completes or rolls back.
14. Critical Context Capsules reject empty or placeholder task sections, enforce
    the configured character budget without truncation, and save byte-identical
    edge copies with the correct SHA-256 digest.
15. Finish and accept brief tokens are bound to the current task and attempt;
    finish tokens are also lease-bound. Replacement, replay, and stale tokens
    are rejected without consumption, successful use consumes the token, and
    return, decide, and cancel invalidate review tokens.
16. Orchestrator handoff writes and start-phase consumption are atomic. Start
    marks the handoff consumed without deleting it.
17. Claude Code hook adapters cap their output and fail open without stdout or
    stderr when Baton state is missing or broken.
18. Claude Code settings merges are idempotent and do not clobber existing
    settings or hook arrays.
19. A start brief asks the exact persistent plain-text hard/medium/easy onboarding
    question only while any conventional route is missing, invalid, or not
    executable. Valid project-local routes produce only a safe settings reminder,
    including after compaction; no startup question asks about harness memory or
    fresh sessions.
20. A close brief counts recorded worker launches across active and archived
    tasks, including retries, and reports a natural total with conventional and
    other-level breakdowns without exposing commands.
21. `stats --task ID` accepts repeatable request task ids, deduplicates them,
    resolves active and archived tasks, rejects unknown ids before output, counts
    retries, and prints only the copy-ready request-scoped worker breakdown.
    Ordinary `stats` output remains unchanged, and manuals never present the
    runtime-wide close count as a per-request count.
22. Selection uses direct execution for a small bounded verifiable goal when
    delegation adds no expected quality/context benefit unless workers were
    expressly requested. Direct mode avoids live task scopes, makes no worker
    review/acceptance claim, verifies, and prints the exact zero-worker sentence.
23. Delegation uses easy-first residual complexity rather than size or tier
    quotas: coherent cheap work is batched, genuine uncertainty is isolated, and
    risky or weakly verified reasoning receives stronger routing/review. A
    settled design can leave easier mechanical implementation.
24. `task return ID --reason TEXT [--tier NAME]` validates any configured route
    before mutation, preserves tier when omitted, retains old work/history, and
    snapshots reroutes. `stats --routing [--task ID]...` reports routing/outcomes
    read-only without changing original stats text or inferring token use or
    quality.

Read the local specification and tests when present. Compare every promise in
the manuals with actual CLI behavior. Keep the shared-working-tree limitation
explicit: approval records review but does not apply or revert code.

For each fix, first exercise a falsifiable regression, run the smallest relevant
checks, and run broader tests once at integration unless risk requires more. Use
failed evidence to revise a retry rather than blindly promoting its tier.

Run the complete test suite once at final integration, with additional runs
only when risk requires them. Your final report must list changed files, exact
commands and results, remaining limitations, and a clear yes or no on
readiness.
