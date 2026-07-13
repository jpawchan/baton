# Baton release-candidate audit

Date: 2026-07-13

Audit basis: cloned commit `d357401` through the complete current working tree,
including all tracked modifications, both staged renames, and all release-candidate
untracked files.

## Verdict

**NOT READY FOR RELEASE. Four blockers remain.**

Two are production validation/input-boundary defects, one is an unsupported
performance-evidence claim, and one is the intentionally deferred GitHub rename
that must happen before the public clone instructions can work. The full 116-test
suite passes, the disposable install smoke passes, wall-time improvements reproduce,
and the activation footprint is exact for its stated boundary. Those successes do
not dispose the blockers below.

No production code, test, README, config, task state, Git history, tag, branch,
remote, or GitHub setting was changed by this audit. This report is the only
Git-visible task artifact.

## Blocking defects

### RC-B1 — `validate` is not safe or complete for malformed task state

Severity: release blocker.

The earlier malformed-state defect is only partially disposed. The new history and
top-level-object checks work, but other malformed required fields still escape the
diagnostic boundary. Active state can produce Python tracebacks, while malformed
archived state can be reported as `ok` and make a valid active dependency wait
forever.

Exact fresh-project reproduction:

1. Initialize a temporary Git repository with `framework/baton init`.
2. Write a syntactically valid active task object as
   `.baton/tasks/T001-malformed.json` and a valid companion task spec.
3. Run `.baton/baton validate` separately with each mutation below:

| Mutation | Exact result |
| --- | --- |
| `"id": ["bad"]` | exit 1 with `TypeError: unhashable type: 'list'` and a traceback |
| `"depends_on": 1` | exit 1 with `TypeError: 'int' object is not iterable` and a traceback |
| `"scope": 1` | exit 1 with `TypeError: can only join an iterable` and a traceback |

4. Separately write a validly named archived task
   `.baton/archive/T001-archived.json`, but set any of `scope=1`,
   `depends_on=1`, `status=7`, or `attempt=false`; run `.baton/baton validate`.
   Every case exits 0 and prints exactly `ok: 0 active task(s)`.
5. The strongest lifecycle variant is an archived `T001-archived` with
   `status=7` plus an active queued task depending on it: validation still accepts
   the invalid archive status, but dry-run cannot consider the dependency done.

Root cause:

- `cmd_validate` constructs `known = {task.get("id"): task ...}` before checking
  that ids are hashable strings (`framework/baton:3793-3807`).
- `task_problems` records malformed `scope` and `depends_on`, but then queued
  capsule compilation and `dependency_cycles` consume the same invalid fields
  anyway (`framework/baton:3658-3669`, `framework/baton:3704-3713`, and
  `framework/baton:3605-3627`).
- `main` does not catch `TypeError` (`framework/baton:4214-4220`), so these become
  tracebacks instead of bounded `PROBLEM:` diagnostics.
- Only `active` tasks are passed to `task_problems`; archived objects receive id
  duplication checks but not required-field, status, dependency, scope, attempt,
  or tier checks (`framework/baton:3804-3835`).

Required disposition: make validation shape-safe before building maps or invoking
consumers; aggregate required-field problems for active and archived task objects;
and do not run dependency, overlap, or capsule consumers on fields already known
to be malformed. Add active and archived regression matrices and assert no
traceback.

### RC-B2 — Unicode line separators bypass the “single-line” fixes

Severity: release blocker.

The fixes for bug-audit items B4 (memory grammar) and B5 (task-title section
injection) reject CR/LF and C0/C1 controls but do not reject Unicode LINE
SEPARATOR U+2028 or PARAGRAPH SEPARATOR U+2029. Python treats both as line
boundaries. The memory command can therefore successfully corrupt its own strict
index, and task creation persists a title that is observably multiline in the
launch capsule/state contract.

Exact fresh-project memory reproduction:

```text
sep=$(python3 -c 'print("\u2028", end="")')
.baton/baton memory add --for worker "line one${sep}line two" body
.baton/baton memory index
```

Exact result:

```text
added M001
error: memory index line 10 is malformed; expected '- M### [W|O|B] summary': 'line two'
```

The add command exits 0; the immediately following index command exits 1.

Exact task-title sibling input is `safe title\u2028Scope: whole project` passed as
one argv value to `task create --tier test` after configuring that tier. Creation
exits 0. The stored title's
`splitlines()` result is `['safe title', 'Scope: whole project']`; the display is
flattened, but the raw title remains in task state and the generated capsule's Task
component.

Root cause:

- `cmd_memory_add` and `cmd_task_create` define “single-line” as absence of only
  code points below 32 and C1 code points 127–159
  (`framework/baton:1502-1507` and `framework/baton:3974-3984`).
