import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from ledger.cli import main
from ledger.csvio import read_events
from ledger.report import summarize


class CsvParsingTests(unittest.TestCase):
    def test_empty_and_header_only_input(self):
        self.assertEqual(read_events("\n\r\n"), [])
        self.assertEqual(
            read_events("\n event_id , sku , quantity , unit_price , when \n\n"),
            [],
        )

    def test_bom_reordered_header_quoting_and_multiline(self):
        text = (
            "\ufeff\n\n when , event_id , sku , quantity , unit_price \r\n"
            '2025-01-01T02:00:00+02:00,"e,1","Blue\nWidget",-0002,001.2\r\n'
        )
        events = read_events(text)
        self.assertEqual(events[0]["event_id"], "e,1")
        self.assertEqual(events[0]["sku"], "Blue\nWidget")
        self.assertEqual(events[0]["quantity"], -2)
        self.assertEqual(events[0]["unit_cents"], 120)
        self.assertEqual(
            events[0]["timestamp"],
            datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            set(events[0]),
            {"event_id", "sku", "quantity", "unit_cents", "timestamp"},
        )

    def test_csv_errors_width_and_empty_rows_are_value_errors(self):
        cases = [
            'event_id,sku,quantity,unit_price,when\n"unterminated',
            "event_id,sku,quantity,unit_price,when\ne1,sku,1,1.00",
            "event_id,sku,quantity,unit_price,when\n,,,,",
            "event_id,sku,quantity,unit_price,when\n,,,,,extra",
        ]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    read_events(text)

    def test_headers_must_be_exactly_the_expected_fields(self):
        for header in (
            "event_id,sku,quantity,unit_price,extra",
            "event_id,sku,quantity,unit_price",
            "event_id,sku,quantity,unit_price,when,other",
            "event_id,sku,quantity,when,when",
        ):
            with self.subTest(header=header):
                with self.assertRaises(ValueError):
                    read_events(header + "\n")

    def test_strict_numeric_validation_and_leading_zeroes(self):
        valid = read_events(
            "event_id,sku,quantity,unit_price,when\n"
            "e,sku,+0002,0000001.2,2025-01-01T00:00:00Z\n"
        )[0]
        self.assertEqual(valid["quantity"], 2)
        self.assertEqual(valid["unit_cents"], 120)

        bad_quantities = ("0", "+0", "-0", "1.0", "1e2", "١", "1000001", "-1000001", "+")
        for quantity in bad_quantities:
            with self.subTest(quantity=quantity):
                with self.assertRaises(ValueError):
                    read_events(
                        "event_id,sku,quantity,unit_price,when\n"
                        f"e,sku,{quantity},1.00,2025-01-01T00:00:00Z\n"
                    )

        bad_prices = ("-1", "1.", "1.234", "1e2", "NaN", "inf", "+1", "1000000.01")
        for price in bad_prices:
            with self.subTest(price=price):
                with self.assertRaises(ValueError):
                    read_events(
                        "event_id,sku,quantity,unit_price,when\n"
                        f"e,sku,1,{price},2025-01-01T00:00:00Z\n"
                    )

    def test_timestamp_validation_and_utc_normalization(self):
        event = read_events(
            "event_id,sku,quantity,unit_price,when\n"
            "e,sku,1,1.00,2025-01-01T02:00:00+02:00\n"
        )[0]
        self.assertEqual(event["timestamp"].tzinfo, timezone.utc)
        self.assertEqual(event["timestamp"].hour, 0)

        for timestamp in (
            "2025-01-01T00:00:00",
            "not-a-time",
            "9999-12-31T23:59:59-14:00",
        ):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(ValueError):
                    read_events(
                        "event_id,sku,quantity,unit_price,when\n"
                        f"e,sku,1,1.00,{timestamp}\n"
                    )


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2025, 1, 1, tzinfo=timezone.utc)

    def event(self, event_id, sku, quantity, cents, timestamp=None):
        return {
            "event_id": event_id,
            "sku": sku,
            "quantity": quantity,
            "unit_cents": cents,
            "timestamp": timestamp or self.start,
        }

    def test_reconciliation_deduplicates_sorts_and_keeps_zero_net_items(self):
        events = [
            self.event("a", "b", 2, 125),
            self.event("a", "b", 2, 125),
            self.event("z", "A", -1, 200),
            self.event("y", "A", 1, 200),
        ]
        original = [dict(event) for event in events]
        self.assertEqual(
            summarize((event for event in events)),
            {
                "events": 3,
                "items": [
                    {"sku": "A", "quantity": 0, "value_cents": 0},
                    {"sku": "b", "quantity": 2, "value_cents": 250},
                ],
                "total_value_cents": 250,
            },
        )
        self.assertEqual(events, original)

    def test_window_is_half_open_and_bounds_are_absolute(self):
        later = self.start + timedelta(hours=1)
        events = [
            self.event("before", "sku", 1, 1, self.start - timedelta(seconds=1)),
            self.event("at", "sku", 2, 1, self.start),
            self.event("end", "sku", 4, 1, later),
        ]
        self.assertEqual(
            summarize(events, since=self.start, until=later),
            {
                "events": 1,
                "items": [{"sku": "sku", "quantity": 2, "value_cents": 2}],
                "total_value_cents": 2,
            },
        )
        self.assertEqual(summarize(events, since=later, until=later)["events"], 0)

    def test_conflicting_duplicate_is_checked_outside_window(self):
        outside = self.start - timedelta(days=1)
        events = [
            self.event("same", "sku-a", 1, 100, outside),
            self.event("same", "sku-b", 1, 100, outside),
        ]
        with self.assertRaises(ValueError):
            summarize(events, since=self.start)

    def test_equal_absolute_timestamps_are_matching_duplicates(self):
        plus_one = self.start + timedelta(hours=1)
        same_instant = datetime(
            2025, 1, 1, 2, tzinfo=timezone(timedelta(hours=1))
        )
        events = [
            self.event("same", "sku", 1, 100, plus_one),
            self.event("same", "sku", 1, 100, same_instant),
        ]
        self.assertEqual(summarize(events)["events"], 1)

    def test_bounds_must_be_aware_and_ordered(self):
        with self.assertRaises(ValueError):
            summarize([], since=datetime(2025, 1, 1))
        with self.assertRaises(ValueError):
            summarize([], until=datetime(2025, 1, 1))
        with self.assertRaises(ValueError):
            summarize([], since=self.start + timedelta(days=1), until=self.start)


