from contextlib import redirect_stderr, redirect_stdout
import io
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


PROJECT = Path(__file__).resolve().parents[1]
HEADER = "event_id,sku,quantity,unit_price,when\n"
CSV = (HEADER + "e1,apple,2,1.25,2026-01-01T00:00:00Z\n"
       "e2,Apple,-1,0.29,2026-01-02T00:00:00Z\n")
REPORT = {"events": 2,
          "items": [{"sku": "Apple", "quantity": -1, "value_cents": -29},
                    {"sku": "apple", "quantity": 2, "value_cents": 250}],
          "total_value_cents": 221}
JSON = json.dumps(REPORT, sort_keys=True) + "\n"


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=PROJECT)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.input = self.directory / "input.csv"
        self.output = self.directory / "output.json"
        self.input.write_text(CSV, encoding="utf-8")

    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_error(self, result):
        code, stdout, stderr = result
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("error", stderr.lower())
        self.assertNotIn("Traceback", stderr)

    def test_stdout_json_and_argv_none(self):
        self.assertEqual(self.invoke([str(self.input)]), (0, JSON, ""))
        with patch.object(sys, "argv", ["ledger", str(self.input)]):
            self.assertEqual(self.invoke(None), (0, JSON, ""))

    def test_filter_arguments(self):
        result = self.invoke([str(self.input), "--since", "2026-01-01T01:00:00+01:00",
                              "--until", "2026-01-02T00:00:00Z"])
        self.assertEqual(result[0], 0)
        self.assertEqual(json.loads(result[1]), {
            "events": 1, "items": [{"sku": "apple", "quantity": 2, "value_cents": 250}],
            "total_value_cents": 250,
        })
        self.assertEqual(result[2], "")
        equal = self.invoke([str(self.input), "--since", "2026-01-01T00:00:00Z",
                             "--until", "2026-01-01T00:00:00Z"])
        self.assertEqual(equal, (0, '{"events": 0, "items": [], "total_value_cents": 0}\n', ""))

    def test_atomic_output_creation_and_replacement(self):
        for existing in [False, True]:
            with self.subTest(existing=existing):
                if existing:
                    self.output.write_bytes(b"old output\x00\xff")
                real_replace, real_fsync = os.replace, os.fsync
                calls = []

                def fsync(fd):
                    self.assertTrue(stat.S_ISREG(os.fstat(fd).st_mode))
                    calls.append("fsync")
                    real_fsync(fd)

                def replace(source, destination):
                    source = Path(source)
                    self.assertEqual(source.parent, self.output.parent)
                    self.assertEqual(source.read_bytes(), JSON.encode("utf-8"))
                    self.assertEqual(Path(destination), self.output)
                    self.assertEqual(calls, ["fsync"])
                    if existing:
                        self.assertEqual(self.output.read_bytes(), b"old output\x00\xff")
                    calls.append("replace")
                    real_replace(source, destination)

                with patch("ledger.cli.os.fsync", side_effect=fsync), \
                     patch("ledger.cli.os.replace", side_effect=replace):
                    self.assertEqual(self.invoke([str(self.input), "--output", str(self.output)]),
                                     (0, "", ""))
                self.assertEqual(calls, ["fsync", "replace"])
                self.assertEqual(self.output.read_bytes(), JSON.encode("utf-8"))
                self.assertEqual(set(self.directory.iterdir()), {self.input, self.output})

    def test_output_may_be_input(self):
        self.assertEqual(self.invoke([str(self.input), "--output", str(self.input)]),
                         (0, "", ""))
        self.assertEqual(self.input.read_text(encoding="utf-8"), JSON)
        self.assertEqual(list(self.directory.iterdir()), [self.input])

    def test_invalid_data_preserves_output_without_partial_stdout(self):
        bad_documents = [
            b"bad,header\n", CSV.encode() + b"bad,row\n", CSV.encode() + b"\xff",
            (CSV + "e1,apple,3,1.25,2026-01-01T00:00:00Z\n").encode(),
            (HEADER + "e1,apple,0,1,2026-01-01T00:00:00Z\n").encode(),
        ]
        self.output.write_bytes(b"preserve me\x00\xff")
        for data in bad_documents:
            for to_file in [False, True]:
                with self.subTest(data=data, to_file=to_file):
                    self.input.write_bytes(data)
                    args = [str(self.input)]
                    if to_file:
                        args += ["--output", str(self.output)]
                    self.assert_error(self.invoke(args))
                    self.assertEqual(self.output.read_bytes(), b"preserve me\x00\xff")
                    self.assertEqual(set(self.directory.iterdir()), {self.input, self.output})

    def test_bad_arguments_and_windows_preserve_output(self):
        self.output.write_bytes(b"old")
        bad_args = [[], ["--unknown"], [str(self.input), "extra"],
                    [str(self.input), "--output"], [str(self.input), "--since"],
                    [str(self.input), "--out", str(self.output)]]
        for args in bad_args:
            with self.subTest(args=args):
                self.assert_error(self.invoke(args))
        for args in [["--since", "bad"], ["--since", "2026-01-01T00:00:00"],
                     ["--until", "2026-01-01"], ["--until", ""],
                     ["--since", "2026-01-02T00:00:00Z", "--until", "2026-01-01T00:00:00Z"]]:
            with self.subTest(args=args):
                self.assert_error(self.invoke([str(self.input), "--output", str(self.output)] + args))
                self.assertEqual(self.output.read_bytes(), b"old")
                self.assertEqual(set(self.directory.iterdir()), {self.input, self.output})
        self.input.write_text(HEADER, encoding="utf-8")
        self.assert_error(self.invoke([str(self.input), "--since", "2026-01-01"]))

    def test_conflict_outside_window_still_preserves_output(self):
        self.input.write_text(CSV + "e1,apple,3,1.25,2026-01-01T00:00:00Z\n", encoding="utf-8")
        self.output.write_bytes(b"old")
        self.assert_error(self.invoke([str(self.input), "--output", str(self.output),
                                       "--since", "2030-01-01T00:00:00Z"]))
        self.assertEqual(self.output.read_bytes(), b"old")

    def test_fsync_and_replace_failures_clean_only_temporary_file(self):
        self.output.write_bytes(b"old")
        unrelated = self.directory / "unrelated.tmp"
        unrelated.write_bytes(b"not ours")
        for function in ["fsync", "replace"]:
            with self.subTest(function=function):
                with patch("ledger.cli.os." + function, side_effect=OSError("simulated failure")):
                    self.assert_error(self.invoke([str(self.input), "--output", str(self.output)]))
                self.assertEqual(self.output.read_bytes(), b"old")
                self.assertEqual(unrelated.read_bytes(), b"not ours")
                self.assertEqual(set(self.directory.iterdir()), {self.input, self.output, unrelated})

    def test_temporary_creation_write_and_flush_failures(self):
        self.output.write_bytes(b"old")
        factory = tempfile.NamedTemporaryFile

        def fail(*args, **kwargs):
            raise OSError("simulated temporary file failure")

        for operation in ["create", "write", "flush"]:
            with self.subTest(operation=operation):
                def temporary(*args, **kwargs):
                    if operation == "create":
                        return fail()
                    stream = factory(*args, **kwargs)
                    setattr(stream, operation, fail)
                    return stream

                with patch("ledger.cli.tempfile.NamedTemporaryFile", side_effect=temporary):
                    self.assert_error(self.invoke([str(self.input), "--output", str(self.output)]))
                self.assertEqual(self.output.read_bytes(), b"old")
                self.assertEqual(set(self.directory.iterdir()), {self.input, self.output})

    def test_directory_destination_and_missing_parent(self):
        self.output.mkdir()
        child = self.output / "keep"
        child.write_bytes(b"keep")
        before = set(self.directory.iterdir())
        self.assert_error(self.invoke([str(self.input), "--output", str(self.output)]))
        self.assertTrue(self.output.is_dir())
        self.assertEqual(child.read_bytes(), b"keep")
        self.assertEqual(set(self.directory.iterdir()), before)
        self.assert_error(self.invoke([str(self.input), "--output",
                                       str(self.directory / "missing" / "output.json")]))
        self.assertEqual(set(self.directory.iterdir()), before)

    def test_symlink_destination_survives_failed_replace(self):
        target = self.directory / "target"
        target.write_bytes(b"target contents")
        self.output.symlink_to(target)
        with patch("ledger.cli.os.replace", side_effect=OSError("replacement failed")):
            self.assert_error(self.invoke([str(self.input), "--output", str(self.output)]))
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(target.read_bytes(), b"target contents")
        self.assertEqual(set(self.directory.iterdir()), {self.input, self.output, target})

    def test_read_and_stdout_io_failures(self):
        self.assert_error(self.invoke([str(self.directory / "missing.csv")]))
        self.assert_error(self.invoke([str(self.directory)]))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(stdout, "write", side_effect=OSError("write failed")), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([str(self.input)])
        self.assert_error((code, stdout.getvalue(), stderr.getvalue()))

    def test_bom_unicode_and_empty_input(self):
        self.input.write_text("\ufeff" + HEADER + "é1,🍎,1,0.29,2026-01-01T00:00:00Z\n",
                              encoding="utf-8")
        result = self.invoke([str(self.input)])
        self.assertEqual(result[0], 0)
        self.assertEqual(json.loads(result[1])["items"][0]["sku"], "🍎")
        self.assertTrue(result[1].endswith("\n"))
        self.input.write_bytes(b"")
        self.assertEqual(self.invoke([str(self.input)]),
                         (0, '{"events": 0, "items": [], "total_value_cents": 0}\n', ""))

    def test_module_closed_stdout_pipe_has_clean_error_exit(self):
        reader, writer = os.pipe()
        os.close(reader)
        try:
            result = subprocess.run([sys.executable, "-m", "ledger", str(self.input)],
                                    cwd=PROJECT, stdout=writer, stderr=subprocess.PIPE,
                                    text=True, check=False)
        finally:
            os.close(writer)
        self.assert_error((result.returncode, "", result.stderr))
        self.assertNotIn("Exception ignored", result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)

    def test_help_and_module_entry_point(self):
        result = self.invoke(["--help"])
        self.assertEqual(result[0], 0)
        self.assertIn("usage:", result[1])
        self.assertEqual(result[2], "")
        result = subprocess.run([sys.executable, "-m", "ledger", str(self.input)],
                                cwd=PROJECT, capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, JSON, ""))
        result = subprocess.run([sys.executable, "-m", "ledger", "--unknown"],
                                cwd=PROJECT, capture_output=True, text=True, check=False)
        self.assert_error((result.returncode, result.stdout, result.stderr))


if __name__ == "__main__":
    unittest.main()
