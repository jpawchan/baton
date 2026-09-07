#!/usr/bin/env python3
"""Focused tests for the strict paired-run delegation evaluator."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "evaluate_delegation.py"
SPEC = importlib.util.spec_from_file_location("evaluate_delegation", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load tools/evaluate_delegation.py")
EVALUATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVALUATOR)


VERDICTS = (
    "inconclusive",
    "quality_gate_failed",
    "quality_regression",
    "strict_win",
    "noninferior_saving",
    "quality_gain_only",
    "no_win",
)


def run_data(
    quality=0.8,
    critical_checks_passed=True,
    usage_complete=True,
    logical_input_tokens=100,
    output_tokens=20,
    elapsed_seconds=1.0,
):
    return {
        "quality": quality,
        "critical_checks_passed": critical_checks_passed,
        "usage_complete": usage_complete,
        "logical_input_tokens": logical_input_tokens,
        "output_tokens": output_tokens,
        "elapsed_seconds": elapsed_seconds,
    }


def pair(case_id="case-1", rubric_id="rubric-1", direct=None, baton=None):
    return {
        "case_id": case_id,
        "rubric_id": rubric_id,
        "direct": direct if direct is not None else run_data(),
        "baton": baton if baton is not None else run_data(),
    }


def document(*pairs):
    return {"schema_version": 1, "pairs": list(pairs)}


class EvaluateDelegationTests(unittest.TestCase):
    def test_all_seven_verdicts_and_pair_metrics(self):
        pairs = [
            pair(
                "incomplete",
                direct=run_data(usage_complete=False),
                baton=run_data(usage_complete=True, logical_input_tokens=1, output_tokens=1),
            ),
            pair(
                "gate",
                direct=run_data(critical_checks_passed=False),
                baton=run_data(quality=0.9, logical_input_tokens=1, output_tokens=1),
            ),
            pair(
                "regression",
                direct=run_data(quality=0.8),
                baton=run_data(quality=0.7, logical_input_tokens=1, output_tokens=1),
            ),
            pair(
                "strict",
                direct=run_data(quality=0.8),
                baton=run_data(quality=0.9, logical_input_tokens=90, output_tokens=10),
            ),
            pair(
                "equal-saving",
                direct=run_data(quality=0.8),
                baton=run_data(quality=0.8, logical_input_tokens=90, output_tokens=10),
            ),
            pair(
                "quality-only",
                direct=run_data(quality=0.8),
                baton=run_data(quality=0.9, logical_input_tokens=120, output_tokens=20),
            ),
            pair(
                "none",
                direct=run_data(quality=0.8),
                baton=run_data(quality=0.8, logical_input_tokens=100, output_tokens=20),
            ),
        ]
        result = EVALUATOR.evaluate(document(*pairs))
        self.assertEqual(result["summary"]["pairs"], 7)
        self.assertEqual(result["summary"]["verdicts"], {name: 1 for name in VERDICTS})
        by_case = {item["case_id"]: item for item in result["pairs"]}
        self.assertEqual(by_case["strict"]["direct_total_tokens"], 120)
        self.assertEqual(by_case["strict"]["baton_total_tokens"], 100)
        self.assertEqual(by_case["strict"]["token_delta"], -20)
        self.assertAlmostEqual(by_case["strict"]["quality_delta"], 0.1)
        self.assertIsNone(by_case["incomplete"]["token_delta"])
        self.assertIsNone(by_case["incomplete"]["direct_total_tokens"])

    def test_equality_zero_and_elapsed_does_not_affect_verdict(self):
        direct = run_data(quality=0, logical_input_tokens=0, output_tokens=0, elapsed_seconds=0)
        baton = run_data(quality=0, logical_input_tokens=0, output_tokens=0, elapsed_seconds=999)
        result = EVALUATOR.evaluate(document(pair(direct=direct, baton=baton)))
        item = result["pairs"][0]
        self.assertEqual(item["verdict"], "no_win")
        self.assertEqual(item["direct_total_tokens"], 0)
        self.assertEqual(item["baton_total_tokens"], 0)
        self.assertEqual(item["token_delta"], 0)
        self.assertEqual(item["quality_delta"], 0)

        saving = pair(
            direct=run_data(quality=1, logical_input_tokens=1, output_tokens=0),
            baton=run_data(quality=1, logical_input_tokens=0, output_tokens=0),
        )
        self.assertEqual(EVALUATOR.evaluate(document(saving))["pairs"][0]["verdict"], "noninferior_saving")

    def test_incomplete_usage_never_imputes_or_passes(self):
        cases = [
            (run_data(usage_complete=False), run_data()),
            (run_data(logical_input_tokens=None), run_data()),
            (run_data(output_tokens=None), run_data()),
            (run_data(), run_data(usage_complete=False)),
            (run_data(), run_data(logical_input_tokens=None)),
            (run_data(), run_data(output_tokens=None)),
        ]
        for direct, baton in cases:
            with self.subTest(direct=direct, baton=baton):
                item = EVALUATOR.evaluate(document(pair(direct=direct, baton=baton)))["pairs"][0]
                self.assertEqual(item["verdict"], "inconclusive")
                self.assertIsNone(item["token_delta"])

    def test_required_and_extra_keys_are_rejected_at_each_object_level(self):
        base = document(pair())
        for location, key in (
            ("top", "schema_version"),
            ("pair", "case_id"),
            ("direct", "quality"),
        ):
            with self.subTest(location=location):
                bad = copy.deepcopy(base)
                if location == "top":
                    del bad[key]
                elif location == "pair":
                    del bad["pairs"][0][key]
                else:
                    del bad["pairs"][0]["direct"][key]
                with self.assertRaisesRegex(ValueError, "missing required key"):
                    EVALUATOR.evaluate(bad)

        for location in ("top", "pair", "baton"):
            with self.subTest(location=location):
                bad = copy.deepcopy(base)
                if location == "top":
                    bad["unexpected"] = 1
                elif location == "pair":
                    bad["pairs"][0]["unexpected"] = 1
                else:
                    bad["pairs"][0]["baton"]["unexpected"] = 1
                with self.assertRaisesRegex(ValueError, "unexpected key"):
                    EVALUATOR.evaluate(bad)

    def test_schema_types_ids_and_duplicate_case_ids(self):
        for version in (True, 1.0, 2, None):
            with self.subTest(version=version):
                bad = document(pair())
                bad["schema_version"] = version
                with self.assertRaises(ValueError):
                    EVALUATOR.evaluate(bad)

        for bad_id in ("", " \t ", "line\nfeed", "control\x00", "\u2028", "x" * 161):
            with self.subTest(bad_id=repr(bad_id)):
                with self.assertRaises(ValueError):
                    EVALUATOR.evaluate(document(pair(case_id=bad_id)))

        duplicate = document(pair("same"), pair("same", rubric_id="other"))
        with self.assertRaisesRegex(ValueError, "duplicate case_id"):
            EVALUATOR.evaluate(duplicate)

    def test_bool_negative_nan_and_infinity_are_rejected(self):
        fields = (
            ("quality", True),
            ("quality", -0.1),
            ("quality", float("nan")),
            ("quality", float("inf")),
            ("quality", -float("inf")),
            ("critical_checks_passed", 1),
            ("usage_complete", 0),
            ("logical_input_tokens", True),
            ("logical_input_tokens", -1),
            ("output_tokens", False),
            ("output_tokens", -1),
            ("elapsed_seconds", True),
            ("elapsed_seconds", -1.0),
            ("elapsed_seconds", float("nan")),
            ("elapsed_seconds", float("inf")),
        )
        for field, value in fields:
            with self.subTest(field=field, value=repr(value)):
                bad = document(pair())
                bad["pairs"][0]["direct"][field] = value
                with self.assertRaises(ValueError):
                    EVALUATOR.evaluate(bad)

    def test_json_malformed_duplicate_and_nonfinite_values_are_rejected(self):
        malformed = ("{", "[]", '{"schema_version":1,"pairs":}')
        for text in malformed:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    EVALUATOR.evaluate(
                        EVALUATOR.load_json(self._write_json_text(text))
                    )

        duplicate = '{"schema_version":1,"schema_version":1,"pairs":[]}'
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            EVALUATOR.load_json(self._write_json_text(duplicate))

        for text in (
            '{"schema_version":1,"pairs":[],"x":NaN}',
            '{"schema_version":1,"pairs":[],"x":Infinity}',
            '{"schema_version":1,"pairs":[],"x":-Infinity}',
        ):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "non-finite"):
                    EVALUATOR.load_json(self._write_json_text(text))

    def test_nonfinite_exponent_overflow_and_python_input_immutability(self):
        source = document(pair())
        original = copy.deepcopy(source)
        result = EVALUATOR.evaluate(source)
        self.assertEqual(source, original)
        result["pairs"][0]["case_id"] = "changed-result-only"
        self.assertEqual(source, original)

        overflow = '{"schema_version":1,"pairs":[{"case_id":"c","rubric_id":"r","direct":' \
            '{"quality":1e999,"critical_checks_passed":true,"usage_complete":true,' \
            '"logical_input_tokens":1,"output_tokens":1,"elapsed_seconds":0},' \
            '"baton":' + json.dumps(run_data()) + '} ]}'
        with self.assertRaises(ValueError):
            EVALUATOR.evaluate(EVALUATOR.load_json(self._write_json_text(overflow)))

    def test_cli_success_json_and_text(self):
        with tempfile.TemporaryDirectory(prefix="evaluate-delegation-") as directory:
            path = Path(directory) / "measurements.json"
            path.write_text(json.dumps(document(pair())), encoding="utf-8")
            json_run = subprocess.run(
                [sys.executable, str(MODULE_PATH), str(path), "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(json_run.returncode, 0, json_run.stderr)
            self.assertEqual(json.loads(json_run.stdout)["summary"]["pairs"], 1)
            self.assertEqual(json_run.stderr, "")

            text_run = subprocess.run(
                [sys.executable, str(MODULE_PATH), str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(text_run.returncode, 0, text_run.stderr)
            self.assertIn("case_id=case-1", text_run.stdout)
            self.assertIn("verdict=no_win", text_run.stdout)
            self.assertIn("token_delta=0", text_run.stdout)
            self.assertIn("quality_delta=0.0", text_run.stdout)
            self.assertIn("verdict counts:", text_run.stdout)

    def test_cli_bad_input_is_status_two_without_traceback(self):
        with tempfile.TemporaryDirectory(prefix="evaluate-delegation-") as directory:
            path = Path(directory) / "bad.json"
            path.write_text('{"schema_version":1,"pairs":[]}', encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(MODULE_PATH), str(path), "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("error:", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertEqual(completed.stdout, "")

    def _write_json_text(self, text):
        temporary = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        )
        self.addCleanup(Path(temporary.name).unlink, missing_ok=True)
        try:
            temporary.write(text)
            return Path(temporary.name)
        finally:
            temporary.close()


if __name__ == "__main__":
    unittest.main()
