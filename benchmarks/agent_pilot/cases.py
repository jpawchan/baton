"""Frozen controlled coding tasks. These are fixtures, not production-repo samples."""

COMMON = """Complete the coding request below autonomously in this Git project.
Use Python 3.11+ and the standard library only. Do not install dependencies, use
network services, inspect credentials, or read outside this project. The only
exception is invoking the explicitly configured worker CLIs in the Baton arm.
Do not inspect other runs, benchmark tooling, or evaluation material. Do not
change .gitignore or tests/test_smoke.py; you may add your own tests. Do not
commit, change Git configuration, or run background jobs. Keep changes focused.
Run python3 -m unittest discover -s tests -v and any useful focused checks.
There are additional withheld tests of the SAME requirements below. Do not ask
for clarification or test answers. Report any blocker honestly. Finish with a
concise description of changes, verification, and remaining risks.
"""

RETRY_GOAL = """Repair retry_after.retry_delay(value, now, default=1.0, cap=300.0).
It returns seconds to wait after an HTTP Retry-After header, always as a float.

Contract:
- Validate arguments on EVERY call, including when the header is missing:
  now must be a timezone-aware datetime; default and cap must be int/float
  values (not bool), representable as finite nonnegative floats. Invalid
  arguments raise ValueError, never silently fall back. cap=0 is allowed.
- A missing, blank, malformed, or non-string header returns min(default, cap).
- Strip surrounding whitespace. An ASCII digits-only header is a delay in
  seconds; allow leading zeroes. Saturate at cap without integer-conversion
  limits even for a 5,000-digit header. Signs, fractions, exponent notation,
  and non-ASCII digits are malformed, not numeric delay forms.
- Also accept dates parseable by email.utils.parsedate_to_datetime, ONLY when
  the parsed datetime is timezone-aware. Use the absolute instant difference
  from now, preserving fractional seconds. Past dates give 0; future delays
  saturate at cap. Invalid/naive dates use the fallback.
- Do not mutate input values. Add targeted regressions and update README.md.

Allowed project changes: retry_after.py, README.md, and new tests/test_*.py files.
"""

LEDGER_GOAL = """Finish this small inventory-ledger package: CSV parsing, deterministic
reconciliation, and an atomic-output CLI. Implement the existing module APIs;
add targeted tests and update README.md. No new dependencies.

ledger.csvio.read_events(text) -> list[dict]:
- Parse CSV using its quoting/multiline rules. Permit one leading UTF-8 BOM.
  Header must contain event_id,sku,quantity,unit_price,when exactly once each,
  in any order; reject missing, duplicate, or extra columns. Strip header and
  field boundary whitespace. Skip physically empty CSV records only; an
  all-empty-field record is invalid. Wrong-width rows raise ValueError.
- event_id and sku are nonblank case-sensitive strings. quantity matches ASCII
  [+-]?[0-9]+, is nonzero, and has absolute value <=1,000,000. unit_price matches
  ASCII digits with an optional one/two-digit fractional part, is nonnegative
  and <=1,000,000.00; reject signs, exponents, NaN, infinity, and >2 decimals.
- when is an ISO-8601 datetime accepted by datetime.fromisoformat (trailing Z
  allowed) WITH a timezone offset. Normalize it to timezone.utc; naive/invalid
  timestamps raise ValueError. Malformed rows raise ValueError, not a partial
  result. Empty/header-only input gives [] (but nonempty invalid header fails).
- Each returned record has exactly event_id, sku, quantity (int), unit_cents
  (exact integer cents, no binary-float rounding), timestamp (aware UTC datetime).
  Preserve source event order. Duplicate event IDs are handled by summarize.

ledger.report.summarize(events, since=None, until=None) -> dict:
- events is an iterable of the normalized records above. Do not mutate inputs.
- Deduplicate identical event_id records. Equality means the same sku, quantity,
  unit_cents, and absolute timestamp. A conflicting duplicate raises ValueError
  even if one/both records lie outside the requested time window. Validate all
  duplicates BEFORE discarding out-of-window events. Distinct IDs both count.
- since/until, when present, must be aware datetimes. Reject a reversed window
  (since > until); equal endpoints are valid. Filter on [since, until).
- Return exactly {"events": N, "items": [...], "total_value_cents": T}. N is the
  number of unique included events. Items are sorted by case-sensitive sku,
  each exactly {"sku": S, "quantity": Q, "value_cents": V}; Q is summed signed
  quantity and V is sum(quantity * unit_cents). Retain zero-net-quantity items.
  T is the sum of item values. An empty result has events=0, items=[], total=0.

ledger.cli.main(argv=None) -> int, and python3 -m ledger:
- Usage: INPUT [--output PATH] [--since ISO] [--until ISO]. Read UTF-8 CSV, apply
  the modules above, emit deterministic sort_keys JSON with a trailing newline.
- Without --output write JSON to stdout. With --output publish via a temporary
  regular file in the destination directory, flush/fsync it, then os.replace;
  clean temporary files on failure. Existing output bytes must survive invalid
  input, conflicting IDs, bad date windows, or a failed replacement.
- Success returns 0. Bad data/arguments, invalid UTF-8, and I/O failures return
  2, with concise stderr containing 'error' and no traceback or partial stdout.
  Creating missing output directories is not required. Do not delete existing
  directories or other paths while cleaning up a failed publication.

Allowed project changes: ledger/*.py, README.md, and new tests/test_*.py files.
"""

