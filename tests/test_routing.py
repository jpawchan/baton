#!/usr/bin/env python3
"""Focused regression tests for retry routing and routing statistics."""

import copy
import json
import os
import runpy
import shlex
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# Compose the small repository helpers without importing BatonTests into this
# module's namespace (which would make unittest discover the large old suite).
TESTS_DIR = ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import test_baton as test_baton_helpers  # noqa: E402


SOURCE_BATON = ROOT / "framework" / "baton"
ROUTES = ("hard", "medium", "easy")


class RoutingTests(unittest.TestCase):
    """Use the existing disposable Git fixture, but only focused tests here."""

    def setUp(self):
        self.repo = test_baton_helpers.BatonTests("runTest")
        self.repo.setUp()
        self.addCleanup(self.repo.tearDown)

    def configure_routes(self, project, worker):
        command = shlex.join([sys.executable, str(worker), "{prompt_file}"])
        sections = [
            "[limits]",
            "max_parallel = 3",
            "capsule_max_chars = 4000",
            "worker_timeout_minutes = 1",
            "",
        ]
        for route in ROUTES:
            sections.extend((f"[tiers.{route}]", f"command = {json.dumps(command)}", ""))
        (project / ".baton" / "config.toml").write_text("\n".join(sections))

    def add_missing_route(self, project):
        missing = project / "missing-worker"
        command = shlex.join([str(missing), "{prompt_file}"])
        config = project / ".baton" / "config.toml"
        config.write_text(
            config.read_text() + "\n[tiers.missing]\n"
            f"command = {json.dumps(command)}\n"
        )

    def make_routed_project(self, name="routing"):
        project = self.repo.make_project(name)
        worker = self.repo.write_worker(test_baton_helpers.GOOD_WORKER)
        self.configure_routes(project, worker)
        return project

    def create_task(self, project, title, scope, tier="easy"):
        return self.repo.create_task(project, title, [scope], tier=tier)

    def run_and_review(self, project, task_id):
        self.repo.baton(project, "run", task_id, check=True)
        return self.repo.review_brief_token(project, task_id)

    @staticmethod
    def task_bytes(project, task_id, attempt=1):
        runtime = project / ".baton"
        work = runtime / "work" / task_id
        paths = [
            runtime / "tasks" / f"{task_id}.json",
            runtime / "tasks" / f"{task_id}.md",
            work / f"attempt-{attempt}.brief.md",
            work / f"attempt-{attempt}.report.md",
            work / f"attempt-{attempt}.result.json",
            work / f"attempt-{attempt}.diff",
            work / "review-brief-token.json",
        ]
        return {path: path.read_bytes() for path in paths}

    @staticmethod
    def runtime_snapshot(runtime):
        snapshot = {}
        for path in sorted(runtime.rglob("*")):
            if path.is_file() and not path.is_symlink():
                snapshot[path.relative_to(runtime).as_posix()] = (
                    path.read_bytes(), path.stat().st_mtime_ns,
                )
        return snapshot

    @staticmethod
    def routing_table(rows):
        return "\n".join([
            "Routing outcomes (recorded launches; not token or quality measurements):",
            "Tier | Launches | Retry launches | Needs review | Failed | Blocked | Accepted tasks",
            *rows,
        ]) + "\n"

    def test_reroute_retry_preserves_prior_evidence_and_accounts_route(self):
        project = self.make_routed_project("reroute")
        task_id = self.create_task(project, "reroute task", "reroute/**", tier="easy")

        _first_run = self.repo.baton(project, "run", task_id, check=True)
        first_state = self.repo.state(project, task_id)
        first_launch = next(
            entry for entry in first_state["history"]
            if entry.get("event") == "launched"
        )
        self.assertEqual(first_launch["tier"], "easy")

        _review, stale_token = self.repo.review_brief_token(project, task_id)
        runtime = project / ".baton"
        work = runtime / "work" / task_id
        prior_history = copy.deepcopy(first_state["history"])
        prior_spec = (runtime / "tasks" / f"{task_id}.md").read_bytes()
        prior_attempt_evidence = {
            path: content for path, content in self.task_bytes(project, task_id).items()
            if "attempt-1." in path.name
        }
        token_path = work / "review-brief-token.json"
        self.assertEqual(json.loads(token_path.read_text())["token"], stale_token)

        returned = self.repo.baton(
            project, "task", "return", task_id,
            "--reason", "the easy route needs bounded integration review",
            "--tier", "medium", check=True,
        )
        self.assertIn(f"{task_id} returned for attempt 2", returned.stdout)
        after_return = self.repo.state(project, task_id)
        self.assertEqual(after_return["status"], "queued")
        self.assertEqual(after_return["attempt"], 2)
        self.assertEqual(after_return["tier"], "medium")
        self.assertEqual(after_return["history"][:-1], prior_history)
        reroute = after_return["history"][-1]
        self.assertEqual(reroute["event"], "returned")
        self.assertEqual(reroute["from_tier"], "easy")
        self.assertEqual(reroute["to_tier"], "medium")
        self.assertIn("the easy route needs bounded integration review", reroute["reason"])
        self.assertFalse(token_path.exists())

        for path, content in prior_attempt_evidence.items():
            self.assertEqual(path.read_bytes(), content)
        updated_spec = (runtime / "tasks" / f"{task_id}.md").read_bytes()
        self.assertTrue(updated_spec.startswith(prior_spec))
        self.assertIn(b"Review feedback", updated_spec)
        self.assertIn(b"- attempt 1: the easy route needs bounded integration review", updated_spec)

        self.repo.baton(project, "run", task_id, check=True)
        second_state = self.repo.state(project, task_id)
        second_launch = [
            entry for entry in second_state["history"]
            if entry.get("event") == "launched" and entry.get("attempt") == 2
        ]
        self.assertEqual(len(second_launch), 1)
        self.assertEqual(second_launch[0]["tier"], "medium")

        stale_accept = self.repo.baton(
            project, "task", "accept", task_id, "--brief", stale_token,
        )
        self.assertNotEqual(stale_accept.returncode, 0)
        self.assertEqual(stale_accept.stdout, "")
        self.assertIn("fresh review-phase brief token", stale_accept.stderr)

        _second_review, current_token = self.repo.review_brief_token(project, task_id)
        self.repo.baton(
            project, "task", "accept", task_id, "--brief", current_token, check=True,
        )
        self.assertEqual(self.repo.state(project, task_id)["status"], "done")

        request = self.repo.baton(
            project, "stats", "--task", task_id, check=True,
        )
        self.assertEqual(
            request.stdout,
            "I used 2 workers for this request: 0 on hard, 1 on medium, and 1 on easy.\n",
        )
        routing = self.repo.baton(
            project, "stats", "--routing", "--task", task_id, check=True,
        )
        self.assertEqual(
            routing.stdout,
            self.routing_table([
                "hard | 0 | 0 | 0 | 0 | 0 | 0",
                "medium | 1 | 1 | 1 | 0 | 0 | 1",
                "easy | 1 | 0 | 1 | 0 | 0 | 0",
                "other | 0 | 0 | 0 | 0 | 0 | 0",
            ]),
        )

        self.repo.baton(project, "validate", check=True)

    def test_omitted_and_same_tier_returns_preserve_route(self):
        project = self.make_routed_project("same-route")
        task_id = self.create_task(project, "same route", "same-route/**", tier="easy")

        self.run_and_review(project, task_id)
        self.repo.baton(
            project, "task", "return", task_id,
            "--reason", "retry without changing the selected route", check=True,
        )
        state = self.repo.state(project, task_id)
        self.assertEqual(state["tier"], "easy")
        first_return = state["history"][-1]
        self.assertEqual(first_return["event"], "returned")
        self.assertNotIn("from_tier", first_return)
        self.assertNotIn("to_tier", first_return)

        self.run_and_review(project, task_id)
        self.repo.baton(
            project, "task", "return", task_id,
            "--reason", "retry with the explicitly same route",
            "--tier", "easy", check=True,
        )
        state = self.repo.state(project, task_id)
        self.assertEqual(state["tier"], "easy")
        returns = [entry for entry in state["history"] if entry.get("event") == "returned"]
        self.assertEqual(len(returns), 2)
        for entry in returns:
            self.assertNotIn("from_tier", entry)
            self.assertNotIn("to_tier", entry)

        routing = self.repo.baton(
            project, "stats", "--routing", "--task", task_id, check=True,
        )
        self.assertEqual(
            routing.stdout,
            self.routing_table([
                "hard | 0 | 0 | 0 | 0 | 0 | 0",
                "medium | 0 | 0 | 0 | 0 | 0 | 0",
                "easy | 2 | 1 | 2 | 0 | 0 | 0",
                "other | 0 | 0 | 0 | 0 | 0 | 0",
            ]),
        )

    def test_invalid_retry_routes_are_pre_mutation_and_status_guards_remain(self):
        project = self.make_routed_project("invalid-routes")
        task_id = self.create_task(project, "invalid retry", "invalid/**", tier="easy")
        self.run_and_review(project, task_id)
        self.add_missing_route(project)

        before = self.task_bytes(project, task_id)
        for tier, error in (
            ("default", "unknown tier 'default'"),
            ("unknown", "unknown tier 'unknown'"),
            ("", "tier name must be non-blank"),
            ("missing", "worker executable not found"),
        ):
            with self.subTest(tier=repr(tier)):
                result = self.repo.baton(
                    project, "task", "return", task_id,
                    "--reason", "must not publish this invalid route",
                    "--tier", tier,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn(error, result.stderr)
                self.assertEqual(self.task_bytes(project, task_id), before)

        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        live_state = self.repo.state(project, task_id)
        live_state["runner"] = {
            "pid": os.getpid(), "started_at": live_state["updated_at"],
            "lease": "live-test-lease",
        }
        state_path.write_text(json.dumps(live_state))
        live_before = state_path.read_bytes()
        denied_return = self.repo.baton(
            project, "task", "return", task_id,
            "--reason", "live worker must remain protected", "--tier", "medium",
        )
        self.assertNotEqual(denied_return.returncode, 0)
        self.assertIn("still has a live worker", denied_return.stderr)
        denied_accept = self.repo.baton(
            project, "task", "accept", task_id, "--brief", "not-used",
        )
        self.assertNotEqual(denied_accept.returncode, 0)
        self.assertIn("still has a live worker", denied_accept.stderr)
        self.assertEqual(state_path.read_bytes(), live_before)

        live_state.pop("runner")
        state_path.write_text(json.dumps(live_state))
        _review, token = self.repo.review_brief_token(project, task_id)
        self.repo.baton(
            project, "task", "accept", task_id, "--brief", token, check=True,
        )
        done_return = self.repo.baton(
            project, "task", "return", task_id, "--reason", "done is final",
        )
        self.assertNotEqual(done_return.returncode, 0)
        self.assertIn(f"{task_id} is done; it cannot be returned", done_return.stderr)

        cancelled = self.create_task(project, "cancelled retry", "cancelled/**", tier="easy")
        self.repo.baton(project, "task", "cancel", cancelled, check=True)
        cancelled_return = self.repo.baton(
            project, "task", "return", cancelled, "--reason", "cancelled is final",
        )
        self.assertNotEqual(cancelled_return.returncode, 0)
        self.assertIn(f"{cancelled} is cancelled; it cannot be returned", cancelled_return.stderr)

    def test_routing_stats_empty_request_archive_filter_and_read_only(self):
        empty = self.repo.make_project("empty-routing-stats")
        runtime = empty / ".baton"
        before = self.runtime_snapshot(runtime)
        stats = self.repo.baton(empty, "stats", "--routing", check=True)
        self.assertEqual(
            stats.stdout,
            self.routing_table([
                "hard | 0 | 0 | 0 | 0 | 0 | 0",
                "medium | 0 | 0 | 0 | 0 | 0 | 0",
                "easy | 0 | 0 | 0 | 0 | 0 | 0",
                "other | 0 | 0 | 0 | 0 | 0 | 0",
            ]),
        )
        self.assertEqual(self.runtime_snapshot(runtime), before)

        project = self.make_routed_project("archive-routing-stats")
        archived_id = self.create_task(project, "archived accepted", "archived/**", tier="easy")
        self.run_and_review(project, archived_id)
        self.repo.baton(
            project, "task", "accept", archived_id,
            "--brief", self.repo.review_brief_token(project, archived_id)[1], check=True,
        )
        self.repo.baton(project, "archive", check=True)

        active_id = self.create_task(project, "selected active", "selected/**", tier="medium")
        self.run_and_review(project, active_id)
        unrelated_id = self.create_task(project, "unrelated hard", "unrelated/**", tier="hard")
        self.run_and_review(project, unrelated_id)

        runtime = project / ".baton"
        before = self.runtime_snapshot(runtime)
        request = self.repo.baton(
            project, "stats", "--routing",
            "--task", archived_id, "--task", active_id, "--task", archived_id,
            check=True,
        )
        self.assertEqual(
            request.stdout,
            self.routing_table([
                "hard | 0 | 0 | 0 | 0 | 0 | 0",
                "medium | 1 | 0 | 1 | 0 | 0 | 0",
                "easy | 1 | 0 | 1 | 0 | 0 | 1",
                "other | 0 | 0 | 0 | 0 | 0 | 0",
            ]),
        )
        self.assertEqual(self.runtime_snapshot(runtime), before)

        aggregate = self.repo.baton(project, "stats", "--routing", check=True)
        self.assertEqual(
            aggregate.stdout,
            self.routing_table([
                "hard | 1 | 0 | 1 | 0 | 0 | 0",
                "medium | 1 | 0 | 1 | 0 | 0 | 0",
                "easy | 1 | 0 | 1 | 0 | 0 | 1",
                "other | 0 | 0 | 0 | 0 | 0 | 0",
            ]),
        )
        self.assertEqual(self.runtime_snapshot(runtime), before)

        unknown = self.repo.baton(
            project, "stats", "--routing", "--task", archived_id,
            "--task", "T999-unknown-routing",
        )
        self.assertNotEqual(unknown.returncode, 0)
        self.assertEqual(unknown.stdout, "")
        self.assertIn("unknown task T999-unknown-routing", unknown.stderr)
        self.assertEqual(self.runtime_snapshot(runtime), before)

        denied = self.repo.baton(
            project, "stats", "--routing",
            env={"BATON_TASK_ID": "T999-worker", "BATON_ATTEMPT": "1",
                 "BATON_LEASE": "worker"},
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(denied.stdout, "")
        self.assertIn("worker processes cannot run orchestrator commands", denied.stderr)

    def test_cancelled_dependants_preserve_valid_active_and_archived_audit_links(self):
        project = self.make_routed_project("cancelled-graph")
        parent = self.create_task(project, "prerequisite", "parent/**")
        child = self.repo.create_task(
            project, "dependent", ["child/**"], depends_on=[parent], tier="easy",
        )
        self.repo.baton(project, "task", "cancel", parent, check=True)
        invalid = self.repo.baton(project, "validate")
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn(f"cancelled dependency {parent}", invalid.stdout)
        self.repo.baton(project, "task", "cancel", child, check=True)
        self.assertEqual(self.repo.state(project, child)["depends_on"], [parent])
        self.repo.baton(project, "validate", check=True)
        self.repo.baton(project, "archive", check=True)
        self.repo.baton(project, "validate", check=True)

    def test_worker_usage_reconstructs_legacy_routes_without_mutation(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="routing_usage_probe")
        tasks = [
            {
                "tier": "hard",
                "history": [
                    {"event": "launched", "attempt": 1},
                    {"event": "returned", "from_tier": "easy", "to_tier": "medium"},
                    {"event": "launched", "attempt": 2},
                    {"event": "returned", "from_tier": "medium", "to_tier": "hard"},
                    {"event": "launched", "attempt": 3},
                ],
            },
            {"tier": "easy", "history": [{"event": "launched", "tier": "medium"}]},
            {"tier": "easy", "history": [{"event": "launched", "tier": []}]},
            {"tier": "default", "history": [{"event": "launched"}]},
            {"tier": "custom-route", "history": [{"event": "launched"}]},
            {"tier": [], "history": [{"event": "launched"}]},
            {"tier": "hard", "history": "not-a-list"},
            {"tier": "easy", "history": [None, {"event": "launched"}]},
        ]
        original = copy.deepcopy(tasks)
        self.assertEqual(
            module["worker_usage_sentence"](tasks),
            "I used 9 workers for this Baton runtime so far: 1 for a hard task, "
            "2 for medium tasks, 2 for easy tasks, and 4 for other levels.",
        )
        self.assertEqual(
            module["worker_usage_sentence"](tasks, for_request=True),
            "I used 9 workers for this request: 1 on hard, 2 on medium, "
            "2 on easy, and 4 on other levels.",
        )
        self.assertEqual(tasks, original)
        self.assertEqual(
            module["worker_usage_sentence"]([{"tier": "hard", "history": "bad"}]),
            "I used 0 workers for this Baton runtime so far: 0 for hard tasks, "
            "0 for medium tasks, and 0 for easy tasks.",
        )

    def test_routing_outcome_table_is_exact_and_ignores_bad_attribution(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="routing_aggregate_probe")
        tasks = [
            {
                "status": "needs_review", "tier": "hard",
                "history": [
                    {"event": "launched", "tier": "hard", "attempt": 1},
                    {"event": "worker_exited", "attempt": 1, "status": "needs_review"},
                    {"event": "launched", "tier": "hard", "attempt": 2},
                    {"event": "worker_exited", "attempt": 2, "status": "failed"},
                    {"event": "worker_exited", "attempt": 99, "status": "blocked"},
                ],
            },
            {
                "status": "blocked", "tier": "medium",
                "history": [
                    {"event": "launched", "tier": "medium", "attempt": 1},
                    {"event": "worker_exited", "attempt": 1, "status": "failed"},
                    {"event": "worker_exited", "attempt": 1, "status": "blocked"},
                    {"event": "launched", "tier": "medium", "attempt": 2},
                    {"event": "worker_exited", "attempt": 2, "status": []},
                    {"event": "launched", "tier": "medium", "attempt": True},
                    {"event": "worker_exited", "attempt": True, "status": "failed"},
                ],
            },
            {
                "status": "done", "tier": "easy",
                "history": [
                    {"event": "launched", "tier": "easy", "attempt": 1},
                    {"event": "worker_exited", "attempt": 1, "status": "needs_review"},
                    {"event": "accepted"},
                ],
            },
            {
                "status": "needs_review", "tier": "provider-route",
                "history": [
                    {"event": "launched", "tier": [], "attempt": 1},
                    {"event": "worker_exited", "attempt": 1, "status": {},
                     "note": "do-not-print-private-history"},
                    {"event": "launched", "tier": "provider-route", "attempt": "2"},
                    {"event": "launched", "tier": "provider-route", "attempt": 0},
                    {"event": "worker_exited", "attempt": 0, "status": "failed"},
                ],
            },
            {
                # An exit in a different task cannot satisfy this task's launch.
                "status": "failed", "tier": "hard",
                "history": [{"event": "worker_exited", "attempt": 1, "status": "failed"}],
            },
            {
                "status": "needs_review", "tier": "hard",
                "history": [
                    {"event": "launched", "attempt": 1},
                    {"event": "returned", "from_tier": "easy", "to_tier": "medium"},
                    {"event": "launched", "attempt": 2},
                    {"event": "returned", "from_tier": "medium", "to_tier": "hard"},
                    {"event": "launched", "attempt": 3},
                    {"event": "worker_exited", "attempt": 3, "status": "blocked"},
                ],
            },
        ]
        original = copy.deepcopy(tasks)
        self.assertEqual(
            module["routing_stats_lines"](tasks),
            [
                "Routing outcomes (recorded launches; not token or quality measurements):",
                "Tier | Launches | Retry launches | Needs review | Failed | Blocked | Accepted tasks",
                "hard | 3 | 2 | 1 | 1 | 1 | 0",
                "medium | 4 | 2 | 0 | 0 | 1 | 0",
                "easy | 2 | 0 | 1 | 0 | 0 | 1",
                "other | 3 | 0 | 0 | 0 | 0 | 0",
            ],
        )
        self.assertEqual(tasks, original)

    def test_plan_checklist_announces_route_reason_check_and_is_bounded(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="routing_plan_probe")
        emitted = []
        module["orchestrator_plan_brief"].__globals__["say"] = emitted.append
        module["orchestrator_plan_brief"]([])
        graph_index = emitted.index("Queued/blocked graph:")
        checklist = "\n".join(emitted[:graph_index])
        self.assertIn(
            "announce one configured tier, reason, and concrete check", checklist,
        )
        self.assertIn("easy", checklist)
        self.assertIn("medium", checklist)
        self.assertIn("hard", checklist)
        self.assertLessEqual(len(checklist), 1000)
        self.assertEqual(emitted[graph_index + 1], "- (none)")


if __name__ == "__main__":
    unittest.main()
