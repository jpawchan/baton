#!/usr/bin/env python3
"""End-to-end tests for Agent Relay."""

import json
import os
import runpy
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_RELAY = ROOT / "framework" / "relay"
AUTHOR_EMAIL = "78247292+jpawchan@users.noreply.github.com"

GOOD_WORKER = r'''
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path(os.environ["RELAY_ROOT"])
rd = Path(os.environ["RELAY_DIR"])
tid = os.environ["RELAY_TASK_ID"]
attempt = os.environ["RELAY_ATTEMPT"]
task = json.loads((rd / "tasks" / f"{tid}.json").read_text())

starts = os.environ.get("STARTS")
if starts:
    with open(starts, "a", encoding="utf-8") as f:
        f.write(tid + "\n")

barrier = os.environ.get("BARRIER")
if barrier:
    barrier_dir = Path(barrier)
    barrier_dir.mkdir(parents=True, exist_ok=True)
    (barrier_dir / tid).write_text("ready\n")
    deadline = time.monotonic() + 3
    while len(list(barrier_dir.iterdir())) < int(os.environ.get("BARRIER_SIZE", "2")):
        if time.monotonic() > deadline:
            raise SystemExit("parallel barrier timed out")
        time.sleep(0.01)

scope = (task.get("scope") or ["work/**"])[0]
prefix = scope.split("*")[0].rstrip("/") or "work"
target = root / prefix
target.mkdir(parents=True, exist_ok=True)
target_file = target / f"{tid}.txt"
target_file.write_text(f"attempt {attempt}\n")
changed = [target_file.relative_to(root).as_posix()]

outside = os.environ.get("WRITE_OUTSIDE")
outside_task = os.environ.get("WRITE_OUTSIDE_TASK")
if outside and (not outside_task or outside_task == tid):
    outside_path = root / outside
    outside_path.parent.mkdir(parents=True, exist_ok=True)
    outside_path.write_text(f"changed by {tid}\n")
    changed.append(Path(outside).as_posix())

marker = os.environ.get("FINISH_MARKER")
if marker:
    changed.append((target / "after-finish.txt").relative_to(root).as_posix())

report = rd / "work" / tid / f"attempt-{attempt}.report.md"
report.parent.mkdir(parents=True, exist_ok=True)
report.write_text(f"# {tid} report\n\nResult: needs_review\n")
status = os.environ.get("SUBMIT_STATUS", "needs_review")
finish = [sys.executable, str(rd / "relay"), "task", "finish", tid,
          "--status", status]
for path in changed:
    finish.extend(["--changed", path])
subprocess.run(finish, cwd=root, check=True)

self_accept = os.environ.get("SELF_ACCEPT_RESULT")
if self_accept:
    result = subprocess.run([sys.executable, str(rd / "relay"), "task", "accept", tid],
                            cwd=root)
    Path(self_accept).write_text(str(result.returncode))

if marker:
    Path(marker).write_text("finished\n")
    time.sleep(0.5)
    (target / "after-finish.txt").write_text("late but in scope\n")
if os.environ.get("SLEEP_AFTER_FINISH"):
    time.sleep(float(os.environ["SLEEP_AFTER_FINISH"]))
'''

NO_CHANGE_WORKER = r'''
import os
from pathlib import Path
import subprocess
import sys

root = Path(os.environ["RELAY_ROOT"])
rd = Path(os.environ["RELAY_DIR"])
tid = os.environ["RELAY_TASK_ID"]
attempt = os.environ["RELAY_ATTEMPT"]
report = rd / "work" / tid / f"attempt-{attempt}.report.md"
report.parent.mkdir(parents=True, exist_ok=True)
report.write_text("# no-change report\n")
subprocess.run([sys.executable, str(rd / "relay"), "task", "finish", tid,
                "--status", "needs_review"], cwd=root, check=True)
'''

TIMEOUT_WORKER = r'''
import os
from pathlib import Path
import subprocess
import sys
import time

marker = os.environ["LATE_MARKER"]
subprocess.Popen([sys.executable, "-c",
                  "import pathlib,time; time.sleep(0.6); pathlib.Path(%r).write_text('late')" % marker])
time.sleep(10)
'''

