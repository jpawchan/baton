# Baton final release-candidate reaudit

Date: 2026-07-13

Audit basis: original cloned commit
`d357401a146a66117a3df9da57cb211e1cf99f21` through the complete current
worktree after T010, including staged renames, unstaged tracked changes, and
untracked release artifacts. This is a fresh audit; the conclusions below do
not rely on the T009 or T010 worker reports alone.

## Verdict

**GO — no source blockers remain. The source tree is ready to commit and
publish.**

RC-B1 and RC-B2 are closed by independently reproduced behavior, RC-B3's
unsupported process peak-RSS claim has been removed, and local/GitHub
attribution resolves to the sole credited identity `jpawchan`. The future
`https://github.com/jpawchan/baton` URL still returns 404 only because the
in-place GitHub rename is intentionally deferred. That is a publication
operation, not a source defect.

Publication must still perform the exact remote operations in “Deferred
publication operations” and pass the post-publication checks.

## Blocking defects

None.

## Audit coverage

### Complete release diff

I generated and reviewed one complete patch from `d357401` through the
worktree, including every untracked release file. Before this report it was
10,604 lines / 542,156 bytes. The tracked portion spans 15 paths with 2,417
insertions and 1,389 deletions, including the `framework/relay` →
`framework/baton` and `tests/test_relay.py` → `tests/test_baton.py` renames.
The complete patch additionally includes 11 untracked source-release
artifacts: `.mailmap`, six release/evidence documents, the context-footprint
test, and three files under `tools/`.

The review covered the current 4,292-line production CLI and 4,260-line
primary suite; the normative SPEC and embedded creation-prompt copy; README,
maintainer guide, skill, manuals, configuration, prompts, CI and ignore rules;
all release, correctness, context, performance, and research documents; both
tools and provider evidence; and the focused context tests. Relevant unchanged
call paths were traced through task loading/schema validation, dependency maps
and cycle checks, archive selection and rollback, scheduler dependency
resolution, title creation, memory parsing/addition, capsule construction,
worker preparation, Git snapshots/diffs, init, and author/committer display.

T010's task record, scoped diff, and bounded report were read. Its four changed
paths were then checked independently: `framework/baton`,
`tests/test_baton.py`, `docs/performance.md`, and `.mailmap`. Raw untrusted
worker logs were not used as audit evidence.

### RC-B1 — malformed task state is closed

`task_validation_shape` now validates every active and archived object before
ids enter maps or scopes, dependencies, statuses, attempts, tiers, titles,
history, capsules, overlaps, or cycles reach their consumers. `task_problems`
receives the shape result, and `cmd_validate` builds maps and downstream input
sets only from fields whose shapes are valid. Archived records now pass through
the same required-field checks as active records.

The exact T010 active and archive regression matrices passed. A separate
16-case CLI driver, using only fresh committed temporary Git projects and the
installed executable, independently produced these results:

| Fresh mutation | Result |
| --- | --- |
| active `id=["bad"]` | exit 1, bounded `PROBLEM: task state: task id must look like T001-short-slug`, no traceback |
| active `scope=1` | exit 1, bounded `scope must be a list of text`, no traceback |
| active `depends_on=1` | exit 1, bounded `depends_on must be a list of task ids`, no traceback |
| archived `scope=1` | exit 1 with a bounded archive diagnostic, no traceback |
| archived `depends_on=1` | exit 1 with a bounded archive diagnostic, no traceback |
| archived `status=7` | exit 1 for the invalid archive status and for the active dependent's invalid dependency status, no traceback |
| archived `attempt=false` | exit 1 with `attempt must be a positive integer`, no traceback |

Every diagnostic was below an 8 KiB audit bound. The strongest archived-status
case printed both:

```text
PROBLEM: T002-dependent-on-malformed-status: dependency T001-archive-malformed-status has invalid status
PROBLEM: T001-archive-malformed-status: invalid status 7
```