IGNORE = ".baton/\n__pycache__/\n*.pyc\n"

CASES = {
    "retry_after": {
        "goal": RETRY_GOAL,
        "baton_policy": "auto",
        "files": {
            ".gitignore": IGNORE,
            "README.md": "# HTTP retry delay helper\n\nRun: python3 -m unittest discover -s tests -v\n",
            "retry_after.py": '''"""HTTP retry delay helper; the legacy implementation needs repair."""


def retry_delay(value, now, default=1.0, cap=300.0):
    try:
        return min(float(value), cap)
    except (TypeError, ValueError):
        return default
''',
            "tests/test_smoke.py": '''import unittest
from datetime import datetime, timezone
from retry_after import retry_delay


class SmokeTests(unittest.TestCase):
    def test_numeric_and_cap(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(retry_delay("12", now), 12.0)
        self.assertEqual(retry_delay("999", now, cap=20), 20.0)

    def test_missing(self):
        self.assertEqual(retry_delay(None, datetime.now(timezone.utc)), 1.0)


if __name__ == "__main__":
    unittest.main()
''',
        },
    },
    "ledger": {
        "goal": LEDGER_GOAL,
        "baton_policy": "delegated",
        "files": {
            ".gitignore": IGNORE,
            "README.md": "# Inventory ledger\n\nRun: python3 -m unittest discover -s tests -v\n",
            "ledger/__init__.py": '"""Standard-library inventory ledger."""\n',
            "ledger/__main__.py": "from .cli import main\n\nraise SystemExit(main())\n",
            "ledger/csvio.py": '''"""CSV ingestion for normalized stock events."""


def read_events(text):
    raise NotImplementedError("implement CSV ingestion")
''',
            "ledger/report.py": '''"""Deterministic inventory reconciliation."""


def summarize(events, since=None, until=None):
    raise NotImplementedError("implement reconciliation")
''',
            "ledger/cli.py": '''"""Inventory-ledger command-line interface."""


def main(argv=None):
    raise NotImplementedError("implement CLI")
''',
            "tests/test_smoke.py": '''import unittest
from ledger.csvio import read_events
from ledger.report import summarize


class SmokeTests(unittest.TestCase):
    def test_pipeline(self):
        events = read_events("event_id,sku,quantity,unit_price,when\\n"
                             "e1,apple,2,1.25,2026-01-01T00:00:00Z\\n")
        self.assertEqual(summarize(events), {
            "events": 1, "items": [{"sku": "apple", "quantity": 2,
                                     "value_cents": 250}],
            "total_value_cents": 250,
        })

    def test_empty(self):
        self.assertEqual(summarize(read_events("")), {
            "events": 0, "items": [], "total_value_cents": 0,
        })


if __name__ == "__main__":
    unittest.main()
''',
        },
    },
}


def prompt(case_name, arm, activation):
    case = CASES[case_name]
    if arm == "direct":
        mode = "\nSolve this request yourself with one agent; do not delegate or use Baton.\n"
    elif arm == "baton":
        mode = "\n" + activation + "\n"
        if case["baton_policy"] == "delegated":
            mode += ("For this request I explicitly require Baton worker delegation. "
                     "Choose the task grouping and configured tiers yourself; no tier "
                     "quota or minimum wave size is imposed.\n")
        else:
            mode += ("Use the installed selection policy: direct execution is allowed "
                     "if worker delegation is not worthwhile for this small request.\n")
        mode += ("Worker routes are already configured from my explicit choice. "
                 "Do not alter config or lower model/reasoning settings. "
                 "Use synchronous .baton/baton run, never background it.\n")
    else:
        raise ValueError("unknown arm")
    return COMMON + mode + "\n## Coding request\n" + case["goal"]