RESULT_WITHOUT_REPORT_WORKER = r'''
import json
import os
from pathlib import Path

rd = Path(os.environ["RELAY_DIR"])
tid = os.environ["RELAY_TASK_ID"]
attempt = os.environ["RELAY_ATTEMPT"]
path = rd / "work" / tid / f"attempt-{attempt}.result.json"
path.write_text(json.dumps({
    "status": "needs_review", "note": "manual", "at": "now",
    "lease": os.environ["RELAY_LEASE"], "changed_paths": [],
}))
'''

MALFORMED_OUTPUT_WORKER = r'''
import json
import os
from pathlib import Path

rd = Path(os.environ["RELAY_DIR"])
tid = os.environ["RELAY_TASK_ID"]
attempt = os.environ["RELAY_ATTEMPT"]
report = rd / "work" / tid / f"attempt-{attempt}.report.md"
report.mkdir()
result = rd / "work" / tid / f"attempt-{attempt}.result.json"
result.write_text(json.dumps({
    "status": "needs_review", "note": {"not": "text"}, "at": "now",
    "lease": os.environ["RELAY_LEASE"],
}))
'''

NON_UTF8_RESULT_WORKER = r'''
import os
from pathlib import Path

rd = Path(os.environ["RELAY_DIR"])
tid = os.environ["RELAY_TASK_ID"]
attempt = os.environ["RELAY_ATTEMPT"]
path = rd / "work" / tid / f"attempt-{attempt}.result.json"
path.write_bytes(b"\xff\xfe")
'''


