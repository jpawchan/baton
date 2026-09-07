#!/usr/bin/env python3
"""Focused tests for the reproducible activation-context measurement."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLED_MANUAL_MAX_BYTES = 12_000
TOTAL_ACTIVATION_MAX_BYTES = 14_500
SPEC = importlib.util.spec_from_file_location(
    "measure_context", ROOT / "tools" / "measure_context.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load tools/measure_context.py")
MEASURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEASURE)


class ContextFootprintTests(unittest.TestCase):
    def test_assembly_is_deterministic_and_rejects_boundary_changes(self):
        artifacts = {
            "activation_instructions": b"activate\n",
            "installed_orchestrator_manual": b"manual\n",
            "generated_start_brief": b"brief\n",
        }
        self.assertEqual(MEASURE.assemble_artifacts(artifacts), b"activate\nmanual\nbrief\n")
        with self.assertRaisesRegex(ValueError, "canonical order and boundary"):
            MEASURE.assemble_artifacts({**artifacts, "worker.md": b"excluded"})

    def test_fresh_configured_measurements_match_and_exclude_non_activation_files(self):
        first = MEASURE.measure(ROOT)
        second = MEASURE.measure(ROOT)
        self.assertEqual(first["artifacts"], second["artifacts"])
        self.assertEqual(first["total"], second["total"])
        self.assertEqual(
            first["boundary"]["included"],
            list(MEASURE.ARTIFACT_ORDER),
        )
        excluded = " ".join(first["boundary"]["excluded"])
        for label in ("summary.md", "source code", "worker.md", "task specifications"):
            self.assertIn(label, excluded)
        with tempfile.TemporaryDirectory(prefix="baton-context-test-") as temporary:
            artifacts = MEASURE.generate_artifacts(
                ROOT, Path(temporary) / "fresh-project"
            )
        brief = artifacts["generated_start_brief"].decode("utf-8")
        manual = artifacts["installed_orchestrator_manual"].decode("utf-8")
        self.assertIn("Worker routing:", brief)
        self.assertIn("change these settings at any time", brief)
        self.assertNotIn("Which model and reasoning level should Baton use", brief)
        self.assertIn("Which model and reasoning level should Baton use", manual)
        self.assertNotIn("GPT 5.6", manual)
        self.assertNotIn("Claude Opus", manual)

    def test_current_activation_stays_within_fixed_footprint_ceilings(self):
        result = MEASURE.measure(ROOT)
        manual_bytes = result["artifacts"]["installed_orchestrator_manual"]["bytes"]
        total_bytes = result["total"]["bytes"]
        self.assertGreater(manual_bytes, 0)
        self.assertGreater(total_bytes, manual_bytes)
        self.assertLessEqual(manual_bytes, INSTALLED_MANUAL_MAX_BYTES)
        self.assertLessEqual(total_bytes, TOTAL_ACTIVATION_MAX_BYTES)

    def test_default_result_uses_only_reproducible_offline_estimates(self):
        result = MEASURE.measure(ROOT)
        rendered = MEASURE.render_text(result)
        for model in ("GPT 5.6 Sol", "Claude Opus 4.8"):
            self.assertIn(model, rendered)
        self.assertEqual(rendered.count("ESTIMATE — authoritative"), 2)
        self.assertNotIn("PROVIDER-REPORTED DIFFERENTIAL", rendered)
        self.assertIn("characters=", rendered)
        self.assertIn("bytes=", rendered)
        self.assertIn("lines=", rendered)

    def test_offline_fallback_and_evidence_payload_validation(self):
        fallback = MEASURE.measure(ROOT, provider_evidence=None)
        rendered = MEASURE.render_text(fallback)
        self.assertEqual(rendered.count("ESTIMATE — authoritative"), 2)
        self.assertIn("conservative_range_tokens=", rendered)

        evidence = json.loads(MEASURE.PROVIDER_EVIDENCE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="baton-context-evidence-") as temporary:
            path = Path(temporary) / "evidence.json"
            path.write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "retired"):
                MEASURE.measure(ROOT, provider_evidence=path)

if __name__ == "__main__":
    unittest.main()