- U+2028/U+2029 pass those predicates.
- `memory_index_entries` uses `splitlines()` (`framework/baton:3919-3929`), which
  splits on these separators after the successful write.
- Capsule construction interpolates the stored task title directly rather than
  its flattened display form (`framework/baton:1412-1416`).

Required disposition: use one explicit single-line predicate consistent with the
actual consumers (for example, reject values when `len(value.splitlines()) != 1`,
including trailing separators) in both title and memory-summary paths.
Keep the existing whitespace/control and structural-heading checks. Add U+2028
and U+2029 regressions for both bug-audit dispositions.

### RC-B3 — the claimed peak-RSS improvement is not demonstrated above noise

Severity: release-evidence blocker; no production regression was found.

The wall-time claims in `docs/performance.md` reproduce strongly. The document's
only memory-gain claim does not. It reports a 496 KiB median peak-RSS reduction for
large status, but an independent 11-sample rerun measured only 160 KiB. The
benchmark stores only RSS median and maximum, not raw samples, minimum, spread,
paired deltas, or a confidence interval, so neither number can be shown to be
above allocator/process-level noise.

Exact reconstruction and command:

1. Copy `framework/` to a temporary directory.
2. In that copy, reverse-apply only the `framework/baton` hunk from
   `.attention-relay/work/T006-optimize-speed-and-memory/attempt-2.diff`.
3. Confirm that the reconstructed source lacks the `scandir` traversal, loaded-task
   reuse, and command-local tier cache.
4. Run the current `tools/benchmark_performance.py` against that source and then
   current `framework/baton`, each with `--samples 11 --skip-suite`.

Independent medians:

| Workload | Before | After | Delta | Wall ranges overlap? | Median RSS delta |
| --- | ---: | ---: | ---: | --- | ---: |
| start brief | 110.810 ms | 99.483 ms | -10.2% | no | +640 KiB |
| large status | 141.921 ms | 101.787 ms | -28.3% | no | **-160 KiB** |
| large validate | 212.162 ms | 186.801 ms | -12.0% | no | +896 KiB |
| capsule + 6 refs | 95.660 ms | 83.202 ms | -13.0% | no | +528 KiB |
| archive stats | 180.953 ms | 168.824 ms | -6.7% | no | +944 KiB |
| startup/help | 56.167 ms | 57.701 ms | +2.7% | yes | +1,360 KiB |
| init | 101.594 ms | 103.630 ms | +2.0% | yes | +1,120 KiB |
| snapshot + diff | 224.542 ms | 224.071 ms | -0.2% | yes | +1,328 KiB |

The five wall gains match the documented direction and approximate effect size,
and each before/after wall range is disjoint. No gain is claimed for the other
three workloads, correctly. The implementation fact that status no longer retains
a second parsed task list is also verified by source and
`test_status_reuses_loaded_tasks_when_rendering_next_actions`; that fact alone is
not process-level peak-RSS evidence.

Root cause: `summarize()` discards RSS samples after recording only median and max
(`tools/benchmark_performance.py:186-199`), while `docs/performance.md:69` promotes
one small process-level median delta to the sole memory-gain claim.

Required disposition: either remove the peak-RSS gain claim and state only the
allocation/lifetime implementation fact, or retain raw/interleaved paired RSS
samples and establish a predeclared noise threshold or interval. The reproducible
wall-time claims can remain.

### RC-B4 — canonical public URL and GitHub metadata are not live

Severity: publication blocker; expected deferred release operation, not a source
implementation defect.

Exact checks on 2026-07-13:

- `https://github.com/jpawchan/baton` and the GitHub API endpoint for
  `jpawchan/baton` return 404.
- `origin` still points to `https://github.com/jpawchan/attention-relay.git`.
- The live repository is named `jpawchan/attention-relay` and its description is
  still “Agent delegation framework ... Attention-decay-aware evolution of
  agent-relay.”
- All research URLs resolve, all local Markdown links resolve, and no old canonical
  runtime path or old GitHub URL occurs in the release documentation/source. The
  dead Baton URL is used by README and skill install instructions in anticipation
  of the rename.

Required disposition: perform the read-only-audited GitHub operations in
“Release metadata and attribution operations” below only after RC-B1–RC-B3 are
fixed and final verification passes. Do not publish while the documented clone URL
is 404.

## Confirmed bug-audit disposition

