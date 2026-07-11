# Review and improve Agent Relay

You have write access to an Agent Relay implementation. Test it, fix confirmed
bugs, remove unnecessary code, and leave it ready for use. Do not stop at a
review report.

Agent Relay is a Python 3.11+ CLI for Git projects on macOS and Linux. It uses no
third-party Python packages and creates `.agent-relay/` with:

```text
relay
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

Read the local specification and tests when present. Compare every promise in
the manuals with actual CLI behavior. Keep the shared-working-tree limitation
explicit: approval records review but does not apply or revert code.

Run the complete test suite after each fix. Your final report must list changed
files, exact commands and results, remaining limitations, and a clear yes or no
on readiness.