Its dry run also explicitly said the dependent was waiting on the malformed
archive id. Thus invalid archive state is neither accepted as `ok` nor allowed
to strand an active dependent silently.

A compatibility probe archived one valid `done` and one valid `cancelled`
record, created a valid active task depending on the archived `done` task, and
obtained `ok: 1 active task(s)`. Dry run selected the dependent. The primary
suite's valid archive/dependency, cycles, preflight rollback, signal masking,
and scheduler cases also passed.

### RC-B2 — Unicode line-separator writes are closed

`is_single_line_text` is one reusable predicate based on Python's own
`splitlines()` behavior and requires exactly one unterminated line. Both
`cmd_task_create` and `cmd_memory_add` apply it before taking the write path,
while retaining the existing blank/whitespace, C0/C1 control, and structural
heading checks.

The exact T010 Unicode tests passed. Independent fresh CLI probes tested U+2028
LINE SEPARATOR and U+2029 PARAGRAPH SEPARATOR in both embedded and trailing
positions for both input boundaries:

- all eight `memory add` calls exited 1 with a bounded `single-line` error and
  no traceback; `memory.md` remained byte-for-byte unchanged and a following
  `memory index` exited 0;
- all eight `task create` calls exited 1 with a bounded `single-line` error and
  no traceback; the task directory remained empty.

The same driver successfully created the title `aperçu 東京 😀 — valid`, added
the memory summary `Résumé 東京 😀`, and read both back unchanged. Valid Unicode
that is not a Python-recognized line separator therefore remains compatible.

### Earlier B1–B7 behavior and adjacent safety paths

The eight exact earlier regressions passed together:

- literal POSIX changed paths;
- non-object task state and malformed history;
- empty fenced report sections;
- structural memory values and indexed show behavior;
- multiline title section injection;
- tier flags after punctuation;
- report drift after finish; and
- report drift before review/accept.

The complete 121-test primary suite also passed, covering the adjacent valid
lifecycle, dependency, archive, capsule, memory, scope, symlink, Git snapshot,
lock, lease, signal, timeout, report/evidence, hook, init, and routing behavior.
No broad `TypeError` catch was added to hide programmer defects.

## Performance evidence

`docs/performance.md` now says explicitly that its RSS medians and maxima are
raw process-level observations, not a demonstrated memory improvement, and
that independent peak-RSS reruns were noisy/inconclusive. It claims no RSS
effect size. Its only remaining memory statement is the source/test-backed
implementation fact that status passes its already loaded task list to next
actions instead of constructing and retaining a second parsed list;
`test_status_reuses_loaded_tasks_when_rendering_next_actions` verifies one task
load.

For independent wall-time checking, I reconstructed the actual pre-T006
`framework/baton` blob (`b22b67f`) beside the current framework templates. The
reconstruction uses `os.walk`, lacks `scandir`, lacks the command-local tier
cache, and has the old status next-action reload. I then ran the current
standard-library benchmark unchanged against that source and current
`framework/baton`.

The first 11-sample before→after pass was noisy and is not hidden: start brief
was +1.4%, while the other four claimed workloads still moved in the documented
faster direction (-15.0% status, -2.4% validate, -6.3% capsule, -6.1% archive).
Several after ranges contained scheduler outliers. A fresh 15-sample pass in
the reverse source order reproduced all five documented directions:

| Workload | Before median (range), ms | After median (range), ms | Median direction | Range overlap? |
| --- | ---: | ---: | ---: | --- |
| start brief | 117.881 (114.473–119.879) | 101.528 (100.289–103.277) | -13.9% | no |
| large status | 149.449 (148.068–150.575) | 107.225 (104.505–109.817) | -28.3% | no |
| large validate | 221.676 (217.505–231.354) | 205.023 (200.187–218.820) | -7.5% | slight |
| capsule + 6 refs | 100.414 (98.046–106.013) | 86.034 (84.646–96.616) | -14.3% | no |
| archive stats | 187.090 (185.166–190.741) | 172.701 (171.355–174.588) | -7.7% | no |

