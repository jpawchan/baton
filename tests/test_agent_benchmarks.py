#!/usr/bin/env python3
"""Offline checks for the live benchmark harness; never call a provider."""

from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks" / "agent_pilot"))
from cases import CASES
from reference import SOLUTIONS
from usage import collect_usage, session_usage

SPEC = importlib.util.spec_from_file_location("agent_benchmark", ROOT / "tools" / "benchmark_agents.py")
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


class AgentBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="baton-benchmark-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def session(self, name="s1", extra=None, stop="stop", usage=None):
        usage = usage or dict(input=10, output=3, cacheRead=20, cacheWrite=2, totalTokens=35)
        entries = [
            {"type": "session", "id": name, "version": 3},
            {"type": "model_change", "id": "m", "modelId": "model", "provider": "provider"},
            {"type": "thinking_level_change", "id": "t", "thinkingLevel": "high"},
            {"type": "message", "id": "a", "message": {
                "role": "assistant", "model": "model", "provider": "provider",
                "content": [{"type": "text", "text": "PRIVATE_PROMPT_MATERIAL"}],
                "stopReason": stop, "usage": usage,
            }},
        ]
        entries.extend(extra or [])
        path = self.root / (name + ".jsonl")
        path.write_text("\n".join(map(json.dumps, entries)) + "\n")
        return path, entries

    def stream(self, entries, name="stream.log", ends=True, assistant_count=1):
        events = [entries[0]]
        for _ in range(assistant_count):
            events += [
                {"type": "message_update", "usage": entries[3]["message"]["usage"]},
                {"type": "message_end", "message": entries[3]["message"]},
                {"type": "turn_end", "message": entries[3]["message"]},
            ]
        if ends:
            events.append({"type": "agent_end", "messages": [entries[3]["message"]]})
        path = self.root / name
        path.write_text("\n".join(map(json.dumps, events)) + "\n")
        return path

    def test_partition_and_stream_copies_are_counted_once_without_prompt_text(self):
        _path, entries = self.session()
        log = self.stream(entries)
        result = collect_usage(self.root, [("solver", log)])
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["logical_input_tokens"], 32)
        self.assertEqual(result["output"], 3)
        self.assertEqual(result["totalTokens"], 35)
        self.assertNotIn("PRIVATE_PROMPT_MATERIAL", json.dumps(result))

    def test_compaction_usage_counted_once_not_retained_copies(self):
        summary_usage = dict(input=5, output=2, cacheRead=0, cacheWrite=0, totalTokens=7)
        path, _ = self.session(extra=[{
            "type": "compaction", "id": "c", "usage": summary_usage,
            "retainedTail": [{"role": "assistant", "usage": summary_usage}],
        }])
        result = session_usage(path)
        self.assertEqual(result["issues"], [])
        self.assertEqual(sum(call["totalTokens"] for call in result["calls"]), 42)
        self.assertEqual(len(result["calls"]), 2)

    def test_missing_summary_usage_or_bad_partition_is_incomplete(self):
        path, _ = self.session(extra=[{"type": "compaction", "id": "c"}])
        self.assertIn("missing_or_invalid_usage", session_usage(path)["issues"])
        path, _ = self.session(usage=dict(input=10, output=3, cacheRead=20, cacheWrite=2, totalTokens=15))
        self.assertIn("inconsistent_token_partition", session_usage(path)["issues"])

    def test_error_response_remains_incomplete_even_when_agent_end_exists(self):
        _path, entries = self.session(stop="error")
        result = collect_usage(self.root, [("solver", self.stream(entries))])
        self.assertFalse(result["complete"])
        self.assertIn("provider_error_or_incomplete_response", result["issues"])
        self.assertEqual(result["totalTokens"], 35)

    def test_unfinished_stream_and_extra_session_are_not_omitted(self):
        _path, entries = self.session()
        self.session("s2")
        result = collect_usage(self.root, [("solver", self.stream(entries, ends=False))])
        self.assertFalse(result["complete"])
        self.assertEqual(result["totalTokens"], 70)
        self.assertIn("unmatched_session_or_stream", result["issues"])

    def test_duplicate_entries_malformed_ids_and_missing_fields_fail_closed(self):
        path, entries = self.session()
        entries += [copy.deepcopy(entries[-1]), {"type": "message", "id": []}]
        path.write_text("\n".join(map(json.dumps, entries)) + "\n{\n")
        result = session_usage(path)
        for issue in ("duplicate_entry", "missing_entry_id", "malformed_session_line"):
            self.assertIn(issue, result["issues"])
        self.assertEqual(len(result["calls"]), 1)

    def test_withheld_grader_passes_answer_keys_and_rejects_starting_code(self):
        for name, case in CASES.items():
            project = self.root / name
            for relative, content in case["files"].items():
                path = project / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            def grade():
                result = subprocess.run([sys.executable, str(BENCH.PILOT / "holdout.py"), name, str(project)],
                                        cwd=self.root, env=env, text=True, capture_output=True,
                                        stdin=subprocess.DEVNULL, timeout=30, check=True)
                return json.loads(result.stdout)
            with self.subTest(case=name, state="starting"):
                self.assertLess(grade()["quality"], 1.0)
            for relative, content in SOLUTIONS[name].items():
                (project / relative).write_text(content)
            with self.subTest(case=name, state="answer-key"):
                result = grade()
                self.assertEqual(result["passed"], result["total"], result)
                self.assertTrue(result["critical_checks_passed"])

    def test_prepare_is_balanced_frozen_and_does_not_invoke_agents(self):
        binaries = self.root / "binaries"
        binaries.mkdir()
        fake = binaries / "pi"
        fake.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo stub-pi; else exit 99; fi\n')
        fake.chmod(0o700)
        config = self.root / "config.toml"
        config.write_text("\n".join(
            f'[tiers.{tier}]\ncommand = "pi --print --mode json --provider provider --model model --thinking high -- {{prompt}}"\n'
            for tier in ("hard", "medium", "easy")
        ))
        base = self.root / "pilot"
        args = SimpleNamespace(directory=base, config=config, repeats=2, timeout_seconds=60,
                               seed=123, provider="provider", model="model", effort="high")
        with mock.patch.dict(os.environ, {"PATH": str(binaries) + os.pathsep + os.environ["PATH"]}), redirect_stdout(io.StringIO()):
            BENCH.prepare(args)
        protocol = BENCH.load_protocol(base)
        self.assertEqual(len(protocol["jobs"]), 8)
        for name in CASES:
            jobs = [job for job in protocol["jobs"] if job["case"] == name]
            self.assertEqual(len({job["initial_commit"] for job in jobs}), 1)
            order1 = [job["arm"] for job in jobs if job["repeat"] == 1]
            order2 = [job["arm"] for job in jobs if job["repeat"] == 2]
            self.assertEqual(order1, list(reversed(order2)))
        self.assertFalse(list(base.glob("*/started.json")))
        (base / "worker-config.toml").write_text("changed")
        with self.assertRaisesRegex(ValueError, "config changed"):
            BENCH.load_protocol(base)


if __name__ == "__main__":
    unittest.main()
