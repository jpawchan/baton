#!/usr/bin/env python3
"""Mode-blind, frozen functional checks; never copied into solver workspaces."""

import argparse
import copy
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import importlib
import io
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

UTC = timezone.utc
PROJECT = None
HEADER = "event_id,sku,quantity,unit_price,when\n"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class RetryChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("retry_after")

    def delay(self, value, **kwargs):
        return self.module.retry_delay(value, kwargs.pop("now", NOW), **kwargs)

    def test_critical_numeric_and_cap(self):
        self.assertEqual(self.delay("42", cap=20), 20.0)
        self.assertEqual(self.delay(" 003 "), 3.0)
        self.assertIs(type(self.delay("2")), float)

    def test_critical_http_date(self):
        self.assertEqual(self.delay("Thu, 01 Jan 2026 12:00:17 GMT"), 17.0)

    def test_fallback_is_capped_and_float(self):
        for value in (None, "", "  ", "bogus", 1, [], {}):
            with self.subTest(value=value):
                self.assertEqual(self.delay(value, default=8, cap=3), 3.0)
                self.assertIs(type(self.delay(value, default=8, cap=3)), float)

    def test_invalid_numeric_forms(self):
        for value in ("+2", "-2", "1.5", "1e2", "NaN", "inf", "١٢", "１２"):
            with self.subTest(value=value):
                self.assertEqual(self.delay(value, default=7), 7.0)

    def test_very_long_digits_saturate_without_int_limit(self):
        self.assertEqual(self.delay("9" * 5000, cap=17), 17.0)
        self.assertEqual(self.delay("0" * 5000 + "2", cap=17), 2.0)

    def test_past_and_far_future(self):
        self.assertEqual(self.delay("Wed, 31 Dec 2025 12:00:00 GMT"), 0.0)
        self.assertEqual(self.delay("Fri, 02 Jan 2026 12:00:00 GMT", cap=21), 21.0)

    def test_absolute_timezone_instant(self):
        now = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
        self.assertEqual(self.delay("Thu, 01 Jan 2026 07:00:09 -0500", now=now), 9.0)

    def test_fractional_date_delay(self):
        self.assertEqual(self.delay("Thu, 01 Jan 2026 12:00:01 GMT",
                                    now=NOW.replace(microsecond=750000)), 0.25)

    def test_naive_and_impossible_dates_fall_back(self):
        for value in ("Thu, 01 Jan 2026 12:00:10", "Thu, 32 Jan 2026 12:00:10 GMT"):
            self.assertEqual(self.delay(value, default=4), 4.0)

    def test_critical_invalid_now(self):
        for now in (datetime(2026, 1, 1), None, "2026-01-01", True):
            for value in (None, "2"):
                with self.subTest(now=now, value=value), self.assertRaises(ValueError):
                    self.delay(value, now=now)

    def test_invalid_default_on_valid_or_missing_header(self):
        for default in (True, -1, math.inf, math.nan, "2", None, 10 ** 400):
            for value in (None, "12"):
                with self.subTest(default=str(default)[:40]), self.assertRaises(ValueError):
                    self.delay(value, default=default)

    def test_invalid_cap_on_valid_or_missing_header(self):
        for cap in (True, -1, math.inf, math.nan, "2", None, 10 ** 400):
            for value in (None, "12"):
                with self.subTest(cap=str(cap)[:40]), self.assertRaises(ValueError):
                    self.delay(value, cap=cap)

    def test_zero_cap_and_fractional_bounds(self):
        for value in (None, "4", "Thu, 01 Jan 2026 12:00:08 GMT", "x"):
            self.assertEqual(self.delay(value, cap=0), 0.0)
        self.assertEqual(self.delay("3", cap=0.125), 0.125)
        self.assertEqual(self.delay(None, default=0.125), 0.125)

    def test_inputs_unchanged(self):
        now = NOW
        header = " 003 "
        self.delay(header, now=now)
        self.assertEqual(header, " 003 ")
        self.assertEqual(now, NOW)


class LedgerChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.csvio = importlib.import_module("ledger.csvio")
        cls.report = importlib.import_module("ledger.report")

    def parse(self, rows, header=HEADER):
        return self.csvio.read_events(header + rows)

    def event(self, identifier="e1", sku="apple", quantity=1, cents=125, when=NOW):
        return dict(event_id=identifier, sku=sku, quantity=quantity,
                    unit_cents=cents, timestamp=when)

    def cli(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
        return subprocess.run([sys.executable, "-m", "ledger", *map(str, args)],
                              cwd=PROJECT, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=15)

    def test_critical_pipeline_and_exact_cents(self):
        events = self.parse("a,apple,3,0.29,2026-01-01T12:00:00Z\n")
        self.assertEqual(events, [self.event("a", quantity=3, cents=29)])
        self.assertEqual(self.report.summarize(events), {
            "events": 1, "items": [{"sku": "apple", "quantity": 3, "value_cents": 87}],
            "total_value_cents": 87,
        })

    def test_csv_quoting_bom_header_order_and_timezone(self):
        data = ('\ufeffwhen, unit_price ,sku,quantity,event_id\n'
                '2026-01-01T17:30:00+05:30,1.2,"red, apple",+2, x \n')
        self.assertEqual(self.csvio.read_events(data),
                         [self.event("x", "red, apple", 2, 120)])
        quoted = self.parse('"a""b","red\napple",1,0,2026-01-01T12:00:00Z\n')
        self.assertEqual(quoted, [self.event('a"b', "red\napple", 1, 0)])

    def test_empty_input_and_blank_records(self):
        for data in ("", HEADER, HEADER + "\n\n"):
            self.assertEqual(self.csvio.read_events(data), [])
        with self.assertRaises(ValueError):
            self.parse(",,,,\n")

    def test_rejects_wrong_headers_and_widths(self):
        for header in ("event_id,sku,quantity,unit_price\n",
                       "event_id,sku,quantity,unit_price,when,extra\n",
                       "event_id,sku,quantity,unit_price,sku\n"):
            with self.subTest(header=header), self.assertRaises(ValueError):
                self.csvio.read_events(header)
        for row in ("a,b,1,1\n", "a,b,1,1,2026-01-01T00:00:00Z,extra\n"):
            with self.assertRaises(ValueError):
                self.parse(row)

    def test_rejects_blank_ids_and_skus(self):
        for row in (" ,a,1,1,2026-01-01T00:00:00Z\n",
                    "a, ,1,1,2026-01-01T00:00:00Z\n"):
            with self.assertRaises(ValueError):
                self.parse(row)

    def test_quantity_validation(self):
        for value in ("0", "-0", "1.0", "1e2", "١", "1000001", "-1000001", "--1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse(f"a,b,{value},1,2026-01-01T00:00:00Z\n")
        self.assertEqual(self.parse("a,b,-1000000,1,2026-01-01T00:00:00Z\n")[0]["quantity"], -1000000)

    def test_price_validation_and_boundaries(self):
        for value in ("-1", "+1", "NaN", "Infinity", "1e2", "0.001", "١", "1000000.01"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse(f"a,b,1,{value},2026-01-01T00:00:00Z\n")
        self.assertEqual(self.parse("a,b,1,1000000.00,2026-01-01T00:00:00Z\n")[0]["unit_cents"], 100000000)

    def test_timestamp_validation_and_no_partial_parse(self):
        for value in ("2026-01-01T00:00:00", "not-a-date", "2026-02-30T00:00:00Z"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse("ok,a,1,1,2026-01-01T00:00:00Z\n" + f"bad,a,1,1,{value}\n")

    def test_identical_duplicates_and_distinct_ids(self):
        first = self.event()
        same = dict(first, timestamp=NOW.astimezone(timezone(timedelta(hours=-4))))
        self.assertEqual(self.report.summarize([first, same, self.event("e2")])["events"], 2)

    def test_critical_conflicting_duplicates(self):
        for change in ({"sku": "pear"}, {"quantity": 2}, {"unit_cents": 126},
                       {"timestamp": NOW + timedelta(seconds=1)}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.report.summarize([self.event(), dict(self.event(), **change)])

    def test_conflict_is_checked_before_time_filter(self):
        with self.assertRaises(ValueError):
            self.report.summarize([self.event(), self.event(quantity=2)],
                                  since=NOW + timedelta(days=1))
        with self.assertRaises(ValueError):
            self.report.summarize([self.event(), self.event(when=NOW + timedelta(days=2))],
                                  until=NOW + timedelta(days=1))

    def test_half_open_window_and_empty_equal_window(self):
        events = [self.event("a", when=NOW - timedelta(seconds=1)), self.event("b"),
                  self.event("c", when=NOW + timedelta(seconds=1))]
        result = self.report.summarize(events, since=NOW, until=NOW + timedelta(seconds=1))
        self.assertEqual(result["events"], 1)
        self.assertEqual(self.report.summarize(events, since=NOW, until=NOW)["items"], [])

    def test_invalid_windows(self):
        for kwargs in ({"since": NOW.replace(tzinfo=None)}, {"until": "bad"},
                       {"since": NOW + timedelta(seconds=1), "until": NOW}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.report.summarize([], **kwargs)

    def test_deterministic_signed_aggregation_and_zero_net(self):
        events = [self.event("a", "z", 2, 100), self.event("b", "A", -1, 300),
                  self.event("c", "z", -2, 50)]
        self.assertEqual(self.report.summarize(iter(events)), {
            "events": 3,
            "items": [{"sku": "A", "quantity": -1, "value_cents": -300},
                      {"sku": "z", "quantity": 0, "value_cents": 100}],
            "total_value_cents": -200,
        })

    def test_input_immutability(self):
        events = [self.event("b", "z"), self.event("a", "A"), self.event("b", "z")]
        original = copy.deepcopy(events)
        self.report.summarize(events, since=NOW)
        self.assertEqual(events, original)

    def test_critical_cli_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.csv"
            source.write_text(HEADER + "a,x,2,1.25,2026-01-01T12:00:00Z\n")
            result = self.cli(source)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            expected = {"events": 1, "items": [{"sku": "x", "quantity": 2, "value_cents": 250}],
                        "total_value_cents": 250}
            self.assertEqual(result.stdout, json.dumps(expected, sort_keys=True) + "\n")

    def test_cli_date_window(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.csv"
            source.write_text(HEADER + "a,x,2,1.25,2026-01-01T12:00:00Z\n")
            result = self.cli(source, "--since", "2026-01-01T12:00:01Z")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["events"], 0)

    def test_cli_file_publication_and_no_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "input.csv", Path(directory) / "out.json"
            source.write_text(HEADER)
            target.write_text("old data")
            result = self.cli(source, "--output", target)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(json.loads(target.read_text()), {"events": 0, "items": [], "total_value_cents": 0})
            self.assertTrue(target.read_bytes().endswith(b"\n"))
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["input.csv", "out.json"])

    def test_critical_cli_invalid_input_preserves_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "input.csv", Path(directory) / "out.json"
            target.write_bytes(b"keep these bytes")
            rows = [b"\xff", b"wrong,header\n", (HEADER + "a,x,1,1,2026-01-01T12:00:00Z\n"
                    "a,x,2,1,2026-01-01T12:00:00Z\n").encode()]
            for contents in rows:
                source.write_bytes(contents)
                result = self.cli(source, "--output", target)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("error", result.stderr.lower())
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(target.read_bytes(), b"keep these bytes")
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["input.csv", "out.json"])

    def test_critical_atomic_failure_after_regular_file_fsync(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "input.csv", root / "out.json"
            source.write_text(HEADER)
            target.write_bytes(b"previous result")
            regular_syncs = []
            replacements = []
            real_fsync = os.fsync

            def sync(fd):
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    regular_syncs.append(fd)
                return real_fsync(fd)

            def fail_replace(src, dst, *args, **kwargs):
                replacements.append((src, dst))
                self.assertTrue(regular_syncs, "publish must follow a regular-file fsync")
                self.assertEqual(Path(src).resolve().parent, root)
                self.assertEqual(json.loads(Path(src).read_text())["events"], 0)
                raise OSError("injected replacement failure")

            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch("os.fsync", side_effect=sync), mock.patch("os.replace", side_effect=fail_replace):
                cli_module = importlib.import_module("ledger.cli")
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli_module.main([str(source), "--output", str(target)])
            self.assertEqual(code, 2)
            self.assertTrue(replacements, "atomic publication must use os.replace")
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("error", stderr.getvalue().lower())
            self.assertEqual(target.read_bytes(), b"previous result")
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["input.csv", "out.json"])

    def test_cli_bad_args_io_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.csv"
            source.write_text(HEADER)
            target = root / "existing-dir"
            target.mkdir()
            (target / "keep").write_text("safe")
            for args in ((root / "missing.csv",), (source, "--since", "nonsense"),
                         (source, "--since", "2026-01-02T00:00:00Z", "--until", "2026-01-01T00:00:00Z"),
                         (source, "--output", target), (source, "--output", root / "absent" / "out.json")):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("error", result.stderr.lower())
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(result.stdout, "")
            self.assertEqual((target / "keep").read_text(), "safe")
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["existing-dir", "input.csv"])


class Results(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.passed = []
        self.bad = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id().split(".")[-1])

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.bad.append(test.id().split(".")[-1])

    def addError(self, test, err):
        super().addError(test, err)
        self.bad.append(test.id().split(".")[-1])


def main():
    global PROJECT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=("retry_after", "ledger"))
    parser.add_argument("project", type=Path)
    args = parser.parse_args()
    PROJECT = args.project.resolve()
    sys.path.insert(0, str(PROJECT))
    cls = RetryChecks if args.case == "retry_after" else LedgerChecks
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(cls)
    total = suite.countTestCases()
    critical = {name for name in unittest.defaultTestLoader.getTestCaseNames(cls)
                if name.startswith("test_critical_")}
    result = Results()
    suite.run(result)
    print(json.dumps({
        "total": total, "passed": len(result.passed),
        "quality": len(result.passed) / total,
        "critical_checks_passed": critical.issubset(result.passed),
        "passed_checks": sorted(result.passed), "failed_checks": sorted(result.bad),
        "tests_run": result.testsRun,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