class CliTests(unittest.TestCase):
    CSV = (
        "event_id,sku,quantity,unit_price,when\n"
        "e,sku,2,1.25,2025-01-01T00:00:00Z\n"
    )

    def make_input(self, directory, content=None, name="input.csv"):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8", newline="") as stream:
            stream.write(self.CSV if content is None else content)
        return path

    def test_deterministic_stdout_and_bad_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = self.make_input(directory)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = main([input_path])
            self.assertEqual(status, 0)
            expected = json.dumps(
                {
                    "events": 1,
                    "items": [{"sku": "sku", "quantity": 2, "value_cents": 250}],
                    "total_value_cents": 250,
                },
                sort_keys=True,
            ) + "\n"
            self.assertEqual(stdout.getvalue(), expected)
            self.assertEqual(stderr.getvalue(), "")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = main([])
        self.assertEqual(status, 2)
        self.assertIn("error", stderr.getvalue().lower())
        self.assertNotIn("traceback", stderr.getvalue().lower())

    def test_buffered_stdout_flush_failure_returns_two_without_shutdown_diagnostic(self):
        class FlushFailingStdout(io.StringIO):
            def flush(self):
                raise BrokenPipeError("flush failed")

        with tempfile.TemporaryDirectory() as directory:
            input_path = self.make_input(directory)
            stdout = FlushFailingStdout()
            stderr = io.StringIO()
            with mock.patch("ledger.cli.sys.stdout", stdout), contextlib.redirect_stderr(
                stderr
            ):
                status = main([input_path])

        self.assertEqual(status, 2)
        self.assertIn("error", stderr.getvalue().lower())
        self.assertNotIn("traceback", stderr.getvalue().lower())
        self.assertTrue(stdout.getvalue())

    def test_module_broken_pipe_uses_project_local_temp_and_returns_two(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory(dir=project_root) as directory:
            input_path = self.make_input(directory)
            read_fd, write_fd = os.pipe()
            os.close(read_fd)
            try:
                completed = subprocess.run(
                    [sys.executable, "-m", "ledger", input_path],
                    stdout=write_fd,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=project_root,
                )
            finally:
                os.close(write_fd)

        self.assertEqual(completed.returncode, 2)
        self.assertRegex(completed.stderr, r"(?i)^error: .*\n$")
        self.assertNotIn("exception ignored", completed.stderr.lower())
        self.assertNotIn("traceback", completed.stderr.lower())

    def test_failed_replace_preserves_destination_cleans_temp_and_fsyncs_first(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = self.make_input(directory)
            output_path = os.path.join(directory, "report.json")
            with open(output_path, "wb") as stream:
                stream.write(b"old bytes")
            before = set(os.listdir(directory))
            calls = []
            real_fsync = os.fsync

            def fsync(fd):
                calls.append("fsync")
                return real_fsync(fd)

            def fail_replace(source, destination):
                calls.append("replace")
                raise OSError("replace failed")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("ledger.cli.os.fsync", side_effect=fsync), mock.patch(
                "ledger.cli.os.replace", side_effect=fail_replace
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = main([input_path, "--output", output_path])

            self.assertEqual(status, 2)
            self.assertEqual(calls, ["fsync", "replace"])
            with open(output_path, "rb") as stream:
                self.assertEqual(stream.read(), b"old bytes")
            self.assertEqual(set(os.listdir(directory)), before)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("error", stderr.getvalue().lower())

    def test_invalid_input_bounds_and_utf8_do_not_touch_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "report.json")
            with open(output_path, "wb") as stream:
                stream.write(b"keep")

            invalid_path = self.make_input(
                directory,
                "event_id,sku,quantity,unit_price,when\n"
                "e,sku,1,1.00,2025-01-01T00:00:00\n",
                "invalid.csv",
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(
                    main([invalid_path, "--output", output_path]),
                    2,
                )
            self.assertIn("error", stderr.getvalue().lower())
            with open(output_path, "rb") as stream:
                self.assertEqual(stream.read(), b"keep")

            valid_path = self.make_input(directory, name="valid.csv")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(
                    main(
                        [
                            valid_path,
                            "--output",
                            output_path,
                            "--since",
                            "2025-01-02T00:00:00",
                        ]
                    ),
                    2,
                )
            with open(output_path, "rb") as stream:
                self.assertEqual(stream.read(), b"keep")

            bad_utf8 = os.path.join(directory, "bad.csv")
            with open(bad_utf8, "wb") as stream:
                stream.write(b"\xff")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(main([bad_utf8, "--output", output_path]), 2)
            with open(output_path, "rb") as stream:
                self.assertEqual(stream.read(), b"keep")


if __name__ == "__main__":
    unittest.main()