The second pass also correctly supplied no gain claim for startup/help (+0.5%),
init (-0.5%), or snapshot/diff (-2.3%): these small/raw directions are not
interpreted. Large-status median RSS happened to be 432 KiB lower in that pass,
while the other workload RSS medians were 560–1,824 KiB higher. Those unpaired
raw observations reinforce the document's inconclusive treatment and are not
promoted to a memory claim.

The wall measurements are host- and run-specific; the historical table's exact
effect sizes and disjoint original seven-sample ranges remain descriptions of
that recorded run, not universal guarantees. The independent 15-sample result
is sufficient to confirm the five remaining directional claims without
reviving RC-B3.

## Attribution and publication state

`.mailmap` contains exactly:

```text
jpawchan <78247292+jpawchan@users.noreply.github.com> naz <78247292+jpawchan@users.noreply.github.com>
```

Raw history has 19 `jpawchan` and one `naz` author/committer record; the lone
raw record is commit `26a9789`, and both names use the same GitHub noreply
address. Mailmap-aware author and committer inspection resolves all 20 records
to:

```text
jpawchan <78247292+jpawchan@users.noreply.github.com>
```

Apple Git 2.50.1 does not implement the requested shortlog-only
`--use-mailmap` flag: the exact command
`git shortlog -sne --all --use-mailmap` exits 129 with `unknown option`.
This Git applies `.mailmap` to `shortlog` by default, and
`git shortlog -sne --all` prints one line, `20 jpawchan ...`. The independent
`git log --all --use-mailmap` author and committer checks likewise print only
20 canonical `jpawchan` records. This tool-version syntax limitation does not
indicate attribution drift and does not require history rewriting.

The read-only public Contributors API returns exactly
`[(jpawchan, 20)]`. The current repository is still
`jpawchan/attention-relay`; its old description remains live. Both the API and
browser URL for `jpawchan/baton` return 404 before rename, as expected. Local
Git identity is already `jpawchan` with the canonical noreply address.

## Verification evidence

### Compile, tests, footprint, and smoke

```text
python3 -m py_compile framework/baton tests/test_baton.py tools/benchmark_performance.py tools/measure_context.py tests/test_context_footprint.py
PY_COMPILE_OK

python3 tests/test_baton.py
Ran 121 tests in 163.755s
OK

python3 tests/test_context_footprint.py
Ran 4 tests in 3.342s
OK
```

The four exact-name T010 malformed-state/Unicode tests ran in 10.529 seconds
and passed. The eight exact-name B1–B7 tests ran in 19.266 seconds and passed.

`python3 tools/measure_context.py --json` regenerated:

- 15,124 Unicode characters, 15,126 UTF-8 bytes, 334 lines;
- payload SHA-256
  `f3ca7044b52318effe0e371048e12b4fff4927be928a9d1b11827fefe80d0616`;
- 3,426 GPT-path logical input tokens; and
- 5,323 Claude-path logical input tokens.

The bundled evidence has the same byte count/hash and identical bracketing
baselines. These are correctly scoped provider-reported harness/API
differentials, not universal tokenizer counts.

A fresh committed temporary repository passed source `init`, then installed
`orchestrator brief --phase start`, `status`, and `validate`. The installed
executable mode was `0700`; `.gitignore` contained `.baton/` once. This historical
audit exercised the startup sections present in that revision, task counts,
decision/review state, and a real recommended command; status printed
`tasks: none`; validation printed `ok: 0 active task(s)`.

### Documentation, paths, names, and links

- `SPEC.md` is byte-identical to the 36,911-byte text between the creation
  prompt's `BEGIN SPEC` / `END SPEC` markers.
- Sixteen pre-report Markdown files and all seven local Markdown links were
  checked; no local target is missing.