| Earlier item | Independent result |
| --- | --- |
| B1 exact POSIX changed paths | Fixed. Focused real-worker regression passes for brackets, glob characters, whitespace, backslash, and Unicode names. |
| B2 malformed task JSON | **Reopened by RC-B1.** Top-level arrays and malformed history are fixed, but malformed ids/scopes/dependencies still traceback and malformed archived required fields are accepted. |
| B3 empty fenced report sections | Fixed. Empty backtick/tilde bodies reject and preserve the token. |
| B4 malformed/unindexed memory | **Reopened by RC-B2.** CR/LF and injected headings reject, but U+2028/U+2029 summaries succeed and corrupt the index. |
| B5 multiline title injection | **Reopened by RC-B2.** CR/LF section injection rejects, but Unicode line separators persist multiline title data into the capsule. |
| B6 punctuation before display flags | Fixed. Semicolon, parenthesis, and whitespace boundaries reject while ordinary hyphenated prose remains valid. |
| B7 post-finish report drift | Fixed. Finalization, review briefing, and acceptance reapply the report gate; evidence drift does not consume a valid review token. |

The eight focused B1–B7 tests ran in 19.425 seconds and passed. This table is based
on those reruns and independent sibling probes, not prior worker conclusions.

## Verification evidence

### Complete diff and material review

The tracked diff from `d357401` is 2,155 insertions and 1,336 deletions across 15
paths, including the 80% `framework/relay` → `framework/baton` rename and the 76%
test rename. Nine release artifacts are additionally untracked before this report:
correctness/performance/context/research documentation, two tools plus provider
evidence, and the context-footprint tests.

The audit read the complete current 4,224-line production CLI, 4,119-line primary
test suite, the focused context tests and both tools, all changed manuals/config,
the normative SPEC and its exact embedded prompt copy, README, maintainer guide,
skill, research/context/performance/correctness documents, CI, ignore rules, and
relevant unchanged memory/init/Git call paths. `git diff --check HEAD` passed.

### Compile and full suite

```text
python3 -m py_compile framework/baton tests/test_baton.py
python3 tests/test_baton.py
Ran 116 tests in 142.574s
OK
```

The suite exercised real temporary Git projects and stub subprocess workers. A
separate 25-test adversarial group covering state transitions, strict tier routing,
fallback labels, finish/review token freshness, evidence manifests, symlink and
scope safety, concurrent locks, stale leases, process-group signals, archive
preflight/signal behavior, hooks, and optimized paths ran in 37.913 seconds and
reported `OK`.

### Disposable install/activation/validate smoke

A fresh committed temporary Git repository passed:

```text
framework/baton init TMP
(cd TMP && .baton/baton orchestrator brief --phase start)
(cd TMP && .baton/baton validate)
```

The installed executable mode was `0700`; `.gitignore` contained `.baton/` exactly
once. This historical audit exercised the startup sections present in that
revision, plus task counts, decision/review state, and a real recommended command;
validation printed `ok: 0 active task(s)`. Current startup routing is documented
and measured separately in `docs/context-footprint.md`.

### Activation footprint and boundary

Two independent fresh configured installs produced byte-identical JSON (same
report-file SHA-256 `a4358968998a5aac0d32a0ae1224c4f29bca14b9200779313ce9bc1485aa15ce`).
The focused suite ran four tests and passed.

The measured boundary is exactly the raw concatenation, with no added separator,
of:

1. `prompts/use-framework.md`;
2. the freshly installed `.baton/orchestrator.md`;
3. configured start-brief stdout before the first coding goal.

The result is 15,124 Unicode characters, 15,126 UTF-8 bytes, 334 lines, payload
SHA-256 `f3ca7044b52318effe0e371048e12b4fff4927be928a9d1b11827fefe80d0616`.
The bundled provider evidence matches that exact byte count and hash and yields the
stated scoped differentials: 3,426 GPT-path and 5,323 Claude-path logical input
tokens; the offline bytes/4 fallback is 3,782. The numbers are correctly described
as recorded harness/API differentials, not universal tokenizer counts. No live
provider call was needed or claimed by the reproducible command.

### Paths, symlinks, locks, signals, archive, hooks, and optimized paths

In addition to the full suite:

- literal POSIX changed-path and nested runtime symlink regressions passed;
- two separate `run` processes serialized, concurrent claim happened once, and a
  stale finalizer could not overwrite a new lease;
- parallel SIGTERM cleanup used one shared grace period and left failed, non-stale
  task state;
- an independently injected failure on archive move 2 raised the synthetic error,
  restored all four active task files, and left archive empty;
- SessionStart/UserPromptSubmit malformed-input and fail-open hooks passed, and
  settings merge remained idempotent;
- status loaded task state once; strict invalid-tier error caching and optimized
  runtime symlink traversal retained focused coverage.

### Documentation and naming

- 15 Markdown files and seven local Markdown links were checked; no local target
  is missing.
