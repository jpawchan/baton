from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ledger.cli import main


ROOT = Path(__file__).resolve().parents[1]
HEADER = "event_id,sku,quantity,unit_price,when\n"
CSV = (HEADER + "e1,apple,2,1.25,2026-01-01T00:00:00Z\n"
       "e2,Apple,-1,0.29,2026-01-02T00:00:00Z\n")
REPORT = {"events": 2,
          "items": [{"sku": "Apple", "quantity": -1, "value_cents": -29},
                    {"sku": "apple", "quantity": 2, "value_cents": 250}],
          "total_value_cents": 221}
JSON = json.dumps(REPORT, sort_keys=True) + "\n"
OLD_BYTES = b"existing output\x00\xff\n"


class CliTests(unittest.TestCase):
    def setUp(self):
        # Keep all test files inside the project, including atomic staging files.
        directory = tempfile.TemporaryDirectory(dir=ROOT, prefix=".test-ledger-")
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)
        self.input = self.work / "input.csv"
        self.output = self.work / "output.json"
        self.input.write_text(CSV, encoding="utf-8")

    def run_main(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main([str(arg) for arg in args])
        return status, stdout.getvalue(), stderr.getvalue()

    def assert_error(self, result):
        status, stdout, stderr = result
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("error", stderr.lower())
        self.assertNotIn("Traceback", stderr)

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.work.rglob(".ledger-*.tmp")), [])

    def test_stdout_is_deterministic_sorted_json_with_newline(self):
        self.assertEqual(self.run_main(self.input), (0, JSON, ""))
        rows = CSV.splitlines()
        self.input.write_text("\n".join([rows[0], rows[2], rows[1]]) + "\n", encoding="utf-8")
        self.assertEqual(self.run_main(self.input), (0, JSON, ""))

    def test_time_window_and_empty_result(self):
        status, stdout, stderr = self.run_main(
            self.input, "--since", "2025-12-31T19:00:00-05:00",
            "--until", "2026-01-02T00:00:00Z")
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), {
            "events": 1, "items": [{"sku": "apple", "quantity": 2, "value_cents": 250}],
            "total_value_cents": 250,
        })
        self.assertEqual(json.loads(self.run_main(self.input, "--since", "2027-01-01T00:00Z")[1]),
                         {"events": 0, "items": [], "total_value_cents": 0})

    def test_empty_input(self):
        self.input.write_bytes(b"")
        self.assertEqual(self.run_main(self.input),
                         (0, '{"events": 0, "items": [], "total_value_cents": 0}\n', ""))

    def test_file_output_is_synced_and_atomically_replaced(self):
        destination = self.work / "nested"
        destination.mkdir()
        output = destination / "report.json"
        output.write_bytes(OLD_BYTES)
        real_fsync, real_replace = os.fsync, os.replace
        synced = []

        def fsync(fd):
            self.assertTrue(stat.S_ISREG(os.fstat(fd).st_mode))
            self.assertEqual(os.fstat(fd).st_size, len(JSON.encode("utf-8")))
            real_fsync(fd)
            synced.append(fd)

        def replace(source, target):
            self.assertEqual(len(synced), 1)
            self.assertEqual(Path(source).parent, output.parent)
            self.assertNotEqual(Path(source), output)
            self.assertTrue(stat.S_ISREG(os.stat(source).st_mode))
            self.assertEqual(Path(source).read_bytes(), JSON.encode("utf-8"))
            self.assertEqual(output.read_bytes(), OLD_BYTES)
            real_replace(source, target)

        with patch("ledger.cli.os.fsync", side_effect=fsync), \
                patch("ledger.cli.os.replace", side_effect=replace) as replaced:
            self.assertEqual(self.run_main(self.input, "--output", output), (0, "", ""))
        replaced.assert_called_once()
        self.assertEqual(output.read_bytes(), JSON.encode("utf-8"))
        self.assert_no_temporary_files()

    def test_output_can_replace_the_input_after_reading_it(self):
        self.assertEqual(self.run_main(self.input, "--output", self.input), (0, "", ""))
        self.assertEqual(self.input.read_text(encoding="utf-8"), JSON)
        self.assert_no_temporary_files()

    def test_bad_data_and_windows_preserve_existing_output(self):
        cases = [
            (b"invalid header\n", []),
            ((CSV + "e3,apple,0,1,2026-01-01T00:00Z\n").encode(), []),
            (CSV.encode() + b"\xff", []),
            ((CSV + "e1,apple,3,1.25,2026-01-01T00:00:00Z\n").encode(),
             ["--since", "2027-01-01T00:00:00Z"]),
            (CSV.encode(), ["--since", "not-a-date"]),
            (CSV.encode(), ["--until", "2026-01-01T00:00:00"]),
            (CSV.encode(), ["--since", "2026-01-03T00:00:00Z",
                            "--until", "2026-01-01T00:00:00Z"]),
        ]
        for data, options in cases:
            with self.subTest(data=data, options=options):
                self.input.write_bytes(data)
                self.output.write_bytes(OLD_BYTES)
                self.assert_error(self.run_main(self.input, "--output", self.output, *options))
                self.assertEqual(self.output.read_bytes(), OLD_BYTES)
                self.assert_no_temporary_files()
                self.assert_error(self.run_main(self.input, *options))

    def test_bad_arguments_return_instead_of_exiting(self):
        for args in ([], [self.input, "--unknown"], [self.input, "extra"],
                     [self.input, "--out", self.output],
                     [self.input, "--output"], [self.input, "--since"]):
            with self.subTest(args=args):
                self.assert_error(self.run_main(*args))
        status, stdout, stderr = self.run_main("--help")
        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("usage:", stdout)

    def test_missing_input_and_destination_directory(self):
        self.output.write_bytes(OLD_BYTES)
        self.assert_error(self.run_main(self.work / "missing.csv", "--output", self.output))
        self.assertEqual(self.output.read_bytes(), OLD_BYTES)
        missing = self.work / "missing-dir" / "out.json"
        self.assert_error(self.run_main(self.input, "--output", missing))
        self.assertFalse(missing.parent.exists())
        self.assert_no_temporary_files()
        self.assert_error(self.run_main(self.work))

    def test_failed_replace_or_fsync_preserves_output_and_cleans_stage(self):
        for operation in ("replace", "fsync"):
            with self.subTest(operation=operation):
                self.output.write_bytes(OLD_BYTES)
                with patch(f"ledger.cli.os.{operation}", side_effect=OSError("simulated failure")):
                    self.assert_error(self.run_main(self.input, "--output", self.output))
                self.assertEqual(self.output.read_bytes(), OLD_BYTES)
                self.assert_no_temporary_files()

    def test_failed_staging_write_or_flush_cleans_partial_file(self):
        real_temporary = tempfile.NamedTemporaryFile
        for operation in ("write", "flush"):
            with self.subTest(operation=operation):
                self.output.write_bytes(OLD_BYTES)

                def failing_temporary(*args, **kwargs):
                    stream = real_temporary(*args, **kwargs)

                    def fail(*values):
                        if operation == "write":
                            stream.file.write(values[0][:10])
                            stream.file.flush()
                        raise OSError("simulated staging failure")

                    setattr(stream, operation, fail)
                    return stream

                with patch("ledger.cli.tempfile.NamedTemporaryFile", side_effect=failing_temporary), \
                        patch("ledger.cli.os.replace") as replaced:
                    self.assert_error(self.run_main(self.input, "--output", self.output))
                replaced.assert_not_called()
                self.assertEqual(self.output.read_bytes(), OLD_BYTES)
                self.assert_no_temporary_files()

    def test_existing_directory_is_not_removed_on_publication_failure(self):
        for nonempty in (False, True):
            with self.subTest(nonempty=nonempty):
                output = self.work / ("nonempty" if nonempty else "empty")
                output.mkdir()
                if nonempty:
                    (output / "keep").write_bytes(OLD_BYTES)
                before = set(self.work.rglob("*"))
                self.assert_error(self.run_main(self.input, "--output", output))
                self.assertTrue(output.is_dir())
                self.assertEqual(set(self.work.rglob("*")), before)
                if nonempty:
                    self.assertEqual((output / "keep").read_bytes(), OLD_BYTES)

    def test_failed_output_write_is_reported(self):
        class FailingOutput(StringIO):
            def write(self, text):
                raise OSError("stdout unavailable")

        stdout, stderr = FailingOutput(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main([str(self.input)])
        self.assertTrue(stdout.closed)
        self.assert_error((status, "", stderr.getvalue()))

    def test_broken_stdout_pipe_has_status_two_without_shutdown_traceback(self):
        for args in ([str(self.input)], ["--help"]):
            with self.subTest(args=args):
                reader, writer = os.pipe()
                os.close(reader)
                try:
                    result = subprocess.run([sys.executable, "-m", "ledger", *args],
                                            cwd=ROOT, stdout=writer, stderr=subprocess.PIPE,
                                            text=True, check=False)
                finally:
                    os.close(writer)
                self.assert_error((result.returncode, "", result.stderr))
                self.assertNotIn("Exception ignored", result.stderr)

    def test_crlf_multiline_fields_and_utf8_survive_cli_input(self):
        self.input.write_bytes(("\ufeff" + HEADER +
                                'e1," café\r\nfruit ",1,0.29,2026-01-01T00:00Z\r\n').encode("utf-8"))
        status, stdout, stderr = self.run_main(self.input)
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["items"],
                         [{"sku": "café\r\nfruit", "quantity": 1, "value_cents": 29}])

    def test_default_argv(self):
        stdout, stderr = StringIO(), StringIO()
        with patch.object(sys, "argv", ["ledger", str(self.input)]), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(), 0)
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), (JSON, ""))

    def test_python_module_entrypoint(self):
        result = subprocess.run([sys.executable, "-m", "ledger", str(self.input)],
                                cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, JSON, ""))
        self.input.write_bytes(b"\xff")
        result = subprocess.run([sys.executable, "-m", "ledger", str(self.input)],
                                cwd=ROOT, capture_output=True, text=True, check=False)
        self.assert_error((result.returncode, result.stdout, result.stderr))


if __name__ == "__main__":
    unittest.main()