STAGE_WORKER = r'''
import os
from pathlib import Path
import subprocess
import sys

root = Path(os.environ["RELAY_ROOT"])
rd = Path(os.environ["RELAY_DIR"])
tid = os.environ["RELAY_TASK_ID"]
attempt = os.environ["RELAY_ATTEMPT"]
path = root / "new" / "staged.txt"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("staged\n")
subprocess.run(["git", "add", str(path)], cwd=root, check=True)
report = rd / "work" / tid / f"attempt-{attempt}.report.md"
report.parent.mkdir(parents=True, exist_ok=True)
report.write_text("# staged report\n")
subprocess.run([sys.executable, str(rd / "relay"), "task", "finish", tid,
                "--status", "needs_review", "--changed", "new/staged.txt"],
               cwd=root, check=True)
'''


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-relay-test-")
        self.base = Path(self.temp.name)
        self.worker_number = 0

    def tearDown(self):
        self.temp.cleanup()

    def command(self, argv, cwd, env=None, check=False, timeout=15):
        merged = os.environ.copy()
        if env:
            merged.update({k: str(v) for k, v in env.items()})
        result = subprocess.run(
            [str(arg) for arg in argv], cwd=cwd, env=merged, text=True,
            capture_output=True, timeout=timeout,
        )
        if check and result.returncode:
            self.fail(
                f"command failed ({result.returncode}): {' '.join(map(str, argv))}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def git(self, project, *args, check=True):
        return self.command(["git", *args], project, check=check)

    def make_project(self, name="project", commit=True, initialize=True):
        project = self.base / name
        project.mkdir()
        self.git(project, "init", "-q")
        self.git(project, "config", "user.name", "JPawchan")
        self.git(project, "config", "user.email", AUTHOR_EMAIL)
        if commit:
            (project / "seed.txt").write_text("seed\n")
            self.git(project, "add", "seed.txt")
            self.git(project, "commit", "-qm", "seed")
        if initialize:
            self.command([SOURCE_RELAY, "init", project], project, check=True)
        return project

    def relay(self, project, *args, env=None, check=False, timeout=15):
        return self.command(
            [project / ".agent-relay" / "relay", *args], project,
            env=env, check=check, timeout=timeout,
        )

    def write_worker(self, body):
        self.worker_number += 1
        path = self.base / f"worker-{self.worker_number}.py"
        path.write_text(body)
        return path

    def configure(self, project, worker, max_parallel=3, timeout_minutes: int | float = 1):
        command = f"{sys.executable} {worker} {{prompt_file}}"
        config = (
            "[commands]\n"
            f"worker = {json.dumps(command)}\n\n"
            "[limits]\n"
            f"max_parallel = {max_parallel}\n"
            f"worker_timeout_minutes = {timeout_minutes}\n"
        )
        (project / ".agent-relay" / "config.toml").write_text(config)

    def task_create_command(self, title, scope=None, depends_on=None):
        args = ["task", "create", "--title", title]
        for item in scope or []:
            args += ["--scope", item]
        for item in depends_on or []:
            args += ["--depends-on", item]
        return args

    def create_task(self, project, title, scope=None, depends_on=None):
        args = self.task_create_command(title, scope, depends_on)
        result = self.relay(project, *args, check=True)
        return result.stdout.split()[1]

    def try_create_task(self, project, title, scope=None, depends_on=None):
        args = self.task_create_command(title, scope, depends_on)
        return self.relay(project, *args)

    def state(self, project, task_id):
        path = project / ".agent-relay" / "tasks" / f"{task_id}.json"
        return json.loads(path.read_text())

    def test_init_requires_git_and_creates_only_runtime_files(self):
        plain = self.base / "plain"
        plain.mkdir()
        result = self.command([SOURCE_RELAY, "init", plain], plain)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((plain / ".agent-relay").exists())

        project = self.make_project()
        self.command([SOURCE_RELAY, "init", project], project, check=True)
        lines = (project / ".gitignore").read_text().splitlines()
        self.assertEqual(lines.count(".agent-relay/"), 1)
        runtime = project / ".agent-relay"
        self.assertTrue(os.access(runtime / "relay", os.X_OK))
        self.assertTrue((runtime / "config.toml").exists())
        self.assertFalse((runtime / "config.example.toml").exists())

        config = runtime / "config.toml"
        memory = runtime / "memory.md"
        config.write_text("# preserved config\n")
        memory.write_text("# preserved memory\n")
        self.command([SOURCE_RELAY, "init", project, "--force"], project, check=True)
        self.assertEqual(config.read_text(), "# preserved config\n")
        self.assertEqual(memory.read_text(), "# preserved memory\n")

        nested = project / "nested"
        nested.mkdir()
        nested_result = self.command([SOURCE_RELAY, "init", nested], nested)
        self.assertNotEqual(nested_result.returncode, 0)
        self.assertFalse((nested / ".agent-relay").exists())

        symlink_project = self.make_project("symlink-project", initialize=False)
        external = self.base / "external"
        external.mkdir()
        (external / "sentinel").write_text("unchanged\n")
        (symlink_project / ".agent-relay").symlink_to(
            external, target_is_directory=True,
        )
        escaped = self.command(
            [SOURCE_RELAY, "init", symlink_project], symlink_project,
        )
        self.assertNotEqual(escaped.returncode, 0)
        self.assertEqual(
            sorted(path.name for path in external.iterdir()), ["sentinel"],
        )

        subrepo = self.base / "subrepo"
        subrepo.mkdir()
        self.git(subrepo, "init", "-q")
        self.git(subrepo, "config", "user.name", "JPawchan")
        self.git(subrepo, "config", "user.email", AUTHOR_EMAIL)
        (subrepo / "lib.txt").write_text("library\n")
        self.git(subrepo, "add", "lib.txt")
        self.git(subrepo, "commit", "-qm", "library")
        submodule_project = self.make_project("submodule-project", initialize=False)
        self.git(
            submodule_project, "-c", "protocol.file.allow=always",
            "submodule", "add", "-q", str(subrepo), "vendor",
        )
        self.git(submodule_project, "commit", "-qam", "add submodule")
        unsupported = self.command(
            [SOURCE_RELAY, "init", submodule_project], submodule_project,
        )
        self.assertNotEqual(unsupported.returncode, 0)
        self.git(submodule_project, "rm", "--cached", "-q", "vendor")
        staged_removal = self.command(
            [SOURCE_RELAY, "init", submodule_project], submodule_project,
        )
        self.assertNotEqual(staged_removal.returncode, 0)
        self.assertFalse((submodule_project / ".agent-relay").exists())

    def test_scope_normalization_and_input_validation(self):
        project = self.make_project()
        first = self.create_task(project, "plain scope", ["src/**"])
        second = self.create_task(project, "dot scope", ["./src/**"])
        dry = self.relay(project, "run", "--dry-run", check=True)
        self.assertIn(first, dry.stdout)
        self.assertIn(f"skip {second}: scope conflicts", dry.stdout)

        upper = self.create_task(project, "upper scope", ["Case/**"])
        lower = self.create_task(project, "lower scope", ["case/**"])
        case_dry = self.relay(project, "run", upper, lower, "--dry-run", check=True)
        self.assertIn(f"would run: {upper}", case_dry.stdout)
        self.assertIn(f"skip {lower}: scope conflicts", case_dry.stdout)

        whole = self.create_task(project, "whole", ["."])
        self.assertEqual(self.state(project, whole)["scope"], [])
        absolute_scope = str(self.base / "absolute") + "/**"
        for bad in ("../src/**", absolute_scope, "src/[ab].py", "src/**x/file"):
            result = self.try_create_task(project, "bad scope", [bad])
            self.assertNotEqual(result.returncode, 0, bad)
        rejected_id = self.relay(
            project, "task", "create", "--title", "bad", "--id", "T999-bad",
        )
        self.assertNotEqual(rejected_id.returncode, 0)
        secret = project / "secret.json"
        secret.write_text('{"sentinel": "do-not-read"}\n')
        traversal = self.relay(project, "task", "show", "../../secret")
        self.assertNotEqual(traversal.returncode, 0)
        self.assertNotIn("do-not-read", traversal.stdout)
        for bad_id in ("T000-lower", "T1-bad", "../T001-bad"):
            result = self.relay(project, "task", "show", bad_id)
            self.assertNotEqual(result.returncode, 0, bad_id)
        empty_title = self.relay(
            project, "task", "create", "--title", "", "--scope", "empty/**",
        )
        self.assertNotEqual(empty_title.returncode, 0)
        for value in ("0", "-1"):
            result = self.relay(project, "run", "--dry-run", "--max-parallel", value)
            self.assertNotEqual(result.returncode, 0, value)

    def test_parallel_wave_reports_diffs_and_lifecycle(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        one = self.create_task(project, "alpha", ["alpha/**"])
        two = self.create_task(project, "beta", ["beta/**"])
        barrier = self.base / "barrier"
        run = self.relay(project, "run", env={"BARRIER": barrier}, check=True)
        self.assertIn(one, run.stdout)
        for task_id in (one, two):
            self.assertEqual(self.state(project, task_id)["status"], "needs_review")
            work = project / ".agent-relay" / "work" / task_id
            self.assertTrue((work / "attempt-1.report.md").stat().st_size)
            diff = (work / "attempt-1.diff").read_text()
            self.assertIn(f"{task_id}.txt", diff)
        self.relay(project, "task", "accept", one, check=True)
        self.relay(project, "task", "accept", two, check=True)

    def test_attempt_diff_starts_at_attempt_baseline(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        first = self.create_task(project, "first", ["same/**"])
        self.relay(project, "run", check=True)
        self.relay(project, "task", "accept", first, check=True)

        human = project / "same" / "human.txt"
        human.write_text("already here\n")
        second = self.create_task(project, "second", ["same/**"], [first])
        self.relay(project, "run", check=True)
        diff = (project / ".agent-relay" / "work" / second / "attempt-1.diff").read_text()
        self.assertIn(f"{second}.txt", diff)
        self.assertNotIn(f"{first}.txt", diff)
        self.assertNotIn("human.txt", diff)

        self.relay(project, "task", "return", second, "--reason", "retry", check=True)
        no_change = self.write_worker(NO_CHANGE_WORKER)
        self.configure(project, no_change)
        self.relay(project, "run", second, check=True)
        retry_diff = project / ".agent-relay" / "work" / second / "attempt-2.diff"
        self.assertEqual(retry_diff.read_text(), "")

    def test_scope_violation_blocks_acceptance_even_when_file_was_dirty(self):
        project = self.make_project()
        outside = project / "outside.txt"
        outside.write_text("before\n")
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "scoped", ["inside/**"])
        run = self.relay(project, "run", env={"WRITE_OUTSIDE": "outside.txt"}, check=True)
        self.assertIn("scope violation", run.stdout.lower())
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "blocked")
        self.assertIn("outside.txt", state["scope_violations"])
        violations_diff = (
            project / ".agent-relay" / "work" / task_id
            / "attempt-1.violations.diff"
        )
        self.assertIn("outside.txt", violations_diff.read_text())
        accept = self.relay(project, "task", "accept", task_id)
        self.assertNotEqual(accept.returncode, 0)
        returned = self.relay(
            project, "task", "return", task_id, "--reason", "retry",
        )
        self.assertNotEqual(returned.returncode, 0)
        outside.write_text("before\n")
        self.relay(
            project, "task", "return", task_id,
            "--reason", "outside file restored", check=True,
        )
        self.assertEqual(self.state(project, task_id)["status"], "queued")

    def test_dotfile_scope_matches_dotfile_paths(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "workflow", [".github/**"])
        self.relay(project, "run", task_id, check=True)
        self.assertEqual(self.state(project, task_id)["status"], "needs_review")
        diff = project / ".agent-relay" / "work" / task_id / "attempt-1.diff"
        self.assertIn(f".github/{task_id}.txt", diff.read_text())

    def test_needs_decision_round_trip(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "decision", ["decision/**"])
        self.relay(
            project, "run", task_id,
            env={"SUBMIT_STATUS": "needs_decision"}, check=True,
        )
        self.assertEqual(self.state(project, task_id)["status"], "needs_decision")
        self.relay(
            project, "task", "decide", task_id,
            "--answer", "Use option A", check=True,
        )
        self.assertEqual(self.state(project, task_id)["attempt"], 2)
        spec = project / ".agent-relay" / "tasks" / f"{task_id}.md"
        self.assertIn("Use option A", spec.read_text())

    def test_worker_role_and_live_runner_guards(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "guarded", ["guarded/**"])
        marker = self.base / "finished"
        self_accept = self.base / "self-accept"
        proc = subprocess.Popen(
            [str(project / ".agent-relay" / "relay"), "run", task_id],
            cwd=project,
            env=dict(os.environ, FINISH_MARKER=str(marker), SELF_ACCEPT_RESULT=str(self_accept)),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists())
        self.assertEqual(self.state(project, task_id)["status"], "running")
        self.assertNotEqual(self.relay(project, "task", "accept", task_id).returncode, 0)
        dry = self.relay(project, "run", task_id, "--dry-run")
        self.assertIn("status is running", dry.stdout)
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, stdout + stderr)
        self.assertNotEqual(self_accept.read_text(), "0")
        self.assertEqual(self.state(project, task_id)["status"], "needs_review")
        diff = project / ".agent-relay" / "work" / task_id / "attempt-1.diff"
        self.assertIn("after-finish.txt", diff.read_text())

    def test_concurrent_run_claims_task_once(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "once", ["once/**"])
        starts = self.base / "starts"
        env = dict(os.environ, STARTS=str(starts), FINISH_MARKER=str(self.base / "wait"))
        commands = [str(project / ".agent-relay" / "relay"), "run", task_id]
        first = subprocess.Popen(commands, cwd=project, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(commands, cwd=project, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        out1, err1 = first.communicate(timeout=10)
        out2, err2 = second.communicate(timeout=10)
        self.assertIn(first.returncode, (0, 1), out1 + err1)
        self.assertIn(second.returncode, (0, 1), out2 + err2)
        self.assertEqual(starts.read_text().splitlines(), [task_id])
        self.assertEqual(self.state(project, task_id)["status"], "needs_review")

    def test_separate_run_processes_serialize_snapshot_windows(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        one = self.create_task(project, "alpha", ["alpha/**"])
        two = self.create_task(project, "beta", ["beta/**"])
        relay = str(project / ".agent-relay" / "relay")
        env = dict(os.environ, SLEEP_AFTER_FINISH="0.8")
        first = subprocess.Popen(
            [relay, "run", one], cwd=project, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.state(project, one)["status"] == "running":
                break
            time.sleep(0.02)
        else:
            self.fail("first run did not claim its task")
        second = subprocess.Popen(
            [relay, "run", two], cwd=project, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(0.2)
        self.assertIsNone(second.poll())
        self.assertEqual(self.state(project, two)["status"], "queued")
        out1, err1 = first.communicate(timeout=10)
        out2, err2 = second.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, out1 + err1)
        self.assertEqual(second.returncode, 0, out2 + err2)
        for task_id in (one, two):
            state = self.state(project, task_id)
            self.assertEqual(state["status"], "needs_review")
            self.assertNotIn("scope_violations", state)

    def test_invalid_command_fails_before_task_claim(self):
        project = self.make_project()
        config = project / ".agent-relay" / "config.toml"
        config.write_text(
            '[commands]\nworker = "true {prompt} embedded{prompt}"\n'
            '[limits]\nmax_parallel = 1\n'
        )
        task_id = self.create_task(project, "bad command", ["a/**"])
        self.assertNotEqual(self.relay(project, "validate").returncode, 0)
        result = self.relay(project, "run")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state(project, task_id)["status"], "queued")

    def test_review_result_without_report_fails(self):
        project = self.make_project()
        worker = self.write_worker(RESULT_WITHOUT_REPORT_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "missing report", ["src/**"])
        self.relay(project, "run", task_id, check=True)
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "missing_review_report")

    def test_malformed_result_and_directory_report_are_rejected(self):
        project = self.make_project()
        worker = self.write_worker(MALFORMED_OUTPUT_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "malformed output", ["src/**"])
        self.relay(project, "run", task_id, check=True)
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "invalid_worker_output")
        self.assertIsInstance(state["last_note"], str)

    def test_non_utf8_result_fails_without_stale_runner(self):
        project = self.make_project()
        worker = self.write_worker(NON_UTF8_RESULT_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "non utf8 output", ["src/**"])
        self.relay(project, "run", task_id, check=True)
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "invalid_worker_output")
        self.assertNotIn("runner", state)

    def test_nested_runtime_symlink_is_rejected_without_overwrite(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "symlink artifact", ["src/**"])
        sentinel = self.base / "log-sentinel"
        sentinel.write_text("unchanged\n")
        directory = project / ".agent-relay" / "work" / task_id
        directory.mkdir(parents=True)
        (directory / "attempt-1.log").symlink_to(sentinel)
        result = self.relay(project, "run", task_id)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(), "unchanged\n")
        self.assertEqual(self.state(project, task_id)["status"], "queued")

    def test_cross_scope_write_fails_changed_path_attribution(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker, max_parallel=2)
        alpha = self.create_task(project, "alpha", ["alpha/**"])
        beta = self.create_task(project, "beta", ["beta/**"])
        result = self.relay(
            project, "run", alpha, beta,
            env={"WRITE_OUTSIDE": "beta/injected.txt", "WRITE_OUTSIDE_TASK": alpha},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for task_id in (alpha, beta):
            state = self.state(project, task_id)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["last_note"], "changed_paths_mismatch")

    def test_stale_finalizer_cannot_overwrite_a_new_lease(self):
        project = self.make_project()
        task_id = self.create_task(project, "lease guard", ["src/**"])
        runtime = project / ".agent-relay"
        state_path = runtime / "tasks" / f"{task_id}.json"
        old_task = json.loads(state_path.read_text())
        old_task["status"] = "running"
        old_task["runner"] = {"pid": None, "lease": "old"}
        current = dict(old_task)
        current["attempt"] = 2
        current["runner"] = {"pid": None, "lease": "new"}
        state_path.write_text(json.dumps(current))
        result = runtime / "work" / task_id / "attempt-1.result.json"
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_text(json.dumps({
            "status": "needs_review", "note": "old", "at": "now",
            "lease": "old", "changed_paths": [],
        }))
        (result.parent / "attempt-1.report.md").write_text("# old report\n")
        relay_module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_module")
        finalized = relay_module["finalize_task"](
            str(runtime), old_task, {"returncode": 0}, [], [],
            "baseline", "old",
        )
        self.assertFalse(finalized)
        after = json.loads(state_path.read_text())
        self.assertEqual(after["attempt"], 2)
        self.assertEqual(after["status"], "running")
        self.assertEqual(after["runner"]["lease"], "new")

    def test_worker_command_does_not_invoke_a_shell(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        command = f'{sys.executable} {worker} "{{prompt}}"'
        config = (
            "[commands]\n"
            f"worker = {json.dumps(command)}\n\n"
            "[limits]\n"
            "max_parallel = 1\n"
            "worker_timeout_minutes = 1\n"
        )
        (project / ".agent-relay" / "config.toml").write_text(config)
        marker = self.base / "injected"
        scope = f"safe/$(touch {marker})/**"
        task_id = self.create_task(project, "literal prompt", [scope])
        self.relay(project, "run", task_id, check=True)
        self.assertFalse(marker.exists())
        self.assertEqual(self.state(project, task_id)["status"], "needs_review")

    def test_timeout_kills_worker_process_group(self):
        project = self.make_project()
        worker = self.write_worker(TIMEOUT_WORKER)
        self.configure(project, worker, max_parallel=1, timeout_minutes=0.005)
        task_id = self.create_task(project, "timeout", ["timeout/**"])
        marker = self.base / "late-marker"
        self.relay(project, "run", task_id, env={"LATE_MARKER": marker}, check=True)
        time.sleep(0.8)
        self.assertFalse(marker.exists())
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "worker_timeout")

    def test_interrupt_stops_workers_without_waiting_for_timeout(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signal.Signals(signum).name):
                name = "signal-{}".format(signum)
                project = self.make_project(name)
                worker = self.write_worker(TIMEOUT_WORKER)
                self.configure(project, worker, max_parallel=1, timeout_minutes=0.05)
                task_id = self.create_task(project, "interrupt", ["interrupt/**"])
                marker = self.base / (name + "-late-marker")
                process = subprocess.Popen(
                    [str(project / ".agent-relay" / "relay"), "run", task_id],
                    cwd=project,
                    env=dict(os.environ, LATE_MARKER=str(marker)),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        state = self.state(project, task_id)
                        if (state["status"] == "running"
                                and state.get("runner", {}).get("pid")):
                            break
                        time.sleep(0.02)
                    else:
                        self.fail("worker did not start")
                    started = time.monotonic()
                    process.send_signal(signum)
                    stdout, stderr = process.communicate(timeout=5)
                    elapsed = time.monotonic() - started
                    self.assertEqual(
                        process.returncode, 128 + signum, stdout + stderr,
                    )
                    self.assertLess(elapsed, 2)
                    time.sleep(0.7)
                    self.assertFalse(marker.exists())
                    state = self.state(project, task_id)
                    self.assertEqual(state["status"], "failed")
                    self.assertEqual(
                        state["last_note"], "orchestrator_interrupted",
                    )
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    state = self.state(project, task_id)
                    pid = state.get("runner", {}).get("pid")
                    if pid:
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_sigterm_stops_parallel_groups_with_one_shared_grace_period(self):
        project = self.make_project()
        worker = self.write_worker(TIMEOUT_WORKER)
        self.configure(project, worker, max_parallel=4, timeout_minutes=0.05)
        task_ids = [
            self.create_task(project, f"parallel signal {index}", [f"signal-{index}/**"])
            for index in range(4)
        ]
        marker = self.base / "parallel-late-marker"
        process = subprocess.Popen(
            [str(project / ".agent-relay" / "relay"), "run", *task_ids],
            cwd=project, env=dict(os.environ, LATE_MARKER=str(marker)),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                states = [self.state(project, task_id) for task_id in task_ids]
                if all(state.get("runner", {}).get("pid") for state in states):
                    break
                time.sleep(0.02)
            else:
                self.fail("parallel workers did not start")
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            elapsed = time.monotonic() - started
            self.assertEqual(process.returncode, 128 + signal.SIGTERM, stdout + stderr)
            self.assertLess(elapsed, 1.5)
            time.sleep(0.7)
            self.assertFalse(marker.exists())
            for task_id in task_ids:
                state = self.state(project, task_id)
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["last_note"], "orchestrator_interrupted")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_archived_done_dependency_remains_satisfied_and_cycles_fail_validation(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        first = self.create_task(project, "first", ["first/**"])
        self.relay(project, "run", check=True)
        self.relay(project, "task", "accept", first, check=True)
        second = self.create_task(project, "second", ["second/**"], [first])
        self.relay(project, "archive", check=True)
        dry = self.relay(project, "run", "--dry-run", check=True)
        self.assertIn(f"would run: {second}", dry.stdout)
        self.assertEqual(self.relay(project, "validate").returncode, 0)

        third = self.create_task(project, "third", ["third/**"])
        self.assertTrue(third.startswith("T003-"), third)
        second_path = project / ".agent-relay" / "tasks" / f"{second}.json"
        third_path = project / ".agent-relay" / "tasks" / f"{third}.json"
        second_state = json.loads(second_path.read_text())
        third_state = json.loads(third_path.read_text())
        second_state["depends_on"] = [third]
        third_state["depends_on"] = [second]
        second_path.write_text(json.dumps(second_state))
        third_path.write_text(json.dumps(third_state))
        validation = self.relay(project, "validate")
        self.assertNotEqual(validation.returncode, 0)
        self.assertIn("dependency cycle", validation.stdout)

    def test_archive_preflights_all_destinations_before_moving(self):
        project = self.make_project()
        task_ids = [
            self.create_task(project, "archive one", ["one/**"]),
            self.create_task(project, "archive two", ["two/**"]),
        ]
        runtime = project / ".agent-relay"
        for task_id in task_ids:
            state_path = runtime / "tasks" / f"{task_id}.json"
            state = json.loads(state_path.read_text())
            state["status"] = "done"
            state_path.write_text(json.dumps(state))
            work = runtime / "work" / task_id
            work.mkdir(parents=True)
            (work / "artifact").write_text("test\n")
        collision = runtime / "archive" / f"{task_ids[1]}.work"
        collision.write_text("collision\n")
        archived = self.relay(project, "archive")
        self.assertNotEqual(archived.returncode, 0)
        for task_id in task_ids:
            self.assertTrue((runtime / "tasks" / f"{task_id}.json").exists())
            self.assertFalse((runtime / "archive" / f"{task_id}.json").exists())

    def test_archive_defers_sigterm_until_transaction_is_complete(self):
        project = self.make_project()
        task_id = self.create_task(project, "archive signal", ["archive/**"])
        runtime = project / ".agent-relay"
        state_path = runtime / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "done"
        state_path.write_text(json.dumps(state))
        work = runtime / "work" / task_id
        work.mkdir(parents=True)
        (work / "artifact").write_text("test\n")
        marker = self.base / "archive-move-started"
        code = r'''
import os
from pathlib import Path
import runpy
import sys
import time
from types import SimpleNamespace

module = runpy.run_path(sys.argv[1], run_name="relay_archive_probe")
original = module["shutil"].move
marker = Path(sys.argv[3])

def slow_move(source, target):
    result = original(source, target)
    marker.write_text("moved\n")
    time.sleep(0.25)
    return result

module["shutil"].move = slow_move
os.chdir(sys.argv[2])
module["cmd_archive"](SimpleNamespace())
'''
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(SOURCE_RELAY), str(project), str(marker)],
            cwd=project, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists())
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, -signal.SIGTERM)
        self.assertFalse((runtime / "tasks" / f"{task_id}.json").exists())
        self.assertFalse((runtime / "tasks" / f"{task_id}.md").exists())
        self.assertFalse((runtime / "work" / task_id).exists())
        self.assertTrue((runtime / "archive" / f"{task_id}.json").exists())
        self.assertTrue((runtime / "archive" / f"{task_id}.md").exists())
        self.assertTrue((runtime / "archive" / f"{task_id}.work").exists())

    def test_unborn_repository_diff_uses_worktree_content(self):
        project = self.make_project(commit=False)
        worker = self.write_worker(STAGE_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "stage", ["new/**"])
        self.relay(project, "run", task_id, check=True)
        diff = project / ".agent-relay" / "work" / task_id / "attempt-1.diff"
        self.assertIn("new/staged.txt", diff.read_text())

    def test_memory_archive_and_prompt_spec_alignment(self):
        project = self.make_project()
        self.relay(project, "memory", "add", "--for", "worker",
                   "Use the local environment", "Do not install global packages.", check=True)
        index = self.relay(project, "memory", "index", "--for", "worker", check=True)
        self.assertIn("M001", index.stdout)
        shown = self.relay(project, "memory", "show", "M001", check=True)
        self.assertIn("Do not install global packages.", shown.stdout)
        self.assertEqual(self.relay(project, "validate").returncode, 0)

        spec = (ROOT / "SPEC.md").read_text()
        prompt = (ROOT / "prompts" / "create-framework.md").read_text()
        embedded = prompt.split("<!-- BEGIN SPEC -->\n", 1)[1].split(
            "\n<!-- END SPEC -->", 1
        )[0]
        self.assertEqual(embedded, spec.rstrip())

        with (ROOT / "framework" / "config.example.toml").open("rb") as source:
            config = tomllib.load(source)
        command = shlex.split(config["commands"]["worker"])
        query = command.index("-q")
        self.assertEqual(command[query + 1], "{prompt}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