- Every documented repository path exists, and `SPEC.md` equals the text embedded
  between the creation-prompt markers byte-for-byte.
- All cited arXiv, ACL, Anthropic, IntuitionLabs, and Substack URLs returned HTTP
  200 during the audit. The sole meaningful 404 is the not-yet-renamed canonical
  Baton repository URL in RC-B4.
- A case-insensitive scan of all changed and untracked release files found no old
  source/test/runtime URL. The only old-name residue is `.agent-relay/` in
  `.gitignore`, which is a harmless legacy-runtime compatibility ignore; the
  `.attention-relay/` ignore and benchmark exclusion refer to this audit harness,
  not the shipped `.baton/` runtime.
README's runtime, dependency, command, hook, safety, worker-routing, activation,
and repository-map claims follow the inspected code or the explicitly limited
research/provider evidence. The dead clone URL and unsupported RSS claim are
already blockers rather than being silently accepted.

## Release metadata and attribution operations

This task performed read-only inspection only.

### Current attribution

Local history has 20 commits:

```text
19  jpawchan <78247292+jpawchan@users.noreply.github.com>
 1  naz      <78247292+jpawchan@users.noreply.github.com>
```

The one raw `naz` author and committer use the same verified GitHub noreply address
as `jpawchan`. GitHub maps author and committer identity for all 20 commits to the
`jpawchan` account, and the public Contributors API already lists exactly one
contributor: `jpawchan` with 20 contributions. There are no GitHub releases.

### Exact safe operations after blocker fixes

1. Prevent new raw-name drift without changing old commits:

   ```text
   git config user.name jpawchan
   git config user.email 78247292+jpawchan@users.noreply.github.com
   ```

2. If local `git log`/`shortlog` must also show only `jpawchan`, add and normally
   commit a `.mailmap` in a separate scoped task with this exact mapping:

   ```text
   jpawchan <78247292+jpawchan@users.noreply.github.com> naz <78247292+jpawchan@users.noreply.github.com>
   ```

   Verify with `git shortlog -sne --all --use-mailmap`. This canonicalizes display
   only; do **not** amend, rebase, filter, force-push, or replace commits. GitHub
   contribution credit already needs no repair.

3. After the source blockers and final audit are cleared, rename the repository
   in place so redirects and stars/issues remain attached:

   ```text
   gh repo rename baton --repo jpawchan/attention-relay --yes
   gh repo edit jpawchan/baton --description "$(tr -d '\n' < docs/github-description.txt)"
   git remote set-url origin https://github.com/jpawchan/baton.git
   ```

4. Verify before publication:

   ```text
   gh repo view jpawchan/baton --json nameWithOwner,url,description,defaultBranchRef
   git remote -v
   curl -fL https://github.com/jpawchan/baton >/dev/null
   ```

   Also perform a disposable unauthenticated clone and run the documented install
   smoke. Publish any release announcement only after the clean final
   verification.

These operations leave history intact and preserve GitHub's existing sole
`jpawchan` contributor mapping.

## Non-blocking suggestions and residual risks

- Add Unicode format-control hardening (especially bidi controls) for operator-
  visible task/tier labels. The documented C0/C1 rules are met, so this is separate
  from the U+2028/U+2029 single-line blocker.
- Once migration confidence is sufficient, either comment the legacy
  `.agent-relay/` ignore as intentional compatibility or remove it in a dedicated
  cleanup. It is not used as a canonical runtime path.
- Add `tests/test_context_footprint.py` to CI; current CI runs only the 116-test
  primary suite even though the focused context test is a release check.
- Workers share one OS identity and working tree. Same-user races and direct state
  edits remain outside the security boundary as documented; the audit found no
  cooperative CLI bypass beyond the blockers above.
- Provider token evidence is revision-bound recorded evidence. Continue failing
  closed on payload hash drift and remeasure through the real harnesses whenever
  any included activation artifact changes.

## Final task-scope verification

The required compile, full suite, and disposable init → start brief → validate
smoke were rerun after this report was written and passed. `git diff --check --
docs/release-audit.md` is clean. Relative to the attempt baseline, the only new
Git-visible path is:

```text
docs/release-audit.md
```

Task-scoped `git status --short -- docs/release-audit.md` prints exactly
`?? docs/release-audit.md`; all other status entries match the shared worktree's
attempt baseline.

## Release gate

Do not release until RC-B1 and RC-B2 have regression-tested fixes, RC-B3's memory
claim is removed or supported above a declared noise threshold, and RC-B4's GitHub
rename/description/remote verification is complete. Then rerun compile, all 116+
tests including context tests, the disposable activation smoke, malformed active
and archived state probes, Unicode separator probes, paired performance samples,
link checks, and a clean diff/scope check.