- All 44 concrete README/maintainer repository-map paths exist.
- Twelve distinct external research/practitioner URLs returned HTTP 200.
  The sole expected non-2xx publication URL was
  `https://github.com/jpawchan/baton`, HTTP 404 before rename. README's two
  clone examples and the skill install example use that same future URL.
- A case-insensitive old-name/path scan found no canonical old production or
  test path. Remaining matches are intentional: immutable audit/history text,
  `.attention-relay/` audit-harness exclusions, and the legacy
  `.agent-relay/` compatibility ignore.
- `framework/baton` imports only Python standard-library modules, and no package
  manifest or lockfile has been introduced.

### Diff and task scope

`git diff --check HEAD` passes. Relative to the attempt baseline, no pre-existing
tracked, staged, or untracked path changed during this audit. The sole new
Git-visible path from this attempt is:

```text
docs/final-release-audit.md
```

No production, test, README, config, benchmark, mailmap, task JSON, history,
tag, branch, remote, push, or GitHub setting was changed.

## Non-blocking suggestions and residual risks

- `summary.md` still says the primary suite contains 116 tests. It immediately
  warns maintainers to run rather than rely on that count, and the actual suite
  is self-discovering and passes 121 tests, so this stale informational count is
  not a behavior, safety, install, or publication blocker. Update it in a later
  documentation-only cleanup.
- CI runs the complete primary suite on macOS/Ubuntu and Python 3.11/3.13 but
  does not separately invoke `tests/test_context_footprint.py`. Keep the focused
  footprint suite as a release check or add it to CI later.
- The benchmark discards raw RSS samples and does not interleave paired runs.
  Keep process-level RSS conclusions explicitly inconclusive unless that
  methodology changes.
- Existing shared-user/shared-worktree and ignored-file limitations remain the
  documented security boundary. Baton detects cooperative scope/result errors;
  it is not an OS sandbox.
- Provider token evidence is revision-bound. Re-measure through the real
  harness paths whenever an included activation artifact changes.
## Deferred publication operations

After this GO report is independently reviewed and accepted:

1. Commit the complete verified source tree on `main`. Do not amend, rebase,
   filter, replace, or otherwise rewrite the 20 historical commits.
2. Rename the existing GitHub repository in place, preserving redirects,
   issues, stars, and contributor mapping:

   ```text
   gh repo rename baton --repo jpawchan/attention-relay --yes
   ```

3. Set the intended description from the tracked one-line source:

   ```text
   gh repo edit jpawchan/baton --description "$(tr -d '\n' < docs/github-description.txt)"
   ```

4. Point local Git at the canonical remote and publish `main`:

   ```text
   git remote set-url origin https://github.com/jpawchan/baton.git
   git push origin main
   ```

## Post-publication verification

Before announcing a release:

1. Confirm owner/name, URL, description, and default branch:

   ```text
   gh repo view jpawchan/baton --json nameWithOwner,url,description,defaultBranchRef
   git remote -v
   curl -fL https://github.com/jpawchan/baton >/dev/null
   curl -fL https://api.github.com/repos/jpawchan/baton/contributors >/tmp/baton-contributors.json
   ```

   The description must equal `docs/github-description.txt`, the default branch
   must be `main`, both fetch/push remotes must use the Baton URL, and the
   contributors response must still contain only `jpawchan`.

2. Make a disposable unauthenticated clone from the README URL. In that clone,
   rerun py_compile, all 121 primary tests, the four context tests, context
   measurement/hash assertions, and the committed init → installed start brief
   → status → validate smoke.
3. Recheck all local links/map paths and external URLs; the README and skill
   Baton URL must now return 200 rather than the one expected pre-rename 404.
4. Confirm `git status --short` is empty, local `main` equals `origin/main`, and
   `.mailmap` still yields one canonical author and committer identity.
5. Only then publish the release announcement.
