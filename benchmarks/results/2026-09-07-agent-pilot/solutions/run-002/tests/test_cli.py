import contextlib
import io
import os
from pathlib import Path
import runpy
import stat
import tempfile
import unittest
from unittest import mock

from ledger import cli


REPORT = {
    "total_value_cents": 250,
    "items": [{"value_cents": 250, "quantity": 2, "sku": "café"}],
    "events": 1,
}
JSON_LINE = (
    '{"events": 1, "items": [{"quantity": 2, "sku": "caf\\u00e9", '
    '"value_cents": 250}], "total_value_cents": 250}\n'
)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=".")
        self.directory = Path(self.temp.name)
        self.input = self.directory / "events.csv"
        self.input.write_text("sku\ncafé\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, argv, read_result=None, report=REPORT):
        stdout = io.StringIO()
        stderr = io.StringIO()
        if read_result is None:
            read_result = [{"event_id": "e1"}]
        with (
            mock.patch("ledger.cli.read_events", return_value=read_result) as read,
            mock.patch("ledger.cli.summarize", return_value=report) as summarize,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = cli.main(argv)
        return status, stdout.getvalue(), stderr.getvalue(), read, summarize

    def test_stdout_is_sorted_json_with_one_newline_and_utf8_input(self):
        status, stdout, stderr, read, summarize = self.invoke([str(self.input)])

        self.assertEqual(status, 0)
        self.assertEqual(stdout, JSON_LINE)
        self.assertEqual(stderr, "")
        read.assert_called_once_with("sku\ncafé\n")
        summarize.assert_called_once_with([{"event_id": "e1"}], since=None, until=None)

    def test_aware_bounds_are_normalized_to_utc(self):
        status, stdout, stderr, _, summarize = self.invoke([
            str(self.input),
            "--since", "2026-01-02T03:04:05+02:30",
            "--until", "2026-01-03T04:05:06Z",
        ])

        self.assertEqual((status, stdout, stderr), (0, JSON_LINE, ""))
        kwargs = summarize.call_args.kwargs
        self.assertEqual(kwargs["since"].isoformat(), "2026-01-02T00:34:05+00:00")
        self.assertEqual(kwargs["until"].isoformat(), "2026-01-03T04:05:06+00:00")

    def test_equal_bounds_are_valid(self):
        status, _, stderr, _, summarize = self.invoke([
            str(self.input), "--since", "2026-01-02T00:00:00Z",
            "--until", "2026-01-02T00:00:00+00:00",
        ])

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            summarize.call_args.kwargs["since"],
            summarize.call_args.kwargs["until"],
        )

    def test_bad_bounds_return_two_without_calling_core_or_replacing_output(self):
        output = self.directory / "report.json"
        output.write_bytes(b"old bytes")
        cases = [
            ["--since", "not-a-date"],
            ["--since", "2026-01-02T00:00:00"],
            ["--since", "2026-01-03T00:00:00Z", "--until", "2026-01-02T00:00:00Z"],
        ]
        for options in cases:
            with self.subTest(options=options):
                status, stdout, stderr, read, summarize = self.invoke(
                    [str(self.input), "--output", str(output), *options]
                )
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn("error", stderr.lower())
                self.assertNotIn("Traceback", stderr)
                self.assertEqual(output.read_bytes(), b"old bytes")
                read.assert_not_called()
                summarize.assert_not_called()

    def test_argparse_errors_return_two_and_help_returns_zero(self):
        for argv in ([], [str(self.input), "--unknown"]):
            with self.subTest(argv=argv):
                status, stdout, stderr, read, summarize = self.invoke(argv)
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn("error", stderr.lower())
                read.assert_not_called()
                summarize.assert_not_called()

        status, stdout, stderr, read, summarize = self.invoke(["--help"])
        self.assertEqual(status, 0)
        self.assertIn("usage:", stdout)
        self.assertEqual(stderr, "")
        read.assert_not_called()
        summarize.assert_not_called()

    def test_core_errors_preserve_existing_output_and_publish_nothing(self):
        output = self.directory / "report.json"
        output.write_bytes(b"old bytes")
        errors = (
            ("ledger.cli.read_events", ValueError("invalid CSV")),
            ("ledger.cli.summarize", ValueError("conflicting event IDs")),
        )
        for target, error in errors:
            with self.subTest(target=target):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    mock.patch("ledger.cli.read_events", return_value=[]),
                    mock.patch("ledger.cli.summarize", return_value=REPORT),
                    mock.patch(target, side_effect=error),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    status = cli.main([str(self.input), "--output", str(output)])
                self.assertEqual(status, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("error", stderr.getvalue().lower())
                self.assertNotIn("Traceback", stderr.getvalue())
                self.assertEqual(output.read_bytes(), b"old bytes")

    def test_atomic_output_is_regular_beside_destination_fsynced_and_complete(self):
        output = self.directory / "report.json"
        actual_fsync = os.fsync
        actual_replace = os.replace
        events = []

        def fsync(fd):
            self.assertTrue(stat.S_ISREG(os.fstat(fd).st_mode))
            events.append("fsync")
            return actual_fsync(fd)

        def replace(source, destination):
            source = Path(source)
            self.assertEqual(source.parent.resolve(), output.parent.resolve())
            self.assertTrue(source.is_file())
            self.assertTrue(stat.S_ISREG(source.stat().st_mode))
            self.assertEqual(source.read_bytes(), JSON_LINE.encode("utf-8"))
            self.assertEqual(events, ["fsync"])
            events.append("replace")
            return actual_replace(source, destination)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch("ledger.cli.read_events", return_value=[]),
            mock.patch("ledger.cli.summarize", return_value=REPORT),
            mock.patch("ledger.cli.os.fsync", side_effect=fsync),
            mock.patch("ledger.cli.os.replace", side_effect=replace),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = cli.main([str(self.input), "--output", str(output)])

        self.assertEqual(status, 0)
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        self.assertEqual(events, ["fsync", "replace"])
        self.assertEqual(output.read_text(encoding="utf-8"), JSON_LINE)
        self.assertEqual(set(self.directory.iterdir()), {self.input, output})

    def test_replace_failure_preserves_old_bytes_and_cleans_temporary_file(self):
        output = self.directory / "report.json"
        output.write_bytes(b"old bytes")
        baseline = set(self.directory.iterdir())
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch("ledger.cli.read_events", return_value=[]),
            mock.patch("ledger.cli.summarize", return_value=REPORT),
            mock.patch("ledger.cli.os.replace", side_effect=OSError("replace failed")),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = cli.main([str(self.input), "--output", str(output)])

        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("error", stderr.getvalue().lower())
        self.assertEqual(output.read_bytes(), b"old bytes")
        self.assertEqual(set(self.directory.iterdir()), baseline)

    def test_fsync_failure_preserves_old_bytes_and_cleans_temporary_file(self):
        output = self.directory / "report.json"
        output.write_bytes(b"old bytes")
        baseline = set(self.directory.iterdir())
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch("ledger.cli.read_events", return_value=[]),
            mock.patch("ledger.cli.summarize", return_value=REPORT),
            mock.patch("ledger.cli.os.fsync", side_effect=OSError("fsync failed")),
            mock.patch("ledger.cli.os.replace") as replace,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = cli.main([str(self.input), "--output", str(output)])

        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("error", stderr.getvalue().lower())
        replace.assert_not_called()
        self.assertEqual(output.read_bytes(), b"old bytes")
        self.assertEqual(set(self.directory.iterdir()), baseline)

    def test_directory_destination_is_preserved(self):
        output = self.directory / "report.json"
        output.mkdir()
        marker = output / "keep"
        marker.write_bytes(b"keep")
        baseline = set(self.directory.iterdir())

        status, stdout, stderr, _, _ = self.invoke(
            [str(self.input), "--output", str(output)]
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("error", stderr.lower())
        self.assertTrue(output.is_dir())
        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertEqual(set(self.directory.iterdir()), baseline)

    def test_missing_input_and_missing_output_parent_are_errors(self):
        cases = [
            [str(self.directory / "missing.csv")],
            [str(self.input), "--output", str(self.directory / "missing" / "out.json")],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                status, stdout, stderr, _, _ = self.invoke(argv)
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn("error", stderr.lower())
                self.assertNotIn("Traceback", stderr)

    def test_unicode_decode_error_is_reported(self):
        self.input.write_bytes(b"\xff")
        status, stdout, stderr, read, summarize = self.invoke([str(self.input)])
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("error", stderr.lower())
        read.assert_not_called()
        summarize.assert_not_called()

    def test_module_entrypoint_reflects_main_status(self):
        with mock.patch("ledger.cli.main", return_value=7):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_module("ledger.__main__", run_name="__main__")
        self.assertEqual(raised.exception.code, 7)


if __name__ == "__main__":
    unittest.main()
