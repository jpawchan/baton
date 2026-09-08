# Agent benchmarks

- [Live pilot design and commands](agent_pilot/README.md)
- [September 2026 measured results and limitations](results/2026-09-07-agent-pilot/README.md)

## Temporary-directory portability

Use a canonical temporary directory before offline checks or live execution:

```bash
export TMPDIR="$(python3.11 -c 'from pathlib import Path; import tempfile; print(Path(tempfile.gettempdir()).resolve())')"
python3.11 tests/test_agent_benchmarks.py
```

The frozen v1 atomic-publication check resolves a candidate temporary-file path
but compares it with an unresolved temporary-root path. On macOS, `/var` can alias
`/private/var`, causing a false failure despite correct same-directory publication.
CI canonicalizes `TMPDIR` rather than altering that frozen grader or its historical
hash. The measured Linux pilot used canonical `/tmp`; its grades and measurements
are unchanged. The issue and setup-only remedy were reproduced using a symlinked
temporary root on Linux. Future grader versions should normalize both operands.

The initial protocol hashes correspond to commit `4ddb31f`; preserve that version
and the raw experiment directory when auditing the original frozen experiment.
Do not silently replace historical hashes or outcomes with results from a revised
rubric. Live runs consume quota; CI runs offline tests only.
