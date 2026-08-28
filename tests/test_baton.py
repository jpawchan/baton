#!/usr/bin/env python3
"""End-to-end tests for Baton."""

import hashlib
import io
import json
import os
import re
import runpy
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SOURCE_BATON = ROOT / "framework" / "baton"
AUTHOR_EMAIL = "78247292+jpawchan@users.noreply.github.com"
BATON_ENVIRONMENT_KEYS = (
    "BATON_TASK_ID", "BATON_ATTEMPT", "BATON_LEASE", "BATON_DIR", "BATON_ROOT",
)
NEVER_STARTED_ADVISORY = (
    "the worker produced no output and recorded no phase brief, "
    "so it probably never started."
)
NEVER_STARTED_STDIN_ADVISORY = (
    "check that the worker command runs standalone and does not "
    "wait on standard input."
)


def clean_test_environment(overrides=None):
    environment = os.environ.copy()
    for key in BATON_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    if overrides:
        environment.update({key: str(value) for key, value in overrides.items()})
    return environment

GOOD_WORKER = r'''
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path(os.environ["BATON_ROOT"])
rd = Path(os.environ["BATON_DIR"])
tid = os.environ["BATON_TASK_ID"]
attempt = os.environ["BATON_ATTEMPT"]
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
brief = subprocess.run(
    [sys.executable, str(rd / "baton"), "task", "brief", tid, "--phase", "report"],
    cwd=root, check=True, capture_output=True, text=True,
)
token = next(line.removeprefix("Brief token: ") for line in brief.stdout.splitlines()
             if line.startswith("Brief token: "))
status = os.environ.get("SUBMIT_STATUS", "needs_review")
report.write_text(
    f"# {tid} report\n\n## Result\n{status}\n\n## Changes\n- updated task output\n\n"
    "## Verification\n- worker completed\n\n## Decisions and risks\n- none\n"
)
finish = [sys.executable, str(rd / "baton"), "task", "finish", tid,
          "--status", status, "--brief", token]
for path in changed:
    finish.extend(["--changed", path])
subprocess.run(finish, cwd=root, check=True)

self_accept = os.environ.get("SELF_ACCEPT_RESULT")
if self_accept:
    result = subprocess.run([sys.executable, str(rd / "baton"), "task", "accept", tid],
                            cwd=root)
    Path(self_accept).write_text(str(result.returncode))

if marker:
    Path(marker).write_text("finished\n")
    time.sleep(0.5)
    (target / "after-finish.txt").write_text("late but in scope\n")
if os.environ.get("SLEEP_AFTER_FINISH"):
    time.sleep(float(os.environ["SLEEP_AFTER_FINISH"]))
if os.environ.get("EXIT_CODE"):
    raise SystemExit(int(os.environ["EXIT_CODE"]))
'''

NO_CHANGE_WORKER = r'''
import os
from pathlib import Path
import subprocess
import sys

root = Path(os.environ["BATON_ROOT"])
rd = Path(os.environ["BATON_DIR"])
tid = os.environ["BATON_TASK_ID"]
attempt = os.environ["BATON_ATTEMPT"]
report = rd / "work" / tid / f"attempt-{attempt}.report.md"
report.parent.mkdir(parents=True, exist_ok=True)
brief = subprocess.run(
    [sys.executable, str(rd / "baton"), "task", "brief", tid, "--phase", "report"],
    cwd=root, check=True, capture_output=True, text=True,
)
token = next(line.removeprefix("Brief token: ") for line in brief.stdout.splitlines()
             if line.startswith("Brief token: "))
report.write_text(
    "# no-change report\n\n## Result\nneeds_review\n\n## Changes\n- no changes\n\n"
    "## Verification\n- worker completed\n\n## Decisions and risks\n- none\n"
)
subprocess.run([sys.executable, str(rd / "baton"), "task", "finish", tid,
                "--status", "needs_review", "--brief", token], cwd=root, check=True)
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


SILENT_TIMEOUT_WORKER = r'''
import time

time.sleep(10)
'''


TIER_TIMEOUT_WORKER = r'''
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path(os.environ["BATON_ROOT"])
rd = Path(os.environ["BATON_DIR"])
tid = os.environ["BATON_TASK_ID"]
attempt = os.environ["BATON_ATTEMPT"]
task = json.loads((rd / "tasks" / f"{tid}.json").read_text())
if task["tier"] == "short":
    time.sleep(10)

report = rd / "work" / tid / f"attempt-{attempt}.report.md"
report.parent.mkdir(parents=True, exist_ok=True)
brief = subprocess.run(
    [sys.executable, str(rd / "baton"), "task", "brief", tid, "--phase", "report"],
    cwd=root, check=True, capture_output=True, text=True,
)
token = next(line.removeprefix("Brief token: ") for line in brief.stdout.splitlines()
             if line.startswith("Brief token: "))
report.write_text(
    "# tier timeout report\n\n## Result\nneeds_review\n\n## Changes\n- no changes\n\n"
    "## Verification\n- worker completed\n\n## Decisions and risks\n- none\n"
)
subprocess.run([sys.executable, str(rd / "baton"), "task", "finish", tid,
                "--status", "needs_review", "--brief", token], cwd=root, check=True)
'''


RESULT_WITHOUT_REPORT_WORKER = r'''
import json
import os
from pathlib import Path

rd = Path(os.environ["BATON_DIR"])
tid = os.environ["BATON_TASK_ID"]
attempt = os.environ["BATON_ATTEMPT"]
path = rd / "work" / tid / f"attempt-{attempt}.result.json"
path.write_text(json.dumps({
    "status": "needs_review", "note": "manual", "at": "2026-01-01T00:00:00Z",
    "lease": os.environ["BATON_LEASE"], "changed_paths": [],
}))
'''

MALFORMED_OUTPUT_WORKER = r'''
import json
import os
from pathlib import Path

rd = Path(os.environ["BATON_DIR"])
tid = os.environ["BATON_TASK_ID"]
attempt = os.environ["BATON_ATTEMPT"]
report = rd / "work" / tid / f"attempt-{attempt}.report.md"
report.mkdir()
result = rd / "work" / tid / f"attempt-{attempt}.result.json"
result.write_text(json.dumps({
    "status": "needs_review", "note": {"not": "text"}, "at": "now",
    "lease": os.environ["BATON_LEASE"],
}))
'''

NON_UTF8_RESULT_WORKER = r'''
import os
from pathlib import Path

rd = Path(os.environ["BATON_DIR"])
tid = os.environ["BATON_TASK_ID"]
attempt = os.environ["BATON_ATTEMPT"]
path = rd / "work" / tid / f"attempt-{attempt}.result.json"
path.write_bytes(b"\xff\xfe")
'''


OVERSIZED_INTEGER_RESULT_WORKER = r'''
import json
import os
from pathlib import Path

rd = Path(os.environ["BATON_DIR"])
tid = os.environ["BATON_TASK_ID"]
attempt = os.environ["BATON_ATTEMPT"]
path = rd / "work" / tid / f"attempt-{attempt}.result.json"
path.write_text(
    '{"status":"failed","note":"otherwise valid","at":"now","lease":'
    + json.dumps(os.environ["BATON_LEASE"])
    + ',"changed_paths":[],"oversized":' + "9" * 5000 + "}\n"
)
'''


STAGE_WORKER = r'''
import os
from pathlib import Path
import subprocess
import sys

root = Path(os.environ["BATON_ROOT"])
rd = Path(os.environ["BATON_DIR"])
tid = os.environ["BATON_TASK_ID"]
attempt = os.environ["BATON_ATTEMPT"]
path = root / "new" / "staged.txt"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("staged\n")
subprocess.run(["git", "add", str(path)], cwd=root, check=True)
report = rd / "work" / tid / f"attempt-{attempt}.report.md"
report.parent.mkdir(parents=True, exist_ok=True)
brief = subprocess.run(
    [sys.executable, str(rd / "baton"), "task", "brief", tid, "--phase", "report"],
    cwd=root, check=True, capture_output=True, text=True,
)
token = next(line.removeprefix("Brief token: ") for line in brief.stdout.splitlines()
             if line.startswith("Brief token: "))
report.write_text(
    "# staged report\n\n## Result\nneeds_review\n\n## Changes\n- staged file\n\n"
    "## Verification\n- worker completed\n\n## Decisions and risks\n- none\n"
)
subprocess.run([sys.executable, str(rd / "baton"), "task", "finish", tid,
                "--status", "needs_review", "--brief", token,
                "--changed", "new/staged.txt"],
               cwd=root, check=True)
'''


class BatonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="baton-test-")
        self.base = Path(self.temp.name)
        self.worker_number = 0

    def tearDown(self):
        self.temp.cleanup()

    def command(self, argv, cwd, env=None, check=False, timeout=15):
        merged = clean_test_environment(env)
        result = subprocess.run(
            [str(arg) for arg in argv], cwd=cwd, env=merged, text=True,
            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
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
            self.command([SOURCE_BATON, "init", project], project, check=True)
        return project

    def baton(self, project, *args, env=None, check=False, timeout=15):
        return self.command(
            [project / ".baton" / "baton", *args], project,
            env=env, check=check, timeout=timeout,
        )

    def write_worker(self, body):
        self.worker_number += 1
        path = self.base / f"worker-{self.worker_number}.py"
        path.write_text(body)
        return path

    def configure(self, project, worker, max_parallel=3,
                  timeout_minutes: int | float = 1, capsule_max_chars=4000):
        command = f"{sys.executable} {worker} {{prompt_file}}"
        config = (
            "[commands]\n"
            f"worker = {json.dumps(command)}\n\n"
            "[tiers.test]\n\n"
            "[limits]\n"
            f"max_parallel = {max_parallel}\n"
            f"capsule_max_chars = {capsule_max_chars}\n"
            f"worker_timeout_minutes = {timeout_minutes}\n"
        )
        (project / ".baton" / "config.toml").write_text(config)

    def task_create_command(self, title, scope=None, depends_on=None, tier="test"):
        args = ["task", "create", "--title", title]
        for item in scope or []:
            args += ["--scope", item]
        for item in depends_on or []:
            args += ["--depends-on", item]
        if tier is not None:
            args += ["--tier", tier]
        return args

    def ensure_test_tier(self, project):
        config_path = project / ".baton" / "config.toml"
        try:
            config = tomllib.loads(config_path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            return
        if "test" not in config.get("tiers", {}):
            config_path.write_text(config_path.read_text() + (
                '\n[tiers.test]\ncommand = "/usr/bin/true {prompt_file}"\n'
            ))

    def create_task(self, project, title, scope=None, depends_on=None, tier="test"):
        if tier == "test":
            self.ensure_test_tier(project)
        args = self.task_create_command(title, scope, depends_on, tier)
        result = self.baton(project, *args, check=True)
        task_id = result.stdout.split()[1]
        spec = project / ".baton" / "tasks" / f"{task_id}.md"
        content = spec.read_text().replace(
            "Replace this line with one clear outcome.",
            f"Complete the {title} task.",
        ).replace(
            "- Add observable requirements.",
            "- The targeted task behavior is verified.",
        )
        spec.write_text(content)
        return task_id

    def write_memory(self, project, entries):
        runtime = project / ".baton"
        index = "\n".join(
            f"- {memory_id} [{audience}] {summary}"
            for memory_id, audience, summary, _body in entries
        )
        bodies = "\n\n".join(
            f"### {memory_id} [{audience}] {summary}\n{body}"
            for memory_id, audience, summary, body in entries
        )
        (runtime / "memory.md").write_text(
            "# Memory\n\n## Index\n" + index + "\n\n## Entries\n\n" + bodies + "\n"
        )

    def try_create_task(self, project, title, scope=None, depends_on=None, tier="test"):
        if tier == "test":
            self.ensure_test_tier(project)
        args = self.task_create_command(title, scope, depends_on, tier)
        return self.baton(project, *args)

    def state(self, project, task_id):
        path = project / ".baton" / "tasks" / f"{task_id}.json"
        return json.loads(path.read_text())

    def require_case_sensitive_filesystem(self):
        probe = self.base / "case-sensitive-probe"
        probe.mkdir()
        (probe / "Probe").write_text("probe\n")
        if (probe / "probe").exists():
            self.skipTest("temporary filesystem is not case-sensitive")

    def lease_task(self, project, task_id, lease):
        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        task = json.loads(state_path.read_text())
        task["status"] = "running"
        task["runner"] = {"pid": None, "started_at": "now", "lease": lease}
        state_path.write_text(json.dumps(task))
        spec = (runtime / "tasks" / f"{task_id}.md").read_text()
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_brief_probe")
        memory_entries = module["memory_index_entries"](
            (runtime / "memory.md").read_text()
        )
        capsule = module["compile_context_capsule"](task, spec, memory_entries)
        digest = hashlib.sha256(capsule.encode()).hexdigest()
        brief = runtime / "work" / task_id / f"attempt-{task['attempt']}.brief.md"
        brief.parent.mkdir(parents=True, exist_ok=True)
        brief.write_text(f"Content digest: sha256:{digest}\n\n{capsule}")
        return {
            "BATON_TASK_ID": task_id,
            "BATON_ATTEMPT": str(task["attempt"]),
            "BATON_LEASE": lease,
            "BATON_DIR": runtime,
            "BATON_ROOT": project,
        }

    def report_brief_token(self, project, task_id, env):
        brief = self.baton(
            project, "task", "brief", task_id, "--phase", "report",
            env=env, check=True,
        )
        token = next(
            line.removeprefix("Brief token: ")
            for line in brief.stdout.splitlines()
            if line.startswith("Brief token: ")
        )
        return brief, token

    def report_text(self, status="needs_review", newline="\n"):
        lines = [
            "# task report", "", "## Result", status, "", "## Changes",
            "- test changes", "", "## Verification", "- test verification",
            "", "## Decisions and risks", "- none", "",
        ]
        return newline.join(lines)

    def prepare_finish(self, name):
        project = self.make_project(name)
        task_id = self.create_task(project, name)
        env = self.lease_task(project, task_id, name + "-lease")
        _brief, token = self.report_brief_token(project, task_id, env)
        work = project / ".baton" / "work" / task_id
        return project, task_id, env, work / "attempt-1.report.md", token

    def review_brief_token(self, project, task_id, env=None, include_log_tail=False):
        args = ["orchestrator", "brief", "--phase", "review", task_id]
        if include_log_tail:
            args.append("--include-log-tail")
        brief = self.baton(
            project, *args, env=env, check=True,
        )
        token = next(
            line.removeprefix("Review token: ")
            for line in brief.stdout.splitlines()
            if line.startswith("Review token: ")
        )
        return brief, token

    def accept_task(self, project, task_id):
        _brief, token = self.review_brief_token(project, task_id)
        return self.baton(
            project, "task", "accept", task_id, "--brief", token, check=True,
        )

    def instrument_atomic_write_os(self, module, fail_operation=None):
        real_os = os
        events = []
        directory_descriptors = set()
        injected = OSError("injected parent durability failure")

        class ObservedOS:
            def __getattr__(self, name):
                return getattr(real_os, name)

            def fsync(self, descriptor):
                if descriptor in directory_descriptors:
                    events.append("parent-fsync")
                    if fail_operation == "parent-fsync":
                        raise injected
                else:
                    events.append("temp-fsync")
                return real_os.fsync(descriptor)

            def replace(self, source, destination):
                events.append("replace")
                if fail_operation == "replace":
                    raise injected
                return real_os.replace(source, destination)

            def open(self, path, flags):
                events.append(("parent-open", real_os.fspath(path), flags))
                if fail_operation == "parent-open":
                    raise injected
                descriptor = real_os.open(path, flags)
                directory_descriptors.add(descriptor)
                return descriptor

            def close(self, descriptor):
                if descriptor in directory_descriptors:
                    events.append("parent-close")
                    directory_descriptors.remove(descriptor)
                return real_os.close(descriptor)

        module["atomic_write"].__globals__["os"] = ObservedOS()
        return events, directory_descriptors, injected

    def test_atomic_write_fsyncs_parent_after_replace_and_closes_descriptor(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_atomic_order_probe")
        parent = self.base / "durable" / "nested"
        parent.mkdir(parents=True)
        target = parent / "state.json"
        target.write_text("old\n")
        events, directory_descriptors, _injected = self.instrument_atomic_write_os(
            module,
        )

        module["atomic_write"](target, "new\n", mode=0o640)

        self.assertEqual(target.read_text(), "new\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(events, [
            "temp-fsync", "replace",
            ("parent-open", str(parent), os.O_RDONLY | os.O_DIRECTORY),
            "parent-fsync", "parent-close",
        ])
        self.assertEqual(directory_descriptors, set())
        self.assertEqual(list(parent.glob(".baton-write-*")), [])

    def test_atomic_write_supports_plain_relative_paths_and_syncs_dot(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_atomic_relative_probe")
        working = self.base / "relative"
        working.mkdir()
        events, directory_descriptors, _injected = self.instrument_atomic_write_os(
            module,
        )
        previous = Path.cwd()
        try:
            os.chdir(working)
            module["atomic_write"]("state.json", "relative\n")
        finally:
            os.chdir(previous)

        self.assertEqual((working / "state.json").read_text(), "relative\n")
        self.assertEqual(events, [
            "temp-fsync", "replace",
            ("parent-open", ".", os.O_RDONLY | os.O_DIRECTORY),
            "parent-fsync", "parent-close",
        ])
        self.assertEqual(directory_descriptors, set())
        self.assertEqual(list(working.glob(".baton-write-*")), [])

    def test_atomic_write_parent_sync_failures_block_dependents_and_retry_cleanly(self):
        for operation in ("parent-open", "parent-fsync"):
            with self.subTest(operation=operation):
                module = runpy.run_path(
                    str(SOURCE_BATON),
                    run_name="baton_atomic_{}_failure_probe".format(operation),
                )
                parent = self.base / operation
                parent.mkdir()
                target = parent / "state.json"
                target.write_text("old\n")
                events, directory_descriptors, injected = (
                    self.instrument_atomic_write_os(module, operation)
                )
                dependent_publications = []

                caught = None
                try:
                    module["atomic_write"](target, "durable-or-failed\n")
                    dependent_publications.append("published")
                except OSError as error:
                    caught = error

                self.assertIs(caught, injected)
                self.assertEqual(dependent_publications, [])
                self.assertEqual(target.read_text(), "durable-or-failed\n")
                self.assertEqual(events[:3], [
                    "temp-fsync", "replace",
                    ("parent-open", str(parent), os.O_RDONLY | os.O_DIRECTORY),
                ])
                self.assertEqual(
                    events[3:],
                    [] if operation == "parent-open"
                    else ["parent-fsync", "parent-close"],
                )
                self.assertEqual(directory_descriptors, set())
                self.assertEqual(list(parent.glob(".baton-write-*")), [])

                module["atomic_write"].__globals__["os"] = os
                module["atomic_write"](target, "durable-or-failed\n")
                dependent_publications.append("published")
                self.assertEqual(dependent_publications, ["published"])
                self.assertEqual(target.read_text(), "durable-or-failed\n")
                self.assertEqual(list(parent.glob(".baton-write-*")), [])

    def test_atomic_write_replace_failure_preserves_target_and_cleans_temp(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_atomic_cleanup_probe")
        parent = self.base / "replace-failure"
        parent.mkdir()
        target = parent / "state.json"
        target.write_text("old\n")
        events, directory_descriptors, injected = self.instrument_atomic_write_os(
            module, "replace",
        )

        caught = None
        try:
            module["atomic_write"](target, "new\n")
        except OSError as error:
            caught = error

        self.assertIs(caught, injected)
        self.assertEqual(target.read_text(), "old\n")
        self.assertEqual(events, ["temp-fsync", "replace"])
        self.assertEqual(directory_descriptors, set())
        self.assertEqual(list(parent.glob(".baton-write-*")), [])

    def test_init_requires_git_and_creates_only_runtime_files(self):
        plain = self.base / "plain"
        plain.mkdir()
        result = self.command([SOURCE_BATON, "init", plain], plain)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((plain / ".baton").exists())

        project = self.make_project()
        initialized = self.command([SOURCE_BATON, "init", project], project, check=True)
        self.assertIn(
            "next: have your coding agent read .baton/orchestrator.md",
            initialized.stdout,
        )
        self.assertNotIn("orchestrator brief --phase start", initialized.stdout)
        lines = (project / ".gitignore").read_text().splitlines()
        self.assertEqual(lines.count(".baton/"), 1)
        runtime = project / ".baton"
        self.assertTrue(os.access(runtime / "baton", os.X_OK))
        self.assertTrue((runtime / "config.toml").exists())
        self.assertFalse((runtime / "config.example.toml").exists())

        with (runtime / "config.toml").open("rb") as source:
            fresh_config = tomllib.load(source)
        self.assertNotIn("worker", fresh_config.get("commands", {}))
        self.assertNotIn("tiers", fresh_config)
        for forbidden in ("model", "provider", "reasoning", "fallback"):
            self.assertNotIn(forbidden, fresh_config)

        config = runtime / "config.toml"
        memory = runtime / "memory.md"
        cursor = runtime / "orchestrator-handoff-cursor.json"
        config.write_text("# preserved config\n")
        memory.write_text("# preserved memory\n")
        cursor.write_text(
            '{"version": 1, "accepted_at": "2026-01-01T00:00:00Z", '
            '"seen_ids": []}\n'
        )
        self.command([SOURCE_BATON, "init", project, "--force"], project, check=True)
        self.assertEqual(config.read_text(), "# preserved config\n")
        self.assertEqual(memory.read_text(), "# preserved memory\n")
        self.assertEqual(
            cursor.read_text(),
            '{"version": 1, "accepted_at": "2026-01-01T00:00:00Z", '
            '"seen_ids": []}\n',
        )

        nested = project / "nested"
        nested.mkdir()
        nested_result = self.command([SOURCE_BATON, "init", nested], nested)
        self.assertNotEqual(nested_result.returncode, 0)
        self.assertFalse((nested / ".baton").exists())

        symlink_project = self.make_project("symlink-project", initialize=False)
        external = self.base / "external"
        external.mkdir()
        (external / "sentinel").write_text("unchanged\n")
        (symlink_project / ".baton").symlink_to(
            external, target_is_directory=True,
        )
        escaped = self.command(
            [SOURCE_BATON, "init", symlink_project], symlink_project,
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
            [SOURCE_BATON, "init", submodule_project], submodule_project,
        )
        self.assertNotEqual(unsupported.returncode, 0)
        self.git(submodule_project, "rm", "--cached", "-q", "vendor")
        staged_removal = self.command(
            [SOURCE_BATON, "init", submodule_project], submodule_project,
        )
        self.assertNotEqual(staged_removal.returncode, 0)
        self.assertFalse((submodule_project / ".baton").exists())

    def test_runtime_safety_rejects_nested_file_and_directory_symlinks(self):
        for target_is_directory in (False, True):
            with self.subTest(target_is_directory=target_is_directory):
                project = self.make_project(
                    "runtime-symlink-{}".format(
                        "directory" if target_is_directory else "file"
                    )
                )
                external = self.base / (
                    "external-directory" if target_is_directory else "external-file"
                )
                if target_is_directory:
                    external.mkdir()
                else:
                    external.write_text("outside runtime\n")
                link = project / ".baton" / "work" / "nested-link"
                link.symlink_to(external, target_is_directory=target_is_directory)

                rejected = self.baton(project, "status")
                self.assertEqual(rejected.returncode, 1)
                self.assertIn(
                    "managed runtime paths cannot be symlinks:",
                    rejected.stderr,
                )
                self.assertIn("nested-link", rejected.stderr)

        project = self.make_project("runtime-cursor-symlink")
        external = self.base / "external-cursor.json"
        external.write_text("{}\n")
        cursor = project / ".baton" / "orchestrator-handoff-cursor.json"
        cursor.symlink_to(external)

        rejected = self.baton(project, "status")
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("managed runtime file is not a regular file:", rejected.stderr)
        self.assertIn("orchestrator-handoff-cursor.json", rejected.stderr)

    def test_status_reuses_loaded_tasks_when_rendering_next_actions(self):
        project = self.make_project("status-load-count")
        self.create_task(project, "status load count", ["status/**"])
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_status_load_probe")
        globals_dict = module["cmd_status"].__globals__
        original = globals_dict["load_all_tasks"]
        calls = []

        def counted_load(baton_dir, validate_history=True):
            calls.append((baton_dir, validate_history))
            return original(baton_dir, validate_history)

        globals_dict["load_all_tasks"] = counted_load
        previous_directory = os.getcwd()
        previous_baton_dir = os.environ.get("BATON_DIR")
        try:
            os.chdir(project)
            os.environ["BATON_DIR"] = str(project / ".baton")
            with redirect_stdout(io.StringIO()):
                module["cmd_status"](SimpleNamespace())
        finally:
            globals_dict["load_all_tasks"] = original
            os.chdir(previous_directory)
            if previous_baton_dir is None:
                os.environ.pop("BATON_DIR", None)
            else:
                os.environ["BATON_DIR"] = previous_baton_dir
        self.assertEqual(len(calls), 1)

    def test_scope_overlap_index_matches_conservative_overlap_rules(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_scope_index_probe")
        overlap = module["scopes_overlap"]
        index_type = module["ScopeOverlapIndex"]
        scope_sets = [
            [],
            ["*"],
            ["**"],
            ["?ase/**"],
            ["src"],
            ["Src/Feature/**"],
            ["src/feature/file?.py"],
            ["src/features/**"],
            ["docs/**", "tests/unit/?ase.py"],
            ["other/place/**"],
        ]
        lease_cases = [
            [],
            [["src/Feature/**"]],
            [["docs/**"], ["other/place/**"]],
            [["?"]],
            [["**"]],
            [[]],
            [["src/feature/file?.py"], ["tests/unit/**"]],
        ]
        for leased in lease_cases:
            index = index_type(leased)
            for candidate in scope_sets:
                with self.subTest(leased=leased, candidate=candidate):
                    expected = any(overlap(candidate, item) for item in leased)
                    self.assertEqual(index.overlaps(candidate), expected)

        index = index_type([["src/Feature/file?.py"]])
        self.assertTrue(index.overlaps(["SRC"]))
        self.assertTrue(index.overlaps(["src/feature/deeper/**"]))
        self.assertFalse(index.overlaps(["src/features/**"]))

    def test_pick_wave_preserves_order_dependencies_and_skip_reasons(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_wave_index_probe")

        def task(task_id, scope, status="queued", dependencies=None):
            return {
                "id": task_id, "scope": scope, "status": status,
                "depends_on": dependencies or [],
            }

        tasks = [
            task("T001-status", ["status/**"], "running"),
            task("T002-waiting", ["waiting/**"], dependencies=["T900-missing"]),
            task("T003-busy", ["BUSY/root/child/**"]),
            task("T004-first", ["src/one/**"], dependencies=["T800-done"]),
            task("T005-conflict", ["SRC/one/deeper/*.py"]),
            task("T006-second", ["docs/**"]),
            task("T007-capacity", ["other/**"]),
            task("T008-late-conflict", ["src/**"]),
        ]
        requested = [task["id"] for task in tasks]
        archived = [task("T800-done", ["old/**"], "done")]
        wave, skipped = module["pick_wave"](
            tasks, archived, requested, 2, [["busy/root/**"]],
        )
        self.assertEqual([task["id"] for task in wave], [
            "T004-first", "T006-second",
        ])
        self.assertEqual(skipped, [
            ("T001-status", "status is running"),
            ("T002-waiting", "waiting on T900-missing"),
            ("T003-busy", "scope conflicts with another lease"),
            ("T005-conflict", "scope conflicts with another lease"),
            ("T007-capacity", "max_parallel reached"),
            ("T008-late-conflict", "scope conflicts with another lease"),
        ])

    def test_task_create_loads_active_and_archive_once(self):
        project = self.make_project("task-create-load-count")
        dependency = self.create_task(project, "archived dependency", ["old/**"])
        runtime = project / ".baton"
        (runtime / "tasks" / f"{dependency}.json").replace(
            runtime / "archive" / f"{dependency}.json"
        )
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_create_load_probe")
        globals_dict = module["cmd_task_create"].__globals__
        original_load = globals_dict["load_tasks_from"]
        calls = []

        def counted_load(directory, validate_history=True):
            calls.append((Path(directory).name, validate_history))
            return original_load(directory, validate_history)

        globals_dict["load_tasks_from"] = counted_load
        globals_dict["require_orchestrator"] = lambda: None
        globals_dict["require_baton_dir"] = lambda: str(runtime)
        try:
            with redirect_stdout(io.StringIO()):
                module["cmd_task_create"](SimpleNamespace(
                    title="snapshot creation", scope=["new/**"],
                    depends_on=[dependency], tier="test",
                ))
        finally:
            globals_dict["load_tasks_from"] = original_load

        self.assertEqual(calls, [("tasks", True), ("archive", True)])
        created = self.state(project, "T002-snapshot-creation")
        self.assertEqual(created["depends_on"], [dependency])

    def test_scope_normalization_and_input_validation(self):
        project = self.make_project()
        first = self.create_task(project, "plain scope", ["src/**"])
        second = self.create_task(project, "dot scope", ["./src/**"])
        dry = self.baton(project, "run", "--dry-run", check=True)
        self.assertIn(first, dry.stdout)
        self.assertIn(f"skip {second}: scope conflicts", dry.stdout)

        upper = self.create_task(project, "upper scope", ["Case/**"])
        lower = self.create_task(project, "lower scope", ["case/**"])
        case_dry = self.baton(project, "run", upper, lower, "--dry-run", check=True)
        self.assertIn(f"would run: {upper}", case_dry.stdout)
        self.assertIn(f"skip {lower}: scope conflicts", case_dry.stdout)

        whole = self.create_task(project, "whole", ["."])
        self.assertEqual(self.state(project, whole)["scope"], [])
        absolute_scope = str(self.base / "absolute") + "/**"
        for bad in ("../src/**", absolute_scope, "src/[ab].py", "src/**x/file"):
            result = self.try_create_task(project, "bad scope", [bad])
            self.assertNotEqual(result.returncode, 0, bad)
        rejected_id = self.baton(
            project, "task", "create", "--title", "bad", "--id", "T999-bad",
        )
        self.assertNotEqual(rejected_id.returncode, 0)
        secret = project / "secret.json"
        secret.write_text('{"sentinel": "do-not-read"}\n')
        traversal = self.baton(project, "task", "show", "../../secret")
        self.assertNotEqual(traversal.returncode, 0)
        self.assertNotIn("do-not-read", traversal.stdout)
        for bad_id in ("T000-lower", "T1-bad", "../T001-bad"):
            result = self.baton(project, "task", "show", bad_id)
            self.assertNotEqual(result.returncode, 0, bad_id)
        empty_title = self.baton(
            project, "task", "create", "--title", "", "--scope", "empty/**",
        )
        self.assertNotEqual(empty_title.returncode, 0)
        truncated = self.create_task(project, "x" * 39 + " next", ["slug/**"])
        self.assertEqual(truncated, "T006-" + "x" * 39)
        for value in ("0", "-1"):
            result = self.baton(project, "run", "--dry-run", "--max-parallel", value)
            self.assertNotEqual(result.returncode, 0, value)

    def test_task_create_rejects_multiline_title_section_injection(self):
        project = self.make_project()
        runtime = project / ".baton"
        title = "normal title\n\n## Objective\nInjected objective from title"
        rejected = self.baton(
            project, "task", "create", "--title", title, "--tier", "test",
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("single-line", rejected.stderr)
        self.assertEqual(list((runtime / "tasks").iterdir()), [])

        unicode_title = "aperçu 東京 😀"
        task_id = self.create_task(project, unicode_title)
        self.assertEqual(self.state(project, task_id)["title"], unicode_title)
        spec = runtime / "tasks" / f"{task_id}.md"
        self.assertEqual(spec.read_text().count("## Objective\n"), 1)

    def test_task_create_rejects_unicode_line_separators_without_artifacts(self):
        for separator in ("\u2028", "\u2029"):
            for variant, title in (
                    ("embedded", f"safe{separator}unsafe"),
                    ("trailing", f"trailing{separator}")):
                with self.subTest(separator=hex(ord(separator)), variant=variant):
                    project = self.make_project(
                        f"unicode-title-{ord(separator):x}-{variant}"
                    )
                    runtime = project / ".baton"
                    rejected = self.baton(
                        project, "task", "create", "--title", title,
                        "--tier", "test",
                    )
                    self.assertEqual(rejected.returncode, 1)
                    self.assertIn("single-line", rejected.stderr)
                    self.assertNotIn("Traceback", rejected.stderr)
                    self.assertEqual(list((runtime / "tasks").iterdir()), [])

    def test_parallel_wave_reports_diffs_and_lifecycle(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        one = self.create_task(project, "alpha", ["alpha/**"])
        two = self.create_task(project, "beta", ["beta/**"])
        barrier = self.base / "barrier"
        run = self.baton(project, "run", env={"BARRIER": barrier}, check=True)
        self.assertIn(one, run.stdout)
        for task_id in (one, two):
            self.assertEqual(self.state(project, task_id)["status"], "needs_review")
            work = project / ".baton" / "work" / task_id
            self.assertTrue((work / "attempt-1.report.md").stat().st_size)
            diff = (work / "attempt-1.diff").read_text()
            self.assertIn(f"{task_id}.txt", diff)
        self.accept_task(project, one)
        self.accept_task(project, two)

    def test_attempt_diff_starts_at_attempt_baseline(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        first = self.create_task(project, "first", ["same/**"])
        self.baton(project, "run", check=True)
        self.accept_task(project, first)

        human = project / "same" / "human.txt"
        human.write_text("already here\n")
        second = self.create_task(project, "second", ["same/**"], [first])
        self.baton(project, "run", check=True)
        diff = (project / ".baton" / "work" / second / "attempt-1.diff").read_text()
        self.assertIn(f"{second}.txt", diff)
        self.assertNotIn(f"{first}.txt", diff)
        self.assertNotIn("human.txt", diff)

        self.baton(project, "task", "return", second, "--reason", "retry", check=True)
        no_change = self.write_worker(NO_CHANGE_WORKER)
        self.configure(project, no_change)
        self.baton(project, "run", second, check=True)
        retry_diff = project / ".baton" / "work" / second / "attempt-2.diff"
        self.assertEqual(retry_diff.read_text(), "")

    def test_scope_violation_blocks_acceptance_even_when_file_was_dirty(self):
        project = self.make_project()
        outside = project / "outside.txt"
        outside.write_text("before\n")
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "scoped", ["inside/**"])
        run = self.baton(project, "run", env={"WRITE_OUTSIDE": "outside.txt"}, check=True)
        self.assertIn("scope violation", run.stdout.lower())
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "blocked")
        self.assertIn("outside.txt", state["scope_violations"])
        violations_diff = (
            project / ".baton" / "work" / task_id
            / "attempt-1.violations.diff"
        )
        self.assertIn("outside.txt", violations_diff.read_text())
        accept = self.baton(project, "task", "accept", task_id)
        self.assertNotEqual(accept.returncode, 0)
        returned = self.baton(
            project, "task", "return", task_id, "--reason", "retry",
        )
        self.assertNotEqual(returned.returncode, 0)
        outside.write_text("before\n")
        self.baton(
            project, "task", "return", task_id,
            "--reason", "outside file restored", check=True,
        )
        self.assertEqual(self.state(project, task_id)["status"], "queued")

    def test_dotfile_scope_matches_dotfile_paths(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "workflow", [".github/**"])
        self.baton(project, "run", task_id, check=True)
        self.assertEqual(self.state(project, task_id)["status"], "needs_review")
        diff = project / ".baton" / "work" / task_id / "attempt-1.diff"
        self.assertIn(f".github/{task_id}.txt", diff.read_text())

    def test_changed_paths_preserve_literal_posix_filename_characters(self):
        worker = self.write_worker(GOOD_WORKER.replace(
            'target_file = target / f"{tid}.txt"',
            'target_file = target / os.environ["LITERAL_NAME"]',
        ))
        names = (
            "[id].txt", "what?.txt", "star*.txt", " trailing.txt ",
            "back\\slash.txt",
        )
        for index, name in enumerate(names):
            with self.subTest(name=name):
                project = self.make_project(f"literal-path-{index}")
                self.configure(project, worker)
                task_id = self.create_task(project, f"literal path {index}", ["src/**"])
                self.baton(
                    project, "run", task_id, env={"LITERAL_NAME": name}, check=True,
                )
                state = self.state(project, task_id)
                self.assertEqual(state["status"], "needs_review")
                worker_exit = state["history"][-1]
                expected = ["src/" + name]
                self.assertEqual(worker_exit["declared_paths"], expected)
                self.assertEqual(worker_exit["observed_paths"], expected)

    def test_case_colliding_observed_paths_fail_changed_path_attribution(self):
        self.require_case_sensitive_filesystem()
        project = self.make_project("case-colliding-observed-paths")
        worker = self.write_worker(GOOD_WORKER.replace(
            'target_file = target / f"{tid}.txt"\n'
            'target_file.write_text(f"attempt {attempt}\\n")',
            'target_file = target / "Foo.txt"\n'
            'target_file.write_text(f"attempt {attempt}\\n")\n'
            '(target / "foo.txt").write_text(f"attempt {attempt} lower\\n")',
        ))
        self.configure(project, worker)
        task_id = self.create_task(project, "case collision", ["case/**"])

        self.baton(project, "run", task_id, check=True)

        state = self.state(project, task_id)
        worker_exit = state["history"][-1]
        expected = ["case/Foo.txt", "case/foo.txt"]
        self.assertEqual(worker_exit["observed_paths"], expected)
        diff = (
            project / ".baton" / "work" / task_id / "attempt-1.diff"
        ).read_text()
        for path in expected:
            self.assertIn(path, diff)
        self.assertEqual(worker_exit["declared_paths"], ["case/Foo.txt"])
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "changed_paths_mismatch")

    def test_single_observed_path_accepts_case_variant_declaration(self):
        self.require_case_sensitive_filesystem()
        project = self.make_project("single-case-variant-declaration")
        worker = self.write_worker(GOOD_WORKER.replace(
            'target_file = target / f"{tid}.txt"',
            'target_file = target / "Foo.txt"',
        ).replace(
            'changed = [target_file.relative_to(root).as_posix()]',
            'changed = [target_file.relative_to(root).as_posix().lower()]',
        ))
        self.configure(project, worker)
        task_id = self.create_task(project, "case variant declaration", ["case/**"])

        self.baton(project, "run", task_id, check=True)

        state = self.state(project, task_id)
        worker_exit = state["history"][-1]
        self.assertEqual(state["status"], "needs_review")
        self.assertEqual(worker_exit["declared_paths"], ["case/foo.txt"])
        self.assertEqual(worker_exit["observed_paths"], ["case/Foo.txt"])

    def test_multiple_distinct_observed_paths_accept_exact_declarations(self):
        project = self.make_project("multiple-distinct-observed-paths")
        worker = self.write_worker(GOOD_WORKER.replace(
            'changed = [target_file.relative_to(root).as_posix()]',
            'second_file = target / "second.txt"\n'
            'second_file.write_text(f"attempt {attempt} second\\n")\n'
            'changed = [target_file.relative_to(root).as_posix(),\n'
            '           second_file.relative_to(root).as_posix()]',
        ))
        self.configure(project, worker)
        task_id = self.create_task(project, "multiple distinct paths", ["many/**"])

        self.baton(project, "run", task_id, check=True)

        state = self.state(project, task_id)
        worker_exit = state["history"][-1]
        expected = [f"many/{task_id}.txt", "many/second.txt"]
        self.assertEqual(state["status"], "needs_review")
        self.assertEqual(worker_exit["declared_paths"], expected)
        self.assertEqual(worker_exit["observed_paths"], expected)

    def test_case_variant_duplicate_worker_result_paths_are_invalid(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_worker_paths_probe")
        self.assertIsNone(module["worker_changed_paths"](
            ["case/Foo.txt", "case/foo.txt"],
        ))

    def test_needs_decision_round_trip(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "decision", ["decision/**"])
        self.baton(
            project, "run", task_id,
            env={"SUBMIT_STATUS": "needs_decision"}, check=True,
        )
        self.assertEqual(self.state(project, task_id)["status"], "needs_decision")
        self.baton(
            project, "task", "decide", task_id,
            "--answer", "Use option A", check=True,
        )
        self.assertEqual(self.state(project, task_id)["attempt"], 2)
        spec = project / ".baton" / "tasks" / f"{task_id}.md"
        self.assertIn("Use option A", spec.read_text())

    def test_decision_question_text_is_sanitized_flattened_and_exactly_bounded(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_decision_text_probe")
        flatten = module["flatten_bounded_text"]
        raw = (
            "\x1b]terminal title\x07 First\r\n\t\x1b[31msecond\x1b[0m"
            "\v\f\x85third\x00tail " + "x" * 200
        )
        clean = "First second thirdtail " + "x" * 200
        bounded = flatten(raw, 32)
        self.assertEqual(bounded, clean[:31] + "…")
        self.assertEqual(len(bounded), 32)
        self.assertEqual(flatten("  one\n\ttwo  ", 160), "one two")
        self.assertEqual(flatten({"not": "text"}, 160), "")
        self.assertEqual(flatten("", 160), "")

        history_note = "Question recovered from history?"
        task = {
            "last_note": "Current worker question?",
            "history": [{
                "event": "worker_exited", "status": "needs_decision",
                "note": history_note,
            }],
        }
        self.assertEqual(module["decision_question"](task), "Current worker question?")
        task["last_note"] = {"not": "text"}
        self.assertEqual(module["decision_question"](task), history_note)
        task["history"][0]["note"] = {"not": "text"}
        self.assertEqual(module["decision_question"](task), "")

    def test_next_actions_and_status_inline_bounded_worker_questions(self):
        project = self.make_project()
        reviews = [self.create_task(project, f"review {index}") for index in range(4)]
        decisions = [self.create_task(project, f"decision {index}") for index in range(3)]
        runtime = project / ".baton"
        raw_question = (
            "Choose\n\x1b[31moption\x1b[0m \x00\x85 carefully: " + "x" * 200
        )

        for task_id in reviews:
            path = runtime / "tasks" / f"{task_id}.json"
            task = json.loads(path.read_text())
            task["status"] = "needs_review"
            path.write_text(json.dumps(task))
        for index, task_id in enumerate(decisions):
            path = runtime / "tasks" / f"{task_id}.json"
            task = json.loads(path.read_text())
            question = raw_question if index == 0 else f"Question {index}?"
            task["status"] = "needs_decision"
            task["last_note"] = question
            task["history"].append({
                "event": "worker_exited", "status": "needs_decision", "note": question,
            })
            path.write_text(json.dumps(task))

        status = self.baton(project, "status", check=True)
        self.assertNotIn("\x1b", status.stdout)
        self.assertNotIn("\x00", status.stdout)
        self.assertIn(" - worker question: Choose option carefully: ", status.stdout)
        actions = status.stdout.rsplit("Next actions:\n", 1)[1].splitlines()
        self.assertLessEqual(len(actions), 5)
        self.assertTrue(all(line.startswith("- review") for line in actions[:3]))
        self.assertEqual(actions[2], "- review: +2 more")
        self.assertTrue(actions[3].startswith(
            f"- decide {decisions[0]}: worker question: Choose option carefully: ",
        ))
        rendered_question = actions[3].split("worker question: ", 1)[1]
        self.assertEqual(len(rendered_question), 160)
        self.assertTrue(rendered_question.endswith("…"))
        self.assertEqual(actions[4], "- decide: +2 more")

        fallback_project = self.make_project("decision-fallback")
        fallback = self.create_task(fallback_project, "missing question")
        fallback_path = (
            fallback_project / ".baton" / "tasks" / f"{fallback}.json"
        )
        fallback_task = json.loads(fallback_path.read_text())
        fallback_task["status"] = "needs_decision"
        fallback_task["last_note"] = {"not": "text"}
        fallback_task["history"].append({
            "event": "worker_exited", "status": "needs_decision", "note": None,
        })
        fallback_path.write_text(json.dumps(fallback_task))
        fallback_status = self.baton(fallback_project, "status", check=True)
        self.assertIn(f"\n- decide {fallback}\n", fallback_status.stdout)
        self.assertNotIn("worker question:", fallback_status.stdout)

    def test_start_brief_bounds_questions_and_recommends_a_real_decision_id(self):
        project = self.make_project()
        decisions = [self.create_task(project, f"start decision {index}") for index in range(5)]
        runtime = project / ".baton"
        for index, task_id in enumerate(decisions):
            path = runtime / "tasks" / f"{task_id}.json"
            task = json.loads(path.read_text())
            question = f"Worker\nquestion \x1b[31m{index}\x1b[0m?"
            task["status"] = "needs_decision"
            task["last_note"] = question
            task["history"].append({
                "event": "worker_exited", "status": "needs_decision", "note": question,
            })
            path.write_text(json.dumps(task))

        started = self.baton(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        )
        decision_block = started.stdout.split("Needs decision: ", 1)[1].split(
            "Needs review:", 1,
        )[0]
        self.assertIn("+1 more", decision_block.splitlines()[0])
        question_lines = [
            line for line in decision_block.splitlines() if "worker question:" in line
        ]
        self.assertEqual(len(question_lines), 2)
        self.assertEqual(
            question_lines[0], f"- {decisions[0]}: worker question: Worker question 0?",
        )
        self.assertEqual(
            question_lines[1], f"- {decisions[1]}: worker question: Worker question 1?",
        )
        self.assertIn(
            f"Recommended next command: .baton/baton task decide {decisions[0]} --answer ANSWER",
            started.stdout,
        )
        self.assertNotIn(".baton/baton task decide +1 more", started.stdout)

    def test_start_brief_onboards_only_until_all_conventional_routes_are_valid(self):
        project = self.make_project()
        question = (
            "Which model and reasoning level should Baton use for hard, medium, "
            "and easy tasks? You can specify each one or ask me to derive the "
            "settings from the current orchestrator."
        )
        ui_rule = (
            "Ask this as a persistent plain-text question that remains visible "
            "until answered. Never use a transient form; expiration or dismissal "
            "is not an answer and must not be treated as selecting any option."
        )

        def routing_section(output):
            marker = "\nWorker routing:\n"
            self.assertEqual(output.count(marker), 1)
            body = output.split(marker, 1)[1].split("\n\n", 1)[0]
            return ["Worker routing:", *body.splitlines()]

        started = self.baton(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        ).stdout
        section = routing_section(started)
        self.assertIn(
            "- Current safe settings: hard: not configured; medium: not configured; easy: not configured.",
            section,
        )
        self.assertEqual(sum(question in line for line in section), 1)
        self.assertEqual(sum(ui_rule in line for line in section), 1)
        self.assertNotIn("Harness memory:", started)
        self.assertNotIn("GPT", "\n".join(section))
        self.assertNotIn("Claude", "\n".join(section))
        self.assertNotIn("command =", "\n".join(section))

        config = project / ".baton" / "config.toml"
        config.write_text(
            '[tiers.hard]\ncommand = "/usr/bin/true {prompt_file}"\n'
            '[tiers.medium]\ncommand = "/missing/worker {prompt_file}"\n'
            '[tiers.easy]\ncommand = "/usr/bin/true {prompt_file}"\n'
        )
        partial = self.baton(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        ).stdout
        partial_section = routing_section(partial)
        self.assertIn(
            "- Current safe settings: hard: unlabeled worker; medium: not configured; easy: unlabeled worker.",
            partial_section,
        )
        self.assertEqual(sum(question in line for line in partial_section), 1)

        config.write_text(config.read_text().replace("/missing/worker", "/usr/bin/true"))
        complete = self.baton(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        ).stdout
        complete_section = routing_section(complete)
        self.assertIn(
            "- Current safe settings: hard: unlabeled worker; medium: unlabeled worker; easy: unlabeled worker.",
            complete_section,
        )
        self.assertNotIn(question, "\n".join(complete_section))
        self.assertTrue(any(
            "change these settings at any time" in line for line in complete_section
        ))

    def test_worker_role_and_live_runner_guards(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "guarded", ["guarded/**"])
        marker = self.base / "finished"
        self_accept = self.base / "self-accept"
        proc = subprocess.Popen(
            [str(project / ".baton" / "baton"), "run", task_id],
            cwd=project,
            env=clean_test_environment({
                "FINISH_MARKER": marker, "SELF_ACCEPT_RESULT": self_accept,
            }),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists())
        self.assertEqual(self.state(project, task_id)["status"], "running")
        self.assertNotEqual(self.baton(project, "task", "accept", task_id).returncode, 0)
        dry = self.baton(project, "run", task_id, "--dry-run")
        self.assertIn("status is running", dry.stdout)
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, stdout + stderr)
        self.assertNotEqual(self_accept.read_text(), "0")
        self.assertEqual(self.state(project, task_id)["status"], "needs_review")
        diff = project / ".baton" / "work" / task_id / "attempt-1.diff"
        self.assertIn("after-finish.txt", diff.read_text())

    def test_worker_phase_briefs_and_default_finish_gate(self):
        project = self.make_project()
        task_id = self.create_task(project, "brief gate", ["brief/**"])
        env = self.lease_task(project, task_id, "lease-one")
        runtime = project / ".baton"
        token_path = runtime / "work" / task_id / "finish-brief-token.json"

        unleased = self.baton(project, "task", "brief", task_id, "--phase", "edit")
        self.assertNotEqual(unleased.returncode, 0)
        for phase, heading in (("edit", "Edit"), ("verify", "Verify")):
            output = self.baton(
                project, "task", "brief", task_id, "--phase", phase,
                env=env, check=True,
            )
            self.assertTrue(output.stdout.startswith("# Critical Context Capsule\n"))
            self.assertIn(f"## {heading} phase checklist", output.stdout)
            self.assertNotIn("Brief token:", output.stdout)
            self.assertFalse(token_path.exists())

        first_brief, first_token = self.report_brief_token(project, task_id, env)
        second_brief, second_token = self.report_brief_token(project, task_id, env)
        self.assertTrue(first_brief.stdout.startswith("# Critical Context Capsule\n"))
        self.assertIn("## Report phase checklist", second_brief.stdout)
        self.assertNotEqual(first_token, second_token)
        report = runtime / "work" / task_id / "attempt-1.report.md"
        report.write_text(self.report_text())

        finish = ["task", "finish", task_id, "--status", "needs_review"]
        for token in (None, "foreign-token", first_token):
            command = finish + (["--brief", token] if token else [])
            rejected = self.baton(project, *command, env=env)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("report-phase brief token is required", rejected.stderr)

        self.baton(project, *finish, "--brief", second_token, env=env, check=True)
        self.assertFalse(token_path.exists())
        result = runtime / "work" / task_id / "attempt-1.result.json"
        result.unlink()
        replay = self.baton(
            project, *finish, "--brief", second_token, env=env,
        )
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("report-phase brief token is required", replay.stderr)

    def test_phase_brief_receipts_are_bounded_and_malformed_files_are_replaced(self):
        project = self.make_project()
        task_id = self.create_task(project, "brief receipts", ["receipts/**"])
        env = self.lease_task(project, task_id, "receipt-lease")
        receipt_path = (
            project / ".baton" / "work" / task_id
            / "attempt-1.briefs.json"
        )
        receipt_path.write_text('{"phases": ["malformed"], "token": "must-disappear"}\n')

        self.baton(
            project, "task", "brief", task_id, "--phase", "edit",
            env=env, check=True,
        )
        first_size = receipt_path.stat().st_size
        first = json.loads(receipt_path.read_text())
        self.assertEqual(
            set(first), {"task_id", "attempt", "lease", "capsule_digest", "phases"},
        )
        self.assertEqual(first["task_id"], task_id)
        self.assertEqual(first["attempt"], 1)
        self.assertEqual(first["lease"], "receipt-lease")
        self.assertRegex(first["capsule_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(set(first["phases"]), {"edit"})
        self.assertEqual(first["phases"]["edit"]["count"], 1)
        self.assertNotIn("token", receipt_path.read_text())

        self.baton(
            project, "task", "brief", task_id, "--phase", "edit",
            env=env, check=True,
        )
        second = json.loads(receipt_path.read_text())
        self.assertEqual(receipt_path.stat().st_size, first_size)
        self.assertEqual(second["phases"]["edit"]["count"], 2)
        self.assertEqual(
            second["phases"]["edit"]["first_at"],
            first["phases"]["edit"]["first_at"],
        )

    def test_oversized_integer_phase_receipt_is_replaced(self):
        project = self.make_project()
        task_id = self.create_task(project, "oversized receipt", ["receipts/**"])
        env = self.lease_task(project, task_id, "oversized-receipt-lease")
        work = project / ".baton" / "work" / task_id
        receipt_path = work / "attempt-1.briefs.json"
        digest = (work / "attempt-1.brief.md").read_text().splitlines()[0].removeprefix(
            "Content digest: ",
        )
        receipt_path.write_text(
            '{"task_id":' + json.dumps(task_id)
            + ',"attempt":1,"lease":"oversized-receipt-lease","capsule_digest":'
            + json.dumps(digest)
            + ',"phases":{"edit":{"first_at":"now","last_at":"now","count":'
            + "9" * 5000 + "}}}\n"
        )

        self.baton(
            project, "task", "brief", task_id, "--phase", "edit",
            env=env, check=True,
        )
        replaced = json.loads(receipt_path.read_text())
        self.assertEqual(set(replaced["phases"]), {"edit"})
        self.assertEqual(replaced["phases"]["edit"]["count"], 1)

    def test_phase_sequence_gate_enforces_order_and_edit_invalidates_report_token(self):
        project = self.make_project()
        config = project / ".baton" / "config.toml"
        config.write_text(config.read_text().replace(
            "phase_sequence_requires_briefs = false",
            "phase_sequence_requires_briefs = true",
        ))
        task_id = self.create_task(project, "phase sequence", ["sequence/**"])
        env = self.lease_task(project, task_id, "sequence-lease")

        verify = self.baton(
            project, "task", "brief", task_id, "--phase", "verify", env=env,
        )
        self.assertEqual(
            verify.stderr,
            f"error: phase sequence requires an edit brief; run `.baton/baton task brief "
            f"{task_id} --phase edit`\n",
        )
        report = self.baton(
            project, "task", "brief", task_id, "--phase", "report", env=env,
        )
        self.assertEqual(report.stderr, verify.stderr)

        self.baton(
            project, "task", "brief", task_id, "--phase", "edit",
            env=env, check=True,
        )
        report = self.baton(
            project, "task", "brief", task_id, "--phase", "report", env=env,
        )
        self.assertEqual(
            report.stderr,
            f"error: phase sequence requires a verify brief; run `.baton/baton task brief "
            f"{task_id} --phase verify`\n",
        )
        self.baton(
            project, "task", "brief", task_id, "--phase", "verify",
            env=env, check=True,
        )
        _brief, stale_token = self.report_brief_token(project, task_id, env)
        token_path = (
            project / ".baton" / "work" / task_id
            / "finish-brief-token.json"
        )
        self.assertTrue(token_path.exists())
        self.baton(
            project, "task", "brief", task_id, "--phase", "edit",
            env=env, check=True,
        )
        self.assertFalse(token_path.exists())
        stale = self.baton(
            project, "task", "finish", task_id, "--status", "failed",
            "--brief", stale_token, env=env,
        )
        self.assertIn("fresh report-phase brief token is required", stale.stderr)
        _brief, fresh_token = self.report_brief_token(project, task_id, env)
        self.baton(
            project, "task", "finish", task_id, "--status", "failed",
            "--brief", fresh_token, env=env, check=True,
        )

    def test_phase_sequence_gate_defaults_off_and_never_blocks_briefs(self):
        project = self.make_project()
        task_id = self.create_task(project, "phase sequence off", ["sequence-off/**"])
        env = self.lease_task(project, task_id, "sequence-off-lease")
        self.report_brief_token(project, task_id, env)
        self.baton(
            project, "task", "brief", task_id, "--phase", "verify",
            env=env, check=True,
        )
        receipt_path = (
            project / ".baton" / "work" / task_id
            / "attempt-1.briefs.json"
        )
        self.assertEqual(
            set(json.loads(receipt_path.read_text())["phases"]), {"report", "verify"},
        )

    def test_finish_gate_can_be_disabled(self):
        project = self.make_project()
        config = project / ".baton" / "config.toml"
        config.write_text(config.read_text().replace(
            "finish_requires_brief = true", "finish_requires_brief = false",
        ))
        task_id = self.create_task(project, "gate off", ["off/**"])
        env = self.lease_task(project, task_id, "gate-off-lease")
        report = (
            project / ".baton" / "work" / task_id
            / "attempt-1.report.md"
        )
        report.write_text(self.report_text())
        self.baton(
            project, "task", "finish", task_id, "--status", "needs_review",
            env=env, check=True,
        )

    def test_report_missing_heading_preserves_token_and_same_token_refinishes(self):
        project, task_id, env, report, token = self.prepare_finish("missing-heading")
        report.write_text(self.report_text().replace(
            "\n## Decisions and risks\n- none\n", "\n",
        ))
        work = report.parent
        token_path = work / "finish-brief-token.json"
        result_path = work / "attempt-1.result.json"
        state_path = project / ".baton" / "tasks" / f"{task_id}.json"
        state_before = state_path.read_bytes()
        token_before = token_path.read_bytes()
        command = [
            "task", "finish", task_id, "--status", "needs_review",
            "--brief", token,
        ]

        rejected = self.baton(project, *command, env=env)
        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(
            rejected.stderr,
            "error: report rejected: missing required report section "
            "`## Decisions and risks`; fix the report to match the worker.md template, "
            "then rerun `task finish` with the same `--brief` token\n",
        )
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(token_path.read_bytes(), token_before)
        self.assertFalse(result_path.exists())

        report.write_text(self.report_text())
        self.baton(project, *command, env=env, check=True)
        self.assertFalse(token_path.exists())
        self.assertEqual(json.loads(result_path.read_text())["status"], "needs_review")

    def test_report_empty_verification_body_is_rejected(self):
        project, task_id, env, report, token = self.prepare_finish("empty-verification")
        report.write_text(self.report_text().replace("- test verification", ""))
        rejected = self.baton(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", token, env=env,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("report section `## Verification` has an empty body", rejected.stderr)
        self.assertIn("same `--brief` token", rejected.stderr)

    def test_report_empty_fenced_core_sections_are_rejected(self):
        project, task_id, env, report, token = self.prepare_finish("empty-fences")
        report.write_text(
            "# report\n\n## Result\nneeds_review\n\n## Changes\n```\n```\n\n"
            "## Verification\n~~~\n~~~\n\n## Decisions and risks\n- none\n"
        )
        command = [
            "task", "finish", task_id, "--status", "needs_review",
            "--brief", token,
        ]
        rejected = self.baton(project, *command, env=env)
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("report section `## Changes` has an empty body", rejected.stderr)
        self.assertIn("report section `## Verification` has an empty body", rejected.stderr)
        self.assertFalse((report.parent / "attempt-1.result.json").exists())
        self.assertTrue((report.parent / "finish-brief-token.json").exists())

        report.write_text(self.report_text())
        self.baton(project, *command, env=env, check=True)

    def test_report_result_must_match_submitted_status(self):
        project, task_id, env, report, token = self.prepare_finish("result-mismatch")
        report.write_text(self.report_text(status="failed"))
        rejected = self.baton(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", token, env=env,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn(
            "report section `## Result` starts with 'failed', not submitted status "
            "'needs_review'",
            rejected.stderr,
        )

    def test_report_heading_inside_fence_does_not_count(self):
        project, task_id, env, report, token = self.prepare_finish("fenced-heading")
        report.write_text(
            "# task report\n\n## Result\nneeds_review\n\n## Changes\n- changes\n\n"
            "```markdown\n## Verification\n- fake verification\n```\n\n"
            "## Decisions and risks\n- none\n"
        )
        rejected = self.baton(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", token, env=env,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("missing required report section `## Verification`", rejected.stderr)

    def test_report_backtick_in_backtick_fence_info_does_not_hide_heading(self):
        project, task_id, env, report, token = self.prepare_finish(
            "invalid-backtick-info",
        )
        report.write_text(
            "# task report\n\n## Result\nneeds_review\n\n## Changes\n- changes\n\n"
            "`````foo`bar\n## Verification\n- verification\n\n"
            "## Decisions and risks\n- none\n"
        )
        self.baton(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", token, env=env, check=True,
        )

    def test_report_heading_inside_three_space_indented_fence_does_not_count(self):
        project, task_id, env, report, token = self.prepare_finish("indented-fence")
        report.write_text(
            "# task report\n\n## Result\nneeds_review\n\n## Changes\n- changes\n\n"
            "   ```\n## Verification\n- fake verification\n   ````\n\n"
            "## Decisions and risks\n- none\n"
        )
        rejected = self.baton(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", token, env=env,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("missing required report section `## Verification`", rejected.stderr)

    def test_report_fence_closes_only_with_matching_character(self):
        project, task_id, env, report, token = self.prepare_finish("fence-character")
        report.write_text(
            "# task report\n\n## Result\nneeds_review\n\n## Changes\n- changes\n\n"
            "```\n~~~\n## Verification\n- fake verification\n```\n\n"
            "## Decisions and risks\n- none\n"
        )
        rejected = self.baton(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", token, env=env,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("missing required report section `## Verification`", rejected.stderr)
        self.assertNotIn("missing required report section `## Decisions", rejected.stderr)

    def test_report_fence_closes_only_at_opening_length_or_longer(self):
        project, task_id, env, report, token = self.prepare_finish("fence-length")
        report.write_text(
            "# task report\n\n## Result\nneeds_review\n\n## Changes\n- changes\n\n"
            "````\n```\n## Verification\n- fake verification\n````\n\n"
            "## Decisions and risks\n- none\n"
        )
        rejected = self.baton(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", token, env=env,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("missing required report section `## Verification`", rejected.stderr)
        self.assertNotIn("missing required report section `## Decisions", rejected.stderr)

    def test_commonmark_fence_state_handles_openers_and_closers(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_fence_state_probe")
        transition = module["commonmark_fence_state"]
        cases = (
            ("backtick info", "```python", None, ("`", 3)),
            ("invalid backtick info", "`````foo`bar", None, None),
            ("tilde info with backtick", "  ~~~~ foo`bar", None, ("~", 4)),
            ("three-space indent", "   ```", None, ("`", 3)),
            ("four-space indent", "    ```", None, None),
            ("shorter closer", "```", ("`", 4), ("`", 4)),
            ("mismatched closer", "~~~~", ("`", 4), ("`", 4)),
            ("longer closer with whitespace", "  `````` \t", ("`", 4), None),
        )
        for name, line, before, after in cases:
            with self.subTest(name=name):
                self.assertEqual(transition(line, before), after)

    def test_report_fence_matrix_preserves_real_section_routing(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_report_fence_probe")
        problems = module["report_section_problems"]
        specimens = (
            ("backtick-lf", "```python\n", "````` \t\n"),
            ("tilde-crlf-info", "   ~~~~ markdown`ok\r\n", " ~~~~~\t\r\n"),
            (
                "mixed-shorter-mismatched",
                "  ````text\r\n",
                "```\n~~~\r\n   ``````\t\n",
            ),
        )
        prefix = (
            "# report\n\n## Result\nneeds_review\n\n## Changes\n- changes\n\n"
        )
        suffix = (
            "## Verification\n- verification\r\n\n"
            "## Decisions and risks\n- none\n"
        )
        for name, opener, closer in specimens:
            with self.subTest(name=name):
                report = prefix + opener + "## Verification\r\n- fenced heading\n" + closer
                self.assertEqual(problems(report + suffix, "needs_review"), [])

        unterminated = prefix + "~~~ info`allowed\r\n" + suffix
        self.assertEqual(
            problems(unterminated, "needs_review"),
            [
                "missing required report section `## Verification`",
                "missing required report section `## Decisions and risks`",
            ],
        )

    def test_crlf_structured_report_is_accepted(self):
        project, task_id, env, report, token = self.prepare_finish("crlf-report")
        report.write_bytes(self.report_text(newline="\r\n").encode("utf-8"))
        self.baton(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", token, env=env, check=True,
        )

    def test_report_section_gate_off_accepts_free_form_report(self):
        project, task_id, env, report, token = self.prepare_finish("section-gate-off")
        config = project / ".baton" / "config.toml"
        config.write_text(config.read_text().replace(
            "report_requires_sections = true", "report_requires_sections = false",
        ))
        report.write_text("free-form review report\n")
        self.baton(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", token, env=env, check=True,
        )

    def test_non_review_statuses_skip_report_section_gate(self):
        for status in ("needs_decision", "blocked", "failed"):
            with self.subTest(status=status):
                project, task_id, env, _report, token = self.prepare_finish(
                    "unstructured-" + status.replace("_", "-"),
                )
                self.baton(
                    project, "task", "finish", task_id, "--status", status,
                    "--brief", token, env=env, check=True,
                )

    def test_unreadable_review_reports_reject_without_consuming_token(self):
        for kind in ("missing", "directory", "bad-utf8"):
            with self.subTest(kind=kind):
                project, task_id, env, report, token = self.prepare_finish(
                    "unreadable-" + kind,
                )
                if kind == "directory":
                    report.mkdir()
                    expected = "report file is not a regular file"
                elif kind == "bad-utf8":
                    report.write_bytes(b"\xff\xfe")
                    expected = "report file is not valid UTF-8"
                else:
                    expected = "report file is missing"
                token_path = report.parent / "finish-brief-token.json"
                rejected = self.baton(
                    project, "task", "finish", task_id, "--status", "needs_review",
                    "--brief", token, env=env,
                )
                self.assertEqual(rejected.returncode, 1)
                self.assertIn("report rejected: " + expected, rejected.stderr)
                self.assertTrue(token_path.exists())

    def test_non_boolean_report_section_gate_fails_validate(self):
        project = self.make_project("invalid-report-gate")
        config = project / ".baton" / "config.toml"
        config.write_text(config.read_text().replace(
            "report_requires_sections = true", 'report_requires_sections = "yes"',
        ))
        validation = self.baton(project, "validate")
        self.assertEqual(validation.returncode, 1)
        self.assertIn(
            "config: report_requires_sections must be true or false",
            validation.stdout,
        )

    def test_non_boolean_phase_sequence_gate_fails_validate(self):
        project = self.make_project("invalid-phase-sequence-gate")
        config = project / ".baton" / "config.toml"
        config.write_text(config.read_text().replace(
            "phase_sequence_requires_briefs = false",
            'phase_sequence_requires_briefs = "yes"',
        ))
        validation = self.baton(project, "validate")
        self.assertEqual(validation.returncode, 1)
        self.assertIn(
            "config: phase_sequence_requires_briefs must be true or false",
            validation.stdout,
        )

    def test_return_then_retry_invalidates_report_brief_token(self):
        project = self.make_project()
        task_id = self.create_task(project, "retry brief", ["retry/**"])
        first_env = self.lease_task(project, task_id, "first-lease")
        _brief, old_token = self.report_brief_token(project, task_id, first_env)

        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        task = json.loads(state_path.read_text())
        task["status"] = "needs_review"
        task.pop("runner")
        state_path.write_text(json.dumps(task))
        self.baton(
            project, "task", "return", task_id, "--reason", "retry token",
            check=True,
        )

        second_env = self.lease_task(project, task_id, "second-lease")
        report = runtime / "work" / task_id / "attempt-2.report.md"
        report.write_text("# retry report\n")
        stale = self.baton(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", old_token, env=second_env,
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("report-phase brief token is required", stale.stderr)

    def assert_multiline_retry_context_is_published_once(
            self, name, status, command, option, value, expected_entry,
            expected_event, expected_stdout):
        project = self.make_project(name + "-multiline-publication")
        task_id = self.create_task(project, name + " multiline publication", ["retry/**"])
        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        original_state = json.loads(state_path.read_text())
        original_state["status"] = status
        state_path.write_text(json.dumps(original_state))
        spec_path = runtime / "tasks" / f"{task_id}.md"

        result = self.baton(
            project, "task", command, task_id, option, value, check=True,
        )

        final_state = self.state(project, task_id)
        spec_text = spec_path.read_text()
        self.assertEqual(result.stdout, expected_stdout.format(task_id=task_id))
        self.assertEqual((final_state["status"], final_state["attempt"]), ("queued", 2))
        new_history = final_state["history"][len(original_state["history"]):]
        self.assertEqual([entry["event"] for entry in new_history], [expected_event])
        self.assertEqual(spec_text.count(expected_entry), 1)
        markers = re.findall(
            r"<!-- baton-retry-publication:v1 transition=(\w+) attempt=(\d+) "
            r"digest=sha256:[0-9a-f]{64} -->",
            spec_text,
        )
        self.assertEqual(markers, [(command, "1")])
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_multiline_retry_proof_probe",
        )
        proof = module["retry_publication_marker"](
            task_id, command, 1, expected_entry,
        )
        self.assertIn(proof + "\n" + expected_entry + "\n", spec_text)
        retry_capsule = self.baton(
            project, "task", "capsule", task_id, "--raw", check=True,
        ).stdout
        self.assertNotIn("baton-retry-publication", retry_capsule)
        label = "Decision" if command == "decide" else "Review feedback"
        self.assertIn(
            label + ": " + expected_entry.splitlines()[0].rstrip(), retry_capsule,
        )

    def test_decide_publishes_multiline_fenced_answer_once(self):
        answer = "Use this implementation:\n```python\nprint('durable')\n```"
        self.assert_multiline_retry_context_is_published_once(
            "decision", "needs_decision", "decide", "--answer", answer,
            "- " + answer, "decided", "{task_id} answered and re-queued\n",
        )

    def test_return_publishes_multiline_fenced_feedback_once(self):
        reason = "Please preserve this example:\n```python\nprint('retry')\n```"
        self.assert_multiline_retry_context_is_published_once(
            "return", "needs_review", "return", "--reason", reason,
            "- attempt 1: " + reason, "returned",
            "{task_id} returned for attempt 2\n",
        )

    def test_decide_preserves_multiline_whitespace_and_embedded_heading(self):
        answer = "Preserve lines  \n\n## Embedded user heading\nfinal line  "
        self.assert_multiline_retry_context_is_published_once(
            "decision-exact-multiline", "needs_decision", "decide", "--answer",
            answer, "- " + answer, "decided",
            "{task_id} answered and re-queued\n",
        )

    def test_retry_publication_precedes_unterminated_fenced_input(self):
        cases = (
            (
                "decision", "needs_decision", "decide", "--answer",
                "Keep this exact:\n```text\nunterminated decision",
                "- Keep this exact:\n```text\nunterminated decision",
                "decided", "{task_id} answered and re-queued\n",
            ),
            (
                "return", "needs_review", "return", "--reason",
                "Keep this exact:\n~~~text\nunterminated feedback",
                "- attempt 1: Keep this exact:\n~~~text\nunterminated feedback",
                "returned", "{task_id} returned for attempt 2\n",
            ),
        )
        for (
                name, status, command, option, value, entry,
                event, stdout,
        ) in cases:
            with self.subTest(command=command):
                self.assert_multiline_retry_context_is_published_once(
                    name + "-unterminated", status, command, option, value,
                    entry, event, stdout,
                )

    def assert_retry_context_append_failure_is_safe(
            self, status, transition_name, transition_argv, expected_context):
        project = self.make_project("{}-append-failure".format(transition_name))
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        task_id = self.create_task(project, "retry context append failure", ["retry/**"])
        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        task = json.loads(state_path.read_text())
        task["status"] = status
        state_path.write_text(json.dumps(task))

        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_retry_append_failure_probe",
        )
        globals_ = module[transition_name].__globals__
        globals_["require_baton_dir"] = lambda: str(runtime)
        globals_["require_orchestrator"] = lambda: None

        def fail_append(*_args):
            raise OSError("simulated specification publication failure")

        globals_["append_md_section"] = fail_append
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as stopped:
                module["main"](["task", *transition_argv(task_id)])

        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("simulated specification publication failure", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        failed_state = self.state(project, task_id)
        self.assertEqual((failed_state["status"], failed_state["attempt"]), (status, 1))
        self.assertNotIn(expected_context, (
            runtime / "tasks" / f"{task_id}.md"
        ).read_text())

        run = self.baton(project, "run", task_id)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(f"skip {task_id}: status is {status}", run.stdout)
        self.assertEqual(self.state(project, task_id), failed_state)
        work = runtime / "work" / task_id
        self.assertFalse(work.exists() and any(work.glob("attempt-2.*")))

    def test_return_append_failure_keeps_retry_non_runnable(self):
        self.assert_retry_context_append_failure_is_safe(
            "needs_review", "cmd_task_return",
            lambda task_id: [
                "return", task_id, "--reason", "Publish durable feedback",
            ],
            "- attempt 1: Publish durable feedback",
        )

    def test_decide_append_failure_keeps_retry_non_runnable(self):
        self.assert_retry_context_append_failure_is_safe(
            "needs_decision", "cmd_task_decide",
            lambda task_id: [
                "decide", task_id, "--answer", "Use the durable answer",
            ],
            "- Use the durable answer",
        )

    def assert_retry_final_state_failure_is_idempotent(
            self, status, transition_name, transition_argv, expected_context,
            expected_stdout, expected_event):
        project = self.make_project("{}-state-failure".format(transition_name))
        task_id = self.create_task(project, "retry final state failure", ["retry/**"])
        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        task = json.loads(state_path.read_text())
        task["status"] = status
        state_path.write_text(json.dumps(task))
        original_state = self.state(project, task_id)
        token_path = runtime / "work" / task_id / "review-brief-token.json"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("{}\n")

        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_retry_state_failure_probe",
        )
        globals_ = module[transition_name].__globals__
        globals_["require_baton_dir"] = lambda: str(runtime)
        globals_["require_orchestrator"] = lambda: None
        original_save_task = globals_["save_task"]

        def fail_final_save(baton_dir, current):
            if current.get("id") == task_id and current.get("status") == "queued":
                raise OSError("simulated final task state persistence failure")
            return original_save_task(baton_dir, current)

        globals_["save_task"] = fail_final_save
        first_stdout = io.StringIO()
        first_stderr = io.StringIO()
        with redirect_stdout(first_stdout), redirect_stderr(first_stderr):
            with self.assertRaises(SystemExit) as stopped:
                module["main"](["task", *transition_argv(task_id)])

        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(first_stdout.getvalue(), "")
        self.assertIn(
            "simulated final task state persistence failure", first_stderr.getvalue(),
        )
        self.assertNotIn("Traceback", first_stderr.getvalue())
        self.assertEqual(self.state(project, task_id), original_state)
        self.assertFalse(token_path.exists())
        spec_path = runtime / "tasks" / f"{task_id}.md"
        first_spec = spec_path.read_text()
        self.assertEqual(first_spec.count(expected_context), 1)
        marker_pattern = (
            r"<!-- baton-retry-publication:v1 transition={} attempt=1 "
            r"digest=sha256:[0-9a-f]{{64}} -->"
        ).format(transition_name.removeprefix("cmd_task_"))
        self.assertEqual(len(re.findall(marker_pattern, first_spec)), 1)

        globals_["save_task"] = original_save_task
        second_stdout = io.StringIO()
        second_stderr = io.StringIO()
        with redirect_stdout(second_stdout), redirect_stderr(second_stderr):
            module["main"](["task", *transition_argv(task_id)])

        self.assertEqual(second_stdout.getvalue(), expected_stdout.format(task_id=task_id))
        self.assertEqual(second_stderr.getvalue(), "")
        final_state = self.state(project, task_id)
        self.assertEqual((final_state["status"], final_state["attempt"]), ("queued", 2))
        new_history = final_state["history"][len(original_state["history"]):]
        self.assertEqual([entry["event"] for entry in new_history], [expected_event])
        final_spec = spec_path.read_text()
        self.assertEqual(final_spec.count(expected_context), 1)
        self.assertEqual(len(re.findall(marker_pattern, final_spec)), 1)

    def test_return_final_state_failure_retries_without_duplicate_feedback(self):
        self.assert_retry_final_state_failure_is_idempotent(
            "needs_review", "cmd_task_return",
            lambda task_id: [
                "return", task_id, "--reason", "Publish durable feedback",
            ],
            "- attempt 1: Publish durable feedback",
            "{task_id} returned for attempt 2\n", "returned",
        )

    def test_decide_final_state_failure_retries_without_duplicate_answer(self):
        self.assert_retry_final_state_failure_is_idempotent(
            "needs_decision", "cmd_task_decide",
            lambda task_id: [
                "decide", task_id, "--answer", "Use the durable answer",
            ],
            "- Use the durable answer",
            "{task_id} answered and re-queued\n", "decided",
        )

    def test_bare_cr_retry_publication_routes_and_retries_idempotently(self):
        cases = (
            (
                "decide", "Decisions", "needs_decision", "cmd_task_decide",
                lambda task_id: [
                    "decide", task_id, "--answer", "Use the bare CR answer",
                ],
                "- Use the bare CR answer", "decided",
                b"## Decisions\n\n## Review feedback\n",
                b"## Decisions\r## Review feedback\n",
                b"## Review feedback\n",
            ),
            (
                "return", "Review feedback", "needs_review", "cmd_task_return",
                lambda task_id: [
                    "return", task_id, "--reason", "Fix the bare CR feedback",
                ],
                "- attempt 1: Fix the bare CR feedback", "returned",
                b"## Review feedback\n",
                b"## Review feedback\r## Retry tail\ntail bytes stay exact\n",
                b"## Retry tail\n",
            ),
        )
        for (
                command, section, status, transition_name, transition_argv,
                entry, expected_event, original_target, mixed_target,
                later_heading,
        ) in cases:
            with self.subTest(command=command):
                project = self.make_project("bare-cr-retry-" + command)
                task_id = self.create_task(
                    project, "bare CR retry " + command, ["retry/**"],
                )
                runtime = project / ".baton"
                state_path = runtime / "tasks" / f"{task_id}.json"
                state = json.loads(state_path.read_text())
                state["status"] = status
                state_path.write_text(json.dumps(state))
                original_state = self.state(project, task_id)
                spec_path = runtime / "tasks" / f"{task_id}.md"
                original_spec = spec_path.read_bytes()
                self.assertEqual(original_spec.count(original_target), 1)
                before = original_spec.replace(original_target, mixed_target, 1)
                spec_path.write_bytes(before)

                module = runpy.run_path(
                    str(SOURCE_BATON),
                    run_name="baton_bare_cr_retry_{}_probe".format(command),
                )
                globals_ = module[transition_name].__globals__
                globals_["require_baton_dir"] = lambda: str(runtime)
                globals_["require_orchestrator"] = lambda: None
                original_save_task = globals_["save_task"]

                def fail_final_save(baton_dir, current):
                    if current.get("id") == task_id and current.get("status") == "queued":
                        raise OSError("simulated final task state persistence failure")
                    return original_save_task(baton_dir, current)

                globals_["save_task"] = fail_final_save
                first_stderr = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(first_stderr):
                    with self.assertRaises(SystemExit) as stopped:
                        module["main"](["task", *transition_argv(task_id)])

                self.assertEqual(stopped.exception.code, 1)
                self.assertIn(
                    "simulated final task state persistence failure",
                    first_stderr.getvalue(),
                )
                self.assertEqual(self.state(project, task_id), original_state)
                proof = module["retry_publication_marker"](
                    task_id, command, 1, entry,
                )
                publication = (proof + "\n" + entry + "\n").encode()
                first_spec = spec_path.read_bytes()
                target = ("## " + section).encode() + b"\r"
                target_end = first_spec.index(target) + len(target)
                self.assertEqual(first_spec[target_end:target_end + len(publication)], publication)
                self.assertLess(target_end, first_spec.index(later_heading, target_end))
                self.assertEqual(first_spec.count(proof.encode()), 1)
                self.assertEqual(first_spec.count(entry.encode()), 1)
                self.assertEqual(first_spec.replace(publication, b"", 1), before)
                decoded = first_spec.decode()
                self.assertTrue(module["task_spec_has_retry_publication"](
                    decoded, section, proof, entry,
                ))

                globals_["save_task"] = original_save_task
                second_stdout = io.StringIO()
                with redirect_stdout(second_stdout), redirect_stderr(io.StringIO()):
                    module["main"](["task", *transition_argv(task_id)])

                final_state = self.state(project, task_id)
                self.assertEqual(
                    (final_state["status"], final_state["attempt"]),
                    ("queued", 2),
                )
                new_history = final_state["history"][len(original_state["history"]):]
                self.assertEqual([event["event"] for event in new_history], [expected_event])
                final_spec = spec_path.read_bytes()
                self.assertEqual(final_spec, first_spec)
                self.assertEqual(final_spec.count(proof.encode()), 1)
                self.assertEqual(final_spec.count(entry.encode()), 1)

    def test_return_multiline_final_state_failure_retries_one_publication(self):
        reason = "Preserve this retry:\n```python\nprint('feedback')\n```"
        self.assert_retry_final_state_failure_is_idempotent(
            "needs_review", "cmd_task_return",
            lambda task_id: ["return", task_id, "--reason", reason],
            "- attempt 1: " + reason,
            "{task_id} returned for attempt 2\n", "returned",
        )

    def test_decide_multiline_final_state_failure_retries_one_publication(self):
        answer = "Use this answer:\n```python\nprint('decision')\n```"
        self.assert_retry_final_state_failure_is_idempotent(
            "needs_decision", "cmd_task_decide",
            lambda task_id: ["decide", task_id, "--answer", answer],
            "- " + answer,
            "{task_id} answered and re-queued\n", "decided",
        )

    def test_single_line_retry_publication_migrates_without_duplicate_text(self):
        cases = (
            (
                "decide", "Decisions", "needs_decision", "--answer",
                "Use the existing answer", "- Use the existing answer",
            ),
            (
                "return", "Review feedback", "needs_review", "--reason",
                "Use the existing feedback", "- attempt 1: Use the existing feedback",
            ),
        )
        for command, section, status, option, value, entry in cases:
            with self.subTest(command=command):
                project = self.make_project("legacy-single-line-" + command)
                task_id = self.create_task(project, "legacy retry publication", ["retry/**"])
                runtime = project / ".baton"
                state_path = runtime / "tasks" / f"{task_id}.json"
                state = json.loads(state_path.read_text())
                state["status"] = status
                state_path.write_text(json.dumps(state))
                spec_path = runtime / "tasks" / f"{task_id}.md"
                spec_path.write_text(spec_path.read_text().replace(
                    "## " + section + "\n", "## " + section + "\n" + entry + "\n", 1,
                ))

                self.baton(
                    project, "task", command, task_id, option, value, check=True,
                )

                spec_text = spec_path.read_text()
                self.assertEqual(spec_text.count(entry), 1)
                self.assertEqual(spec_text.count("<!-- baton-retry-publication:v1 "), 1)
                final_state = self.state(project, task_id)
                self.assertEqual(
                    (final_state["status"], final_state["attempt"]), ("queued", 2),
                )

    def assert_retry_context_serializes_with_run(
            self, project, task_id, transition_name, transition_args,
            expected_spec_entry, expected_prompt_context):
        runtime = project / ".baton"
        spec_path = runtime / "tasks" / f"{task_id}.md"
        work = runtime / "work" / task_id
        prior_artifacts = {
            path.name: path.read_bytes()
            for path in work.glob("attempt-1.*")
        }
        self.assertTrue(prior_artifacts)

        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_retry_publication_probe",
        )
        globals_ = module[transition_name].__globals__
        globals_["require_baton_dir"] = lambda: str(runtime)
        globals_["require_orchestrator"] = lambda: None
        original_append = globals_["append_md_section"]
        original_file_lock = globals_["file_lock"]
        original_prepare_worker = globals_["prepare_worker"]
        publication_paused = threading.Event()
        release_publication = threading.Event()
        scheduler_lock_attempted = threading.Event()
        prompt_prepared = threading.Event()
        context_at_preparation = []
        errors = {}

        def synchronized_append(baton_dir, current_id, section, text):
            if (
                    threading.current_thread().name == "retry-publication-probe"
                    and current_id == task_id):
                publication_paused.set()
                if not release_publication.wait(5):
                    raise RuntimeError("retry publication synchronization timed out")
            return original_append(baton_dir, current_id, section, text)

        @contextmanager
        def observed_file_lock(path):
            if (
                    threading.current_thread().name == "retry-run-probe"
                    and path == globals_["lock_path"](str(runtime), "scheduler")):
                scheduler_lock_attempted.set()
            with original_file_lock(path):
                yield

        def observed_prepare_worker(baton_dir, config, task, memory_entries):
            prepared = original_prepare_worker(
                baton_dir, config, task, memory_entries,
            )
            if threading.current_thread().name == "retry-run-probe":
                context_at_preparation.append(
                    expected_spec_entry in spec_path.read_text()
                )
                prompt_prepared.set()
            return prepared

        globals_["append_md_section"] = synchronized_append
        globals_["file_lock"] = observed_file_lock
        globals_["prepare_worker"] = observed_prepare_worker

        def retry():
            try:
                module[transition_name](transition_args)
            except BaseException as error:
                errors["retry"] = error

        context = {
            "lease": "retry-publication-lease",
            "stop_event": threading.Event(),
            "task_ids": [],
        }

        def run():
            try:
                module["run_wave"](
                    SimpleNamespace(ids=[task_id], max_parallel=None, dry_run=False),
                    str(runtime), context,
                )
            except BaseException as error:
                errors["run"] = error

        retry_thread = threading.Thread(
            target=retry, name="retry-publication-probe",
        )
        run_thread = threading.Thread(target=run, name="retry-run-probe")
        previous_directory = os.getcwd()
        previous_baton_dir = os.environ.get("BATON_DIR")
        run_started = False
        prepared_before_release = None
        retry_non_runnable_before_context = None
        try:
            os.chdir(project)
            os.environ["BATON_DIR"] = str(runtime)
            retry_thread.start()
            self.assertTrue(publication_paused.wait(5))
            paused_state = self.state(project, task_id)
            retry_non_runnable_before_context = (
                paused_state["status"] in ("needs_review", "needs_decision")
                and paused_state["attempt"] == 1
                and expected_spec_entry not in spec_path.read_text()
            )

            run_thread.start()
            run_started = True
            self.assertTrue(scheduler_lock_attempted.wait(5))
            prepared_before_release = prompt_prepared.wait(0.5)
        finally:
            release_publication.set()
            retry_thread.join(5)
            if run_started:
                run_thread.join(10)
            os.chdir(previous_directory)
            if previous_baton_dir is None:
                os.environ.pop("BATON_DIR", None)
            else:
                os.environ["BATON_DIR"] = previous_baton_dir

        self.assertFalse(retry_thread.is_alive())
        self.assertFalse(run_thread.is_alive())
        self.assertEqual(errors, {})
        self.assertTrue(retry_non_runnable_before_context)
        final_state = self.state(project, task_id)
        self.assertEqual((final_state["status"], final_state["attempt"]), (
            "needs_review", 2,
        ))
        self.assertNotIn("runner", final_state)
        self.assertIn(expected_spec_entry, spec_path.read_text())
        with self.subTest("scheduler publication fence"):
            self.assertFalse(
                prepared_before_release,
                "run prepared attempt 2 before retry context publication completed",
            )
        with self.subTest("context durable at prompt preparation"):
            self.assertEqual(context_at_preparation, [True])
        with self.subTest("persisted prompt contains retry context"):
            self.assertIn(
                expected_prompt_context,
                (work / "attempt-2.prompt.md").read_text(),
            )
        self.assertEqual(
            prior_artifacts,
            {path.name: path.read_bytes() for path in work.glob("attempt-1.*")},
        )

    def test_return_publishes_feedback_before_retry_run_prepares_prompt(self):
        project = self.make_project("return-run-publication")
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        task_id = self.create_task(project, "return run publication", ["retry/**"])
        self.baton(project, "run", task_id, check=True)

        self.assert_retry_context_serializes_with_run(
            project, task_id, "cmd_task_return",
            SimpleNamespace(id=task_id, reason="Include the corrected retry contract"),
            "- attempt 1: Include the corrected retry contract",
            "Review feedback: - attempt 1: Include the corrected retry contract",
        )

    def test_decide_publishes_answer_before_retry_run_prepares_prompt(self):
        project = self.make_project("decide-run-publication")
        decision_worker = self.write_worker(
            NO_CHANGE_WORKER.replace("needs_review", "needs_decision"),
        )
        self.configure(project, decision_worker)
        task_id = self.create_task(project, "decide run publication", ["decision/**"])
        self.baton(project, "run", task_id, check=True)
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))

        self.assert_retry_context_serializes_with_run(
            project, task_id, "cmd_task_decide",
            SimpleNamespace(id=task_id, answer="Use the durable decision answer"),
            "- Use the durable decision answer",
            "Decision: - Use the durable decision answer",
        )

    def test_decide_routes_answer_to_exact_decisions_heading(self):
        project = self.make_project("exact-decision-heading")
        task_id = self.create_task(project, "exact decision heading", ["decision/**"])
        runtime = project / ".baton"
        spec_path = runtime / "tasks" / f"{task_id}.md"
        context = "Context may mention `## Decisions` without opening that section."
        spec_path.write_text(spec_path.read_text().replace(
            "List the paths and facts the worker needs. Reference memory ids when useful.",
            context,
        ))
        state_path = runtime / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "needs_decision"
        state_path.write_text(json.dumps(state))

        decided = self.baton(
            project, "task", "decide", task_id, "--answer", "Use option A",
            check=True,
        )

        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_decision_sections_probe")
        sections = module["task_spec_sections"](spec_path.read_text())
        self.assertEqual(decided.stdout, f"{task_id} answered and re-queued\n")
        self.assertEqual(sections["Context"], context)
        self.assertEqual(
            module["latest_task_spec_entry"](sections["Decisions"]),
            "- Use option A",
        )
        retry_capsule = self.baton(
            project, "task", "capsule", task_id, "--raw", check=True,
        )
        self.assertIn("Decision: - Use option A", retry_capsule.stdout)
        self.assertNotIn("Use option A", sections["Context"])

    def test_task_spec_fence_scanner_handles_crlf_mixed_endings_and_offsets(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_crlf_scanner_probe")
        headings = module["task_spec_headings"]
        contains_entry = module["task_spec_contains_unfenced_entry"]
        specimens = (
            (
                "invalid-backtick-info",
                "`````foo`bar\r\n## Decisions\n",
                "Decisions",
            ),
            (
                "backtick-crlf-close",
                "   ````python\n## Decisions\r\n```\n"
                "the shorter closer stays fenced\r\n   ``````\r\n"
                "## Decisions\r\n",
                "Decisions",
            ),
            (
                "tilde-mixed-close",
                "  ~~~~~ markdown\r\n## Review feedback\n`````\r\n~~~~\n"
                "the mismatched and shorter closers stay fenced\r\n"
                "   ~~~~~~~\n## Review feedback\r\n",
                "Review feedback",
            ),
        )
        for name, spec, expected_name in specimens:
            with self.subTest(name=name):
                start = spec.rindex("## " + expected_name)
                body_end = start + len("## " + expected_name)
                self.assertEqual(headings(spec), [(expected_name, start, body_end)])
                self.assertEqual(spec[start:body_end], "## " + expected_name)

        entry = "- attempt 1: Preserve CRLF"
        self.assertTrue(contains_entry("preface\r\n" + entry + "\r\ntail", entry))
        self.assertFalse(contains_entry(
            "``` text\r\n" + entry + "\r\n```\r\noutside\n", entry,
        ))
        self.assertEqual(
            headings("~~~ info`allowed\r\n## Decisions\nunterminated"),
            [],
        )

    def test_retry_publication_proof_scanner_handles_all_line_endings(self):
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_retry_line_endings_probe",
        )
        marker = "<!-- baton-retry-publication:v1 proof -->"
        entry = "- exact entry"
        specimens = (
            ("lf", "\n", "\n", "\n"),
            ("crlf", "\r\n", "\r\n", "\r\n"),
            ("bare-cr", "\r", "\r", "\r"),
            ("mixed", "\r", "\r\n", "\n"),
        )
        for name, heading_end, marker_end, entry_end in specimens:
            with self.subTest(name=name):
                prefix = "preamble" + entry_end
                target = "## Decisions" + heading_end
                publication = marker + marker_end + entry + entry_end
                later = "## Review feedback\nuntouched\n"
                spec = prefix + target + publication + later
                body_start = len(prefix + target)
                body_end = len(prefix + target + publication)
                self.assertEqual(
                    module["task_spec_section_bounds"](spec, "Decisions"),
                    (body_start, body_end),
                )
                self.assertTrue(module["task_spec_has_retry_publication"](
                    spec, "Decisions", marker, entry,
                ))
                self.assertFalse(module["task_spec_has_retry_publication"](
                    spec, "Review feedback", marker, entry,
                ))

    def test_retry_publication_routes_past_crlf_fences_without_rewriting_bytes(self):
        cases = (
            (
                "decision", "Decisions", "needs_decision", "decide", "--answer",
                "Preserve decision bytes", "- Preserve decision bytes",
                b"   ````python\r\n## Decisions\r\n- Preserve decision bytes\r\n```\r\n"
                b"shorter closer remains code\n   ``````\r\n",
            ),
            (
                "feedback", "Review feedback", "needs_review", "return", "--reason",
                "Preserve feedback bytes", "- attempt 1: Preserve feedback bytes",
                b"  ~~~~~ markdown\r\n## Review feedback\n"
                b"- attempt 1: Preserve feedback bytes\r\n`````\r\n~~~~\n"
                b"mismatched closers remain code\r\n   ~~~~~~~\r\n",
            ),
        )
        placeholder = (
            b"List the paths and facts the worker needs. Reference memory ids when useful."
        )
        for (
                name, section, status, command, option, value, entry, fence,
        ) in cases:
            with self.subTest(name=name):
                project = self.make_project("crlf-fenced-publication-" + name)
                task_id = self.create_task(project, name + " CRLF publication", [name + "/**"])
                runtime = project / ".baton"
                spec_path = runtime / "tasks" / f"{task_id}.md"
                before = spec_path.read_bytes().replace(b"\n", b"\r\n")
                before = before.replace(placeholder, fence.rstrip(b"\r\n"), 1)
                spec_path.write_bytes(before)
                state_path = runtime / "tasks" / f"{task_id}.json"
                state = json.loads(state_path.read_text())
                state["status"] = status
                state_path.write_text(json.dumps(state))

                self.baton(
                    project, "task", command, task_id, option, value, check=True,
                )

                after = spec_path.read_bytes()
                marker = re.search(
                    rb"<!-- baton-retry-publication:v1 [^\r\n]+ -->\n", after,
                )
                self.assertIsNotNone(marker)
                publication = marker.group(0) + entry.encode() + b"\n"
                self.assertEqual(after.replace(publication, b"", 1), before)
                target = b"## " + section.encode() + b"\r\n"
                self.assertTrue(after.index(fence) < after.index(target))
                self.assertTrue(after.index(target) < after.index(publication))
                self.assertEqual(after.count(fence), 1)

    def test_decide_ignores_fenced_decisions_heading(self):
        project = self.make_project("fenced-decision-heading")
        task_id = self.create_task(project, "fenced decision heading", ["decision/**"])
        runtime = project / ".baton"
        spec_path = runtime / "tasks" / f"{task_id}.md"
        fence = (
            "   ````markdown\n"
            "## Decisions\n"
            "```\n"
            "the shorter backtick run does not close this example\n"
            "   ``````\n"
        )
        before = spec_path.read_text().replace(
            "List the paths and facts the worker needs. Reference memory ids when useful.",
            fence.rstrip("\n"),
        )
        spec_path.write_text(before)
        state_path = runtime / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "needs_decision"
        state_path.write_text(json.dumps(state))

        self.baton(
            project, "task", "decide", task_id, "--answer", "Use option B",
            check=True,
        )

        after = spec_path.read_text()
        self.assertEqual(after.count(fence), 1)
        self.assertRegex(
            after,
            r"## Decisions\n"
            r"<!-- baton-retry-publication:v1 [^\n]+ -->\n"
            r"- Use option B\n",
        )
        self.assertNotIn("- Use option B\n```", after)

    def test_decide_appends_section_when_only_decisions_heading_is_fenced(self):
        project = self.make_project("only-fenced-decision-heading")
        task_id = self.create_task(
            project, "only fenced decision heading", ["decision/**"],
        )
        runtime = project / ".baton"
        spec_path = runtime / "tasks" / f"{task_id}.md"
        fence = "```text\n## Decisions\n```\n"
        before = spec_path.read_text().replace("\n## Decisions\n", "\n", 1).replace(
            "List the paths and facts the worker needs. Reference memory ids when useful.",
            fence.rstrip("\n"),
        )
        spec_path.write_text(before)
        state_path = runtime / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "needs_decision"
        state_path.write_text(json.dumps(state))

        self.baton(
            project, "task", "decide", task_id, "--answer", "Append outside",
            check=True,
        )

        after = spec_path.read_text()
        self.assertEqual(after.count(fence), 1)
        self.assertRegex(
            after,
            r"## Decisions\n"
            r"<!-- baton-retry-publication:v1 transition=decide attempt=1 "
            r"digest=sha256:[0-9a-f]{64} -->\n"
            r"- Append outside\n$",
        )

    def test_return_routes_feedback_to_exact_review_feedback_heading(self):
        project = self.make_project("exact-review-feedback-heading")
        task_id = self.create_task(
            project, "exact review feedback heading", ["feedback/**"],
        )
        runtime = project / ".baton"
        spec_path = runtime / "tasks" / f"{task_id}.md"
        context = "Context may mention `## Review feedback` without opening that section."
        spec_path.write_text(spec_path.read_text().replace(
            "List the paths and facts the worker needs. Reference memory ids when useful.",
            context,
        ))
        state_path = runtime / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "needs_review"
        state_path.write_text(json.dumps(state))

        returned = self.baton(
            project, "task", "return", task_id, "--reason", "Tighten the tests",
            check=True,
        )

        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_feedback_sections_probe")
        sections = module["task_spec_sections"](spec_path.read_text())
        self.assertEqual(returned.stdout, f"{task_id} returned for attempt 2\n")
        self.assertEqual(sections["Context"], context)
        self.assertEqual(
            module["latest_task_spec_entry"](sections["Review feedback"]),
            "- attempt 1: Tighten the tests",
        )
        retry_capsule = self.baton(
            project, "task", "capsule", task_id, "--raw", check=True,
        )
        self.assertIn(
            "Review feedback: - attempt 1: Tighten the tests", retry_capsule.stdout,
        )
        self.assertNotIn("Tighten the tests", sections["Context"])

    def test_return_ignores_fenced_review_feedback_heading(self):
        project = self.make_project("fenced-review-feedback-heading")
        task_id = self.create_task(
            project, "fenced review feedback heading", ["feedback/**"],
        )
        runtime = project / ".baton"
        spec_path = runtime / "tasks" / f"{task_id}.md"
        fence = (
            "  ~~~~~ markdown\n"
            "## Review feedback\n"
            "`````\n"
            "~~~~\n"
            "the mismatched and shorter runs do not close this example\n"
            "   ~~~~~~~\n"
        )
        before = spec_path.read_text().replace(
            "List the paths and facts the worker needs. Reference memory ids when useful.",
            fence.rstrip("\n"),
        )
        spec_path.write_text(before)
        state_path = runtime / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "needs_review"
        state_path.write_text(json.dumps(state))

        self.baton(
            project, "task", "return", task_id, "--reason", "Keep the real section",
            check=True,
        )

        after = spec_path.read_text()
        self.assertEqual(after.count(fence), 1)
        self.assertRegex(
            after,
            r"## Review feedback\n"
            r"<!-- baton-retry-publication:v1 [^\n]+ -->\n"
            r"- attempt 1: Keep the real section\n",
        )
        self.assertNotIn(
            "## Review feedback\n- attempt 1: Keep the real section\n`````", after,
        )

    def test_retry_publication_ignores_matching_entries_inside_fences(self):
        cases = (
            (
                "decision", "Decisions", "needs_decision",
                "decide", "--answer", "Use fenced answer",
                "- Use fenced answer", "   ````text\n- Use fenced answer\n   ``````\n",
            ),
            (
                "return", "Review feedback", "needs_review",
                "return", "--reason", "Use fenced feedback",
                "- attempt 1: Use fenced feedback",
                "  ~~~~~ markdown\n- attempt 1: Use fenced feedback\n  ~~~~~~~\n",
            ),
        )
        for (
                name, section, status, command, option, value, entry, fence,
        ) in cases:
            with self.subTest(name=name):
                project = self.make_project("fenced-retry-entry-" + name)
                task_id = self.create_task(
                    project, "fenced retry entry " + name, [name + "/**"],
                )
                runtime = project / ".baton"
                spec_path = runtime / "tasks" / f"{task_id}.md"
                spec_path.write_text(spec_path.read_text().replace(
                    "## " + section + "\n", "## " + section + "\n" + fence, 1,
                ))
                state_path = runtime / "tasks" / f"{task_id}.json"
                state = json.loads(state_path.read_text())
                state["status"] = status
                state_path.write_text(json.dumps(state))

                result = self.baton(
                    project, "task", command, task_id, option, value, check=True,
                )

                after = spec_path.read_text()
                self.assertEqual(after.count(fence), 1)
                heading_index = after.index("## " + section + "\n")
                marker_index = after.index(
                    "<!-- baton-retry-publication:v1 ", heading_index,
                )
                entry_index = after.index(entry + "\n", marker_index)
                fence_index = after.index(fence, entry_index + len(entry))
                self.assertLess(heading_index, entry_index)
                self.assertLess(heading_index, marker_index)
                self.assertLess(marker_index, entry_index)
                self.assertLess(entry_index, fence_index)
                self.assertEqual(self.state(project, task_id)["attempt"], 2)
                self.assertEqual(result.returncode, 0)

    def test_retry_publication_ignores_matching_proof_inside_fence(self):
        project = self.make_project("fenced-retry-proof")
        task_id = self.create_task(
            project, "fenced retry proof", ["decision/**"],
        )
        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "needs_decision"
        state_path.write_text(json.dumps(state))
        entry = "- Use the durable answer"
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_fenced_retry_proof_probe",
        )
        proof = module["retry_publication_marker"](
            task_id, "decide", 1, entry,
        )
        spec_path = runtime / "tasks" / f"{task_id}.md"
        fenced_proof = "```text\n" + proof + "\n```\n"
        spec_path.write_text(spec_path.read_text().replace(
            "## Decisions\n", "## Decisions\n" + fenced_proof, 1,
        ))

        result = self.baton(
            project, "task", "decide", task_id,
            "--answer", "Use the durable answer", check=True,
        )

        after = spec_path.read_text()
        self.assertEqual(result.stdout, f"{task_id} answered and re-queued\n")
        self.assertEqual(after.count(fenced_proof), 1)
        self.assertIn("## Decisions\n" + proof + "\n" + entry + "\n", after)
        self.assertEqual(after.count(proof), 2)
        self.assertEqual(
            (self.state(project, task_id)["status"],
             self.state(project, task_id)["attempt"]),
            ("queued", 2),
        )

    def test_retry_publication_requires_current_adjacent_entry_in_target_section(self):
        cases = (
            ("wrong-section", "Review feedback", "current", "exact"),
            ("marker-only", "Decisions", "current", "missing"),
            ("edited-entry", "Decisions", "current", "edited"),
            ("stale-attempt", "Decisions", "stale", "exact"),
            ("wrong-transition", "Decisions", "wrong-transition", "exact"),
        )
        for name, poison_section, proof_kind, entry_kind in cases:
            with self.subTest(name=name):
                project = self.make_project("retry-proof-" + name)
                task_id = self.create_task(
                    project, "retry proof " + name, ["decision/**"],
                )
                runtime = project / ".baton"
                state_path = runtime / "tasks" / f"{task_id}.json"
                state = json.loads(state_path.read_text())
                state["status"] = "needs_decision"
                state_path.write_text(json.dumps(state))
                entry = "- Use the exact durable answer"
                module = runpy.run_path(
                    str(SOURCE_BATON),
                    run_name="baton_retry_proof_{}_probe".format(
                        name.replace("-", "_"),
                    ),
                )
                marker = module["retry_publication_marker"]
                current_proof = marker(task_id, "decide", 1, entry)
                poison_proof = marker(
                    task_id,
                    "return" if proof_kind == "wrong-transition" else "decide",
                    0 if proof_kind == "stale" else 1,
                    entry,
                )
                poisoned = poison_proof + "\n"
                if entry_kind == "exact":
                    poisoned += entry + "\n"
                elif entry_kind == "edited":
                    poisoned += entry + " with an edit\n"
                spec_path = runtime / "tasks" / f"{task_id}.md"
                spec_path.write_text(spec_path.read_text().replace(
                    "## " + poison_section + "\n",
                    "## " + poison_section + "\n" + poisoned,
                    1,
                ))

                result = self.baton(
                    project, "task", "decide", task_id,
                    "--answer", "Use the exact durable answer", check=True,
                )

                after = spec_path.read_text()
                self.assertEqual(result.stdout, f"{task_id} answered and re-queued\n")
                self.assertIn(current_proof + "\n" + entry + "\n", after)
                self.assertTrue(module["task_spec_has_retry_publication"](
                    after, "Decisions", current_proof, entry,
                ))
                self.assertIn(poison_proof, after)
                if entry_kind == "exact":
                    self.assertIn(entry, after)
                elif entry_kind == "edited":
                    self.assertIn(entry + " with an edit", after)
                self.assertEqual(
                    (self.state(project, task_id)["status"],
                     self.state(project, task_id)["attempt"]),
                    ("queued", 2),
                )

    def test_orchestrator_review_brief_accept_gate_and_worker_denial(self):
        project = self.make_project()
        self.configure(project, self.write_worker(GOOD_WORKER))
        task_id = self.create_task(project, "review gate", ["review/**"])
        self.baton(project, "run", task_id, check=True)

        missing = self.baton(project, "task", "accept", task_id)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("review-phase brief token is required", missing.stderr)
        brief, first_token = self.review_brief_token(project, task_id)
        self.assertTrue(brief.stdout.startswith("# Critical Context Capsule\n"))
        self.assertIn("attempt-1.report.md", brief.stdout)
        self.assertIn("attempt-1.result.json", brief.stdout)
        self.assertIn("attempt-1.diff", brief.stdout)
        self.assertIn(f"review/{task_id}.txt", brief.stdout)
        self.assertIn("Phase briefs: edit=0 verify=0 report=1", brief.stdout)
        for artifact in ("Report", "Result", "Diff"):
            self.assertRegex(brief.stdout, rf"- {artifact}: .* \(sha256:[0-9a-f]{{12}}\)")
        token_record = json.loads((
            project / ".baton" / "work" / task_id
            / "review-brief-token.json"
        ).read_text())
        self.assertEqual(token_record["token"], first_token)
        self.assertEqual(token_record["task_id"], task_id)
        self.assertEqual(token_record["attempt"], 1)
        self.assertEqual(
            set(token_record["evidence"]),
            {"capsule", "report", "result", "diff", "declared", "observed"},
        )
        for name in ("capsule", "report", "result", "diff"):
            self.assertRegex(token_record["evidence"][name], r"^sha256:[0-9a-f]{64}$")
        _replacement, current_token = self.review_brief_token(project, task_id)
        for token in ("wrong", first_token):
            rejected = self.baton(
                project, "task", "accept", task_id, "--brief", token,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("review-phase brief token is required", rejected.stderr)

        worker_env = {
            "BATON_TASK_ID": task_id, "BATON_ATTEMPT": "1", "BATON_LEASE": "worker",
        }
        denied = self.baton(
            project, "orchestrator", "brief", "--phase", "review", task_id,
            env=worker_env,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("worker processes cannot run orchestrator commands", denied.stderr)

        self.baton(
            project, "task", "accept", task_id, "--brief", current_token, check=True,
        )
        token_path = (
            project / ".baton" / "work" / task_id
            / "review-brief-token.json"
        )
        self.assertFalse(token_path.exists())
        state_path = project / ".baton" / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "needs_review"
        state_path.write_text(json.dumps(state))
        replay = self.baton(
            project, "task", "accept", task_id, "--brief", current_token,
        )
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("review-phase brief token is required", replay.stderr)

    def test_attempt_diff_summary_uses_observed_paths_and_exact_patch_state(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_diff_stat_probe")
        patch = self.base / "attempt.diff"
        patch.write_bytes(
            b"diff --git a/added.txt b/added.txt\n"
            b"new file mode 100644\nindex 0000000..1111111\n"
            b"--- /dev/null\n+++ b/added.txt\n@@ -0,0 +1,2 @@\n"
            b"+alpha\n++++ b/not-a-file.txt\n"
            b"diff --git a/deleted.txt b/deleted.txt\n"
            b"deleted file mode 100644\nindex 2222222..0000000\n"
            b"--- a/deleted.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n"
            b"-gone one\n-gone two\n"
            b"diff --git a/modified.txt b/modified.txt\n"
            b"index 3333333..4444444 100644\n--- a/modified.txt\n+++ b/modified.txt\n"
            b"@@ -1 +1 @@\n-before\n+after\n"
            b"diff --git a/mode.txt b/mode.txt\nold mode 100644\nnew mode 100755\n"
            b"diff --git a/image.bin b/image.bin\nnew file mode 100644\n"
            b"index 0000000..5555555\nGIT binary patch\nliteral 1\nKcmZQz00IC2\n"
            b"diff --git a/outside.txt b/outside.txt\n"
            b"--- a/outside.txt\n+++ b/outside.txt\n@@ -0,0 +1 @@\n+ignore me\n"
        )
        observed = [
            "modified.txt", "phantom.txt", "mode.txt", "image.bin",
            "deleted.txt", "added.txt",
        ]
        summary = module["attempt_diff_summary"](patch, observed)
        self.assertEqual(summary["added"], 3)
        self.assertEqual(summary["removed"], 3)
        self.assertEqual(summary["files"], [
            {"path": "added.txt", "added": 2, "removed": 0, "label": "add"},
            {"path": "deleted.txt", "added": 0, "removed": 2, "label": "delete"},
            {"path": "image.bin", "added": 0, "removed": 0, "label": "binary"},
            {"path": "mode.txt", "added": 0, "removed": 0, "label": "mode"},
            {"path": "modified.txt", "added": 1, "removed": 1, "label": "modify"},
            {"path": "phantom.txt", "added": 0, "removed": 0, "label": "~"},
        ])
        missing = module["attempt_diff_summary"](
            self.base / "missing.diff", ["still-observed.txt"],
        )
        self.assertEqual(
            missing["files"],
            [{"path": "still-observed.txt", "added": 0, "removed": 0, "label": "~"}],
        )

    def test_review_brief_prior_attempts_and_opt_in_sanitized_log_tail(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        task_id = self.create_task(project, "review context", ["context/**"])
        self.baton(project, "run", task_id, check=True)
        runtime = project / ".baton"
        work = runtime / "work" / task_id
        state_path = runtime / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["attempt"] = 5
        launch = next(
            entry for entry in state["history"] if entry.get("event") == "launched"
        )
        worker_exit = next(
            entry for entry in state["history"]
            if entry.get("event") == "worker_exited"
        )
        state["history"].extend((
            {**launch, "attempt": 5},
            {**worker_exit, "attempt": 5},
        ))
        state_path.write_text(json.dumps(state))
        for suffix in ("brief.md", "report.md", "result.json", "diff"):
            (work / f"attempt-5.{suffix}").write_bytes(
                (work / f"attempt-1.{suffix}").read_bytes()
            )
        for attempt in range(2, 5):
            (work / f"attempt-{attempt}.report.md").write_text(f"report {attempt}\n")
            (work / f"attempt-{attempt}.diff").write_text(f"diff {attempt}\n")

        secret = "do-not-leak-environment-value"
        lines = [f"older-{number}" for number in range(14)] + [
            "\x1b[31mred\x1b[0m",
            "\x1b]0;hidden title\x07visible",
            "controls:\x00\x08clean\tkept",
            f"REVIEW_SECRET={secret} standalone {secret}",
            "{'PASSWORD': 'dict-secret'} Bearer bearer-secret",
            "L" * 300,
            "last-line",
        ]
        (work / "attempt-5.log").write_bytes(
            b"X" * 70000 + b"\n" + "\n".join(lines).encode("utf-8") + b"\n"
        )

        default, _token = self.review_brief_token(project, task_id)
        self.assertIn("Diff stat: no changes", default.stdout)
        self.assertNotIn("Untrusted worker log tail", default.stdout)
        self.assertNotIn("last-line", default.stdout)
        prior = default.stdout.split("Prior attempt artifacts (most recent first):\n", 1)[1]
        prior = prior.split("Review checklist:", 1)[0]
        for attempt in (4, 3, 2):
            self.assertIn(f"attempt-{attempt}.report.md", prior)
            self.assertIn(f"attempt-{attempt}.diff", prior)
        self.assertNotIn("attempt-1.report.md", prior)
        self.assertIn("- +1 older attempt", prior)
        self.assertLess(prior.index("attempt-4.report.md"), prior.index("attempt-3.report.md"))

        included, _token = self.review_brief_token(
            project, task_id, env={"REVIEW_SECRET": secret}, include_log_tail=True,
        )
        block = included.stdout.split("Untrusted worker log tail (opt-in):", 1)[1]
        block = "Untrusted worker log tail (opt-in):" + block.split(
            "Review checklist:", 1,
        )[0].rstrip("\n")
        self.assertLessEqual(len(block), 1500)
        self.assertLessEqual(len(block.splitlines()) - 1, 15)
        self.assertIn("red", block)
        self.assertIn("visible", block)
        self.assertIn("controls:clean\tkept", block)
        self.assertIn("REVIEW_SECRET=[redacted]", block)
        self.assertIn("standalone [redacted]", block)
        self.assertIn("last-line", block)
        self.assertNotIn("hidden title", block)
        self.assertNotIn(secret, block)
        self.assertNotIn("dict-secret", block)
        self.assertNotIn("bearer-secret", block)
        self.assertNotIn("\x1b", block)
        self.assertNotIn("\x00", block)
        self.assertTrue(any(len(line) == 240 for line in block.splitlines()))

        (work / "attempt-5.log").unlink()
        unavailable, _token = self.review_brief_token(
            project, task_id, include_log_tail=True,
        )
        self.assertIn(
            "Untrusted worker log tail (opt-in):\nlog tail unavailable",
            unavailable.stdout,
        )
        rejected = self.baton(
            project, "orchestrator", "brief", "--phase", "start",
            "--include-log-tail",
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(
            "--include-log-tail is valid only for the review phase", rejected.stderr,
        )

    def test_sanitize_log_text_redacts_lowercase_and_mixed_case_labels(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_log_sanitizer_probe")
        sanitize = module["sanitize_log_text"]
        self.assertEqual(sanitize("password: hunter2\n"), "password: [redacted]\n")
        labels = (
            "password", "passwd", "pwd", "secret", "token", "key", "api_key",
            "apikey", "auth", "bearer", "credential", "cookie", "session",
        )
        for label in labels:
            mixed_case = label.title()
            for separator in (":", "="):
                with self.subTest(label=mixed_case, separator=separator):
                    self.assertEqual(
                        sanitize(f"{mixed_case}{separator} hunter2\n"),
                        f"{mixed_case}{separator} [redacted]\n",
                    )

    def test_review_token_invalidated_by_return_and_accept_gate_off(self):
        project = self.make_project()
        self.configure(project, self.write_worker(GOOD_WORKER))
        task_id = self.create_task(project, "return review", ["return-review/**"])
        self.baton(project, "run", task_id, check=True)
        _brief, stale_token = self.review_brief_token(project, task_id)
        token_path = (
            project / ".baton" / "work" / task_id
            / "review-brief-token.json"
        )
        self.baton(
            project, "task", "return", task_id, "--reason", "try again", check=True,
        )
        self.assertFalse(token_path.exists())
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        self.baton(project, "run", task_id, check=True)
        invalidated = self.baton(
            project, "task", "accept", task_id, "--brief", stale_token,
        )
        self.assertNotEqual(invalidated.returncode, 0)

        gate_off = self.make_project("gate-off-accept")
        self.configure(gate_off, self.write_worker(GOOD_WORKER))
        config = gate_off / ".baton" / "config.toml"
        config.write_text(config.read_text() + "\n[gates]\naccept_requires_brief = false\n")
        gate_off_id = self.create_task(gate_off, "accept gate off", ["gate-off/**"])
        self.baton(gate_off, "run", gate_off_id, check=True)
        gate_off_report = (
            gate_off / ".baton" / "work" / gate_off_id
            / "attempt-1.report.md"
        )
        gate_off_report.write_text(gate_off_report.read_text() + "changed without brief\n")
        self.baton(gate_off, "task", "accept", gate_off_id, check=True)

    def test_review_result_schema_and_lifecycle_forgery_are_rejected(self):
        missing = object()
        cases = (
            ("wrong-status", "status", "failed", "status does not match worker exit"),
            ("wrong-note", "note", "forged note", "note does not match worker exit"),
            ("wrong-lease", "lease", "forged-lease", "lease does not match launch"),
            ("non-string-lease", "lease", 7, "lease must be non-empty text"),
            ("non-string-time", "at", 7, "at must be a valid UTC timestamp"),
            ("invalid-time", "at", "2026-02-30T00:00:00Z",
             "at must be a valid UTC timestamp"),
            ("non-string-note", "note", 7, "note must be text"),
            ("wrong-paths", "changed_paths", [],
             "changed_paths do not match worker exit declared_paths"),
            ("extra-field", "forged", True, "exactly these fields"),
            ("missing-field", "note", missing, "exactly these fields"),
        )
        for name, field, value, diagnostic in cases:
            with self.subTest(case=name):
                project = self.make_project("result-forgery-" + name)
                self.configure(project, self.write_worker(GOOD_WORKER))
                task_id = self.create_task(project, name, ["evidence/**"])
                self.baton(project, "run", task_id, check=True)
                runtime = project / ".baton"
                state_path = runtime / "tasks" / f"{task_id}.json"
                work = runtime / "work" / task_id
                result_path = work / "attempt-1.result.json"
                result = json.loads(result_path.read_text())
                if value is missing:
                    result.pop(field)
                else:
                    result[field] = value
                result_path.write_text(json.dumps(result))
                state_before = state_path.read_bytes()

                validation = self.baton(project, "validate")
                review = self.baton(
                    project, "orchestrator", "brief", "--phase", "review", task_id,
                )
                for rejected in (validation, review):
                    self.assertEqual(rejected.returncode, 1)
                    self.assertIn(diagnostic, rejected.stdout + rejected.stderr)
                    self.assertNotIn("Traceback", rejected.stdout + rejected.stderr)
                self.assertEqual(state_path.read_bytes(), state_before)
                self.assertFalse((work / "review-brief-token.json").exists())
                self.assertFalse((runtime / "archive" / f"{task_id}.json").exists())

    def test_review_result_rejects_missing_duplicate_reordered_and_wrong_attempt_evidence(self):
        cases = ("missing-launch", "duplicate-launch", "missing-exit", "reordered",
                 "wrong-attempt", "duplicate-exit")
        for case in cases:
            with self.subTest(case=case):
                project = self.make_project("lifecycle-" + case)
                self.configure(project, self.write_worker(NO_CHANGE_WORKER))
                task_id = self.create_task(project, case)
                self.baton(project, "run", task_id, check=True)
                runtime = project / ".baton"
                state_path = runtime / "tasks" / f"{task_id}.json"
                state = json.loads(state_path.read_text())
                history = state["history"]
                launch_index = next(
                    index for index, entry in enumerate(history)
                    if entry.get("event") == "launched"
                )
                exit_index = next(
                    index for index, entry in enumerate(history)
                    if entry.get("event") == "worker_exited"
                )
                if case == "missing-launch":
                    history.pop(launch_index)
                elif case == "duplicate-launch":
                    history.insert(launch_index + 1, dict(history[launch_index]))
                elif case == "missing-exit":
                    history.pop(exit_index)
                elif case == "reordered":
                    launch = history.pop(launch_index)
                    history.insert(exit_index, launch)
                elif case == "wrong-attempt":
                    history[launch_index]["attempt"] = state["attempt"] + 1
                else:
                    history.insert(exit_index + 1, dict(history[exit_index]))
                state_path.write_text(json.dumps(state))
                state_before = state_path.read_bytes()
                work = runtime / "work" / task_id

                validation = self.baton(project, "validate")
                review = self.baton(
                    project, "orchestrator", "brief", "--phase", "review", task_id,
                )
                for rejected in (validation, review):
                    self.assertEqual(rejected.returncode, 1)
                    self.assertIn("lifecycle", rejected.stdout + rejected.stderr)
                    self.assertNotIn("Traceback", rejected.stdout + rejected.stderr)
                self.assertEqual(state_path.read_bytes(), state_before)
                self.assertFalse((work / "review-brief-token.json").exists())

    def test_malformed_review_history_rejects_every_gate_without_mutation(self):
        project = self.make_project("malformed-review-history")
        self.configure(project, self.write_worker(GOOD_WORKER))
        task_id = self.create_task(
            project, "malformed review history", ["evidence/**"],
        )
        self.baton(project, "run", task_id, check=True)
        _brief, token = self.review_brief_token(project, task_id)

        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        work = runtime / "work" / task_id
        state = json.loads(state_path.read_text())
        state["history"].insert(1, "forged-history-entry")
        state_path.write_text(json.dumps(state))
        state_before = state_path.read_bytes()
        evidence_before = {
            path.name: path.read_bytes() for path in work.iterdir() if path.is_file()
        }

        commands = (
            ("validate",),
            ("orchestrator", "brief", "--phase", "review", task_id),
            ("task", "accept", task_id, "--brief", token),
        )
        for command in commands:
            with self.subTest(command=command):
                rejected = self.baton(project, *command)
                output = rejected.stdout + rejected.stderr
                self.assertEqual(rejected.returncode, 1, output)
                self.assertIn("history entry 1 must be an object", output)
                self.assertNotIn("Traceback", output)
                self.assertEqual(state_path.read_bytes(), state_before)
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in work.iterdir() if path.is_file()
                    },
                    evidence_before,
                )
                self.assertFalse((runtime / "archive" / f"{task_id}.json").exists())

        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_malformed_review_history_probe",
        )
        with self.assertRaisesRegex(
                ValueError, "review lifecycle history entry 1 must be an object"):
            module["review_evidence_details"](str(runtime), state)

    def test_post_finalization_result_mutation_blocks_validate_review_and_accept(self):
        project = self.make_project("post-finalization-result-mutation")
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        task_id = self.create_task(project, "post-finalization mutation")
        self.baton(project, "run", task_id, check=True)
        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        work = runtime / "work" / task_id
        worker_exit = self.state(project, task_id)["history"][-1]
        self.assertRegex(worker_exit["result_digest"], r"^sha256:[0-9a-f]{64}$")
        _brief, token = self.review_brief_token(project, task_id)
        token_path = work / "review-brief-token.json"
        token_before = token_path.read_bytes()
        result_path = work / "attempt-1.result.json"
        result = json.loads(result_path.read_text())
        result["at"] = "2026-01-01T00:00:00Z"
        result_path.write_text(json.dumps(result))
        state_before = state_path.read_bytes()

        validation = self.baton(project, "validate")
        review = self.baton(
            project, "orchestrator", "brief", "--phase", "review", task_id,
        )
        accept = self.baton(
            project, "task", "accept", task_id, "--brief", token,
        )
        for rejected in (validation, review, accept):
            self.assertEqual(rejected.returncode, 1)
            self.assertNotIn("Traceback", rejected.stdout + rejected.stderr)
        self.assertIn("changed after finalization", validation.stdout)
        self.assertIn("changed after finalization", review.stderr)
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(token_path.read_bytes(), token_before)
        self.assertFalse((runtime / "archive" / f"{task_id}.json").exists())

    def test_valid_legacy_finalized_review_result_remains_supported(self):
        project = self.make_project("legacy-finalized-result")
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        task_id = self.create_task(project, "legacy finalized result")
        self.baton(project, "run", task_id, check=True)
        state_path = project / ".baton" / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        worker_exit = state["history"][-1]
        worker_exit.pop("attempt")
        worker_exit.pop("result_digest")
        worker_exit.pop("observed_paths")
        state_path.write_text(json.dumps(state))

        self.baton(project, "validate", check=True)
        _brief, token = self.review_brief_token(project, task_id)
        self.baton(
            project, "task", "accept", task_id, "--brief", token, check=True,
        )
        self.baton(project, "archive", check=True)
        self.baton(project, "validate", check=True)

    def test_archived_finalized_result_digest_is_validated_without_mutation(self):
        project = self.make_project("archived-result-mutation")
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        task_id = self.create_task(project, "archived result mutation")
        self.baton(project, "run", task_id, check=True)
        self.accept_task(project, task_id)
        self.baton(project, "archive", check=True)
        runtime = project / ".baton"
        state_path = runtime / "archive" / f"{task_id}.json"
        result_path = (
            runtime / "archive" / f"{task_id}.work" / "attempt-1.result.json"
        )
        result = json.loads(result_path.read_text())
        result["at"] = "2026-01-01T00:00:00Z"
        result_path.write_text(json.dumps(result))
        state_before = state_path.read_bytes()
        work_entries_before = sorted(
            path.name for path in result_path.parent.iterdir()
        )

        validation = self.baton(project, "validate")
        self.assertEqual(validation.returncode, 1)
        self.assertIn("changed after finalization", validation.stdout)
        self.assertNotIn("Traceback", validation.stdout + validation.stderr)
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(
            sorted(path.name for path in result_path.parent.iterdir()),
            work_entries_before,
        )

    def test_review_evidence_mutations_reject_without_consuming_token(self):
        for artifact in ("report.md", "result.json", "diff"):
            with self.subTest(artifact=artifact):
                project = self.make_project("mutated-" + artifact.replace(".", "-"))
                self.configure(project, self.write_worker(GOOD_WORKER))
                task_id = self.create_task(project, "mutate " + artifact, ["mutate/**"])
                self.baton(project, "run", task_id, check=True)
                _brief, token = self.review_brief_token(project, task_id)
                token_path = (
                    project / ".baton" / "work" / task_id
                    / "review-brief-token.json"
                )
                evidence_path = (
                    token_path.parent / f"attempt-1.{artifact}"
                )
                evidence_path.write_text(evidence_path.read_text() + "\n")

                rejected = self.baton(
                    project, "task", "accept", task_id, "--brief", token,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn(
                    "review evidence changed; run a fresh review brief",
                    rejected.stderr,
                )
                self.assertEqual(json.loads(token_path.read_text())["token"], token)
                self.assertEqual(self.state(project, task_id)["status"], "needs_review")

                if artifact == "result.json":
                    fresh = self.baton(
                        project, "orchestrator", "brief", "--phase", "review",
                        task_id,
                    )
                    self.assertEqual(fresh.returncode, 1)
                    self.assertIn("changed after finalization", fresh.stderr)
                    self.assertEqual(
                        json.loads(token_path.read_text())["token"], token,
                    )
                    self.assertEqual(
                        self.state(project, task_id)["status"], "needs_review",
                    )
                else:
                    _fresh, fresh_token = self.review_brief_token(project, task_id)
                    self.baton(
                        project, "task", "accept", task_id,
                        "--brief", fresh_token, check=True,
                    )

    def test_review_and_accept_reject_structurally_invalid_report_drift(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        task_id = self.create_task(project, "semantic report drift", ["drift/**"])
        self.baton(project, "run", task_id, check=True)
        work = project / ".baton" / "work" / task_id
        report = work / "attempt-1.report.md"
        valid_report = report.read_text()
        token_path = work / "review-brief-token.json"

        report.write_text("# malformed before review\n")
        review = self.baton(
            project, "orchestrator", "brief", "--phase", "review", task_id,
        )
        self.assertEqual(review.returncode, 1)
        self.assertIn("report rejected", review.stderr)
        self.assertFalse(token_path.exists())

        report.write_text(valid_report)
        _brief, stale_token = self.review_brief_token(project, task_id)
        report.write_text("# malformed after review brief\n")
        rejected = self.baton(
            project, "task", "accept", task_id, "--brief", stale_token,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("report rejected", rejected.stderr)
        self.assertEqual(json.loads(token_path.read_text())["token"], stale_token)
        self.assertEqual(self.state(project, task_id)["status"], "needs_review")

        report.write_text(valid_report)
        _fresh, fresh_token = self.review_brief_token(project, task_id)
        self.baton(
            project, "task", "accept", task_id, "--brief", fresh_token,
            check=True,
        )

    def test_review_manifest_uses_launch_capsule_and_accepts_empty_diff(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        task_id = self.create_task(project, "empty evidence diff", ["empty/**"])
        self.baton(project, "run", task_id, check=True)
        work = project / ".baton" / "work" / task_id
        self.assertEqual((work / "attempt-1.diff").read_text(), "")
        (work / "attempt-1.briefs.json").unlink()
        brief, token = self.review_brief_token(project, task_id)
        self.assertIn("Diff stat: no changes", brief.stdout)
        self.assertIn("Phase briefs: none recorded", brief.stdout)

        spec = project / ".baton" / "tasks" / f"{task_id}.md"
        spec.write_text(spec.read_text().replace(
            f"Complete the empty evidence diff task.",
            "Complete the edited specification task.",
        ))
        self.baton(
            project, "task", "accept", task_id, "--brief", token, check=True,
        )

    def test_review_manifest_fresh_compile_accepts_without_stored_attempt_brief(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        task_id = self.create_task(project, "fresh review capsule", ["fresh/**"])
        self.baton(project, "run", task_id, check=True)
        work = project / ".baton" / "work" / task_id
        (work / "attempt-1.brief.md").unlink()

        brief, token = self.review_brief_token(project, task_id)
        self.assertTrue(brief.stdout.startswith("# Critical Context Capsule\n"))
        self.assertTrue((work / "review-brief-token.json").exists())
        self.baton(
            project, "task", "accept", task_id, "--brief", token, check=True,
        )
        self.assertEqual(self.state(project, task_id)["status"], "done")

    def test_review_brief_requires_regular_complete_evidence(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_review_hash_probe")
        digest_file = self.base / "digest.bin"
        digest_file.write_bytes(b"x" * (1024 * 1024 + 17))
        self.assertEqual(
            module["sha256_regular_file"](digest_file),
            hashlib.sha256(digest_file.read_bytes()).hexdigest(),
        )
        with self.assertRaises(OSError):
            module["sha256_regular_file"](self.base)
        digest_link = self.base / "digest-link"
        digest_link.symlink_to(digest_file)
        with self.assertRaises(OSError):
            module["sha256_regular_file"](digest_link)

        for missing in ("report.md", "result.json", "diff"):
            with self.subTest(missing=missing):
                project = self.make_project("missing-" + missing.replace(".", "-"))
                self.configure(project, self.write_worker(GOOD_WORKER))
                task_id = self.create_task(project, "missing " + missing, ["missing/**"])
                self.baton(project, "run", task_id, check=True)
                work = project / ".baton" / "work" / task_id
                (work / f"attempt-1.{missing}").unlink()
                rejected = self.baton(
                    project, "orchestrator", "brief", "--phase", "review", task_id,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertFalse((work / "review-brief-token.json").exists())

        project = self.make_project("symlink-report")
        self.configure(project, self.write_worker(GOOD_WORKER))
        task_id = self.create_task(project, "symlink report", ["symlink/**"])
        self.baton(project, "run", task_id, check=True)
        work = project / ".baton" / "work" / task_id
        report = work / "attempt-1.report.md"
        external = self.base / "external-report.md"
        external.write_text(report.read_text())
        report.unlink()
        report.symlink_to(external)
        rejected = self.baton(
            project, "orchestrator", "brief", "--phase", "review", task_id,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("symlink", rejected.stderr.lower())
        self.assertFalse((work / "review-brief-token.json").exists())

    def test_review_brief_survives_fresh_capsule_compile_failure(self):
        project = self.make_project()
        self.configure(project, self.write_worker(GOOD_WORKER))
        self.write_memory(project, [("M001", "W", "Launch-only fact", "Full body")])
        task_id = self.create_task(project, "stored capsule fallback", ["fallback/**"])
        spec = project / ".baton" / "tasks" / f"{task_id}.md"
        spec.write_text(spec.read_text().replace(
            "List the paths and facts the worker needs. Reference memory ids when useful.",
            "Memory: M001.",
        ))
        self.baton(project, "run", task_id, check=True)
        self.write_memory(project, [])

        brief, token = self.review_brief_token(project, task_id)
        self.assertIn("Launch-only fact", brief.stdout)
        self.assertEqual(brief.stdout.count("WARNING:"), 1)
        self.assertIn("referenced memory id M001 is missing", brief.stdout)
        self.assertLess(len(next(
            line for line in brief.stdout.splitlines() if line.startswith("WARNING:")
        )), 600)
        self.baton(
            project, "task", "accept", task_id, "--brief", token, check=True,
        )

        no_launch = self.make_project("no-launch-capsule")
        task_id = self.create_task(no_launch, "no launch compile", ["none/**"])
        runtime = no_launch / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        task = json.loads(state_path.read_text())
        task["status"] = "needs_review"
        state_path.write_text(json.dumps(task))
        work = runtime / "work" / task_id
        work.mkdir(parents=True)
        (work / "attempt-1.report.md").write_text("report\n")
        (work / "attempt-1.result.json").write_text(json.dumps({"changed_paths": []}))
        (work / "attempt-1.diff").write_text("")
        spec = runtime / "tasks" / f"{task_id}.md"
        spec.write_text(spec.read_text().replace(
            f"Complete the no launch compile task.",
            "Replace this line with one clear outcome.",
        ))
        failed = self.baton(
            no_launch, "orchestrator", "brief", "--phase", "review", task_id,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("template placeholder", failed.stderr)
        self.assertFalse((work / "review-brief-token.json").exists())

    def test_start_brief_never_asks_about_harness_memory_or_fresh_sessions(self):
        project = self.make_project()
        started = self.baton(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        ).stdout
        self.assertNotIn("Harness memory:", started)
        self.assertNotIn("fresh harness session", started)
        self.assertNotIn("use your existing harness memory", started)

    def test_compaction_reinjection_uses_the_same_route_validity_rule(self):
        project = self.make_project()
        runtime = project / ".baton"
        compact = subprocess.run(
            [runtime / "baton", "hook-event", "session-start"],
            cwd=project, input='{"source":"compact"}', text=True,
            capture_output=True, env=clean_test_environment(),
        )
        self.assertEqual(compact.returncode, 0)
        self.assertIn("Which model and reasoning level should Baton use", compact.stdout)
        self.assertNotIn("Harness memory:", compact.stdout)

        (runtime / "config.toml").write_text("".join(
            f'[tiers.{name}]\ncommand = "/usr/bin/true {{prompt_file}}"\n'
            for name in ("hard", "medium", "easy")
        ))
        compact = subprocess.run(
            [runtime / "baton", "hook-event", "session-start"],
            cwd=project, input='{"source":"compact"}', text=True,
            capture_output=True, env=clean_test_environment(),
        )
        self.assertEqual(compact.returncode, 0)
        self.assertNotIn("Which model and reasoning level should Baton use", compact.stdout)
        self.assertIn("change these settings at any time", compact.stdout)

    def test_close_brief_counts_worker_launches_across_retries_and_archives(self):
        project = self.make_project()
        runtime = project / ".baton"
        active = [
            {
                "id": "T900-hard", "title": "hard", "status": "done", "tier": "hard",
                "history": [
                    {"event": "launched", "attempt": 1},
                    {"event": "worker_exited", "attempt": 1},
                    {"event": "launched", "attempt": 2},
                    None,
                    {"event": 7},
                ],
            },
            {
                "id": "T901-easy", "title": "easy", "status": "queued", "tier": "easy",
                "history": [{"event": "launched"}] * 3,
            },
            {
                "id": "T902-custom", "title": "custom", "status": "done",
                "tier": "private-route", "history": [{"event": "launched"}],
            },
            {
                "id": "T905-malformed", "title": "malformed", "status": "done",
                "tier": "hard", "history": "launched",
            },
            {
                "id": "T906-unhashable", "title": "unhashable", "status": "done",
                "tier": [], "history": [{"event": "launched"}],
            },
        ]
        archived = [
            {
                "id": "T903-medium", "title": "medium", "status": "done",
                "tier": "medium", "history": [{"event": "launched"}],
            },
            {
                "id": "T904-default", "title": "default", "status": "done",
                "tier": "default", "history": [{"event": "launched"}],
            },
        ]
        expected = (
            "I used 9 workers for this Baton runtime so far: 2 for hard tasks, "
            "1 for a medium task, 3 for easy tasks, and 3 for other levels."
        )
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_worker_count_probe")
        self.assertEqual(
            module["worker_usage_sentence"]([]),
            "I used 0 workers for this Baton runtime so far: 0 for hard tasks, "
            "0 for medium tasks, and 0 for easy tasks.",
        )
        self.assertEqual(
            module["worker_usage_sentence"]([{
                "tier": "hard", "history": [{"event": "launched"}],
            }]),
            "I used 1 worker for this Baton runtime so far: 1 for a hard task, "
            "0 for medium tasks, and 0 for easy tasks.",
        )
        self.assertEqual(
            module["worker_usage_sentence"]([{
                "tier": [], "history": [{"event": "launched"}],
            }]),
            "I used 1 worker for this Baton runtime so far: 0 for hard tasks, "
            "0 for medium tasks, 0 for easy tasks, and 1 for other level.",
        )
        self.assertEqual(module["worker_usage_sentence"](active + archived), expected)
        self.assertEqual(
            module["worker_usage_sentence"]([], for_request=True),
            "I used 0 workers for this request: 0 on hard, 0 on medium, and 0 on easy.",
        )
        self.assertEqual(
            module["worker_usage_sentence"]([{
                "tier": [], "history": [{"event": "launched"}],
            }], for_request=True),
            "I used 1 worker for this request: 0 on hard, 0 on medium, "
            "0 on easy, and 1 on other levels.",
        )
        emitted = []
        module["orchestrator_close_brief"].__globals__["say"] = emitted.append
        module["orchestrator_close_brief"](
            runtime, active, archived, "next goal", [], [], False,
        )
        self.assertIn(expected, emitted)

        for task in active:
            (runtime / "tasks" / (task["id"] + ".json")).write_text(json.dumps(task))
        for task in archived:
            (runtime / "archive" / (task["id"] + ".json")).write_text(json.dumps(task))
        closed = self.baton(
            project, "orchestrator", "brief", "--phase", "close",
            "--goal", "next goal", check=True,
        )
        self.assertIn(expected, closed.stdout)

    def test_orchestrator_phase_handoff_and_next_action_capsules(self):
        project = self.make_project()
        self.configure(project, self.write_worker(GOOD_WORKER))
        task_id = self.create_task(project, "phase output", ["phase/**"])
        started = self.baton(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        )
        self.assertIn("Worker routing:", started.stdout)
        self.assertIn("Which model and reasoning level should Baton use", started.stdout)
        self.assertNotIn("Harness memory:", started.stdout)
        plan = self.baton(
            project, "orchestrator", "brief", "--phase", "plan", check=True,
        )
        self.assertIn("Task-spec quality checklist:", plan.stdout)
        self.assertIn(f"{task_id} [queued]", plan.stdout)
        run_brief = self.baton(
            project, "orchestrator", "brief", "--phase", "run", check=True,
        )
        self.assertIn(f"Would run: {task_id}", run_brief.stdout)

        status = self.baton(project, "status", check=True)
        self.assertIn("\nNext actions:\n", status.stdout)
        run = self.baton(project, "run", task_id, check=True)
        self.assertIn("\nNext actions:\n", run.stdout)
        self.assertIn("attempt-1.report.md", run.stdout.rsplit("Next actions:", 1)[1])
        shown = self.baton(project, "task", "show", task_id, check=True)
        self.assertIn("\nNext actions:\n", shown.stdout)

        closed = self.baton(
            project, "orchestrator", "brief", "--phase", "close",
            "--goal", "Finish\n\x1b[31mhandoff\x1b[0m context",
            "--avoid", "Do not\n\x1b[33minherit\x1b[0m old goals",
            "--avoid", "Keep locks unchanged",
            "--avoid", "Preserve same-second dedupe",
            "--avoid", "Avoid placeholder context",
            "--avoid", "\x1b]0;title\x07" + "x" * 201,
            "--note", "Trusted\noperator context",
            "--note", "Trusted operator context",
            "--note", "\x1b[31m" + "n" * 161,
            check=True,
        )
        handoff = project / ".baton" / "orchestrator-handoff.md"
        self.assertTrue(handoff.exists())
        handoff_text = handoff.read_text()
        self.assertIn("consumed_at: (not yet)", handoff_text)
        self.assertIn("goal: Finish handoff context\n", handoff_text)
        self.assertIn(
            "goal: Finish handoff context\n"
            "warning: uncommitted Git-visible changes at close\n"
            "done:\n",
            handoff_text,
        )
        notes_block = handoff_text.split("notes:\n", 1)[1].split("avoid:\n", 1)[0]
        expected_notes = ["Trusted operator context", "n" * 159 + "…"]
        self.assertEqual(
            notes_block, "".join(f"- {note}\n" for note in expected_notes),
        )
        avoid_block = handoff_text.split("avoid:\n", 1)[1]
        expected_avoids = [
            "Do not inherit old goals", "Keep locks unchanged",
            "Preserve same-second dedupe", "Avoid placeholder context",
            "x" * 199 + "…",
        ]
        self.assertEqual(
            avoid_block, "".join(f"- {note}\n" for note in expected_avoids),
        )
        self.assertNotIn("(fill in)", avoid_block)
        self.assertIn(
            "Start a fresh coding-agent session and tell it to read "
            ".baton/orchestrator.md.",
            closed.stdout,
        )
        self.assertNotIn("orchestrator brief --phase start", closed.stdout)
        started = self.baton(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        )
        self.assertIn("Current handoff:", started.stdout)
        self.assertIn("goal: Finish handoff context", started.stdout)
        for note in expected_avoids:
            self.assertIn("- " + note, started.stdout)
        for note in expected_notes:
            self.assertIn("- " + note, started.stdout)
        self.assertIn("warning: uncommitted Git-visible changes at close", started.stdout)
        self.assertNotIn("consumed_at: (not yet)", handoff.read_text())

    def test_close_brief_validates_required_and_phase_scoped_context(self):
        project = self.make_project()
        remediation = (
            "error: `--phase close` requires a nonblank goal; "
            "add `--goal TEXT`\n"
        )
        for extra in ([], ["--goal", " \n\t "]):
            with self.subTest(extra=extra):
                rejected = self.baton(
                    project, "orchestrator", "brief", "--phase", "close", *extra,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(rejected.stderr, remediation)

        too_many_args = [
            item
            for number in range(6)
            for item in ("--avoid", f"note {number}")
        ]
        too_many = self.baton(
            project, "orchestrator", "brief", "--phase", "close",
            "--goal", "Continue the work", *too_many_args,
        )
        self.assertNotEqual(too_many.returncode, 0)
        self.assertEqual(
            too_many.stderr,
            "error: at most 5 `--avoid` notes are allowed; consolidate them\n",
        )

        too_many_notes = [
            item
            for number in range(4)
            for item in ("--note", f"context {number}")
        ]
        rejected_notes = self.baton(
            project, "orchestrator", "brief", "--phase", "close",
            "--goal", "Continue the work", *too_many_notes,
        )
        self.assertNotEqual(rejected_notes.returncode, 0)
        self.assertEqual(
            rejected_notes.stderr,
            "error: at most 3 `--note` values are allowed; consolidate them\n",
        )

        for phase in ("start", "plan", "run", "review"):
            for flag, value in (
                    ("--goal", "next"), ("--avoid", "risk"), ("--note", "fact")):
                with self.subTest(phase=phase, flag=flag):
                    rejected = self.baton(
                        project, "orchestrator", "brief", "--phase", phase,
                        flag, value,
                    )
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertEqual(
                        rejected.stderr,
                        f"error: `{flag}` is valid only for the close phase\n",
                    )

        closed = self.baton(
            project, "orchestrator", "brief", "--phase", "close",
            "--goal", "g" * 201, "--note", " \n\t ", check=True,
        )
        handoff = project / ".baton" / "orchestrator-handoff.md"
        goal_line = next(
            line for line in handoff.read_text().splitlines() if line.startswith("goal: ")
        )
        self.assertEqual(goal_line, "goal: " + "g" * 199 + "…")
        self.assertIn("avoid:\n- (fill in)\n", handoff.read_text())
        self.assertNotIn("notes:", handoff.read_text())
        self.assertIn(goal_line, closed.stdout)

    def test_handoff_start_and_close_lock_complete_update(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_handoff_lock_probe")
        events = []
        handoff = (
            "# Orchestrator handoff\n"
            "generated_at: 2026-01-01T00:00:00Z\n"
            "consumed_at: (not yet)\n"
            "goal: test\n"
            "done:\n- (none)\n"
        )

        class LockProbe:
            def __init__(self, path):
                self.name = Path(path).name

            def __enter__(self):
                events.append(("enter", self.name))

            def __exit__(self, *_args):
                events.append(("exit", self.name))

        globals_ = module["orchestrator_start_brief"].__globals__
        globals_["file_lock"] = LockProbe
        globals_["read_handoff"] = lambda _baton_dir: events.append("read") or handoff
        globals_["read_handoff_cursor"] = (
            lambda _baton_dir: events.append("cursor-read") or None
        )
        globals_["atomic_write"] = lambda *_args: events.append("write")
        globals_["atomic_json"] = lambda *_args: events.append("cursor-write")
        globals_["load_archived_tasks"] = lambda _baton_dir: events.append("archive") or []
        globals_["task_lock"] = lambda *_args: self.fail(
            "task lock nested in handoff lock"
        )
        globals_["now"] = lambda: "2026-01-01T00:00:01Z"
        globals_["say"] = lambda *_args: None
        globals_["load_config"] = lambda _baton_dir: {}
        globals_["project_root"] = lambda _baton_dir: "/project"
        globals_["worker_routing_lines"] = lambda _config, _root: ["Worker routing:"]

        module["orchestrator_start_brief"](
            "/baton", [], consume_handoff=True,
        )
        self.assertEqual(events, [
            ("enter", "orchestrator-handoff.lock"), "read", "cursor-read", "write",
            ("exit", "orchestrator-handoff.lock"),
        ])
        events.clear()
        archived = globals_["load_archived_tasks"]("/baton")
        module["orchestrator_close_brief"](
            "/baton", [], archived, "next goal", [], [], False,
        )
        self.assertEqual(events, [
            "archive", ("enter", "orchestrator-handoff.lock"), "read",
            "cursor-read", "cursor-write", "write",
            ("exit", "orchestrator-handoff.lock"),
        ])

    def test_handoff_done_outcomes_are_matched_flattened_and_line_bounded(self):
        project = self.make_project()
        runtime = project / ".baton"
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_outcome_probe")
        accepted_at = "2026-01-01T00:00:00Z"
        tasks = [
            {
                "id": "T900-long-outcome", "title": "t" * 400, "status": "done",
                "history": [
                    {"event": "accepted", "at": accepted_at,
                     "note": " Shipped\n" + "o" * 140},
                    {"event": "archived", "at": accepted_at,
                     "note": "must not replace the accepted outcome"},
                ],
            },
            {
                "id": "T901-blank-outcome", "title": "blank note", "status": "done",
                "history": [
                    {"event": "accepted", "at": accepted_at, "note": " \n\t "},
                    {"event": "worker_exited", "at": accepted_at,
                     "note": "must not become an outcome"},
                ],
            },
        ]
        module["orchestrator_close_brief"](
            runtime, tasks, [], "next goal", [], [], False,
        )
        content = (runtime / "orchestrator-handoff.md").read_text()
        done_lines = content.split("done:\n", 1)[1].split(
            "decisions:\n", 1,
        )[0].splitlines()
        long_line = next(line[2:] for line in done_lines if "T900-long-outcome" in line)
        blank_line = next(line[2:] for line in done_lines if "T901-blank-outcome" in line)
        self.assertLessEqual(len(long_line), 240)
        self.assertTrue(long_line.startswith("T900-long-outcome: "))
        self.assertTrue(long_line.endswith(" — outcome: Shipped " + "o" * 111 + "…"))
        self.assertNotIn(" — outcome:", blank_line)
        self.assertNotIn("must not", content)

    def test_close_working_tree_warning_is_clean_dirty_or_unavailable(self):
        clean = self.make_project("clean-warning")
        self.git(clean, "add", ".gitignore")
        self.git(clean, "commit", "-qm", "ignore runtime")
        self.baton(
            clean, "orchestrator", "brief", "--phase", "close",
            "--goal", "clean close", check=True,
        )
        clean_handoff = (
            clean / ".baton" / "orchestrator-handoff.md"
        ).read_text()
        self.assertNotIn("warning:", clean_handoff)
        self.assertIn("goal: clean close\ndone:\n", clean_handoff)

        dirty = self.make_project("dirty-warning")
        leaked_path = "private-path-must-not-leak.txt"
        (dirty / leaked_path).write_text("dirty\n")
        self.baton(
            dirty, "orchestrator", "brief", "--phase", "close",
            "--goal", "dirty close", check=True,
        )
        dirty_handoff = (
            dirty / ".baton" / "orchestrator-handoff.md"
        ).read_text()
        self.assertIn(
            "goal: dirty close\n"
            "warning: uncommitted Git-visible changes at close\n"
            "done:\n",
            dirty_handoff,
        )
        self.assertNotIn(leaked_path, dirty_handoff)

        unavailable = self.make_project("unavailable-warning")
        unavailable_result = self.baton(
            unavailable, "orchestrator", "brief", "--phase", "close",
            "--goal", "unavailable close",
            env={"GIT_DIR": unavailable / "missing-git-dir"}, check=True,
        )
        unavailable_handoff = (
            unavailable / ".baton" / "orchestrator-handoff.md"
        ).read_text()
        self.assertIn(
            "goal: unavailable close\n"
            "warning: working-tree check unavailable at close\n"
            "done:\n",
            unavailable_handoff,
        )
        self.assertNotIn("fatal:", unavailable_result.stdout + unavailable_handoff)

    def test_handoff_total_budget_degrades_whole_sections_in_order(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_budget_probe")
        done = [
            (f"T{number:03d}-done", "t" * 240, "o" * 120)
            for number in range(12)
        ]
        decisions = [f"T{number:03d}-decision: " + "d" * 240 for number in range(12)]
        next_ids = [f"next-{number}-" + "n" * 240 for number in range(12)]
        unresolved = [f"unresolved-{number}-" + "u" * 240 for number in range(12)]
        notes = [character * 160 for character in "abc"]
        avoid = [str(number) * 200 for number in range(5)]
        content = module["render_handoff"](
            "2026-01-01T00:00:00Z", "g" * 200, True, done, decisions,
            next_ids, unresolved, notes, avoid,
        )

        def section(name, next_name):
            return [
                line[2:] for line in content.split(name + ":\n", 1)[1].split(
                    next_name + ":\n", 1,
                )[0].splitlines()
            ]

        self.assertLessEqual(len(content), 3989)
        self.assertNotIn("outcome:", content)
        self.assertEqual(section("done", "decisions"), ["+12 more"])
        self.assertEqual(section("decisions", "next"), ["+12 more"])
        for name, next_name in (("next", "unresolved"), ("unresolved", "notes")):
            lines = section(name, next_name)
            marker = next(line for line in lines if line.startswith("+"))
            self.assertEqual(len(lines) - 1 + int(marker[1:].split()[0]), 12)
        for note in notes:
            self.assertIn("- " + note, content)
        self.assertTrue(content.endswith("- " + avoid[-1] + "\n"))

    def test_handoff_consumption_reserves_and_compacts_every_legacy_boundary(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_consumption_budget")
        globals_ = module["render_handoff"].__globals__
        generated_at = "2026-01-01T00:00:00Z"
        consumed_at = "2026-01-01T00:00:01Z"
        expected_headings = [
            "done:", "decisions:", "next:", "unresolved:", "notes:", "avoid:",
        ]

        def legacy_render(size, task_id):
            # Recreate the former renderer's 4,000-character close budget.
            reserved_limit = globals_["HANDOFF_UNCONSUMED_MAX_CHARS"]
            globals_["HANDOFF_UNCONSUMED_MAX_CHARS"] = 4000
            try:
                arguments = (
                    generated_at, "continue safely", False,
                    [(task_id, "accepted result", "verified")], [], [], [],
                )
                base = module["render_handoff"](
                    *arguments, ["x"], ["avoid regression"],
                )
                content = module["render_handoff"](
                    *arguments, ["x" * (1 + size - len(base))],
                    ["avoid regression"],
                )
            finally:
                globals_["HANDOFF_UNCONSUMED_MAX_CHARS"] = reserved_limit
            self.assertEqual(len(content), size)
            return content

        versions = {3989: None, 3990: 1, 3999: 2, 4000: 3}
        for size, version in versions.items():
            with self.subTest(size=size, version=version):
                project = self.make_project("handoff-consumption-{}".format(size))
                runtime = project / ".baton"
                task_id = "T{}-budget-identity".format(size)
                content = legacy_render(size, task_id)
                if size == 3990:
                    self.assertEqual(
                        len(content.replace(
                            "consumed_at: (not yet)",
                            "consumed_at: " + consumed_at, 1,
                        )),
                        4001,
                    )
                handoff_path = runtime / "orchestrator-handoff.md"
                cursor_path = runtime / "orchestrator-handoff-cursor.json"
                handoff_path.write_text(content)
                if version == 1:
                    cursor_path.write_text(json.dumps({
                        "version": 1, "accepted_at": generated_at,
                        "seen_ids": [task_id],
                    }))
                elif version == 2:
                    cursor_path.write_text(json.dumps({
                        "version": 2, "accepted_at": generated_at,
                        "seen_ids": [task_id], "handoff": content,
                    }))
                elif version == 3:
                    cursor_path.write_text(json.dumps({
                        "version": 3, "reported_ids": [task_id],
                        "handoff": content,
                    }))
                task = {
                    "id": task_id, "title": "accepted result", "status": "done",
                    "history": [{"event": "accepted", "at": generated_at}],
                }
                clock = {"now": consumed_at}
                globals_["now"] = lambda: clock["now"]
                globals_["say"] = lambda *_args: None
                expected_consumed = module["consume_handoff_content"](
                    content, consumed_at,
                )
                self.assertEqual(
                    module["consume_handoff_content"](content, consumed_at),
                    expected_consumed,
                )

                module["orchestrator_start_brief"](runtime, [task])
                consumed = handoff_path.read_text()
                self.assertEqual(consumed, expected_consumed)
                self.assertEqual(len(consumed), 4000)
                self.assertEqual(
                    module["handoff_field"](consumed, "generated_at"), generated_at,
                )
                self.assertEqual(
                    module["handoff_field"](consumed, "consumed_at"), consumed_at,
                )
                self.assertEqual(
                    [line for line in consumed.splitlines() if line in expected_headings],
                    expected_headings,
                )
                self.assertIn(task_id, consumed)
                if version in (2, 3):
                    self.assertEqual(
                        json.loads(cursor_path.read_text())["handoff"], consumed,
                    )
                if version is not None:
                    module["read_handoff_cursor"](runtime)

                clock["now"] = "2026-01-01T00:00:02Z"
                before_second = consumed
                module["orchestrator_start_brief"](runtime, [task])
                second = handoff_path.read_text()
                self.assertEqual(
                    second,
                    before_second.replace(consumed_at, clock["now"], 1),
                )

                clock["now"] = "2026-01-01T00:00:03Z"
                module["orchestrator_close_brief"](
                    runtime, [task], [], "continue after migration", [], [], False,
                )
                closed = handoff_path.read_text()
                migrated = module["read_handoff_cursor"](runtime)
                self.assertLessEqual(len(closed), 3989)
                self.assertEqual(migrated["version"], 3)
                self.assertIn(task_id, migrated["reported_ids"])
                self.assertEqual(migrated["handoff"], closed)

                clock["now"] = "2026-01-01T00:00:04Z"
                module["orchestrator_start_brief"](runtime, [task])
                self.assertLessEqual(len(handoff_path.read_text()), 4000)
                self.assertLessEqual(
                    len(module["read_handoff_cursor"](runtime)["handoff"]), 4000,
                )

    def test_small_handoff_consumption_is_an_exact_metadata_replacement(self):
        project = self.make_project("small-handoff-consumption")
        runtime = project / ".baton"
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_small_consumption")
        content = module["render_handoff"](
            "2026-01-01T00:00:00Z", "preserve ordinary bytes", False,
            [("T900-small", "small task", "verified")], ["decision"],
            ["T901-next"], ["T902-unresolved"], ["note"], ["avoid"],
        )
        cursor_path = runtime / "orchestrator-handoff-cursor.json"
        handoff_path = runtime / "orchestrator-handoff.md"
        handoff_path.write_text(content)
        cursor_path.write_text(json.dumps({
            "version": 3, "reported_ids": [], "handoff": content,
        }))
        globals_ = module["orchestrator_start_brief"].__globals__
        globals_["now"] = lambda: "2026-01-01T00:00:01Z"
        globals_["say"] = lambda *_args: None

        module["orchestrator_start_brief"](runtime, [])

        expected = content.replace(
            "consumed_at: (not yet)",
            "consumed_at: 2026-01-01T00:00:01Z", 1,
        )
        self.assertEqual(handoff_path.read_bytes(), expected.encode())
        self.assertEqual(json.loads(cursor_path.read_text())["handoff"], expected)
        self.assertEqual(
            module["handoff_done_ids"](expected), {"T900-small"},
        )

    def test_handoff_consumption_malformed_or_uncompactable_is_no_mutation(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_consumption_failure")
        globals_ = module["orchestrator_start_brief"].__globals__
        base = module["render_handoff"](
            "2026-01-01T00:00:00Z", "valid goal", False,
            [], [], [], [], [], ["avoid"],
        )
        malformed = (
            base.replace("done:\n", "", 1),
            base.replace(
                "consumed_at: (not yet)\n",
                "consumed_at: (not yet)\nconsumed_at: (not yet)\n", 1,
            ),
        )
        for index, content in enumerate(malformed):
            with self.subTest(kind="malformed", index=index):
                project = self.make_project("malformed-consumption-{}".format(index))
                runtime = project / ".baton"
                handoff_path = runtime / "orchestrator-handoff.md"
                cursor_path = runtime / "orchestrator-handoff-cursor.json"
                handoff_path.write_text(content)
                cursor_path.write_text(json.dumps({
                    "version": 3, "reported_ids": [], "handoff": content,
                }, indent=2) + "\n")
                before = handoff_path.read_bytes(), cursor_path.read_bytes()
                globals_["now"] = lambda: "2026-01-01T00:00:01Z"
                globals_["say"] = lambda *_args: None

                with self.assertRaises(SystemExit):
                    module["orchestrator_start_brief"](runtime, [])
                self.assertEqual(
                    (handoff_path.read_bytes(), cursor_path.read_bytes()), before,
                )

        project = self.make_project("invalid-consumption-timestamp")
        runtime = project / ".baton"
        handoff_path = runtime / "orchestrator-handoff.md"
        cursor_path = runtime / "orchestrator-handoff-cursor.json"
        handoff_path.write_text(base)
        cursor_path.write_text(json.dumps({
            "version": 3, "reported_ids": [], "handoff": base,
        }, indent=2) + "\n")
        before = handoff_path.read_bytes(), cursor_path.read_bytes()
        globals_["now"] = lambda: "2026-02-30T00:00:01Z"
        globals_["say"] = lambda *_args: None
        with self.assertRaises(SystemExit):
            module["orchestrator_start_brief"](runtime, [])
        self.assertEqual((handoff_path.read_bytes(), cursor_path.read_bytes()), before)

        task_prefix = "T900-"
        template = (
            "# Orchestrator handoff\n"
            "generated_at: 2026-01-01T00:00:00Z\n"
            "consumed_at: (not yet)\n"
            "goal: g\n"
            "done:\n- {task_id}:\n"
            "decisions:\n- (none)\n"
            "next:\n- (none)\n"
            "unresolved:\n- (none)\n"
            "avoid:\n- (none)\n"
        )
        short = template.format(task_id=task_prefix + "x")
        uncompactable = template.format(
            task_id=task_prefix + "x" * (1 + 4000 - len(short)),
        )
        self.assertEqual(len(uncompactable), 4000)
        self.assertIsNotNone(module["handoff_structure"](uncompactable))
        project = self.make_project("uncompactable-consumption")
        runtime = project / ".baton"
        handoff_path = runtime / "orchestrator-handoff.md"
        cursor_path = runtime / "orchestrator-handoff-cursor.json"
        handoff_path.write_text(uncompactable)
        cursor_path.write_text(json.dumps({
            "version": 3, "reported_ids": [], "handoff": uncompactable,
        }, indent=2) + "\n")
        before = handoff_path.read_bytes(), cursor_path.read_bytes()
        globals_["now"] = lambda: "2026-01-01T00:00:01Z"
        with self.assertRaises(SystemExit):
            module["orchestrator_start_brief"](runtime, [])
        self.assertEqual((handoff_path.read_bytes(), cursor_path.read_bytes()), before)

    def test_close_to_start_round_trip_includes_outcome_warning_and_notes(self):
        project = self.make_project()
        runtime = project / ".baton"
        task_id = self.create_task(project, "round trip outcome", ["round/**"])
        state_path = runtime / "tasks" / f"{task_id}.json"
        task = json.loads(state_path.read_text())
        task["status"] = "done"
        task["history"].append({
            "event": "accepted", "at": "2026-01-01T00:00:00Z",
            "note": "Verified round-trip result",
        })
        state_path.write_text(json.dumps(task))
        self.baton(
            project, "orchestrator", "brief", "--phase", "close",
            "--goal", "continue round trip", "--note", "User prefers the safe path",
            check=True,
        )
        started = self.baton(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        )
        for expected in (
                f"{task_id}: round trip outcome — outcome: Verified round-trip result",
                "warning: uncommitted Git-visible changes at close",
                "notes:\n- User prefers the safe path",
                "avoid:\n- (fill in)"):
            self.assertIn(expected, started.stdout)

    def test_start_brief_and_hooks_do_not_write_beyond_handoff_consumption(self):
        project = self.make_project()
        runtime = project / ".baton"
        handoff = runtime / "orchestrator-handoff.md"
        handoff.write_text(
            "# Orchestrator handoff\n"
            "generated_at: 2026-01-01T00:00:00Z\n"
            "consumed_at: (not yet)\n"
            "goal: verify read-only onboarding\n"
            "done:\n- (none)\n"
        )
        (runtime / ".locks" / "orchestrator-handoff.lock").touch()

        def snapshot():
            return {
                path.relative_to(runtime).as_posix(): path.read_bytes()
                for path in runtime.rglob("*") if path.is_file()
            }

        before = snapshot()
        started = self.baton(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        )
        self.assertIn("Worker routing:", started.stdout)
        after_start = snapshot()
        changed = {
            path for path in before if before[path] != after_start.get(path)
        } | (set(after_start) - set(before))
        self.assertEqual(changed, {"orchestrator-handoff.md"})
        self.assertNotIn("consumed_at: (not yet)", handoff.read_text())

        hook_command = [
            runtime / "baton", "hook-event", "session-start",
        ]
        for hook_input in ('{"source":"startup"}', '{"source":"compact"}'):
            hooked = subprocess.run(
                hook_command, cwd=project, input=hook_input, text=True,
                capture_output=True, env=clean_test_environment(),
            )
            self.assertEqual(hooked.returncode, 0)
            self.assertEqual(hooked.stderr, "")
        self.assertEqual(snapshot(), after_start)

    def test_handoff_same_second_acceptance_is_emitted_once(self):
        project = self.make_project()
        task_id = self.create_task(project, "same second", ["same-second/**"])
        runtime = project / ".baton"
        boundary = "2026-01-01T00:00:00Z"
        handoff_path = runtime / "orchestrator-handoff.md"
        handoff_path.write_text(
            "# Orchestrator handoff\n"
            f"generated_at: {boundary}\n"
            "consumed_at: (not yet)\n"
            "goal: test\n"
            "done:\n- (none)\n"
        )
        task = self.state(project, task_id)
        task["history"].append({
            "event": "accepted", "at": boundary,
            "note": "Reviewer-confirmed result",
        })
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_boundary_probe")
        globals_ = module["orchestrator_close_brief"].__globals__
        globals_["now"] = lambda: boundary
        globals_["say"] = lambda *_args: None

        module["orchestrator_close_brief"](
            runtime, [task], [], "next goal", [], [], False,
        )
        first = handoff_path.read_text().split("done:\n", 1)[1].split(
            "decisions:\n", 1,
        )[0]
        module["orchestrator_close_brief"](
            runtime, [task], [], "next goal", [], [], False,
        )
        second = handoff_path.read_text().split("done:\n", 1)[1].split(
            "decisions:\n", 1,
        )[0]
        self.assertEqual(first.count(task_id) + second.count(task_id), 1)
        self.assertIn(task_id, first)
        self.assertIn(" — outcome: Reviewer-confirmed result", first)
        self.assertNotIn(task_id, second)

    def test_handoff_acceptance_tracking_survives_clock_rollback(self):
        project = self.make_project("handoff-clock-rollback")
        runtime = project / ".baton"
        handoff = runtime / "orchestrator-handoff.md"
        cursor = runtime / "orchestrator-handoff-cursor.json"
        handoff.unlink(missing_ok=True)
        cursor.unlink(missing_ok=True)
        old = {
            "id": "T901-old", "title": "accepted at the first boundary",
            "status": "done", "history": [{
                "event": "accepted", "at": "2026-01-01T00:00:10Z",
            }],
        }
        rollback = {
            "id": "T902-rollback", "title": "accepted after clock rollback",
            "status": "done", "history": [{
                "event": "accepted", "at": "2026-01-01T00:00:05Z",
            }],
        }
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_rollback_probe")
        globals_ = module["orchestrator_close_brief"].__globals__
        clock = iter((
            "2026-01-01T00:00:10Z",
            "2026-01-01T00:00:06Z",
            "2026-01-01T00:00:07Z",
        ))
        globals_["now"] = lambda: next(clock)
        globals_["say"] = lambda *_args: None

        counts = []
        for tasks in ([old], [old, rollback], [old, rollback]):
            module["orchestrator_close_brief"](
                runtime, tasks, [], "next goal", [], [], False,
            )
            done = handoff.read_text().split("done:\n", 1)[1].split(
                "decisions:\n", 1,
            )[0]
            counts.append((done.count(old["id"]), done.count(rollback["id"])))

        self.assertEqual(counts, [(1, 0), (0, 1), (0, 0)])
        state = json.loads(cursor.read_text())
        self.assertEqual(state["version"], 3)
        self.assertEqual(state["reported_ids"], [old["id"], rollback["id"]])

    def test_handoff_close_snapshot_serializes_with_acceptance(self):
        project = self.make_project("handoff-close-accept-race")
        self.configure(project, self.write_worker(GOOD_WORKER))
        config = project / ".baton" / "config.toml"
        config.write_text(
            config.read_text() + "\n[gates]\naccept_requires_brief = false\n"
        )
        task_id = self.create_task(project, "close accept race", ["race/**"])
        self.baton(project, "run", task_id, check=True)
        runtime = project / ".baton"
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_close_accept_probe")
        globals_ = module["cmd_orchestrator_brief"].__globals__
        globals_["require_baton_dir"] = lambda: str(runtime)
        globals_["say"] = lambda *_args: None
        original_load_archived = globals_["load_archived_tasks"]
        original_file_lock = globals_["file_lock"]
        original_atomic_write = globals_["atomic_write"]
        snapshot_taken = threading.Event()
        release_close = threading.Event()
        accept_lock_attempted = threading.Event()
        close_published = threading.Event()
        errors = {}

        def paused_load_archived(baton_dir, validate_history=True):
            if threading.current_thread().name == "close-probe":
                snapshot_taken.set()
                if not release_close.wait(5):
                    raise RuntimeError("close synchronization timed out")
            return original_load_archived(baton_dir, validate_history)

        @contextmanager
        def observed_file_lock(path):
            if (
                    threading.current_thread().name == "accept-probe"
                    and path == globals_["lock_path"](str(runtime), "scheduler")):
                accept_lock_attempted.set()
            with original_file_lock(path):
                yield

        def observed_atomic_write(path, content, mode=0o644):
            original_atomic_write(path, content, mode)
            if (
                    threading.current_thread().name == "close-probe"
                    and path == globals_["orchestrator_handoff_path"](str(runtime))):
                close_published.set()

        later_times = iter((
            "2026-01-01T00:00:04Z", "2026-01-01T00:00:05Z",
        ))

        def deterministic_now():
            thread = threading.current_thread().name
            if thread == "close-probe":
                return "2026-01-01T00:00:02Z"
            if thread == "accept-probe":
                return (
                    "2026-01-01T00:00:03Z" if close_published.is_set()
                    else "2026-01-01T00:00:01Z"
                )
            return next(later_times)

        globals_["load_archived_tasks"] = paused_load_archived
        globals_["file_lock"] = observed_file_lock
        globals_["atomic_write"] = observed_atomic_write
        globals_["now"] = deterministic_now
        close_args = SimpleNamespace(
            phase="close", include_log_tail=False, goal="continue",
            avoid=[], note=[], id=None,
        )

        def close():
            try:
                module["cmd_orchestrator_brief"](close_args)
            except BaseException as error:
                errors["close"] = error

        def accept():
            try:
                module["cmd_task_accept"](SimpleNamespace(
                    id=task_id, brief=None, note="accepted during close",
                ))
            except BaseException as error:
                errors["accept"] = error

        close_thread = threading.Thread(target=close, name="close-probe")
        accept_thread = threading.Thread(target=accept, name="accept-probe")
        close_thread.start()
        self.assertTrue(snapshot_taken.wait(5))
        accept_thread.start()
        self.assertTrue(accept_lock_attempted.wait(5))
        release_close.set()
        close_thread.join(5)
        accept_thread.join(5)
        self.assertFalse(close_thread.is_alive())
        self.assertFalse(accept_thread.is_alive())
        self.assertEqual(errors, {})

        handoff = runtime / "orchestrator-handoff.md"

        def done_count():
            return handoff.read_text().split("done:\n", 1)[1].split(
                "decisions:\n", 1,
            )[0].count(task_id)

        counts = [done_count()]
        globals_["load_archived_tasks"] = original_load_archived
        for _close in range(2):
            module["cmd_orchestrator_brief"](close_args)
            counts.append(done_count())

        task = self.state(project, task_id)
        accepted_at = next(
            entry["at"] for entry in task["history"]
            if entry.get("event") == "accepted"
        )
        first_generated_at = "2026-01-01T00:00:02Z"
        self.assertLess(first_generated_at, accepted_at)
        self.assertEqual(counts, [0, 1, 0])

    def test_handoff_close_recovers_interrupted_presentation_publication(self):
        project = self.make_project("handoff-interrupted-publication")
        self.configure(project, self.write_worker(GOOD_WORKER))
        config = project / ".baton" / "config.toml"
        config.write_text(
            config.read_text() + "\n[gates]\naccept_requires_brief = false\n"
        )
        first_id = self.create_task(project, "published before failure", ["first/**"])
        second_id = self.create_task(project, "accepted after failure", ["second/**"])
        self.baton(project, "run", first_id, check=True)
        self.baton(project, "run", second_id, check=True)
        runtime = project / ".baton"
        handoff = runtime / "orchestrator-handoff.md"
        handoff.write_text(
            "# Orchestrator handoff\n"
            "generated_at: 2026-01-01T00:00:00Z\n"
            "consumed_at: (not yet)\n"
            "goal: previous\n"
            "done:\n- (none)\n"
        )
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_interrupted_publication_probe",
        )
        globals_ = module["orchestrator_close_brief"].__globals__
        globals_["require_baton_dir"] = lambda: str(runtime)
        globals_["say"] = lambda *_args: None
        clock = {"now": "2026-01-01T00:00:01Z"}
        globals_["now"] = lambda: clock["now"]

        module["cmd_task_accept"](SimpleNamespace(
            id=first_id, brief=None, note="first accepted result",
        ))
        clock["now"] = "2026-01-01T00:00:02Z"
        original_atomic_write = globals_["atomic_write"]
        publication_attempted = threading.Event()
        fail_presentation = {"armed": True}

        def interrupted_atomic_write(path, content, mode=0o644):
            if (
                    fail_presentation["armed"]
                    and path == globals_["orchestrator_handoff_path"](str(runtime))):
                fail_presentation["armed"] = False
                publication_attempted.set()
                raise OSError("injected handoff presentation failure")
            original_atomic_write(path, content, mode)

        globals_["atomic_write"] = interrupted_atomic_write
        with self.assertRaisesRegex(OSError, "injected handoff presentation failure"):
            module["orchestrator_close_brief"](
                runtime, [self.state(project, first_id), self.state(project, second_id)],
                [], "failed close", [], [], False,
            )
        self.assertTrue(publication_attempted.is_set())
        state_path = runtime / "orchestrator-handoff-cursor.json"
        state_after_failure = json.loads(state_path.read_text())
        self.assertEqual(state_after_failure["version"], 3)
        self.assertEqual(state_after_failure["reported_ids"], [first_id])
        self.assertIn(first_id, state_after_failure["handoff"])
        self.assertIn("goal: previous\n", handoff.read_text())

        clock["now"] = "2026-01-01T00:00:03Z"
        module["cmd_task_accept"](SimpleNamespace(
            id=second_id, brief=None, note="second accepted result",
        ))
        clock["now"] = "2026-01-01T00:00:04Z"
        tasks = [self.state(project, first_id), self.state(project, second_id)]
        module["orchestrator_close_brief"](
            runtime, tasks, [], "recovered close", [], [], False,
        )
        recovered_done = handoff.read_text().split("done:\n", 1)[1].split(
            "decisions:\n", 1,
        )[0]

        clock["now"] = "2026-01-01T00:00:05Z"
        module["orchestrator_close_brief"](
            runtime, tasks, [], "next close", [], [], False,
        )
        following_done = handoff.read_text().split("done:\n", 1)[1].split(
            "decisions:\n", 1,
        )[0]
        self.assertEqual(recovered_done.count(first_id), 1)
        self.assertEqual(recovered_done.count(second_id), 1)
        self.assertNotIn(first_id, following_done)
        self.assertNotIn(second_id, following_done)

    def test_start_recovers_cursor_handoff_after_interrupted_close_publication(self):
        project = self.make_project("start-recovers-cursor-publication")
        runtime = project / ".baton"
        handoff = runtime / "orchestrator-handoff.md"
        handoff.write_text(
            "# Orchestrator handoff\n"
            "generated_at: 2026-01-01T00:00:00Z\n"
            "consumed_at: (not yet)\n"
            "goal: stale Markdown goal\n"
            "done:\n- T900-stale: stale done entry\n"
            "decisions:\n- stale decision\n"
            "next:\n- T900-stale\n"
            "unresolved:\n- T900-stale\n"
            "notes:\n- stale note\n"
            "avoid:\n- stale warning\n"
        )
        done_id = "T901-canonical-done"
        decision_id = "T902-canonical-decision"
        next_id = "T903-canonical-next"
        unresolved_id = "T904-canonical-unresolved"
        tasks = [
            {
                "id": done_id, "title": "canonical completed work", "status": "done",
                "history": [{
                    "event": "accepted", "at": "2026-01-01T00:00:01Z",
                    "note": "canonical outcome",
                }],
            },
            {
                "id": decision_id, "title": "canonical decision", "status": "done",
                "history": [{
                    "event": "decided", "at": "2026-01-01T00:00:01Z",
                    "answer": "use the cursor presentation",
                }],
            },
            {"id": next_id, "title": "canonical next", "status": "queued"},
            {
                "id": unresolved_id, "title": "canonical unresolved",
                "status": "needs_decision", "last_note": "choose recovery policy",
            },
        ]
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_start_cursor_recovery_probe",
        )
        globals_ = module["cmd_orchestrator_brief"].__globals__
        globals_["require_baton_dir"] = lambda: str(runtime)
        globals_["load_all_tasks"] = lambda *_args, **_kwargs: tasks
        clock = {"now": "2026-01-01T00:00:01Z"}
        globals_["now"] = lambda: clock["now"]
        original_atomic_write = globals_["atomic_write"]
        fail_presentation = {"armed": True}

        def interrupted_atomic_write(path, content, mode=0o644):
            if (
                    fail_presentation["armed"]
                    and path == globals_["orchestrator_handoff_path"](str(runtime))):
                fail_presentation["armed"] = False
                raise OSError("injected handoff presentation failure")
            original_atomic_write(path, content, mode)

        globals_["atomic_write"] = interrupted_atomic_write
        globals_["say"] = lambda *_args: None
        with self.assertRaisesRegex(OSError, "injected handoff presentation failure"):
            module["orchestrator_close_brief"](
                runtime, tasks, [], "canonical recovered goal",
                ["avoid stale Markdown"], ["canonical operator note"], True,
            )

        cursor_path = runtime / "orchestrator-handoff-cursor.json"
        cursor_after_failure = json.loads(cursor_path.read_text())
        canonical = cursor_after_failure["handoff"]
        self.assertIn("goal: canonical recovered goal\n", canonical)
        self.assertIn("goal: stale Markdown goal\n", handoff.read_text())

        brief_args = SimpleNamespace(
            phase="start", include_log_tail=False, goal=None,
            avoid=[], note=[], id=None,
        )
        emitted = []
        globals_["say"] = emitted.append
        clock["now"] = "2026-01-01T00:00:02Z"
        module["cmd_orchestrator_brief"](brief_args)
        output = "\n".join(emitted)
        for expected in (
                "# Orchestrator handoff",
                "generated_at: 2026-01-01T00:00:01Z",
                "consumed_at: 2026-01-01T00:00:02Z",
                "goal: canonical recovered goal",
                "warning: uncommitted Git-visible changes at close",
                f"{done_id}: canonical completed work — outcome: canonical outcome",
                f"{decision_id}: use the cursor presentation",
                f"next:\n- {next_id}",
                f"unresolved:\n- {unresolved_id}",
                "notes:\n- canonical operator note",
                "avoid:\n- avoid stale Markdown"):
            self.assertIn(expected, output)
        self.assertNotIn("stale Markdown goal", output)
        consumed_cursor = json.loads(cursor_path.read_text())
        self.assertEqual(consumed_cursor["handoff"], handoff.read_text())
        self.assertNotIn("consumed_at: (not yet)", consumed_cursor["handoff"])

        emitted.clear()
        clock["now"] = "2026-01-01T00:00:03Z"
        module["cmd_orchestrator_brief"](brief_args)
        self.assertNotIn("consumed_at: (not yet)", "\n".join(emitted))
        self.assertEqual(json.loads(cursor_path.read_text())["handoff"], handoff.read_text())

        clock["now"] = "2026-01-01T00:00:04Z"
        globals_["say"] = lambda *_args: None
        module["orchestrator_close_brief"](
            runtime, tasks, [], "following close", [], [], False,
        )
        following = handoff.read_text()
        self.assertNotIn(done_id, following)
        self.assertEqual(json.loads(cursor_path.read_text())["handoff"], following)

    def test_handoff_same_second_cursor_survives_three_closes(self):
        project = self.make_project("same-second-three-closes")
        self.configure(project, self.write_worker(GOOD_WORKER))
        config = project / ".baton" / "config.toml"
        config.write_text(
            config.read_text() + "\n[gates]\naccept_requires_brief = false\n"
        )
        task_id = self.create_task(project, "three close cursor", ["cursor/**"])
        self.baton(project, "run", task_id, check=True)
        runtime = project / ".baton"
        boundary = "2026-01-01T00:00:00Z"
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_three_close_probe")
        globals_ = module["orchestrator_close_brief"].__globals__
        globals_["require_baton_dir"] = lambda: str(runtime)
        globals_["now"] = lambda: boundary
        globals_["say"] = lambda *_args: None
        module["cmd_task_accept"](SimpleNamespace(
            id=task_id, brief=None, note="accepted at boundary",
        ))

        counts = []
        for _close in range(3):
            module["orchestrator_close_brief"](
                runtime, [self.state(project, task_id)], [],
                "next goal", [], [], False,
            )
            done = (runtime / "orchestrator-handoff.md").read_text().split(
                "done:\n", 1,
            )[1].split("decisions:\n", 1)[0]
            counts.append(done.count(task_id))

        self.assertEqual(counts, [1, 0, 0])

    def test_handoff_same_second_cursor_includes_new_interleaved_acceptance(self):
        project = self.make_project("same-second-interleaved-acceptance")
        self.configure(project, self.write_worker(GOOD_WORKER))
        config = project / ".baton" / "config.toml"
        config.write_text(
            config.read_text() + "\n[gates]\naccept_requires_brief = false\n"
        )
        old_id = self.create_task(project, "old boundary task", ["old/**"])
        new_id = self.create_task(project, "new boundary task", ["new/**"])
        runtime = project / ".baton"
        boundary = "2026-01-01T00:00:00Z"
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_interleave_probe")
        globals_ = module["orchestrator_close_brief"].__globals__
        globals_["require_baton_dir"] = lambda: str(runtime)
        globals_["now"] = lambda: boundary
        globals_["say"] = lambda *_args: None

        def accept(task_id):
            self.baton(project, "run", task_id, check=True)
            module["cmd_task_accept"](SimpleNamespace(
                id=task_id, brief=None, note="accepted at boundary",
            ))

        def close():
            tasks = [self.state(project, task_id) for task_id in (old_id, new_id)]
            module["orchestrator_close_brief"](
                runtime, tasks, [], "next goal", [], [], False,
            )
            return (runtime / "orchestrator-handoff.md").read_text().split(
                "done:\n", 1,
            )[1].split("decisions:\n", 1)[0]

        accept(old_id)
        sections = [close(), close()]
        accept(new_id)
        sections.extend((close(), close()))

        self.assertEqual([section.count(old_id) for section in sections], [1, 0, 0, 0])
        self.assertEqual([section.count(new_id) for section in sections], [0, 0, 1, 0])

    def test_handoff_cursor_remembers_same_second_ids_omitted_by_done_limit(self):
        project = self.make_project("same-second-cursor-overflow")
        runtime = project / ".baton"
        boundary = "2026-01-01T00:00:00Z"
        tasks = [
            {
                "id": f"T{number:03d}-overflow", "title": "t" * 240,
                "status": "done", "history": [{
                    "event": "accepted", "at": boundary, "note": "done",
                }],
            }
            for number in range(1, 13)
        ]
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_overflow_cursor_probe")
        globals_ = module["orchestrator_close_brief"].__globals__
        globals_["now"] = lambda: boundary
        globals_["say"] = lambda *_args: None

        sections = []
        for _close in range(2):
            module["orchestrator_close_brief"](
                runtime, tasks, [], "next goal", [], [], False,
            )
            content = (runtime / "orchestrator-handoff.md").read_text()
            self.assertLessEqual(len(content), 4000)
            sections.append(content.split("done:\n", 1)[1].split(
                "decisions:\n", 1,
            )[0])

        self.assertIn("+4 more", sections[0])
        self.assertTrue(all(task["id"] not in sections[1] for task in tasks))

    def test_handoff_overflow_crash_recovery_and_consumption_preserve_presentation(self):
        project = self.make_project("handoff-overflow-crash-recovery")
        runtime = project / ".baton"
        boundary = "2026-01-01T00:00:00Z"
        handoff_path = runtime / "orchestrator-handoff.md"
        handoff_path.write_text(
            "# Orchestrator handoff\n"
            "generated_at: 2025-12-31T23:59:59Z\n"
            "consumed_at: (not yet)\n"
            "goal: stale presentation\n"
            "done:\n- (none)\n"
        )
        tasks = [
            {
                "id": f"T{number:03d}-recovered", "title": f"result {number:02d}",
                "status": "done", "history": [{
                    "event": "accepted", "at": boundary,
                    "note": f"outcome {number:02d}",
                }],
            }
            for number in range(1, 13)
        ]
        tasks[0]["history"].append({
            "event": "decided", "at": boundary, "answer": "first decision",
        })
        tasks[1]["history"].append({
            "event": "decided", "at": boundary, "answer": "second decision",
        })
        tasks.extend({
            "id": f"T{number:03d}-next", "title": "queued", "status": "queued",
        } for number in range(101, 111))
        tasks.extend({
            "id": f"T{number:03d}-unresolved", "title": "blocked",
            "status": "needs_decision",
        } for number in range(201, 211))
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_overflow_recovery_probe",
        )
        globals_ = module["orchestrator_close_brief"].__globals__
        clock = {"now": boundary}
        globals_["now"] = lambda: clock["now"]
        globals_["say"] = lambda *_args: None
        original_atomic_write = globals_["atomic_write"]
        fail_presentation = {"armed": True}

        def interrupted_atomic_write(path, content, mode=0o644):
            if (
                    fail_presentation["armed"]
                    and path == globals_["orchestrator_handoff_path"](str(runtime))):
                fail_presentation["armed"] = False
                raise OSError("injected overflow handoff presentation failure")
            original_atomic_write(path, content, mode)

        globals_["atomic_write"] = interrupted_atomic_write
        with self.assertRaisesRegex(
                OSError, "injected overflow handoff presentation failure"):
            module["orchestrator_close_brief"](
                runtime, tasks, [], "preserve canonical summary",
                ["avoid duplicate summaries"], ["keep exact presentation"], True,
            )

        cursor_path = runtime / "orchestrator-handoff-cursor.json"
        cursor_after_failure = json.loads(cursor_path.read_text())
        expected_done = [
            f"- T{number:03d}-recovered: result {number:02d}"
            f" — outcome: outcome {number:02d}"
            for number in range(1, 9)
        ] + ["- +4 more"]
        expected = "\n".join([
            "# Orchestrator handoff",
            f"generated_at: {boundary}",
            "consumed_at: (not yet)",
            "goal: preserve canonical summary",
            "warning: uncommitted Git-visible changes at close",
            "done:", *expected_done,
            "decisions:",
            "- T002-recovered: second decision",
            "- T001-recovered: first decision",
            "next:",
            *(f"- T{number:03d}-next" for number in range(101, 109)),
            "- +2 more",
            "unresolved:",
            *(f"- T{number:03d}-unresolved" for number in range(201, 209)),
            "- +2 more",
            "notes:", "- keep exact presentation",
            "avoid:", "- avoid duplicate summaries",
        ]) + "\n"
        self.assertEqual(cursor_after_failure["handoff"], expected)
        self.assertNotEqual(handoff_path.read_text(), expected)

        globals_["atomic_write"] = original_atomic_write
        module["orchestrator_close_brief"](
            runtime, tasks, [], "preserve canonical summary",
            ["avoid duplicate summaries"], ["keep exact presentation"], True,
        )
        recovered_cursor = json.loads(cursor_path.read_text())
        self.assertEqual(recovered_cursor["handoff"], expected)
        self.assertEqual(handoff_path.read_text(), expected)
        done_section = expected.split("done:\n", 1)[1].split("decisions:\n", 1)[0]
        self.assertEqual(sum(
            line.startswith("- T") for line in done_section.splitlines()
        ), 8)
        self.assertIn("- +4 more\n", done_section)
        self.assertNotIn("+1 more", done_section)

        emitted = []
        globals_["say"] = emitted.append
        clock["now"] = "2026-01-01T00:00:01Z"
        module["orchestrator_start_brief"](runtime, tasks)
        consumed = expected.replace(
            "consumed_at: (not yet)",
            "consumed_at: 2026-01-01T00:00:01Z", 1,
        )
        self.assertIn("\nCurrent handoff:\n" + consumed.rstrip(), "\n".join(emitted))
        self.assertEqual(handoff_path.read_text(), consumed)
        self.assertEqual(json.loads(cursor_path.read_text())["handoff"], consumed)

        emitted.clear()
        clock["now"] = "2026-01-01T00:00:02Z"
        module["orchestrator_start_brief"](runtime, tasks)
        consumed_again = consumed.replace(
            "consumed_at: 2026-01-01T00:00:01Z",
            "consumed_at: 2026-01-01T00:00:02Z", 1,
        )
        self.assertEqual(handoff_path.read_text(), consumed_again)
        self.assertEqual(json.loads(cursor_path.read_text())["handoff"], consumed_again)
        self.assertEqual(
            consumed_again.replace("consumed_at: 2026-01-01T00:00:02Z", ""),
            expected.replace("consumed_at: (not yet)", ""),
        )

        clock["now"] = "2026-01-01T00:00:03Z"
        globals_["say"] = lambda *_args: None
        module["orchestrator_close_brief"](
            runtime, tasks, [], "later close", [], [], False,
        )
        later = handoff_path.read_text()
        later_done = later.split("done:\n", 1)[1].split("decisions:\n", 1)[0]
        self.assertTrue(all(task["id"] not in later_done for task in tasks[:12]))
        self.assertEqual(json.loads(cursor_path.read_text())["handoff"], later)

    def test_handoff_cursor_retains_every_reported_acceptance_identity(self):
        project = self.make_project("handoff-cursor-boundary-rollover")
        runtime = project / ".baton"
        first_boundary = "2026-01-01T00:00:00Z"
        second_boundary = "2026-01-01T00:00:01Z"
        historical_id = "T901-historical"
        first_id = "T902-first-boundary"
        second_id = "T903-second-boundary"
        tasks = [
            {
                "id": task_id, "title": task_id, "status": "done",
                "history": [{"event": "accepted", "at": accepted_at}],
            }
            for task_id, accepted_at in (
                (historical_id, "2025-12-31T23:59:59Z"),
                (first_id, first_boundary),
                (second_id, second_boundary),
            )
        ]
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_cursor_rollover_probe")
        globals_ = module["orchestrator_close_brief"].__globals__
        clock = iter((first_boundary, second_boundary))
        globals_["now"] = lambda: next(clock)
        globals_["say"] = lambda *_args: None

        module["orchestrator_close_brief"](
            runtime, tasks[:2], [], "first goal", [], [], False,
        )
        cursor_path = runtime / "orchestrator-handoff-cursor.json"
        first_cursor = json.loads(cursor_path.read_text())
        module["orchestrator_close_brief"](
            runtime, tasks, [], "second goal", [], [], False,
        )
        second_cursor = json.loads(cursor_path.read_text())

        self.assertEqual(first_cursor["version"], 3)
        self.assertEqual(
            first_cursor["reported_ids"], [historical_id, first_id],
        )
        self.assertEqual(second_cursor["version"], 3)
        self.assertEqual(
            second_cursor["reported_ids"], [historical_id, first_id, second_id],
        )

    def test_handoff_cursor_legacy_absence_uses_visible_done_boundary(self):
        project = self.make_project("legacy-handoff-without-cursor")
        runtime = project / ".baton"
        boundary = "2026-01-01T00:00:00Z"
        old_id = "T901-legacy-old"
        new_id = "T902-legacy-new"
        handoff = runtime / "orchestrator-handoff.md"
        handoff.write_text(
            "# Orchestrator handoff\n"
            f"generated_at: {boundary}\n"
            "consumed_at: (not yet)\n"
            "goal: legacy\n"
            f"done:\n- {old_id}: already shown\n"
        )
        tasks = [
            {
                "id": task_id, "title": title, "status": "done",
                "history": [{"event": "accepted", "at": boundary}],
            }
            for task_id, title in ((old_id, "old"), (new_id, "new"))
        ]
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_legacy_cursor_probe")
        globals_ = module["orchestrator_close_brief"].__globals__
        globals_["now"] = lambda: boundary
        globals_["say"] = lambda *_args: None

        module["orchestrator_close_brief"](
            runtime, tasks, [], "next goal", [], [], False,
        )
        done = handoff.read_text().split("done:\n", 1)[1].split(
            "decisions:\n", 1,
        )[0]
        self.assertNotIn(old_id, done)
        self.assertIn(new_id, done)

    def test_handoff_version_one_cursor_is_read_and_migrated(self):
        project = self.make_project("version-one-handoff-cursor")
        runtime = project / ".baton"
        boundary = "2026-01-01T00:00:00Z"
        old_id = "T901-version-one-old"
        new_id = "T902-version-one-new"
        handoff = runtime / "orchestrator-handoff.md"
        handoff.write_text(
            "# Orchestrator handoff\n"
            f"generated_at: {boundary}\n"
            "consumed_at: (not yet)\n"
            "goal: version one\n"
            "done:\n- (none)\n"
        )
        cursor = runtime / "orchestrator-handoff-cursor.json"
        cursor.write_text(json.dumps({
            "version": 1, "accepted_at": boundary, "seen_ids": [old_id],
        }))
        tasks = [
            {
                "id": task_id, "title": title, "status": "done",
                "history": [{"event": "accepted", "at": boundary}],
            }
            for task_id, title in ((old_id, "old"), (new_id, "new"))
        ]
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_v1_cursor_probe")
        globals_ = module["orchestrator_close_brief"].__globals__
        globals_["now"] = lambda: boundary
        globals_["say"] = lambda *_args: None

        module["orchestrator_close_brief"](
            runtime, tasks, [], "migrated", [], [], False,
        )
        done = handoff.read_text().split("done:\n", 1)[1].split(
            "decisions:\n", 1,
        )[0]
        migrated = json.loads(cursor.read_text())
        self.assertNotIn(old_id, done)
        self.assertIn(new_id, done)
        self.assertEqual(migrated["version"], 3)
        self.assertEqual(migrated["reported_ids"], [old_id, new_id])
        self.assertEqual(migrated["handoff"], handoff.read_text())

    def test_handoff_version_two_cursor_migration_is_deterministic(self):
        project = self.make_project("version-two-handoff-cursor")
        runtime = project / ".baton"
        boundary = "2026-01-01T00:00:10Z"
        old_id = "T901-version-two-old"
        rollback_id = "T902-version-two-rollback"
        forward_id = "T903-version-two-forward"
        canonical = (
            "# Orchestrator handoff\n"
            f"generated_at: {boundary}\n"
            "consumed_at: (not yet)\n"
            "goal: canonical version two\n"
            f"done:\n- {old_id}: canonical accepted task\n"
        )
        handoff = runtime / "orchestrator-handoff.md"
        handoff.write_text(
            "# Orchestrator handoff\n"
            "generated_at: 2026-01-01T00:00:09Z\n"
            "consumed_at: (not yet)\n"
            "goal: interrupted stale presentation\n"
            "done:\n- (none)\n"
        )
        cursor = runtime / "orchestrator-handoff-cursor.json"
        cursor.write_text(json.dumps({
            "version": 2, "accepted_at": boundary, "seen_ids": [old_id],
            "handoff": canonical,
        }))
        tasks = [
            {
                "id": task_id, "title": title, "status": "done",
                "history": [{"event": "accepted", "at": accepted_at}],
            }
            for task_id, title, accepted_at in (
                (old_id, "old", boundary),
                (rollback_id, "legacy ambiguous rollback", "2026-01-01T00:00:05Z"),
                (forward_id, "new after boundary", "2026-01-01T00:00:11Z"),
            )
        ]
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_v2_cursor_probe")
        globals_ = module["orchestrator_close_brief"].__globals__
        globals_["now"] = lambda: "2026-01-01T00:00:06Z"
        globals_["say"] = lambda *_args: None

        module["orchestrator_close_brief"](
            runtime, tasks, [], "migrated", [], [], False,
        )
        done = handoff.read_text().split("done:\n", 1)[1].split(
            "decisions:\n", 1,
        )[0]
        migrated = json.loads(cursor.read_text())
        self.assertIn(old_id, done)
        self.assertNotIn(rollback_id, done)
        self.assertIn(forward_id, done)
        self.assertEqual(migrated["version"], 3)
        self.assertEqual(
            migrated["reported_ids"], [old_id, rollback_id, forward_id],
        )
        self.assertEqual(migrated["handoff"], handoff.read_text())

    def test_handoff_version_three_cursor_schema_is_strict_without_mutation(self):
        valid_handoff = (
            "# Orchestrator handoff\n"
            "generated_at: 2026-01-01T00:00:00Z\n"
            "consumed_at: (not yet)\n"
            "goal: preserve version three\n"
            "done:\n- (none)\n"
        )
        cases = (
            {"version": True, "accepted_at": "2026-01-01T00:00:00Z",
             "seen_ids": []},
            {"version": 3.0, "reported_ids": [], "handoff": valid_handoff},
            {"version": None, "reported_ids": [], "handoff": valid_handoff},
            {"version": "3", "reported_ids": [], "handoff": valid_handoff},
            {"version": 4, "reported_ids": [], "handoff": valid_handoff},
            {"version": 3, "reported_ids": [], "handoff": valid_handoff,
             "extra": True},
            {"version": 3, "reported_ids": [], "seen_ids": [],
             "handoff": valid_handoff},
            {"version": 3, "reported_ids": ["T901-duplicate", "T901-duplicate"],
             "handoff": valid_handoff},
            {"version": 3, "reported_ids": ["not-a-task"],
             "handoff": valid_handoff},
            {"version": 3, "reported_ids": [], "handoff": valid_handoff.replace(
                "2026-01-01T00:00:00Z", "2026-02-30T00:00:00Z",
            )},
        )
        for index, cursor_data in enumerate(cases):
            with self.subTest(index=index):
                project = self.make_project(f"invalid-v3-cursor-{index}")
                runtime = project / ".baton"
                handoff = runtime / "orchestrator-handoff.md"
                handoff.write_text(valid_handoff)
                cursor = runtime / "orchestrator-handoff-cursor.json"
                cursor.write_text(json.dumps(cursor_data))
                before = (handoff.read_bytes(), cursor.read_bytes())

                rejected = self.baton(
                    project, "orchestrator", "brief", "--phase", "close",
                    "--goal", "must reject malformed cursor",
                )

                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(
                    rejected.stderr,
                    "error: orchestrator handoff cursor is invalid\n",
                )
                self.assertEqual((handoff.read_bytes(), cursor.read_bytes()), before)

    def test_handoff_cursor_rejects_unaccepted_reported_id_and_recovers_once(self):
        project = self.make_project("unaccepted-reported-handoff-id")
        task_id = self.create_task(
            project, "future accepted handoff task", ["future/**"],
        )
        runtime = project / ".baton"
        handoff = runtime / "orchestrator-handoff.md"
        canonical = (
            "# Orchestrator handoff\n"
            "generated_at: 2026-01-01T00:00:00Z\n"
            "consumed_at: (not yet)\n"
            "goal: preserve trusted acceptance identities\n"
            "done:\n- (none)\n"
        )
        handoff.write_text(canonical)
        cursor = runtime / "orchestrator-handoff-cursor.json"
        cursor.write_text(json.dumps({
            "version": 3, "reported_ids": [task_id], "handoff": canonical,
        }))
        before = (handoff.read_bytes(), cursor.read_bytes())

        rejected = self.baton(
            project, "orchestrator", "brief", "--phase", "close",
            "--goal", "reject an untrusted reported identity",
        )

        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(
            rejected.stderr, "error: orchestrator handoff cursor is invalid\n",
        )
        self.assertNotIn("Traceback", rejected.stdout + rejected.stderr)
        self.assertEqual((handoff.read_bytes(), cursor.read_bytes()), before)

        task_path = runtime / "tasks" / f"{task_id}.json"
        task = json.loads(task_path.read_text())
        task["status"] = "done"
        task["history"].append({
            "event": "accepted", "at": "2026-01-01T00:00:01Z",
        })
        task_path.write_text(json.dumps(task))
        cursor.write_text(json.dumps({
            "version": 3, "reported_ids": [], "handoff": canonical,
        }))

        counts = []
        for goal in ("publish recovered task", "do not publish it twice"):
            self.baton(
                project, "orchestrator", "brief", "--phase", "close",
                "--goal", goal, check=True,
            )
            done = handoff.read_text().split("done:\n", 1)[1].split(
                "decisions:\n", 1,
            )[0]
            counts.append(done.count(task_id))

        self.assertEqual(counts, [1, 0])
        self.assertEqual(json.loads(cursor.read_text())["reported_ids"], [task_id])

    def test_handoff_cursor_rejects_invalid_timestamps_without_mutation(self):
        cases = (
            (1, "2026-99-01T00:00:00Z", "close"),
            (2, "2026-02-30T00:00:00Z", "start"),
            (1, "2026-01-01T99:00:00Z", "start"),
            (2, "2026-1-01T00:00:00Z", "close"),
        )
        for index, (version, accepted_at, phase) in enumerate(cases):
            with self.subTest(version=version, accepted_at=accepted_at, phase=phase):
                project = self.make_project(f"invalid-cursor-{index}")
                runtime = project / ".baton"
                handoff = runtime / "orchestrator-handoff.md"
                handoff.write_text(
                    "# Orchestrator handoff\n"
                    "generated_at: 2026-01-01T00:00:00Z\n"
                    "consumed_at: (not yet)\n"
                    "goal: preserve presentation\n"
                    "done:\n- T900-shown: preserve accepted-task presentation\n"
                )
                cursor_data = {
                    "version": version, "accepted_at": accepted_at,
                    "seen_ids": [],
                }
                if version == 2:
                    cursor_data["handoff"] = (
                        "# Orchestrator handoff\n"
                        f"generated_at: {accepted_at}\n"
                        "consumed_at: (not yet)\n"
                        "goal: preserve canonical cursor presentation\n"
                        "done:\n- T901-canonical: preserve this accepted task\n"
                    )
                cursor = runtime / "orchestrator-handoff-cursor.json"
                cursor.write_text(json.dumps(cursor_data))
                before = (handoff.read_bytes(), cursor.read_bytes())

                args = ["orchestrator", "brief", "--phase", phase]
                if phase == "close":
                    args += ["--goal", "continue safely"]
                rejected = self.baton(project, *args)

                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(
                    rejected.stderr,
                    "error: orchestrator handoff cursor is invalid\n",
                )
                self.assertNotIn("Traceback", rejected.stdout + rejected.stderr)
                self.assertEqual((handoff.read_bytes(), cursor.read_bytes()), before)

    def test_invalid_cursor_close_preserves_accepted_task_for_recovery(self):
        project = self.make_project("invalid-cursor-accepted-task-recovery")
        task_id = self.create_task(project, "recover accepted task", ["recover/**"])
        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        task = json.loads(state_path.read_text())
        task["status"] = "done"
        task["history"].append({
            "event": "accepted", "at": "2000-01-01T00:00:00Z",
            "note": "must survive the rejected close",
        })
        state_path.write_text(json.dumps(task))
        handoff = runtime / "orchestrator-handoff.md"
        handoff.write_text(
            "# Orchestrator handoff\n"
            "generated_at: 1999-12-31T23:59:59Z\n"
            "consumed_at: (not yet)\n"
            "goal: last valid handoff\n"
            "done:\n- (none)\n"
        )
        impossible = "9999-99-99T99:99:99Z"
        cursor = runtime / "orchestrator-handoff-cursor.json"
        cursor.write_text(json.dumps({
            "version": 2, "accepted_at": impossible, "seen_ids": [],
            "handoff": (
                "# Orchestrator handoff\n"
                f"generated_at: {impossible}\n"
                "consumed_at: (not yet)\n"
                "goal: malformed cursor boundary\n"
                "done:\n- (none)\n"
            ),
        }))
        before = (handoff.read_bytes(), cursor.read_bytes())

        rejected = self.baton(
            project, "orchestrator", "brief", "--phase", "close",
            "--goal", "recover accepted work",
        )

        if rejected.returncode == 0:
            self.assertNotIn(task_id, handoff.read_text())
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(
            rejected.stderr, "error: orchestrator handoff cursor is invalid\n",
        )
        self.assertNotIn("Traceback", rejected.stdout + rejected.stderr)
        self.assertEqual((handoff.read_bytes(), cursor.read_bytes()), before)

        cursor.unlink()
        recovered = self.baton(
            project, "orchestrator", "brief", "--phase", "close",
            "--goal", "recover accepted work", check=True,
        )
        self.assertIn(task_id, recovered.stdout)
        self.assertIn(task_id, handoff.read_text())

    def test_handoff_malformed_cursor_fails_without_rewriting_handoff(self):
        project = self.make_project("malformed-handoff-cursor")
        runtime = project / ".baton"
        handoff = runtime / "orchestrator-handoff.md"
        handoff.write_text(
            "# Orchestrator handoff\n"
            "generated_at: 2026-01-01T00:00:00Z\n"
            "consumed_at: (not yet)\n"
            "goal: preserve this handoff\n"
            "done:\n- (none)\n"
        )
        before = handoff.read_bytes()
        (runtime / "orchestrator-handoff-cursor.json").write_text("{not json\n")
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_malformed_cursor_probe")
        module["orchestrator_close_brief"].__globals__["say"] = lambda *_args: None

        with self.assertRaises(SystemExit):
            module["orchestrator_close_brief"](
                runtime, [], [], "next goal", [], [], False,
            )
        self.assertEqual(handoff.read_bytes(), before)

    def test_worker_manual_uses_installed_baton_commands(self):
        worker = (ROOT / "framework" / "worker.md").read_text()
        self.assertNotIn("`baton ", worker)
        self.assertNotIn("`task finish ", worker)
        self.assertIn("python3 .baton/baton task brief", worker)
        self.assertIn("python3 .baton/baton task finish", worker)

    def test_claude_code_hook_fragment_is_exactly_two_matcher_free_commands(self):
        project = self.make_project()
        printed = self.baton(project, "hooks", "claude-code", check=True)
        fragment_text = printed.stdout.rsplit("\n", 2)[0]
        fragment = json.loads(fragment_text)
        commands = {
            "SessionStart": (
                '"$CLAUDE_PROJECT_DIR"/.baton/baton '
                "hook-event session-start"
            ),
            "UserPromptSubmit": (
                '"$CLAUDE_PROJECT_DIR"/.baton/baton '
                "hook-event user-prompt-submit"
            ),
        }

        self.assertEqual(list(fragment), ["hooks"])
        self.assertEqual(list(fragment["hooks"]), list(commands))
        for event, command in commands.items():
            entries = fragment["hooks"][event]
            self.assertEqual(len(entries), 1)
            self.assertNotIn("matcher", entries[0])
            self.assertEqual(entries[0], {
                "hooks": [{"type": "command", "command": command}],
            })

    def test_claude_code_hook_setup_prints_creates_merges_and_is_idempotent(self):
        project = self.make_project()
        printed = self.baton(project, "hooks", "claude-code", check=True)
        fragment_text, instruction = printed.stdout.rsplit("\n", 2)[:2]
        fragment = json.loads(fragment_text)
        self.assertEqual(set(fragment["hooks"]), {"SessionStart", "UserPromptSubmit"})
        self.assertIn(".baton/baton hook-event", printed.stdout)
        self.assertIn("Merge this fragment into .claude/settings.json", instruction)
        for event in ("SessionStart", "UserPromptSubmit"):
            self.assertNotIn("matcher", fragment["hooks"][event][0])

        self.baton(project, "hooks", "claude-code", "--write", check=True)
        created_path = project / ".claude" / "settings.json"
        self.assertEqual(json.loads(created_path.read_text()), fragment)

        merged_project = self.make_project("hooks-merge")
        settings_path = merged_project / ".claude" / "settings.json"
        settings_path.parent.mkdir()
        unrelated = {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "existing-pre-tool"}],
        }
        existing_session = {
            "hooks": [{"type": "command", "command": "existing-session"}],
        }
        settings_path.write_text(json.dumps({
            "permissions": {"allow": ["Read"]},
            "hooks": {
                "PreToolUse": [unrelated],
                "SessionStart": [existing_session],
            },
        }))
        for _ in range(2):
            self.baton(
                merged_project, "hooks", "claude-code", "--write", check=True,
            )
        merged = json.loads(settings_path.read_text())
        self.assertEqual(merged["permissions"], {"allow": ["Read"]})
        self.assertEqual(merged["hooks"]["PreToolUse"], [unrelated])
        self.assertEqual(merged["hooks"]["SessionStart"][0], existing_session)
        self.assertEqual(len(merged["hooks"]["SessionStart"]), 2)
        self.assertEqual(len(merged["hooks"]["UserPromptSubmit"]), 1)

        invalid_project = self.make_project("hooks-invalid")
        invalid_path = invalid_project / ".claude" / "settings.json"
        invalid_path.parent.mkdir()
        invalid_path.write_text("{not valid json\n")
        before = invalid_path.read_text()
        rejected = self.baton(
            invalid_project, "hooks", "claude-code", "--write",
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("cannot parse .claude/settings.json", rejected.stderr)
        self.assertEqual(invalid_path.read_text(), before)

    def test_session_start_hook_marks_compaction_and_caps_reinjected_brief(self):
        project = self.make_project()
        brief = self.baton(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        ).stdout
        command = [
            project / ".baton" / "baton", "hook-event", "session-start",
        ]

        for hook_input in ("", '{"source":"startup"}', "not json", "[]"):
            with self.subTest(hook_input=hook_input):
                session = subprocess.run(
                    command, cwd=project, input=hook_input, text=True,
                    capture_output=True, env=clean_test_environment(),
                )
                self.assertEqual(
                    (session.returncode, session.stdout, session.stderr),
                    (0, brief, ""),
                )

        notice = "Baton: context was compacted; state re-injected below."
        compact = subprocess.run(
            command, cwd=project, input='{"source":"compact"}', text=True,
            capture_output=True, env=clean_test_environment(),
        )
        self.assertEqual(compact.returncode, 0)
        self.assertEqual(compact.stderr, "")
        self.assertEqual(compact.stdout, notice + "\n" + brief)
        self.assertIn("Worker routing:", compact.stdout)

        (project / ".baton" / "orchestrator-handoff.md").write_text(
            "goal: " + "x" * 10000 + "\nlast handoff line\n"
        )
        capped = subprocess.run(
            command, cwd=project, input='{"source":"compact"}', text=True,
            capture_output=True, env=clean_test_environment(),
        )
        self.assertEqual(capped.returncode, 0)
        self.assertEqual(capped.stderr, "")
        self.assertLessEqual(len(capped.stdout), 9000)
        self.assertTrue(capped.stdout.startswith(notice + "\n"))
        self.assertIn("\n(truncated)\n", capped.stdout)

    def test_claude_code_hook_events_match_brief_emit_json_and_fail_open(self):
        project = self.make_project()
        brief = self.baton(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        )
        session = subprocess.run(
            [project / ".baton" / "baton", "hook-event", "session-start"],
            cwd=project, input="not json", text=True, capture_output=True,
            env=clean_test_environment(),
        )
        self.assertEqual(session.returncode, 0)
        self.assertEqual(session.stderr, "")
        self.assertEqual(session.stdout, brief.stdout)
        decision = self.create_task(project, "hook decision")
        decision_path = (
            project / ".baton" / "tasks" / f"{decision}.json"
        )
        decision_task = json.loads(decision_path.read_text())
        question = "May we\n\x1b[31mchange\x1b[0m the interface?"
        decision_task["status"] = "needs_decision"
        decision_task["last_note"] = question
        decision_task["history"].append({
            "event": "worker_exited", "status": "needs_decision", "note": question,
        })
        decision_path.write_text(json.dumps(decision_task))
        prompt = subprocess.run(
            [
                project / ".baton" / "baton", "hook-event",
                "user-prompt-submit",
            ],
            cwd=project, input="{malformed", text=True, capture_output=True,
            env=clean_test_environment(),
        )
        self.assertEqual(prompt.returncode, 0)
        self.assertLessEqual(len(prompt.stdout), 9000)
        payload = json.loads(prompt.stdout)
        specific = payload["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
        self.assertTrue(specific["additionalContext"].startswith(
            "Baton state:\nNext actions:\n",
        ))
        self.assertIn(
            f"- decide {decision}: worker question: May we change the interface?",
            specific["additionalContext"],
        )

        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_hook_cap_probe")
        capped = module["cap_hook_output"]("first\n" + "x" * 10000 + "\nlast\n")
        self.assertLessEqual(len(capped), 9000)
        self.assertTrue(capped.startswith("first\n"))
        self.assertTrue(capped.endswith("(truncated)\nlast\n"))
        bounded_json = module["claude_user_prompt_output"](
            "Baton state:\n" + "x" * 10000 + "\nlast",
        )
        self.assertLessEqual(len(bounded_json), 9000)
        self.assertIn("(truncated)\nlast", json.loads(bounded_json)[
            "hookSpecificOutput"
        ]["additionalContext"])

        broken_runtime = self.base / "empty-runtime"
        broken_runtime.mkdir()
        for name in ("session-start", "user-prompt-submit"):
            broken = self.baton(
                project, "hook-event", name, env={"BATON_DIR": broken_runtime},
            )
            self.assertEqual((broken.returncode, broken.stdout, broken.stderr), (0, "", ""))
        outside = self.command(
            [SOURCE_BATON, "hook-event", "session-start"], self.base,
        )
        self.assertEqual((outside.returncode, outside.stdout, outside.stderr), (0, "", ""))

        worker_env = {
            "BATON_TASK_ID": "T999-worker", "BATON_ATTEMPT": "1",
            "BATON_LEASE": "worker",
        }
        for command in (
                ("hooks", "claude-code"),
                ("hook-event", "session-start")):
            denied = self.baton(project, *command, env=worker_env)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("worker processes cannot run orchestrator commands", denied.stderr)

    def test_user_prompt_hook_reuses_loaded_snapshot_and_preserves_exact_json(self):
        project = self.make_project("hook-load-count")
        self.create_task(project, "hook load count", ["hook/**"])
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_hook_load_probe")
        globals_dict = module["cmd_hook_event"].__globals__
        original_load = globals_dict["load_all_tasks"]
        runtime = str(project / ".baton")
        tasks = original_load(runtime)
        expected = module["claude_user_prompt_output"](
            "Baton state:\n" + module["render_next_actions"](runtime, tasks)
        )
        calls = []

        def counted_load(baton_dir, validate_history=True):
            calls.append((baton_dir, validate_history))
            return original_load(baton_dir, validate_history)

        previous_directory = os.getcwd()
        previous_stdin = sys.stdin
        previous_environment = {
            key: os.environ.get(key) for key in BATON_ENVIRONMENT_KEYS
        }
        globals_dict["load_all_tasks"] = counted_load
        try:
            os.chdir(project)
            sys.stdin = io.StringIO("{malformed")
            for key in BATON_ENVIRONMENT_KEYS:
                os.environ.pop(key, None)
            os.environ["BATON_DIR"] = runtime
            captured = io.StringIO()
            with redirect_stdout(captured):
                module["cmd_hook_event"](SimpleNamespace(name="user-prompt-submit"))
        finally:
            globals_dict["load_all_tasks"] = original_load
            os.chdir(previous_directory)
            sys.stdin = previous_stdin
            for key, value in previous_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        output = captured.getvalue()
        self.assertEqual(len(calls), 1)
        self.assertEqual(output.encode(), expected.encode())
        self.assertLessEqual(len(output), 9000)
        self.assertIsInstance(json.loads(output), dict)

    def test_command_environment_ignores_ambient_worker_identity(self):
        previous = {key: os.environ.get(key) for key in BATON_ENVIRONMENT_KEYS}
        try:
            os.environ.update({
                "BATON_TASK_ID": "T999-ambient",
                "BATON_ATTEMPT": "9",
                "BATON_LEASE": "ambient-lease",
                "BATON_DIR": str(self.base / "ambient-runtime"),
                "BATON_ROOT": str(self.base / "ambient-root"),
            })
            project = self.make_project("ambient-worker-environment")
            self.create_task(project, "ambient fixture", ["ambient/**"])
            status = self.baton(project, "status", check=True)
            self.assertIn("ambient fixture", status.stdout)

            denied = self.baton(
                project, "tiers", env={
                    "BATON_TASK_ID": "T999-explicit",
                    "BATON_ATTEMPT": "1",
                    "BATON_LEASE": "explicit-lease",
                },
            )
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn(
                "worker processes cannot run orchestrator commands", denied.stderr,
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_reduced_performance_benchmark_completes_all_workloads(self):
        output = self.base / "performance-smoke.json"
        benchmark = self.command(
            [
                sys.executable, ROOT / "tools" / "benchmark_performance.py",
                "--samples", "1", "--suite-samples", "1", "--skip-suite",
                "--output", output,
            ],
            ROOT, timeout=120,
        )
        self.assertEqual(benchmark.returncode, 0, benchmark.stderr)
        payload = json.loads(output.read_text())
        self.assertEqual(payload["context"]["tasks"], 500)
        self.assertEqual(payload["context"]["archived_tasks"], 500)
        self.assertEqual(set(payload["benchmarks"]), {
            "startup_help", "start_brief", "large_status", "large_validate",
            "capsule_memory_references", "archive_stats", "init", "snapshot_diff",
        })
        self.assertEqual(
            payload["hot_paths"]["task_create_snapshot"]["directory_loads_per_call"],
            {"active": 1, "archive": 1, "total": 2},
        )

    def make_reduced_performance_fixture(self, name):
        module = runpy.run_path(
            str(ROOT / "tools" / "benchmark_performance.py"),
            run_name="baton_performance_fixture_probe_" + name,
        )
        module["prepare_fixture"].__globals__["TASKS"] = 2
        module["prepare_fixture"].__globals__["ARCHIVED"] = 3
        base = self.base / name
        base.mkdir()
        project = module["prepare_fixture"](SOURCE_BATON, base)
        return project, module

    def test_performance_suite_environment_selects_running_python(self):
        module = runpy.run_path(
            str(ROOT / "tools" / "benchmark_performance.py"),
            run_name="baton_performance_suite_environment_probe",
        )
        base = self.base / "suite-environment"
        base.mkdir()
        environment = module["suite_environment"](base)

        selected = shutil.which("python3", path=environment["PATH"])
        self.assertIsNotNone(selected)
        self.assertEqual(Path(selected).resolve(), Path(sys.executable).resolve())
        for key in module["BATON_ENVIRONMENT_KEYS"]:
            self.assertNotIn(key, environment)

    def test_performance_fixture_has_valid_finalized_review_evidence(self):
        project, module = self.make_reduced_performance_fixture(
            "finalized-performance-fixture",
        )
        archive = project / ".baton" / "archive"
        states = [
            json.loads(path.read_text())
            for path in sorted(archive.glob("*.json"))
        ]
        self.assertEqual(len(states), 3)
        for state in states:
            with self.subTest(task_id=state["id"]):
                evidence = module["finalized_evidence"](state["id"])
                self.assertEqual(state["status"], "done")
                self.assertEqual(
                    [entry["event"] for entry in state["history"]],
                    ["launched", "worker_exited", "accepted"],
                )
                launch, worker_exit, _accepted = state["history"]
                self.assertEqual(launch["attempt"], state["attempt"])
                self.assertEqual(launch["lease"], evidence["lease"])
                self.assertEqual(worker_exit["attempt"], state["attempt"])
                self.assertEqual(worker_exit["status"], "needs_review")
                self.assertEqual(worker_exit["declared_paths"], [evidence["changed_path"]])
                self.assertEqual(worker_exit["observed_paths"], [evidence["changed_path"]])
                work = archive / (state["id"] + ".work")
                result_path = work / "attempt-1.result.json"
                result = json.loads(result_path.read_text())
                self.assertEqual(result["changed_paths"], worker_exit["declared_paths"])
                self.assertEqual(result["lease"], launch["lease"])
                self.assertEqual(result["status"], worker_exit["status"])
                self.assertEqual(result["note"], worker_exit["note"])
                self.assertEqual(
                    worker_exit["result_digest"],
                    "sha256:" + hashlib.sha256(result_path.read_bytes()).hexdigest(),
                )
                self.assertGreater((work / "attempt-1.report.md").stat().st_size, 1500)
                self.assertGreater((work / "attempt-1.diff").stat().st_size, 3000)

        validation = self.command(
            [sys.executable, project / ".baton" / "baton", "validate"], project,
        )
        self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)

    def test_performance_fixture_detects_finalized_result_mutation(self):
        project, _module = self.make_reduced_performance_fixture(
            "mutated-performance-fixture",
        )
        result_path = next(
            (project / ".baton" / "archive").glob(
                "*.work/attempt-1.result.json",
            )
        )
        result_path.write_text(result_path.read_text() + " ")

        validation = self.command(
            [sys.executable, project / ".baton" / "baton", "validate"], project,
        )
        self.assertEqual(validation.returncode, 1)
        self.assertIn("review result changed after finalization", validation.stdout)

    def test_hook_cap_handles_edge_lines_and_preserves_normal_input(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_hook_edges_probe")
        cap = module["cap_hook_output"]
        prompt_output = module["claude_user_prompt_output"]
        normal = "first\nmiddle\nlast\n"
        self.assertEqual(cap(normal), normal)
        self.assertEqual(
            json.loads(prompt_output(normal))["hookSpecificOutput"]["additionalContext"],
            normal,
        )

        for impossible in (
                "x" * 10000 + "\nlast\n",
                "first\n" + "x" * 10000 + "\n"):
            with self.subTest(edge=impossible[:5]):
                self.assertEqual(cap(impossible), "")
                self.assertEqual(prompt_output(impossible), "")

        huge_middle = "first\n" + "x" * 10000 + "\nlast\n"
        capped = cap(huge_middle)
        self.assertLessEqual(len(capped), 9000)
        self.assertTrue(capped.startswith("first\n"))
        self.assertTrue(capped.endswith("(truncated)\nlast\n"))
        encoded = prompt_output(huge_middle)
        self.assertLessEqual(len(encoded), 9000)
        context = json.loads(encoded)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context.startswith("first\n"))
        self.assertTrue(context.endswith("(truncated)\nlast\n"))

    def test_concurrent_run_claims_task_once(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "once", ["once/**"])
        starts = self.base / "starts"
        env = clean_test_environment({
            "STARTS": starts, "FINISH_MARKER": self.base / "wait",
        })
        commands = [str(project / ".baton" / "baton"), "run", task_id]
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
        baton = str(project / ".baton" / "baton")
        env = clean_test_environment({"SLEEP_AFTER_FINISH": "0.8"})
        first = subprocess.Popen(
            [baton, "run", one], cwd=project, env=env,
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
            [baton, "run", two], cwd=project, env=env,
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
        task_id = self.create_task(project, "bad command", ["a/**"])
        config = project / ".baton" / "config.toml"
        config.write_text(
            '[commands]\nworker = "true {prompt} embedded{prompt}"\n'
            '[tiers.test]\n'
            '[limits]\nmax_parallel = 1\n'
        )
        self.assertNotEqual(self.baton(project, "validate").returncode, 0)
        result = self.baton(project, "run")
        self.assertNotEqual(result.returncode, 0)
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "queued")
        self.assertFalse(any(
            entry.get("event") == "launched" for entry in state["history"]
        ))

    def test_review_result_without_report_fails(self):
        project = self.make_project()
        worker = self.write_worker(RESULT_WITHOUT_REPORT_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "missing report", ["src/**"])
        self.baton(project, "run", task_id, check=True)
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "missing_review_report")

    def test_finalization_rejects_report_drift_after_finish(self):
        project = self.make_project()
        worker = self.write_worker(
            NO_CHANGE_WORKER + '\nreport.write_text("# malformed after finish\\n")\n'
        )
        self.configure(project, worker)
        task_id = self.create_task(project, "report drift after finish", ["drift/**"])
        self.baton(project, "run", task_id, check=True)
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "invalid_review_report")

    def test_malformed_result_and_directory_report_are_rejected(self):
        project = self.make_project()
        worker = self.write_worker(MALFORMED_OUTPUT_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "malformed output", ["src/**"])
        self.baton(project, "run", task_id, check=True)
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "invalid_worker_output")
        self.assertIsInstance(state["last_note"], str)

    def test_finalization_rejects_nonexact_and_invalid_timestamp_result_schema(self):
        mutations = (
            ("extra-field", "result['forged'] = True\n"),
            ("invalid-time", "result['at'] = '2026-02-30T00:00:00Z'\n"),
        )
        for name, mutation in mutations:
            with self.subTest(case=name):
                worker = self.write_worker(NO_CHANGE_WORKER + f'''\nimport json
result_path = rd / "work" / tid / f"attempt-{{attempt}}.result.json"
result = json.loads(result_path.read_text())
{mutation}result_path.write_text(json.dumps(result))
''')
                project = self.make_project("finalize-schema-" + name)
                self.configure(project, worker)
                task_id = self.create_task(project, name)
                self.baton(project, "run", task_id, check=True)
                state = self.state(project, task_id)
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["last_note"], "invalid_worker_output")
                self.assertFalse(
                    (project / ".baton" / "work" / task_id
                     / "review-brief-token.json").exists()
                )

    def test_valid_submission_survives_nonzero_exit_with_review_warning(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "submitted before exit", ["src/**"])
        self.baton(project, "run", task_id, env={"EXIT_CODE": "1"}, check=True)

        state = self.state(project, task_id)
        warning = "worker_exit_1_after_submission"
        self.assertEqual(state["status"], "needs_review")
        self.assertEqual(state["warning"], warning)
        worker_exit = state["history"][-1]
        self.assertEqual(worker_exit["event"], "worker_exited")
        self.assertEqual(worker_exit["exit_code"], 1)
        self.assertEqual(worker_exit["warning"], warning)

        status = self.baton(project, "status", check=True)
        self.assertIn(f"WARNING: {warning}", status.stdout)
        brief, token = self.review_brief_token(project, task_id)
        self.assertIn("WARNING: Worker exited with code 1 after submission", brief.stdout)
        self.assertIn(
            f".baton/work/{task_id}/attempt-1.log", brief.stdout,
        )
        self.baton(
            project, "task", "accept", task_id, "--brief", token, check=True,
        )
        self.assertEqual(self.state(project, task_id)["status"], "done")

    def test_nonzero_exit_without_result_keeps_worker_exit_failure(self):
        project = self.make_project()
        self.configure(project, self.write_worker("raise SystemExit(1)\n"))
        task_id = self.create_task(project, "exit without result", ["src/**"])
        self.baton(project, "run", task_id, check=True)
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "worker_exit_1")
        self.assertNotIn("warning", state)

    def test_nonzero_exit_with_malformed_result_is_invalid_output(self):
        project = self.make_project()
        worker = self.write_worker(MALFORMED_OUTPUT_WORKER + "\nraise SystemExit(1)\n")
        self.configure(project, worker)
        task_id = self.create_task(project, "malformed before exit", ["src/**"])
        self.baton(project, "run", task_id, check=True)
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "invalid_worker_output")
        self.assertNotIn("warning", state)

    def test_timeout_overrides_valid_submission(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker, max_parallel=1, timeout_minutes=0.02)
        task_id = self.create_task(project, "submitted before timeout", ["src/**"])
        self.baton(
            project, "run", task_id, env={"SLEEP_AFTER_FINISH": "10"}, check=True,
        )
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "worker_timeout")
        self.assertNotIn("warning", state)

    def test_nonzero_exit_does_not_override_changed_paths_mismatch(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER.replace(
            "for path in changed:\n",
            "changed.append('src/not-observed.txt')\nfor path in changed:\n",
        ))
        self.configure(project, worker)
        task_id = self.create_task(project, "mismatch before exit", ["src/**"])
        self.baton(project, "run", task_id, env={"EXIT_CODE": "1"}, check=True)
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "changed_paths_mismatch")
        self.assertNotIn("warning", state)

    def test_non_utf8_result_fails_without_stale_runner(self):
        project = self.make_project()
        worker = self.write_worker(NON_UTF8_RESULT_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "non utf8 output", ["src/**"])
        self.baton(project, "run", task_id, check=True)
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "invalid_worker_output")
        self.assertNotIn("runner", state)

    def test_oversized_integer_result_fails_without_stale_runner(self):
        project = self.make_project()
        worker = self.write_worker(OVERSIZED_INTEGER_RESULT_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "oversized integer output", ["src/**"])
        self.baton(project, "run", task_id, check=True)
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
        directory = project / ".baton" / "work" / task_id
        directory.mkdir(parents=True)
        (directory / "attempt-1.log").symlink_to(sentinel)
        result = self.baton(project, "run", task_id)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(), "unchanged\n")
        self.assertEqual(self.state(project, task_id)["status"], "queued")

    def test_cross_scope_write_fails_changed_path_attribution(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker, max_parallel=2)
        alpha = self.create_task(project, "alpha", ["alpha/**"])
        beta = self.create_task(project, "beta", ["beta/**"])
        result = self.baton(
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
        runtime = project / ".baton"
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
        baton_module = runpy.run_path(str(SOURCE_BATON), run_name="baton_module")
        finalized = baton_module["finalize_task"](
            str(runtime), old_task, {"returncode": 0}, [], [],
            "baseline", "old", True,
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
            "[tiers.test]\n\n"
            "[limits]\n"
            "max_parallel = 1\n"
            "worker_timeout_minutes = 1\n"
        )
        (project / ".baton" / "config.toml").write_text(config)
        marker = self.base / "injected"
        scope = f"safe/$(touch {marker})/**"
        task_id = self.create_task(project, "literal prompt", [scope])
        self.baton(project, "run", task_id, check=True)
        self.assertFalse(marker.exists())
        self.assertEqual(self.state(project, task_id)["status"], "needs_review")

    def test_task_creation_requires_explicit_difficulty(self):
        project = self.make_project()
        missing = self.baton(project, "task", "create", "--title", "implicit")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("--tier", missing.stderr)

        rejected_default = self.try_create_task(project, "removed route", tier="default")
        self.assertNotEqual(rejected_default.returncode, 0)
        self.assertIn("unknown tier 'default'", rejected_default.stderr)

        config = project / ".baton" / "config.toml"
        config.write_text(config.read_text() + (
            '\n[tiers.explicit]\ncommand = "/usr/bin/true {prompt_file}"\n'
        ))
        task_id = self.create_task(project, "explicit", tier="explicit")
        self.assertEqual(self.state(project, task_id)["tier"], "explicit")

        state_path = project / ".baton" / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state.pop("tier")
        state_path.write_text(json.dumps(state))
        launch = self.baton(project, "run", task_id, "--dry-run")
        self.assertNotEqual(launch.returncode, 0)
        self.assertIn("tier name must be non-blank", launch.stdout + launch.stderr)
        self.assertEqual(self.state(project, task_id)["status"], "queued")

    def test_difficulty_and_worker_label_are_visible_before_and_during_launch(self):
        project = self.make_project()
        worker = self.write_worker(NO_CHANGE_WORKER)
        self.configure(project, worker)
        config = project / ".baton" / "config.toml"
        tier_command = f"{sys.executable} {worker} --api-key do-not-print {{prompt_file}}"
        config.write_text(config.read_text() + (
            "\n[tiers.hard]\n"
            f"command = {json.dumps(tier_command)}\n"
            "\n[tiers.hard.display]\n"
            'model = "GPT 5.6 Sol"\n'
            'harness = "Hermes"\n'
            'effort = "high"\n'
            'engineering_role = "elite senior"\n'
        ))

        created = self.baton(
            project, "task", "create", "--title", "Visible routing",
            "--scope", "routing/**", "--tier", "hard", check=True,
        )
        task_id = created.stdout.split()[1]
        spec = project / ".baton" / "tasks" / f"{task_id}.md"
        spec.write_text(spec.read_text().replace(
            "Replace this line with one clear outcome.", "Verify visible routing.",
        ).replace(
            "- Add observable requirements.", "- Routing is visible.",
        ))
        identity = (
            f"{task_id} | title=Visible routing | difficulty=hard | "
            "worker=model=GPT 5.6 Sol; harness=Hermes; effort=high; "
            "engineering_role=elite senior"
        )
        outputs = [
            created.stdout,
            self.baton(project, "task", "list", check=True).stdout,
            self.baton(project, "task", "list", "--json", check=True).stdout,
            self.baton(project, "task", "show", task_id, check=True).stdout,
            self.baton(project, "status", check=True).stdout,
            self.baton(project, "run", task_id, "--dry-run", check=True).stdout,
            self.baton(project, "tiers", check=True).stdout,
        ]
        for output in outputs:
            self.assertIn("difficulty=hard", output)
            self.assertIn("GPT 5.6 Sol", output)
            self.assertNotIn("--api-key", output)
            self.assertNotIn("do-not-print", output)
        for output in outputs[:6]:
            self.assertIn(task_id, output)
        for output in (outputs[0], outputs[1], outputs[2], outputs[3], outputs[4]):
            self.assertIn("Visible routing", output)
        self.assertIn(identity, created.stdout)

        launched = self.baton(project, "run", task_id, check=True)
        self.assertIn(identity, launched.stdout)
        self.assertNotIn("--api-key", launched.stdout)
        self.assertNotIn("do-not-print", launched.stdout)
        self.assertEqual(self.state(project, task_id)["status"], "needs_review")

    def test_launch_prompt_records_effective_difficulty_and_safe_worker_label(self):
        project = self.make_project()
        worker = self.write_worker(NO_CHANGE_WORKER)
        self.configure(project, worker)
        config = project / ".baton" / "config.toml"
        command = f"{sys.executable} {worker} --credential hidden {{prompt_file}}"
        config.write_text(config.read_text() + (
            "\n[tiers.medium]\n"
            f"command = {json.dumps(command)}\n"
            "\n[tiers.medium.display]\n"
            'model = "GPT 5.6 Sol"\n'
            'harness = "Hermes"\n'
            'effort = "medium"\n'
            'engineering_role = "elite senior"\n'
        ))
        task_id = self.create_task(project, "prompt routing", tier="medium")
        self.baton(project, "run", task_id, check=True)

        prompt = (
            project / ".baton" / "work" / task_id / "attempt-1.prompt.md"
        ).read_text()
        self.assertIn("Difficulty: medium", prompt)
        self.assertIn(
            "Worker: model=GPT 5.6 Sol; harness=Hermes; effort=medium; "
            "engineering_role=elite senior",
            prompt,
        )
        self.assertNotIn("--credential", prompt)
        self.assertNotIn("hidden", prompt)

    def test_worker_label_metadata_is_bounded_sanitized_and_has_safe_fallback(self):
        project = self.make_project()
        worker = self.write_worker(NO_CHANGE_WORKER)
        self.configure(project, worker)
        config = project / ".baton" / "config.toml"
        config.write_text(config.read_text() + (
            "\n[tiers.safe]\n"
            "\n[tiers.safe.display]\n"
            'model = "api_key=worker-secret"\n'
            'harness = "Hermes"\n'
            'effort = "high"\n'
            'engineering_role = "elite senior"\n'
            'fallback = "GPT 5.6 Terra/high when Claude usage is exhausted"\n'
        ))

        fallback_created = self.baton(
            project, "task", "create", "--title", "fallback label",
            "--tier", "test", check=True,
        )
        self.assertIn("difficulty=test | worker=unlabeled worker", fallback_created.stdout)
        safe_created = self.baton(
            project, "task", "create", "--title", "safe label",
            "--tier", "safe", check=True,
        )
        self.assertIn("model=api_key=[redacted]", safe_created.stdout)
        self.assertIn("engineering_role=elite senior", safe_created.stdout)
        self.assertNotIn("worker-secret", safe_created.stdout)
        self.assertNotIn("--", safe_created.stdout)

        tier_output = self.baton(project, "tiers", check=True).stdout
        safe_routing = next(
            line for line in tier_output.splitlines()
            if line.startswith("Routing: difficulty=safe")
        )
        worker_label = safe_routing.split("worker=", 1)[1]
        self.assertLessEqual(len(worker_label), 240)
        self.assertNotIn("worker-secret", tier_output)

        invalid_values = (
            ('display = "not-a-table"\n', "must be a table"),
            ('[tiers.bad.display]\nmodel = "' + "x" * 81 + '"\n', "exceeds 80"),
            ('[tiers.bad.display]\nharness = "Hermes\\u001b"\n', "control characters"),
            ('[tiers.bad.display]\nfallback = "--api-key hidden"\n', "command flags"),
            ('[tiers.bad.display]\nunknown = "value"\n', "unknown tier display metadata"),
        )
        for index, (display_config, message) in enumerate(invalid_values):
            with self.subTest(message=message):
                invalid = self.make_project(f"invalid-display-{index}")
                self.configure(invalid, worker)
                invalid_config = invalid / ".baton" / "config.toml"
                invalid_config.write_text(invalid_config.read_text() + (
                    "\n[tiers.bad]\n" + display_config
                ))
                validation = self.baton(invalid, "validate")
                tiers = self.baton(invalid, "tiers")
                self.assertNotEqual(validation.returncode, 0)
                self.assertNotEqual(tiers.returncode, 0)
                self.assertIn(message, validation.stdout + validation.stderr)
                self.assertIn(message, tiers.stdout + tiers.stderr)
                self.assertNotIn("hidden", tiers.stdout)

    def test_tier_display_rejects_command_flags_after_punctuation(self):
        worker = self.write_worker(NO_CHANGE_WORKER)
        for index, value in enumerate(("x;--flag", "(--flag)", "x --flag")):
            with self.subTest(value=value):
                project = self.make_project(f"punctuated-display-flag-{index}")
                self.configure(project, worker)
                config = project / ".baton" / "config.toml"
                config.write_text(config.read_text() + (
                    "\n[tiers.audit]\n\n[tiers.audit.display]\n"
                    f"model = {json.dumps(value)}\n"
                ))
                validation = self.baton(project, "validate")
                tiers = self.baton(project, "tiers")
                self.assertEqual(validation.returncode, 1)
                self.assertEqual(tiers.returncode, 1)
                self.assertIn("command flags", validation.stdout)
                self.assertIn("command flags", tiers.stderr)

        valid = self.make_project("hyphenated-display-prose")
        self.configure(valid, worker)
        config = valid / ".baton" / "config.toml"
        config.write_text(config.read_text() + (
            "\n[tiers.audit]\n\n[tiers.audit.display]\n"
            'model = "ordinary hyphenated-prose"\n'
        ))
        self.baton(valid, "validate", check=True)

    def test_tiers_are_strict_at_create_validate_preview_and_launch(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        config = project / ".baton" / "config.toml"
        config.write_text(config.read_text() + "\n[tiers.premium]\ncapsule_max_chars = 5000\n")

        unknown = self.try_create_task(project, "unknown tier", tier="mystery")
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("unknown tier 'mystery'", unknown.stderr)
        self.assertIn("known tiers: premium, test", unknown.stderr)
        blank = self.try_create_task(project, "blank tier", tier="")
        self.assertNotEqual(blank.returncode, 0)
        self.assertIn("tier name must be non-blank", blank.stderr)

        task_id = self.create_task(
            project, "strict premium", ["premium/**"], tier="premium",
        )
        brief = self.baton(
            project, "orchestrator", "brief", "--phase", "run", check=True,
        )
        dry = self.baton(project, "run", "--dry-run", check=True)
        annotation = (
            f"{task_id} | title=strict premium | difficulty=premium | "
            "worker=unlabeled worker"
        )
        self.assertIn("Would run: " + annotation, brief.stdout)
        self.assertIn("would run: " + annotation, dry.stdout)

        state_path = project / ".baton" / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["tier"] = "removed"
        state_path.write_text(json.dumps(state))
        validation = self.baton(project, "validate")
        preview = self.baton(project, "task", "capsule", task_id)
        launch = self.baton(project, "run", task_id)
        for result in (validation, preview, launch):
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unknown tier 'removed'", result.stdout + result.stderr)
        self.assertEqual(self.state(project, task_id)["status"], "queued")

    def test_per_tier_capsule_budget_controls_preview_validate_and_launch(self):
        roomy = self.make_project("roomy-tier")
        self.configure(
            roomy, self.write_worker(NO_CHANGE_WORKER), capsule_max_chars=100,
        )
        config = roomy / ".baton" / "config.toml"
        config.write_text(config.read_text() + "\n[tiers.roomy]\ncapsule_max_chars = 4000\n")
        roomy_id = self.create_task(roomy, "roomy capsule", ["roomy/**"], tier="roomy")
        preview = self.baton(roomy, "task", "capsule", roomy_id, check=True)
        self.assertIn("of 4000 chars", preview.stdout)
        self.baton(roomy, "validate", check=True)
        self.baton(roomy, "run", roomy_id, check=True)
        self.assertEqual(self.state(roomy, roomy_id)["status"], "needs_review")

        tight = self.make_project("tight-tier")
        self.configure(tight, self.write_worker(NO_CHANGE_WORKER), capsule_max_chars=4000)
        config = tight / ".baton" / "config.toml"
        config.write_text(config.read_text() + "\n[tiers.tight]\ncapsule_max_chars = 100\n")
        tight_id = self.create_task(tight, "tight capsule", ["tight/**"], tier="tight")
        results = (
            self.baton(tight, "task", "capsule", tight_id),
            self.baton(tight, "validate"),
            self.baton(tight, "run", tight_id),
        )
        for result in results:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("capsule_max_chars=100", result.stdout + result.stderr)
        self.assertEqual(self.state(tight, tight_id)["status"], "queued")

    def test_one_wave_uses_each_limits_only_tier_timeout(self):
        project = self.make_project()
        self.configure(
            project, self.write_worker(TIER_TIMEOUT_WORKER),
            max_parallel=2, timeout_minutes=1,
        )
        config = project / ".baton" / "config.toml"
        config.write_text(config.read_text() + (
            "\n[tiers.short]\nworker_timeout_minutes = 0.005\n"
            "\n[tiers.long]\nworker_timeout_minutes = 0.1\n"
        ))
        short = self.create_task(project, "short timeout", ["short/**"], tier="short")
        long = self.create_task(project, "long timeout", ["long/**"], tier="long")
        self.baton(project, "run", short, long, check=True)
        self.assertEqual(self.state(project, short)["status"], "failed")
        self.assertEqual(self.state(project, short)["last_note"], "worker_timeout")
        self.assertEqual(self.state(project, long)["status"], "needs_review")

    def test_validate_rejects_non_object_task_state_and_malformed_history(self):
        project = self.make_project("non-object-task-state")
        task_id = self.create_task(project, "non object task state")
        state_path = project / ".baton" / "tasks" / f"{task_id}.json"
        state_path.write_text("[]\n")
        validation = self.baton(project, "validate")
        self.assertEqual(validation.returncode, 1)
        self.assertIn("PROBLEM: cannot read task state:", validation.stdout)
        self.assertIn("must contain a JSON object", validation.stdout)
        self.assertNotIn("Traceback", validation.stderr)
        for command in (("task", "list"), ("status",), ("stats",)):
            rejected = self.baton(project, *command)
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("must contain a JSON object", rejected.stderr)
            self.assertNotIn("Traceback", rejected.stderr)

        malformed_entries = (1, None, [], "entry", {}, {"event": 1, "at": []})
        for index, entry in enumerate(malformed_entries):
            with self.subTest(entry=entry):
                project = self.make_project(f"malformed-history-{index}")
                task_id = self.create_task(project, f"malformed history {index}")
                state_path = project / ".baton" / "tasks" / f"{task_id}.json"
                state = json.loads(state_path.read_text())
                state["history"] = [entry]
                state_path.write_text(json.dumps(state))
                validation = self.baton(project, "validate")
                self.assertEqual(validation.returncode, 1)
                self.assertIn("history entry 0", validation.stdout)
                self.assertNotIn("Traceback", validation.stderr)
                if not isinstance(entry, dict):
                    status = self.baton(project, "status")
                    self.assertEqual(status.returncode, 1)
                    self.assertIn("history entry 0", status.stderr)
                    self.assertNotIn("Traceback", status.stderr)

    def test_validate_needs_review_with_malformed_scalar_history_is_diagnostic(self):
        project = self.make_project("needs-review-malformed-scalar-history")
        self.configure(project, self.write_worker(GOOD_WORKER))
        config = project / ".baton" / "config.toml"
        config.write_text(
            config.read_text() + "\n[gates]\nreport_requires_sections = true\n"
        )
        task_id = self.create_task(project, "needs review malformed scalar history")
        self.baton(project, "run", task_id, check=True)
        state_path = project / ".baton" / "tasks" / f"{task_id}.json"
        valid_state = json.loads(state_path.read_text())
        self.baton(project, "validate", check=True)
        report = project / ".baton" / "work" / task_id / "attempt-1.report.md"
        report.write_text("# Incomplete review report\n")

        malformed_histories = ([1], [{"event": "worker_exited", "at": []}])
        for history in malformed_histories:
            with self.subTest(history=history):
                state = dict(valid_state)
                state["history"] = history
                state_path.write_text(json.dumps(state))
                state_before = state_path.read_bytes()

                validation = self.baton(project, "validate")

                output = validation.stdout + validation.stderr
                self.assertEqual(validation.returncode, 1, output)
                self.assertIn(f"{task_id}: history entry 0", validation.stdout, output)
                self.assertIn(
                    f"{task_id}: missing required report section `## Result`",
                    validation.stdout,
                    output,
                )
                self.assertNotIn("Traceback", output)
                self.assertEqual(state_path.read_bytes(), state_before)
                self.assertFalse(
                    (project / ".baton" / "work" / task_id
                     / "review-brief-token.json").exists()
                )

    def test_validate_rejects_malformed_active_task_field_shapes(self):
        cases = (
            ("id", ["bad"], "task id must look like"),
            ("depends_on", 1, "depends_on must be a list of task ids"),
            ("scope", 1, "scope must be a list"),
        )
        for index, (field, value, message) in enumerate(cases):
            with self.subTest(field=field, value=value):
                project = self.make_project(f"malformed-active-{index}")
                task_id = self.create_task(project, f"malformed active {index}")
                state_path = project / ".baton" / "tasks" / f"{task_id}.json"
                state = json.loads(state_path.read_text())
                state[field] = value
                state_path.write_text(json.dumps(state))
                validation = self.baton(project, "validate")
                self.assertEqual(validation.returncode, 1)
                self.assertIn("PROBLEM:", validation.stdout)
                self.assertIn(message, validation.stdout)
                self.assertNotIn("ok:", validation.stdout)
                self.assertNotIn("Traceback", validation.stdout + validation.stderr)

        project = self.make_project("aggregate-malformed-active")
        task_id = self.create_task(project, "aggregate malformed active")
        state_path = project / ".baton" / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state.update({
            "status": 7,
            "attempt": False,
            "tier": [],
            "title": 1,
            "scope": 1,
            "depends_on": 1,
            "history": "bad",
        })
        state_path.write_text(json.dumps(state))
        validation = self.baton(project, "validate")
        self.assertEqual(validation.returncode, 1)
        for message in (
                "invalid status", "attempt must be a positive integer",
                "tier must be non-empty text", "title must be non-empty text",
                "scope must be a list", "depends_on must be a list of task ids",
                "history must be a list"):
            self.assertIn(message, validation.stdout)
        self.assertNotIn("Traceback", validation.stdout + validation.stderr)

    def test_validate_rejects_malformed_archived_task_field_shapes(self):
        cases = (
            ("scope", 1, "scope must be a list"),
            ("depends_on", 1, "depends_on must be a list of task ids"),
            ("status", 7, "invalid status"),
            ("attempt", False, "attempt must be a positive integer"),
        )
        for index, (field, value, message) in enumerate(cases):
            with self.subTest(field=field, value=value):
                project = self.make_project(f"malformed-archive-{index}")
                archived_id = self.create_task(project, f"archived source {index}")
                state_path = project / ".baton" / "tasks" / f"{archived_id}.json"
                state = json.loads(state_path.read_text())
                state["status"] = "done"
                state_path.write_text(json.dumps(state))
                self.baton(project, "archive", check=True)
                dependent_id = self.create_task(
                    project, f"active dependent {index}", depends_on=[archived_id],
                )
                archive_path = project / ".baton" / "archive" / f"{archived_id}.json"
                archived = json.loads(archive_path.read_text())
                archived[field] = value
                archive_path.write_text(json.dumps(archived))

                validation = self.baton(project, "validate")
                self.assertEqual(validation.returncode, 1)
                self.assertIn("PROBLEM:", validation.stdout)
                self.assertIn(message, validation.stdout)
                self.assertNotIn("ok:", validation.stdout)
                self.assertNotIn("Traceback", validation.stdout + validation.stderr)
                if field == "status":
                    self.assertIn(
                        f"{dependent_id}: dependency {archived_id} has invalid status",
                        validation.stdout,
                    )

    def test_validate_rejects_nonterminal_archived_task_status(self):
        for status in ("queued", "needs_review", "failed", "running"):
            with self.subTest(status=status):
                project = self.make_project("nonterminal-archive-" + status)
                task_id = self.create_task(project, "nonterminal archived " + status)
                state_path = project / ".baton" / "tasks" / f"{task_id}.json"
                state = json.loads(state_path.read_text())
                state["status"] = "done"
                state_path.write_text(json.dumps(state))
                self.baton(project, "archive", check=True)

                archive_path = project / ".baton" / "archive" / f"{task_id}.json"
                archived = json.loads(archive_path.read_text())
                archived["status"] = status
                archive_path.write_text(json.dumps(archived))

                validation = self.baton(project, "validate")
                self.assertEqual(validation.returncode, 1)
                self.assertIn(
                    f"{task_id}: archived task status must be done or cancelled",
                    validation.stdout,
                )
                if status == "queued":
                    rejected_run = self.baton(project, "run", task_id)
                    self.assertEqual(rejected_run.returncode, 1)
                    self.assertIn(
                        f"no such active task: {task_id}", rejected_run.stderr,
                    )

    def test_valid_archived_done_and_cancelled_records_validate(self):
        project = self.make_project()
        done_id = self.create_task(project, "valid archived done")
        done_path = project / ".baton" / "tasks" / f"{done_id}.json"
        done = json.loads(done_path.read_text())
        done["status"] = "done"
        done_path.write_text(json.dumps(done))
        cancelled_id = self.create_task(project, "valid archived cancelled")
        self.baton(
            project, "task", "cancel", cancelled_id, "--reason", "not needed",
            check=True,
        )
        self.baton(project, "archive", check=True)
        validation = self.baton(project, "validate")
        self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)
        self.assertEqual(validation.stdout, "ok: 0 active task(s)\n")

    def test_validate_rejects_missing_needs_review_result_before_review(self):
        project = self.make_project("missing-review-result-validation")
        self.configure(project, self.write_worker(GOOD_WORKER))
        task_id = self.create_task(project, "missing review result", ["review/**"])
        self.baton(project, "run", task_id, check=True)
        work = project / ".baton" / "work" / task_id
        (work / "attempt-1.result.json").unlink()

        review = self.baton(
            project, "orchestrator", "brief", "--phase", "review", task_id,
        )
        self.assertEqual(review.returncode, 1)
        self.assertIn("No such file or directory", review.stderr)
        validation = self.baton(project, "validate")
        self.assertEqual(validation.returncode, 1)
        self.assertIn(f"{task_id}: review result", validation.stdout)

    def test_validate_enforces_review_report_section_gate_with_brief_parity(self):
        project = self.make_project("validate-report-section-gate")
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        config = project / ".baton" / "config.toml"
        config.write_text(
            config.read_text() + "\n[gates]\nreport_requires_sections = true\n"
        )
        task_id = self.create_task(project, "validate report sections")
        self.baton(project, "run", task_id, check=True)
        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        work = runtime / "work" / task_id
        (work / "attempt-1.report.md").write_text(
            "# Regular but incomplete report\n\nWork completed successfully.\n"
        )
        state_before = state_path.read_bytes()

        review = self.baton(
            project, "orchestrator", "brief", "--phase", "review", task_id,
        )
        self.assertEqual(review.returncode, 1)
        validation = self.baton(project, "validate")
        self.assertEqual(validation.returncode, 1)
        for section in ("Result", "Changes", "Verification", "Decisions and risks"):
            problem = f"missing required report section `## {section}`"
            self.assertIn(problem, review.stderr)
            self.assertIn(f"{task_id}: {problem}", validation.stdout)
        self.assertNotIn("Traceback", validation.stdout + validation.stderr)
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertFalse((work / "review-brief-token.json").exists())

    def test_validate_report_gate_compatibility_controls(self):
        gate_off = self.make_project("validate-report-gate-off")
        self.configure(gate_off, self.write_worker(NO_CHANGE_WORKER))
        config = gate_off / ".baton" / "config.toml"
        config.write_text(
            config.read_text() + "\n[gates]\nreport_requires_sections = false\n"
        )
        task_id = self.create_task(gate_off, "validate free-form report")
        self.baton(gate_off, "run", task_id, check=True)
        report = (
            gate_off / ".baton" / "work" / task_id / "attempt-1.report.md"
        )
        report.write_text("Free-form review report.\n")
        self.baton(gate_off, "validate", check=True)

        non_review = self.make_project("validate-non-review-states")
        for status in ("needs_decision", "blocked", "failed"):
            task_id = self.create_task(non_review, "validate " + status)
            state_path = non_review / ".baton" / "tasks" / f"{task_id}.json"
            state = json.loads(state_path.read_text())
            state["status"] = status
            state_path.write_text(json.dumps(state))
        self.baton(non_review, "validate", check=True)

    def test_validate_rejects_unusable_needs_review_evidence_shapes(self):
        cases = (
            ("missing-report", "report", "missing", "review report"),
            ("empty-report", "report", "empty", "review report"),
            ("directory-report", "report", "directory", "review report"),
            ("missing-diff", "diff", "missing", "review diff"),
            ("directory-diff", "diff", "directory", "review diff"),
            ("malformed-result", "result", "malformed", "review result JSON"),
            ("wrong-type-result", "result", "wrong-type", "JSON object"),
            ("directory-result", "result", "directory", "review result"),
            ("wrong-changed-paths", "result", "wrong-paths", "changed_paths"),
            ("wrong-observed-paths", "state", "wrong-observed", "observed changed paths"),
        )
        for name, artifact, mutation, message in cases:
            with self.subTest(mutation=mutation):
                project = self.make_project(name)
                self.configure(project, self.write_worker(GOOD_WORKER))
                task_id = self.create_task(project, name, ["evidence/**"])
                self.baton(project, "run", task_id, check=True)
                runtime = project / ".baton"
                state_path = runtime / "tasks" / f"{task_id}.json"
                work = runtime / "work" / task_id
                artifact_path = {
                    "report": work / "attempt-1.report.md",
                    "result": work / "attempt-1.result.json",
                    "diff": work / "attempt-1.diff",
                }.get(artifact)
                if mutation == "missing":
                    artifact_path.unlink()
                elif mutation == "empty":
                    artifact_path.write_text("")
                elif mutation == "directory":
                    artifact_path.unlink()
                    artifact_path.mkdir()
                elif mutation == "malformed":
                    artifact_path.write_text("{")
                elif mutation == "wrong-type":
                    artifact_path.write_text("[]")
                elif mutation == "wrong-paths":
                    result = json.loads(artifact_path.read_text())
                    result["changed_paths"] = "evidence/not-a-list.txt"
                    artifact_path.write_text(json.dumps(result))
                else:
                    state = json.loads(state_path.read_text())
                    state["history"][-1]["observed_paths"] = "evidence/not-a-list.txt"
                    state_path.write_text(json.dumps(state))

                state_before = state_path.read_bytes()
                validation = self.baton(project, "validate")
                self.assertEqual(validation.returncode, 1)
                self.assertIn(f"{task_id}:", validation.stdout)
                self.assertIn(message, validation.stdout)
                self.assertNotIn("Traceback", validation.stdout + validation.stderr)
                self.assertEqual(state_path.read_bytes(), state_before)
                self.assertFalse((work / "review-brief-token.json").exists())

    def test_validate_aggregates_review_evidence_problems_and_accepts_legacy_empty_diff(self):
        invalid = self.make_project("aggregate-review-evidence")
        self.configure(invalid, self.write_worker(NO_CHANGE_WORKER))
        invalid_id = self.create_task(invalid, "aggregate review evidence")
        self.baton(invalid, "run", invalid_id, check=True)
        invalid_runtime = invalid / ".baton"
        invalid_work = invalid_runtime / "work" / invalid_id
        report = invalid_work / "attempt-1.report.md"
        report.write_text("# Incomplete report\n\nEvidence summary.\n")
        (invalid_work / "attempt-1.result.json").write_text("[]")
        diff = invalid_work / "attempt-1.diff"
        diff.unlink()
        diff.mkdir()
        state_path = invalid_runtime / "tasks" / f"{invalid_id}.json"
        state = json.loads(state_path.read_text())
        state["history"][-1]["observed_paths"] = 1
        state_path.write_text(json.dumps(state))
        state_before = state_path.read_bytes()

        validation = self.baton(invalid, "validate")
        self.assertEqual(validation.returncode, 1)
        for message in (
                "missing required report section `## Result`", "review diff", "JSON object",
                "observed changed paths"):
            self.assertIn(message, validation.stdout)
        self.assertNotIn("Traceback", validation.stdout + validation.stderr)
        self.assertEqual(state_path.read_bytes(), state_before)

        legacy = self.make_project("legacy-review-evidence")
        self.configure(legacy, self.write_worker(NO_CHANGE_WORKER))
        legacy_id = self.create_task(legacy, "legacy empty review evidence")
        self.baton(legacy, "run", legacy_id, check=True)
        legacy_runtime = legacy / ".baton"
        legacy_work = legacy_runtime / "work" / legacy_id
        self.assertEqual((legacy_work / "attempt-1.diff").read_text(), "")
        legacy_state_path = legacy_runtime / "tasks" / f"{legacy_id}.json"
        legacy_state = json.loads(legacy_state_path.read_text())
        legacy_state["history"][-1].pop("observed_paths")
        legacy_state_path.write_text(json.dumps(legacy_state))
        self.baton(legacy, "validate", check=True)

    def test_validate_reports_every_malformed_unused_tier_setting_and_name(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        config = project / ".baton" / "config.toml"
        config.write_text(config.read_text() + (
            "\n[tiers.broken]\n"
            "command = \"\"\n"
            "worker_timeout_minutes = \"never\"\n"
            "capsule_max_chars = true\n"
            "\n[tiers.\"\"]\ncapsule_max_chars = 100\n"
            "\n[tiers.default]\ncapsule_max_chars = 200\n"
        ))
        validation = self.baton(project, "validate")
        self.assertNotEqual(validation.returncode, 0)
        for message in (
                "tier 'broken': no worker command configured",
                "worker_timeout_minutes must be a finite non-negative number",
                "capsule_max_chars must be a positive integer",
                "tier name must be non-blank",
                "tier name 'default' is reserved"):
            self.assertIn(message, validation.stdout)

    def test_non_finite_global_and_per_tier_timeouts_are_rejected(self):
        message = "worker_timeout_minutes must be a finite non-negative number"
        for value in ("nan", "inf"):
            for location in ("global", "tier"):
                with self.subTest(value=value, location=location):
                    project = self.make_project(f"non-finite-{location}-{value}")
                    self.configure(project, self.write_worker(NO_CHANGE_WORKER))
                    config = project / ".baton" / "config.toml"
                    if location == "global":
                        config.write_text(config.read_text().replace(
                            "worker_timeout_minutes = 1",
                            f"worker_timeout_minutes = {value}",
                        ))
                    else:
                        config.write_text(config.read_text() + (
                            f"\n[tiers.bad]\nworker_timeout_minutes = {value}\n"
                        ))

                    validation = self.baton(project, "validate")
                    self.assertEqual(validation.returncode, 1)
                    self.assertIn(message, validation.stdout)
                    tiers = self.baton(project, "tiers")
                    self.assertEqual(tiers.returncode, 1)
                    self.assertIn(message, tiers.stderr)
                    self.assertNotIn(f"{value} minutes", tiers.stdout)

    def test_tiers_output_is_exact_read_only_redacted_and_worker_denied(self):
        project = self.make_project()
        worker = self.write_worker(NO_CHANGE_WORKER)
        self.configure(project, worker, timeout_minutes=2.5, capsule_max_chars=4000)
        config = project / ".baton" / "config.toml"
        tier_command = f"{sys.executable} {worker} --secret do-not-print {{prompt_file}}"
        config.write_text(config.read_text() + (
            "\n[tiers.alpha]\ncapsule_max_chars = 5000\n"
            "\n[tiers.zeta]\n"
            f"command = {json.dumps(tier_command)}\n"
            "worker_timeout_minutes = 0\n"
        ))
        runtime = project / ".baton"

        def snapshot():
            return {
                path.relative_to(runtime).as_posix(): path.read_bytes()
                for path in runtime.rglob("*") if path.is_file()
            }

        before = snapshot()
        tiers = self.baton(project, "tiers", check=True)
        executable = sys.executable
        tier_blocks = (
            "Tier: alpha\nRouting: difficulty=alpha | worker=unlabeled worker\n"
            f"Executable: {executable}\nCommand source: global\n"
            "Worker timeout: 2.5 minutes\nCapsule budget: 5000 characters\n\n"
            "Tier: test\nRouting: difficulty=test | worker=unlabeled worker\n"
            f"Executable: {executable}\nCommand source: global\n"
            "Worker timeout: 2.5 minutes\nCapsule budget: 4000 characters\n\n"
            "Tier: zeta\nRouting: difficulty=zeta | worker=unlabeled worker\n"
            f"Executable: {executable}\nCommand source: tier\n"
            "Worker timeout: 0 minutes\nCapsule budget: 4000 characters\n"
        )
        expected = tier_blocks + "Conventional levels missing: hard, medium, easy\n"
        self.assertEqual(tiers.stdout, expected)
        self.assertNotIn("--secret", tiers.stdout)
        self.assertNotIn("do-not-print", tiers.stdout)
        self.assertEqual(snapshot(), before)

        config.write_text(config.read_text() + "\n[tiers.hard]\ncapsule_max_chars = 4100\n")
        partial = self.baton(project, "tiers", check=True)
        self.assertTrue(partial.stdout.endswith(
            "Conventional levels missing: medium, easy\n",
        ))
        config.write_text(config.read_text() + (
            "\n[tiers.medium]\ncapsule_max_chars = 4200\n"
            "\n[tiers.easy]\ncapsule_max_chars = 4300\n"
        ))
        complete = self.baton(project, "tiers", check=True)
        self.assertNotIn("Conventional levels missing:", complete.stdout)
        denied = self.baton(
            project, "tiers", env={"BATON_TASK_ID": "T999-worker"},
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("worker processes cannot run orchestrator commands", denied.stderr)

    def test_timeout_kills_worker_process_group(self):
        project = self.make_project()
        worker = self.write_worker(TIMEOUT_WORKER)
        self.configure(project, worker, max_parallel=1, timeout_minutes=0.005)
        task_id = self.create_task(project, "timeout", ["timeout/**"])
        marker = self.base / "late-marker"
        self.baton(project, "run", task_id, env={"LATE_MARKER": marker}, check=True)
        time.sleep(0.8)
        self.assertFalse(marker.exists())
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "worker_timeout")

    def test_worker_does_not_inherit_orchestrator_stdin(self):
        project = self.make_project()
        worker = self.write_worker("import sys\n\nsys.stdin.read()\n" + GOOD_WORKER)
        self.configure(project, worker, max_parallel=1)
        task_id = self.create_task(project, "stdin insulation", ["src/**"])
        read_end, write_end = os.pipe()
        try:
            result = subprocess.run(
                [str(project / ".baton" / "baton"), "run", task_id],
                cwd=project, env=clean_test_environment(), text=True,
                stdin=read_end, capture_output=True, timeout=15,
            )
        finally:
            os.close(read_end)
            os.close(write_end)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self.state(project, task_id)
        self.assertEqual(
            state["status"], "needs_review", result.stdout + result.stderr,
        )
        self.assertNotIn(NEVER_STARTED_ADVISORY, result.stdout)

    def test_run_one_worker_launches_the_worker_with_devnull_stdin(self):
        project = self.make_project()
        runtime = project / ".baton"
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_worker_stdin_probe")
        recorded = {}

        class PopenRecorder:
            def __init__(self, argv, **kwargs):
                recorded["argv"] = argv
                recorded["kwargs"] = kwargs
                self.pid = None

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        @contextmanager
        def unlocked(*_args):
            yield

        task = {
            "id": "T900-stdin", "attempt": 1, "status": "running",
            "runner": {"lease": "stdin-probe-lease"},
        }
        globals_ = module["run_one_worker"].__globals__
        globals_["subprocess"] = SimpleNamespace(
            Popen=PopenRecorder, DEVNULL=subprocess.DEVNULL,
            STDOUT=subprocess.STDOUT, TimeoutExpired=subprocess.TimeoutExpired,
        )
        globals_["say"] = lambda *_args: None
        globals_["task_lock"] = unlocked
        globals_["load_task"] = lambda *_args: dict(task, runner=dict(task["runner"]))
        globals_["save_task"] = lambda *_args: None

        log_path = runtime / "work" / task["id"] / "attempt-1.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        prepared = {
            "task": task, "lease": task["runner"]["lease"],
            "argv": [sys.executable, "-c", "pass"], "log_path": str(log_path),
            "routing_label": "T900-stdin | stdin probe",
        }
        outcome = module["run_one_worker"](
            str(runtime), prepared, 0, threading.Event(),
        )
        self.assertEqual(outcome, {"returncode": 0})
        self.assertEqual(recorded["argv"], prepared["argv"])
        self.assertEqual(recorded["kwargs"]["stdin"], subprocess.DEVNULL)

    def test_worker_timeout_without_evidence_reports_never_started(self):
        project = self.make_project()
        worker = self.write_worker(SILENT_TIMEOUT_WORKER)
        self.configure(project, worker, max_parallel=1, timeout_minutes=0.005)
        task_id = self.create_task(project, "never started", ["src/**"])
        result = self.baton(project, "run", task_id, check=True)
        work = project / ".baton" / "work" / task_id
        self.assertEqual((work / "attempt-1.log").stat().st_size, 0)
        self.assertFalse((work / "attempt-1.briefs.json").exists())
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "worker_timeout")
        self.assertIn(NEVER_STARTED_ADVISORY, result.stdout)
        self.assertIn(NEVER_STARTED_STDIN_ADVISORY, result.stdout)

    def test_worker_timeout_with_evidence_omits_never_started_advisory(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker, max_parallel=1, timeout_minutes=0.02)
        task_id = self.create_task(project, "evidence before timeout", ["src/**"])
        result = self.baton(
            project, "run", task_id, env={"SLEEP_AFTER_FINISH": "10"}, check=True,
        )
        work = project / ".baton" / "work" / task_id
        self.assertTrue((work / "attempt-1.briefs.json").exists())
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "worker_timeout")
        self.assertNotIn(NEVER_STARTED_ADVISORY, result.stdout)
        self.assertNotIn(NEVER_STARTED_STDIN_ADVISORY, result.stdout)

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
                    [str(project / ".baton" / "baton"), "run", task_id],
                    cwd=project,
                    env=clean_test_environment({"LATE_MARKER": marker}),
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
            [str(project / ".baton" / "baton"), "run", *task_ids],
            cwd=project, env=clean_test_environment({"LATE_MARKER": marker}),
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
        self.baton(project, "run", check=True)
        self.accept_task(project, first)
        second = self.create_task(project, "second", ["second/**"], [first])
        self.baton(project, "archive", check=True)
        dry = self.baton(project, "run", "--dry-run", check=True)
        self.assertIn(f"would run: {second}", dry.stdout)
        self.assertEqual(self.baton(project, "validate").returncode, 0)

        third = self.create_task(project, "third", ["third/**"])
        self.assertTrue(third.startswith("T003-"), third)
        second_path = project / ".baton" / "tasks" / f"{second}.json"
        third_path = project / ".baton" / "tasks" / f"{third}.json"
        second_state = json.loads(second_path.read_text())
        third_state = json.loads(third_path.read_text())
        second_state["depends_on"] = [third]
        third_state["depends_on"] = [second]
        second_path.write_text(json.dumps(second_state))
        third_path.write_text(json.dumps(third_state))
        validation = self.baton(project, "validate")
        self.assertNotEqual(validation.returncode, 0)
        self.assertIn("dependency cycle", validation.stdout)

    def test_stats_empty_runtime_is_read_only_and_denied_to_workers(self):
        project = self.make_project()
        runtime = project / ".baton"

        def snapshot():
            return {
                path.relative_to(runtime).as_posix(): path.read_bytes()
                for path in runtime.rglob("*") if path.is_file()
            }

        before = snapshot()
        stats = self.baton(project, "stats", check=True)
        self.assertEqual(stats.stdout, "no task data\n")
        self.assertEqual(snapshot(), before)
        denied = self.baton(
            project, "stats",
            env={"BATON_TASK_ID": "T999-worker", "BATON_ATTEMPT": "1",
                 "BATON_LEASE": "worker"},
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("worker processes cannot run orchestrator commands", denied.stderr)

    def test_stats_exact_mixed_outcomes_and_archived_receipt_coverage(self):
        project = self.make_project()
        runtime = project / ".baton"
        task_ids = [
            self.create_task(project, "stats queued", ["queued/**"]),
            self.create_task(project, "stats failed", ["failed/**"]),
            self.create_task(project, "stats blocked", ["blocked/**"]),
            self.create_task(project, "stats archived", ["archived/**"]),
        ]
        state_paths = [runtime / "tasks" / f"{task_id}.json" for task_id in task_ids]
        states = [json.loads(path.read_text()) for path in state_paths]
        states[0]["history"].append({
            "event": "launched", "attempt": 1, "capsule_chars": 20,
        })
        states[1]["status"] = "failed"
        states[1]["attempt"] = 2
        states[1]["last_note"] = "private failure text must not appear"
        states[1]["history"].extend([
            {"event": "launched", "attempt": 1, "capsule_chars": 10},
            {"event": "worker_exited", "status": "failed", "note": "worker_timeout"},
            {"event": "launched", "attempt": 2, "capsule_chars": 30},
            {"event": "worker_exited", "status": "failed",
             "note": "private failure text must not appear"},
        ])
        states[2]["status"] = "blocked"
        states[2]["last_note"] = "scope_violation"
        states[2]["warning"] = "worker_exit_9_after_submission"
        states[2]["history"].extend([
            {"event": "launched", "attempt": 1, "capsule_chars": 40},
            {"event": "worker_exited", "status": "blocked", "note": "scope_violation",
             "warning": "worker_exit_9_after_submission"},
        ])
        states[3]["status"] = "done"
        states[3]["history"].append({
            "event": "launched", "attempt": 1, "capsule_chars": 50,
        })
        for path, state in zip(state_paths, states):
            path.write_text(json.dumps(state))

        digest = "sha256:" + "a" * 64

        def write_receipt(task_id, attempt, phases):
            work = runtime / "work" / task_id
            work.mkdir(parents=True, exist_ok=True)
            record = {
                "task_id": task_id,
                "attempt": attempt,
                "lease": f"lease-{task_id}-{attempt}",
                "capsule_digest": digest,
                "phases": {
                    phase: {"first_at": "2026-01-01T00:00:00Z",
                            "last_at": "2026-01-01T00:00:00Z", "count": 1}
                    for phase in phases
                },
            }
            (work / f"attempt-{attempt}.briefs.json").write_text(json.dumps(record))

        write_receipt(task_ids[0], 1, ("edit", "report"))
        write_receipt(task_ids[1], 1, ("edit", "verify", "report"))
        write_receipt(task_ids[1], 2, ("edit",))
        write_receipt(task_ids[3], 1, ("report",))
        self.baton(project, "archive", check=True)
        archived_receipt = (
            runtime / "archive" / f"{task_ids[3]}.work" / "attempt-1.briefs.json"
        )
        self.assertTrue(archived_receipt.exists())

        stats = self.baton(project, "stats", check=True)
        self.assertEqual(
            stats.stdout,
            "Status counts:\n"
            "- blocked=1\n"
            "- done=1\n"
            "- failed=1\n"
            "- queued=1\n"
            "Attempts histogram:\n"
            "- 1=3\n"
            "- 2=1\n"
            "Failure/blocked reason codes:\n"
            "- other=1\n"
            "- scope_violation=1\n"
            "- worker_timeout=1\n"
            "Capsule chars:\n"
            "- min=10 median=30 max=50\n"
            "Phase brief coverage (command-use evidence, not proof of attention):\n"
            "- edit=3/5\n"
            "- verify=1/5\n"
            "- report=3/5\n"
            "Post-submission warnings: 1\n",
        )
        self.assertNotIn("private failure text", stats.stdout)

    def test_stats_request_worker_count_filters_active_and_archived_tasks(self):
        project = self.make_project()
        runtime = project / ".baton"
        hard, medium, easy, other = [
            self.create_task(project, title, [f"{title}/**"])
            for title in ("hard", "medium", "easy", "other")
        ]

        medium_path = runtime / "tasks" / f"{medium}.json"
        medium_state = json.loads(medium_path.read_text())
        medium_state["status"] = "done"
        medium_state["tier"] = "medium"
        medium_state["history"] = [{"event": "launched", "attempt": 1}]
        medium_path.write_text(json.dumps(medium_state))
        (runtime / "work" / medium).mkdir(parents=True)
        self.baton(project, "archive", check=True)

        for task_id, tier, launches in (
                (hard, "hard", 2),
                (easy, "easy", 3),
                (other, [], 1)):
            path = runtime / "tasks" / f"{task_id}.json"
            state = json.loads(path.read_text())
            state["tier"] = tier
            state["history"] = [
                {"event": "launched", "attempt": attempt}
                for attempt in range(1, launches + 1)
            ]
            path.write_text(json.dumps(state))

        request_stats = self.baton(
            project, "stats",
            "--task", hard,
            "--task", medium,
            "--task", hard,
            "--task", easy,
            "--task", other,
            check=True,
        )
        self.assertEqual(
            request_stats.stdout,
            "I used 7 workers for this request: 2 on hard, 1 on medium, "
            "3 on easy, and 1 on other levels.\n",
        )
        self.assertNotIn("Status counts:", request_stats.stdout)

        aggregate = self.baton(project, "stats", check=True)
        self.assertIn("Status counts:\n", aggregate.stdout)
        self.assertNotIn("for this request", aggregate.stdout)

        unknown = self.baton(
            project, "stats", "--task", hard, "--task", "T999-unknown",
        )
        self.assertNotEqual(unknown.returncode, 0)
        self.assertEqual(unknown.stdout, "")
        self.assertIn("unknown task T999-unknown", unknown.stderr)
        self.assertNotIn("I used", unknown.stderr)

        malformed = self.baton(project, "stats", "--task", "not-a-task-id")
        self.assertNotEqual(malformed.returncode, 0)
        self.assertEqual(malformed.stdout, "")
        self.assertIn("task id must look like T001-short-slug", malformed.stderr)

        denied = self.baton(
            project, "stats", "--task", hard,
            env={"BATON_TASK_ID": "T999-worker", "BATON_ATTEMPT": "1",
                 "BATON_LEASE": "worker"},
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("worker processes cannot run orchestrator commands", denied.stderr)

    def test_archive_preflights_all_destinations_before_moving(self):
        project = self.make_project()
        task_ids = [
            self.create_task(project, "archive one", ["one/**"]),
            self.create_task(project, "archive two", ["two/**"]),
        ]
        runtime = project / ".baton"
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
        archived = self.baton(project, "archive")
        self.assertNotEqual(archived.returncode, 0)
        for task_id in task_ids:
            self.assertTrue((runtime / "tasks" / f"{task_id}.json").exists())
            self.assertFalse((runtime / "archive" / f"{task_id}.json").exists())

    def test_atomic_archive_rename_binds_linux_and_macos_no_replace_flags(self):
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_archive_native_binding_probe",
        )

        class Operation:
            def __init__(self):
                self.calls = []

            def __call__(self, *args):
                self.calls.append(args)
                return 0

        for platform_name, attribute, expected in (
                ("linux", "renameat2", (-100, b"source", -100, b"target", 1)),
                ("darwin", "renamex_np", (b"source", b"target", 0x00000004))):
            with self.subTest(platform=platform_name):
                operation = Operation()
                library = SimpleNamespace(**{attribute: operation})
                bound, diagnostic = module["bind_atomic_archive_rename"](
                    platform_name, library,
                )
                self.assertEqual(bound(b"source", b"target"), 0)
                self.assertEqual(operation.calls, [expected])
                self.assertEqual(operation.restype, module["ctypes"].c_int)
                self.assertIn(attribute, diagnostic)

    def test_atomic_archive_rename_validates_and_fails_closed_when_unavailable(self):
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_archive_unavailable_probe",
        )
        globals_ = module["atomic_archive_rename_no_replace"].__globals__
        source = self.base / "unavailable-source"
        destination = self.base / "unavailable-destination"
        source_bytes = b"preserved source\n"
        source.write_bytes(source_bytes)
        boundary_called = False

        def boundary(_source, _destination):
            nonlocal boundary_called
            boundary_called = True

        globals_["archive_atomic_rename_boundary"] = boundary
        globals_["ATOMIC_ARCHIVE_RENAME"] = None
        globals_["ATOMIC_ARCHIVE_RENAME_DIAGNOSTIC"] = "injected unavailable"
        with self.assertRaisesRegex(
                RuntimeError, "atomic no-replace archive move is unavailable"):
            module["durable_archive_rename"](source, destination)
        self.assertFalse(boundary_called)
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertFalse(destination.exists())

        for invalid, message in (("", "must not be empty"), ("bad\0path", "null byte")):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaisesRegex(ValueError, message):
                    module["atomic_archive_rename_no_replace"](invalid, destination)
        with self.assertRaisesRegex(TypeError, "path-like"):
            module["atomic_archive_rename_no_replace"](object(), destination)

    def test_atomic_archive_rename_maps_native_errno_without_mutation(self):
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_archive_native_errno_probe",
        )
        globals_ = module["atomic_archive_rename_no_replace"].__globals__
        source = self.base / "errno-source"
        destination = self.base / "errno-destination"
        source.write_bytes(b"source evidence\n")

        def failing_operation(error_number):
            def operation(_source, _destination):
                module["ctypes"].set_errno(error_number)
                return -1
            return operation

        globals_["ATOMIC_ARCHIVE_RENAME_DIAGNOSTIC"] = "injected native operation"
        for error_number, exception, message in (
                (module["errno"].EEXIST, ValueError, "refusing to overwrite"),
                (module["errno"].EINVAL, RuntimeError, "unavailable on this filesystem"),
                (module["errno"].EIO, OSError, "failed from")):
            with self.subTest(error_number=error_number):
                globals_["ATOMIC_ARCHIVE_RENAME"] = failing_operation(error_number)
                with self.assertRaisesRegex(exception, message) as caught:
                    module["atomic_archive_rename_no_replace"](source, destination)
                self.assertEqual(source.read_bytes(), b"source evidence\n")
                self.assertFalse(destination.exists())
                if error_number == module["errno"].EIO:
                    self.assertEqual(Path(caught.exception.filename), source)
                    self.assertEqual(Path(caught.exception.filename2), destination)

    def test_archive_move_boundary_never_replaces_destination_topologies(self):
        for index, destination_kind in enumerate(
                ("file", "empty-directory", "nonempty-directory", "symlink")):
            with self.subTest(destination_kind=destination_kind):
                project = self.make_project("archive-move-race-{}".format(index))
                task_id = self.create_task(project, "archive move race")
                runtime = project / ".baton"
                state_path = runtime / "tasks" / f"{task_id}.json"
                state = json.loads(state_path.read_text())
                state["status"] = "done"
                state_path.write_text(json.dumps(state))
                source_before = state_path.read_bytes()
                spec_path = runtime / "tasks" / f"{task_id}.md"
                spec_before = spec_path.read_bytes()
                work = runtime / "work" / task_id
                work.mkdir()
                work_evidence = work / "evidence"
                work_evidence.write_bytes(b"unrelated work evidence\n")
                destination = runtime / "archive" / f"{task_id}.json"
                symlink_target = self.base / "archive-race-symlink-target"
                symlink_target.write_bytes(b"symlink target\n")

                module = runpy.run_path(
                    str(SOURCE_BATON),
                    run_name="baton_archive_move_race_probe_{}".format(index),
                )
                globals_ = module["cmd_archive"].__globals__
                globals_["require_baton_dir"] = lambda: str(runtime)
                collided = False

                def collide(source, target):
                    nonlocal collided
                    if collided or Path(target) != destination:
                        return
                    collided = True
                    if destination_kind == "file":
                        destination.write_bytes(b"concurrent sentinel\n")
                    elif destination_kind == "empty-directory":
                        destination.mkdir()
                    elif destination_kind == "nonempty-directory":
                        destination.mkdir()
                        (destination / "sentinel").write_bytes(b"directory sentinel\n")
                    else:
                        destination.symlink_to(symlink_target)

                globals_["archive_atomic_rename_boundary"] = collide
                with self.assertRaisesRegex(RuntimeError, "pending journal preserved"):
                    module["cmd_archive"](SimpleNamespace())

                journal = runtime / "archive-transaction.json"
                self.assertTrue(collided)
                self.assertTrue(journal.is_file())
                self.assertEqual(state_path.read_bytes(), source_before)
                self.assertEqual(spec_path.read_bytes(), spec_before)
                self.assertEqual(work_evidence.read_bytes(), b"unrelated work evidence\n")
                if destination_kind == "file":
                    self.assertEqual(destination.read_bytes(), b"concurrent sentinel\n")
                    destination.unlink()
                elif destination_kind == "empty-directory":
                    self.assertEqual(list(destination.iterdir()), [])
                    destination.rmdir()
                elif destination_kind == "nonempty-directory":
                    self.assertEqual(
                        (destination / "sentinel").read_bytes(),
                        b"directory sentinel\n",
                    )
                    shutil.rmtree(destination)
                else:
                    self.assertTrue(destination.is_symlink())
                    self.assertEqual(destination.readlink(), symlink_target)
                    self.assertEqual(symlink_target.read_bytes(), b"symlink target\n")
                    destination.unlink()

                globals_["archive_atomic_rename_boundary"] = lambda *_args: None
                with redirect_stdout(io.StringIO()):
                    module["cmd_archive"](SimpleNamespace())
                self.assertFalse(journal.exists())
                self.assertFalse(state_path.exists())
                self.assertFalse(spec_path.exists())
                self.assertFalse(work.exists())
                self.assertEqual(destination.read_bytes(), source_before)
                self.assertEqual(
                    (runtime / "archive" / f"{task_id}.work" / "evidence").read_bytes(),
                    b"unrelated work evidence\n",
                )

    def test_archive_recovery_move_boundary_preserves_collision_and_retries(self):
        project = self.make_project("archive-recovery-move-race")
        task_id = self.create_task(project, "archive recovery move race")
        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "done"
        state_path.write_text(json.dumps(state))
        source_before = state_path.read_bytes()
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_archive_recovery_move_race_probe",
        )
        transaction = module["build_archive_transaction"](
            str(runtime), module["load_all_tasks"](str(runtime)),
        )
        journal = runtime / "archive-transaction.json"
        module["atomic_json"](journal, transaction)
        journal_before = journal.read_bytes()
        destination = runtime / "archive" / f"{task_id}.json"
        sentinel = b"recovery collision sentinel\n"
        globals_ = module["recover_archive_transaction"].__globals__

        def collide(_source, target):
            if Path(target) == destination and not destination.exists():
                destination.write_bytes(sentinel)

        globals_["archive_atomic_rename_boundary"] = collide
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            module["recover_archive_transaction"](str(runtime))
        self.assertEqual(journal.read_bytes(), journal_before)
        self.assertEqual(state_path.read_bytes(), source_before)
        self.assertEqual(destination.read_bytes(), sentinel)
        self.assertTrue((runtime / "tasks" / f"{task_id}.md").is_file())

        destination.unlink()
        globals_["archive_atomic_rename_boundary"] = lambda *_args: None
        self.assertTrue(module["recover_archive_transaction"](str(runtime)))
        self.assertFalse(journal.exists())
        self.assertFalse(state_path.exists())
        self.assertEqual(destination.read_bytes(), source_before)

    def test_archive_rollback_move_boundary_preserves_collision_and_retries(self):
        project = self.make_project("archive-rollback-move-race")
        task_id = self.create_task(project, "archive rollback move race")
        runtime = project / ".baton"
        active_state = runtime / "tasks" / f"{task_id}.json"
        state = json.loads(active_state.read_text())
        state["status"] = "done"
        active_state.write_text(json.dumps(state))
        archived_state = runtime / "archive" / f"{task_id}.json"
        source_before = active_state.read_bytes()
        sentinel = b"rollback collision sentinel\n"
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_archive_rollback_move_race_probe",
        )
        globals_ = module["cmd_archive"].__globals__
        globals_["require_baton_dir"] = lambda: str(runtime)

        def fail_forward_then_collide_rollback(source, destination):
            source_path = Path(source)
            destination_path = Path(destination)
            if destination_path == runtime / "archive" / f"{task_id}.md":
                raise RuntimeError("injected ordinary archive failure")
            if source_path == archived_state and destination_path == active_state:
                active_state.write_bytes(sentinel)

        globals_["archive_atomic_rename_boundary"] = fail_forward_then_collide_rollback
        with self.assertRaisesRegex(RuntimeError, "durable rollback could not complete"):
            module["cmd_archive"](SimpleNamespace())

        journal = runtime / "archive-transaction.json"
        self.assertTrue(journal.is_file())
        self.assertEqual(active_state.read_bytes(), sentinel)
        self.assertEqual(archived_state.read_bytes(), source_before)
        self.assertTrue((runtime / "tasks" / f"{task_id}.md").is_file())

        active_state.unlink()
        globals_["archive_atomic_rename_boundary"] = lambda *_args: None
        with redirect_stdout(io.StringIO()):
            module["cmd_archive"](SimpleNamespace())
        self.assertFalse(journal.exists())
        self.assertFalse(active_state.exists())
        self.assertEqual(archived_state.read_bytes(), source_before)

    def test_accept_serializes_done_write_and_token_consumption_with_archive(self):
        project = self.make_project("accept-archive-race")
        self.configure(project, self.write_worker(GOOD_WORKER))
        task_id = self.create_task(project, "accept archive race", ["race/**"])
        self.baton(project, "run", task_id, check=True)
        _brief, token = self.review_brief_token(project, task_id)

        runtime = project / ".baton"
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_accept_archive_probe")
        globals_ = module["cmd_task_accept"].__globals__
        globals_["require_baton_dir"] = lambda: str(runtime)
        original_save_task = globals_["save_task"]
        original_file_lock = globals_["file_lock"]
        accepted_state_saved = threading.Event()
        release_accept = threading.Event()
        archive_lock_attempted = threading.Event()
        archive_finished = threading.Event()
        errors = {}

        def synchronized_save_task(baton_dir, task):
            original_save_task(baton_dir, task)
            if (
                    threading.current_thread().name == "accept-probe"
                    and task.get("id") == task_id
                    and task.get("status") == "done"):
                accepted_state_saved.set()
                if not release_accept.wait(5):
                    raise RuntimeError("accept synchronization timed out")

        @contextmanager
        def observed_file_lock(path):
            if (
                    threading.current_thread().name == "archive-probe"
                    and path == globals_["lock_path"](str(runtime), "scheduler")):
                archive_lock_attempted.set()
            with original_file_lock(path):
                yield

        globals_["save_task"] = synchronized_save_task
        globals_["file_lock"] = observed_file_lock

        def accept():
            try:
                module["cmd_task_accept"](SimpleNamespace(
                    id=task_id, brief=token, note=None,
                ))
            except BaseException as error:
                errors["accept"] = error

        def archive():
            try:
                module["cmd_archive"](SimpleNamespace())
            except BaseException as error:
                errors["archive"] = error
            finally:
                archive_finished.set()

        accept_thread = threading.Thread(target=accept, name="accept-probe")
        archive_thread = threading.Thread(target=archive, name="archive-probe")
        accept_thread.start()
        self.assertTrue(accepted_state_saved.wait(5))
        archive_thread.start()
        self.assertTrue(archive_lock_attempted.wait(5))
        archive_completed_before_accept = archive_finished.wait(0.5)
        release_accept.set()
        accept_thread.join(5)
        archive_thread.join(5)

        self.assertFalse(accept_thread.is_alive())
        self.assertFalse(archive_thread.is_alive())
        self.assertFalse(archive_completed_before_accept)
        self.assertEqual(errors, {})
        self.assertFalse((runtime / "tasks" / f"{task_id}.json").exists())
        archived_work = runtime / "archive" / f"{task_id}.work"
        self.assertTrue(archived_work.is_dir())
        self.assertFalse((archived_work / "review-brief-token.json").exists())

    def test_archive_defers_sigterm_until_transaction_is_complete(self):
        project = self.make_project()
        task_id = self.create_task(project, "archive signal", ["archive/**"])
        runtime = project / ".baton"
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

module = runpy.run_path(sys.argv[1], run_name="baton_archive_probe")
globals_ = module["cmd_archive"].__globals__
original = globals_["durable_archive_rename"]
marker = Path(sys.argv[3])

def slow_move(source, target):
    result = original(source, target)
    marker.write_text("moved\n")
    time.sleep(0.25)
    return result

globals_["durable_archive_rename"] = slow_move
os.chdir(sys.argv[2])
module["cmd_archive"](SimpleNamespace())
'''
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(SOURCE_BATON), str(project), str(marker)],
            cwd=project, env=clean_test_environment(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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

    def test_archive_recovers_sigkill_at_every_durable_boundary(self):
        boundaries = (
            "journal-created", "move-0", "move-1", "move-2", "journal-removed",
        )
        for index, boundary in enumerate(boundaries):
            with self.subTest(boundary=boundary):
                project = self.make_project("archive-crash-{}".format(index))
                task_id = self.create_task(
                    project, "archive crash {}".format(index), ["crash-{}/**".format(index)],
                )
                runtime = project / ".baton"
                state_path = runtime / "tasks" / f"{task_id}.json"
                state = json.loads(state_path.read_text())
                state["status"] = "done"
                state_path.write_text(json.dumps(state))
                work = runtime / "work" / task_id
                work.mkdir(parents=True)
                (work / "artifact").write_text("evidence\n")
                marker = self.base / "archive-crash-boundary-{}".format(index)
                code = r'''
import os
from pathlib import Path
import runpy
import signal
import sys
from types import SimpleNamespace

module = runpy.run_path(sys.argv[1], run_name="baton_archive_crash_probe")
globals_ = module["cmd_archive"].__globals__
marker = Path(sys.argv[3])
target = sys.argv[4]

def stop_at_boundary(name):
    if name == target:
        marker.write_text(name + "\n")
        os.kill(os.getpid(), signal.SIGSTOP)

globals_["archive_transaction_boundary"] = stop_at_boundary
os.chdir(sys.argv[2])
module["cmd_archive"](SimpleNamespace())
'''
                process = subprocess.Popen(
                    [
                        sys.executable, "-c", code, str(SOURCE_BATON), str(project),
                        str(marker), boundary,
                    ],
                    cwd=project, env=clean_test_environment(), text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(marker.exists(), boundary)
                    os.kill(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(
                        process.returncode, -signal.SIGKILL, stdout + stderr,
                    )
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()

                before_recovery = self.baton(project, "validate")
                if boundary == "journal-removed":
                    self.assertEqual(
                        before_recovery.returncode, 0,
                        before_recovery.stdout + before_recovery.stderr,
                    )
                else:
                    self.assertEqual(before_recovery.returncode, 1)
                    self.assertIn(
                        "archive transaction is pending", before_recovery.stdout,
                    )

                recovered = self.baton(project, "archive", check=True)
                self.assertEqual(recovered.stdout, "archived 0 task(s)\n")
                self.assertFalse((runtime / "archive-transaction.json").exists())
                self.assertFalse((runtime / "tasks" / f"{task_id}.json").exists())
                self.assertFalse((runtime / "tasks" / f"{task_id}.md").exists())
                self.assertFalse((runtime / "work" / task_id).exists())
                self.assertTrue((runtime / "archive" / f"{task_id}.json").is_file())
                self.assertTrue((runtime / "archive" / f"{task_id}.md").is_file())
                self.assertTrue((runtime / "archive" / f"{task_id}.work").is_dir())
                retry = self.baton(project, "archive", check=True)
                self.assertEqual(retry.stdout, "archived 0 task(s)\n")
                self.baton(project, "validate", check=True)

    def test_archive_ordinary_exception_rolls_back_and_removes_journal(self):
        project = self.make_project("archive-exception-rollback")
        task_id = self.create_task(project, "archive exception rollback")
        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "done"
        state_path.write_text(json.dumps(state))
        work = runtime / "work" / task_id
        work.mkdir(parents=True)
        (work / "artifact").write_text("evidence\n")

        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_archive_rollback_probe")
        globals_ = module["cmd_archive"].__globals__
        globals_["require_baton_dir"] = lambda: str(runtime)
        original = globals_["durable_archive_rename"]
        forward_moves = 0

        def fail_second_forward_move(source, destination):
            nonlocal forward_moves
            if Path(source).parent.name in ("tasks", "work"):
                forward_moves += 1
                if forward_moves == 2:
                    raise RuntimeError("injected archive move failure")
            return original(source, destination)

        globals_["durable_archive_rename"] = fail_second_forward_move
        with self.assertRaisesRegex(RuntimeError, "injected archive move failure"):
            module["cmd_archive"](SimpleNamespace())

        self.assertFalse((runtime / "archive-transaction.json").exists())
        self.assertTrue((runtime / "tasks" / f"{task_id}.json").is_file())
        self.assertTrue((runtime / "tasks" / f"{task_id}.md").is_file())
        self.assertTrue((runtime / "work" / task_id).is_dir())
        self.assertFalse((runtime / "archive" / f"{task_id}.json").exists())
        self.assertFalse((runtime / "archive" / f"{task_id}.md").exists())
        self.assertFalse((runtime / "archive" / f"{task_id}.work").exists())

    def test_archive_recovery_fails_closed_for_ambiguous_or_tampered_journal(self):
        for index, mode in enumerate(("ambiguous", "tampered")):
            with self.subTest(mode=mode):
                project = self.make_project("archive-journal-{}".format(index))
                task_id = self.create_task(project, "archive journal " + mode)
                runtime = project / ".baton"
                state_path = runtime / "tasks" / f"{task_id}.json"
                state = json.loads(state_path.read_text())
                state["status"] = "done"
                state_path.write_text(json.dumps(state))
                module = runpy.run_path(
                    str(SOURCE_BATON), run_name="baton_archive_journal_probe_" + mode,
                )
                transaction = module["build_archive_transaction"](
                    str(runtime), module["load_all_tasks"](str(runtime)),
                )
                journal = runtime / "archive-transaction.json"
                if mode == "ambiguous":
                    collision = runtime / "archive" / f"{task_id}.json"
                    collision.write_text("pre-existing evidence\n")
                    expected = "both source and destination exist"
                else:
                    transaction["artifacts"][0]["source"] = "../seed.txt"
                    collision = None
                    expected = "artifact paths are not trusted"
                module["atomic_json"](str(journal), transaction)

                recovered = self.baton(project, "archive")
                self.assertEqual(recovered.returncode, 1)
                self.assertIn(expected, recovered.stderr)
                self.assertTrue(journal.is_file())
                self.assertTrue(state_path.is_file())
                if collision is not None:
                    self.assertEqual(collision.read_text(), "pre-existing evidence\n")

    def test_archive_recovery_rejects_non_integer_journal_versions_without_mutation(self):
        invalid_versions = {
            "boolean": True,
            "float": 1.0,
            "null": None,
            "string": "1",
            "unsupported": 2,
        }
        for index, (label, version) in enumerate(invalid_versions.items()):
            with self.subTest(label=label):
                project = self.make_project("archive-version-{}".format(index))
                task_id = self.create_task(project, "archive version " + label)
                runtime = project / ".baton"
                state_path = runtime / "tasks" / f"{task_id}.json"
                state = json.loads(state_path.read_text())
                state["status"] = "done"
                state_path.write_text(json.dumps(state))
                spec_path = runtime / "tasks" / f"{task_id}.md"
                module = runpy.run_path(
                    str(SOURCE_BATON),
                    run_name="baton_archive_version_probe_" + label,
                )
                transaction = module["build_archive_transaction"](
                    str(runtime), module["load_all_tasks"](str(runtime)),
                )
                transaction["version"] = version
                journal = runtime / "archive-transaction.json"
                module["atomic_json"](str(journal), transaction)
                before = {
                    path: path.read_bytes() for path in (journal, state_path, spec_path)
                }

                recovered = self.baton(project, "archive")

                self.assertEqual(recovered.returncode, 1)
                self.assertIn("unsupported version", recovered.stderr)
                self.assertEqual(
                    {path: path.read_bytes() for path in before}, before,
                )
                self.assertFalse((runtime / "archive" / f"{task_id}.json").exists())
                self.assertFalse((runtime / "archive" / f"{task_id}.md").exists())

    def test_archive_recovery_rejects_omitted_work_then_recovers_repaired_journal(self):
        project = self.make_project("archive-omitted-work")
        task_id = self.create_task(project, "archive omitted work")
        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "done"
        state["history"].append({
            "at": "2026-07-17T00:00:00Z", "event": "launched", "attempt": 1,
        })
        state_path.write_text(json.dumps(state))
        spec_path = runtime / "tasks" / f"{task_id}.md"
        active_work = runtime / "work" / task_id
        active_work.mkdir(parents=True)
        evidence = active_work / "evidence.txt"
        evidence.write_text("preserved evidence\n")
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_archive_omitted_work_probe",
        )
        transaction = module["build_archive_transaction"](
            str(runtime), module["load_all_tasks"](str(runtime)),
        )
        tampered = dict(transaction)
        tampered["artifacts"] = [
            artifact for artifact in transaction["artifacts"]
            if artifact["kind"] != "directory"
        ]
        journal = runtime / "archive-transaction.json"
        module["atomic_json"](str(journal), tampered)
        before = {
            path: path.read_bytes()
            for path in (journal, state_path, spec_path, evidence)
        }

        rejected = self.baton(project, "archive")

        self.assertEqual(rejected.returncode, 1)
        self.assertIn("canonical work artifact entry is missing", rejected.stderr)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        for suffix in (".json", ".md", ".work"):
            self.assertFalse((runtime / "archive" / f"{task_id}{suffix}").exists())

        module["atomic_json"](str(journal), transaction)
        recovered = self.baton(project, "archive", check=True)
        self.assertEqual(recovered.stdout, "archived 0 task(s)\n")
        self.assertFalse(journal.exists())
        self.assertFalse(state_path.exists())
        self.assertFalse(spec_path.exists())
        self.assertFalse(active_work.exists())
        self.assertTrue((runtime / "archive" / f"{task_id}.json").is_file())
        self.assertTrue((runtime / "archive" / f"{task_id}.md").is_file())
        self.assertEqual(
            (runtime / "archive" / f"{task_id}.work" / "evidence.txt").read_text(),
            "preserved evidence\n",
        )
        retry = self.baton(project, "archive", check=True)
        self.assertEqual(retry.stdout, "archived 0 task(s)\n")
        self.baton(project, "validate", check=True)

    def test_archive_recovery_requires_launched_work_absent_from_journal_and_disk(self):
        project = self.make_project("archive-required-work-absent")
        task_id = self.create_task(project, "archive required work absent")
        runtime = project / ".baton"
        state_path = runtime / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "done"
        state["history"].append({
            "at": "2026-07-17T00:00:00Z", "event": "launched", "attempt": 1,
        })
        state_path.write_text(json.dumps(state))
        spec_path = runtime / "tasks" / f"{task_id}.md"
        active_work = runtime / "work" / task_id
        active_work.mkdir(parents=True)
        evidence_bytes = b"restored evidence\n"
        (active_work / "evidence.txt").write_bytes(evidence_bytes)
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_archive_required_work_absent_probe",
        )
        transaction = module["build_archive_transaction"](
            str(runtime), module["load_all_tasks"](str(runtime)),
        )
        tampered = dict(transaction)
        tampered["artifacts"] = [
            artifact for artifact in transaction["artifacts"]
            if artifact["kind"] != "directory"
        ]
        shutil.rmtree(active_work)
        journal = runtime / "archive-transaction.json"
        module["atomic_json"](str(journal), tampered)
        before = {
            path: path.read_bytes() for path in (journal, state_path, spec_path)
        }

        rejected = self.baton(project, "archive")

        self.assertEqual(rejected.returncode, 1)
        self.assertIn("canonical required work artifact entry is missing", rejected.stderr)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertFalse(active_work.exists())
        for suffix in (".json", ".md", ".work"):
            self.assertFalse((runtime / "archive" / f"{task_id}{suffix}").exists())

        active_work.mkdir()
        (active_work / "evidence.txt").write_bytes(evidence_bytes)
        module["atomic_json"](str(journal), transaction)
        recovered = self.baton(project, "archive", check=True)
        self.assertEqual(recovered.stdout, "archived 0 task(s)\n")
        self.assertFalse(journal.exists())
        self.assertFalse(state_path.exists())
        self.assertFalse(spec_path.exists())
        self.assertFalse(active_work.exists())
        self.assertEqual(
            (runtime / "archive" / f"{task_id}.work" / "evidence.txt").read_bytes(),
            evidence_bytes,
        )
        retry = self.baton(project, "archive", check=True)
        self.assertEqual(retry.stdout, "archived 0 task(s)\n")
        self.baton(project, "validate", check=True)

    def test_archive_recovery_preflights_extra_and_ambiguous_work_topology(self):
        for index, mode in enumerate(("extra", "ambiguous")):
            with self.subTest(mode=mode):
                project = self.make_project("archive-work-topology-{}".format(index))
                task_id = self.create_task(project, "archive work " + mode)
                runtime = project / ".baton"
                state_path = runtime / "tasks" / f"{task_id}.json"
                state = json.loads(state_path.read_text())
                state["status"] = "done"
                state_path.write_text(json.dumps(state))
                active_work = runtime / "work" / task_id
                active_work.mkdir(parents=True)
                (active_work / "evidence").write_text("evidence\n")
                module = runpy.run_path(
                    str(SOURCE_BATON),
                    run_name="baton_archive_work_topology_probe_" + mode,
                )
                transaction = module["build_archive_transaction"](
                    str(runtime), module["load_all_tasks"](str(runtime)),
                )
                if mode == "extra":
                    shutil.rmtree(active_work)
                    expected = "no active or archived work"
                else:
                    archived_work = runtime / "archive" / f"{task_id}.work"
                    shutil.copytree(active_work, archived_work)
                    expected = "both active and archived work exist"
                journal = runtime / "archive-transaction.json"
                module["atomic_json"](str(journal), transaction)
                state_before = state_path.read_bytes()
                journal_before = journal.read_bytes()

                recovered = self.baton(project, "archive")

                self.assertEqual(recovered.returncode, 1)
                self.assertIn(expected, recovered.stderr)
                self.assertEqual(state_path.read_bytes(), state_before)
                self.assertEqual(journal.read_bytes(), journal_before)
                self.assertFalse((runtime / "archive" / f"{task_id}.json").exists())
                self.assertFalse((runtime / "archive" / f"{task_id}.md").exists())

    def test_validate_rejects_split_orphaned_and_required_companion_layouts(self):
        project = self.make_project("archive-layout-validation")
        runtime = project / ".baton"

        archived_id = self.create_task(project, "split archived state")
        archived_state = runtime / "tasks" / f"{archived_id}.json"
        os.rename(archived_state, runtime / "archive" / archived_state.name)

        active_id = self.create_task(project, "split active state")
        active_spec = runtime / "tasks" / f"{active_id}.md"
        os.rename(active_spec, runtime / "archive" / active_spec.name)

        required_id = self.create_task(project, "missing required work")
        required_state = runtime / "tasks" / f"{required_id}.json"
        required = json.loads(required_state.read_text())
        required["history"].append({"at": "now", "event": "launched", "attempt": 1})
        required_state.write_text(json.dumps(required))

        orphan_spec_id = "T997-orphan-spec"
        orphan_work_id = "T998-orphan-work"
        (runtime / "tasks" / f"{orphan_spec_id}.md").write_text("orphan\n")
        (runtime / "archive" / f"{orphan_work_id}.work").mkdir()

        validation = self.baton(project, "validate")
        self.assertEqual(validation.returncode, 1)
        for message in (
                f"{archived_id}: archived task state has active companion artifacts",
                f"{active_id}: active task state has archived companion artifacts",
                f"{orphan_spec_id}: active task spec has no corresponding active state",
                f"{orphan_work_id}: archived work has no corresponding archived state",
                f"{required_id}: required active work is missing"):
            self.assertIn(message, validation.stdout)

    def test_unborn_repository_diff_uses_worktree_content(self):
        project = self.make_project(commit=False)
        worker = self.write_worker(STAGE_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "stage", ["new/**"])
        self.baton(project, "run", task_id, check=True)
        diff = project / ".baton" / "work" / task_id / "attempt-1.diff"
        self.assertIn("new/staged.txt", diff.read_text())

    def test_capsule_sandwich_brief_digest_and_retry_delta(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "capsule", ["capsule/**"])
        self.baton(project, "run", task_id, check=True)

        work = project / ".baton" / "work" / task_id
        prompt = (work / "attempt-1.prompt.md").read_text()
        brief = (work / "attempt-1.brief.md").read_text()
        digest_line, capsule = brief.split("\n\n", 1)
        digest = hashlib.sha256(capsule.encode()).hexdigest()
        self.assertEqual(digest_line, f"Content digest: sha256:{digest}")
        launched = next(
            entry for entry in self.state(project, task_id)["history"]
            if entry.get("event") == "launched"
        )
        self.assertEqual(launched["capsule_chars"], len(capsule))
        self.assertTrue(prompt.startswith(capsule))
        self.assertTrue(prompt.endswith(capsule))
        self.assertEqual(prompt.count(capsule), 2)

        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_capsule_probe")
        task = self.state(project, task_id)
        spec = (project / ".baton" / "tasks" / f"{task_id}.md").read_text()
        entries = module["memory_index_entries"](
            (project / ".baton" / "memory.md").read_text()
        )
        self.assertEqual(module["compile_context_capsule"](task, spec, entries), capsule)
        self.assertEqual(module["compile_context_capsule"](task, spec, entries), capsule)

        self.baton(
            project, "task", "return", task_id,
            "--reason", "Preserve the capsule boundary", check=True,
        )
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        self.baton(project, "run", task_id, check=True)
        retry_prompt = (work / "attempt-2.prompt.md").read_text()
        retry_brief = (work / "attempt-2.brief.md").read_text()
        _retry_digest, retry_capsule = retry_brief.split("\n\n", 1)
        self.assertTrue(retry_prompt.startswith(retry_capsule))
        self.assertTrue(retry_prompt.endswith(retry_capsule))
        self.assertIn("## Retry delta", retry_capsule)
        self.assertIn("Preserve the capsule boundary", retry_capsule)
        self.assertIn("attempt-1.report.md", retry_prompt)
        self.assertNotEqual(retry_capsule, capsule)

    def test_placeholder_and_empty_specs_are_rejected_by_run_and_validate(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        created = self.baton(
            project, "task", "create", "--title", "unfinished spec",
            "--tier", "test", check=True,
        )
        task_id = created.stdout.split()[1]
        run = self.baton(project, "run", task_id)
        validation = self.baton(project, "validate")
        self.assertNotEqual(run.returncode, 0)
        self.assertNotEqual(validation.returncode, 0)
        shared = "Objective still contains the template placeholder"
        self.assertIn(shared, run.stderr)
        self.assertIn(shared, validation.stdout)
        self.assertEqual(self.state(project, task_id)["status"], "queued")

        spec = project / ".baton" / "tasks" / f"{task_id}.md"
        spec.write_text(spec.read_text().replace(
            "Replace this line with one clear outcome.", "",
        ))
        empty_run = self.baton(project, "run", task_id)
        empty_validation = self.baton(project, "validate")
        self.assertIn("Objective is empty", empty_run.stderr)
        self.assertIn("Objective is empty", empty_validation.stdout)

    def test_capsule_budget_overflow_is_rejected_by_run_and_validate(self):
        project = self.make_project()
        self.configure(
            project, self.write_worker(NO_CHANGE_WORKER), capsule_max_chars=100,
        )
        task_id = self.create_task(project, "over budget", ["budget/**"])
        run = self.baton(project, "run", task_id)
        validation = self.baton(project, "validate")
        self.assertNotEqual(run.returncode, 0)
        self.assertNotEqual(validation.returncode, 0)
        for output in (run.stderr, validation.stdout):
            self.assertIn("capsule_max_chars=100", output)
            self.assertIn("exceeded by", output)
        self.assertEqual(self.state(project, task_id)["status"], "queued")

    def test_task_capsule_running_raw_uses_stored_launch_and_denies_worker(self):
        project = self.make_project()
        task_id = self.create_task(project, "stored capsule", ["capsule/**"])
        env = self.lease_task(project, task_id, "stored-capsule-lease")
        brief = (
            project / ".baton" / "work" / task_id / "attempt-1.brief.md"
        ).read_text()
        _digest_header, stored_capsule = brief.split("\n\n", 1)
        spec = project / ".baton" / "tasks" / f"{task_id}.md"
        spec.write_text(spec.read_text().replace(
            "Complete the stored capsule task.", "This changed after launch.",
        ))

        raw = self.baton(project, "task", "capsule", task_id, "--raw", check=True)
        self.assertEqual(raw.stdout.encode(), stored_capsule.encode())
        shown = self.baton(project, "task", "capsule", task_id, check=True)
        self.assertTrue(shown.stdout.startswith(stored_capsule + "\n\nCapsule diagnostics:\n"))
        self.assertIn("Source: launch (attempt 1)\n", shown.stdout)
        self.assertIn(
            "Digest: sha256:" + hashlib.sha256(stored_capsule.encode()).hexdigest(),
            shown.stdout,
        )
        denied = self.baton(project, "task", "capsule", task_id, env=env)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("worker processes cannot run orchestrator commands", denied.stderr)

    def test_task_capsule_prospective_preview_matches_launch_and_writes_nothing(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        task_id = self.create_task(project, "prospective capsule", ["preview/**"])
        runtime = project / ".baton"

        def snapshot():
            return {
                str(path.relative_to(runtime)): (
                    path.is_dir(), path.stat().st_mtime_ns,
                    b"" if path.is_dir() else path.read_bytes(),
                )
                for path in runtime.rglob("*")
            }

        before = snapshot()
        shown = self.baton(project, "task", "capsule", task_id, check=True)
        self.assertEqual(snapshot(), before)
        self.assertIn("Source: current spec (prospective)\n", shown.stdout)
        prospective = self.baton(
            project, "task", "capsule", task_id, "--raw", check=True,
        ).stdout
        self.assertEqual(snapshot(), before)

        self.baton(project, "run", task_id, check=True)
        brief = runtime / "work" / task_id / "attempt-1.brief.md"
        _digest_header, launched = brief.read_text().split("\n\n", 1)
        self.assertEqual(prospective, launched)

    def test_task_capsule_diagnostics_count_unicode_characters(self):
        project = self.make_project()
        title = "aperçu 😀 東京"
        task_id = self.create_task(project, title, ["café/**"])
        shown = self.baton(project, "task", "capsule", task_id, check=True)
        task_line = f"Task: {task_id}: {title}"
        scope_line = "Scope: café/**"
        objective = f"## Objective\nComplete the {title} task."
        self.assertIn(f"- Task: {len(task_line)} chars\n", shown.stdout)
        self.assertIn(f"- Scope: {len(scope_line)} chars\n", shown.stdout)
        self.assertIn(f"- Objective: {len(objective)} chars\n", shown.stdout)
        capsule, diagnostics = shown.stdout.split("\n\nCapsule diagnostics:\n", 1)
        self.assertIn(f"Capsule: {len(capsule)} of 4000 chars", diagnostics)
        self.assertRegex(diagnostics, r"Digest: sha256:[0-9a-f]{64}\n")

    def test_task_capsule_over_budget_reports_all_diagnostics_and_raw_is_empty(self):
        project = self.make_project()
        self.configure(
            project, self.write_worker(NO_CHANGE_WORKER), capsule_max_chars=100,
        )
        task_id = self.create_task(project, "preview overflow", ["budget/**"])
        shown = self.baton(project, "task", "capsule", task_id)
        self.assertNotEqual(shown.returncode, 0)
        self.assertTrue(shown.stdout.startswith("# Critical Context Capsule\n"))
        self.assertRegex(
            shown.stdout, r"Capsule: \d+ of 100 chars \(\d+ chars overflow\)",
        )
        for label in (
                "Header", "Task", "Scope", "Objective", "Acceptance criteria",
                "Not allowed", "Verification"):
            self.assertRegex(shown.stdout, rf"- {label}: \d+ chars\n")
        self.assertRegex(shown.stdout, r"Digest: sha256:[0-9a-f]{64}\n")
        self.assertIn("Source: current spec (prospective)\n", shown.stdout)
        self.assertIn("capsule_max_chars=100", shown.stderr)

        raw = self.baton(project, "task", "capsule", task_id, "--raw")
        self.assertNotEqual(raw.returncode, 0)
        self.assertEqual(raw.stdout, "")
        self.assertIn("capsule_max_chars=100", raw.stderr)

    def test_task_capsule_errors_pass_through_and_reject_unknown_or_archived(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        created = self.baton(
            project, "task", "create", "--title", "unfinished preview",
            "--tier", "test", check=True,
        )
        unfinished = created.stdout.split()[1]
        preview = self.baton(project, "task", "capsule", unfinished)
        launch = self.baton(project, "run", unfinished)
        self.assertNotEqual(preview.returncode, 0)
        self.assertEqual(preview.stderr, launch.stderr)
        self.assertIn("Objective still contains the template placeholder", preview.stderr)

        unknown = self.baton(project, "task", "capsule", "T999-not-here")
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("no such task: T999-not-here", unknown.stderr)

        archived = self.create_task(project, "archived preview", ["archive/**"])
        state_path = project / ".baton" / "tasks" / f"{archived}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "done"
        state_path.write_text(json.dumps(state))
        self.baton(project, "archive", check=True)
        rejected = self.baton(project, "task", "capsule", archived)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(f"{archived} is archived", rejected.stderr)

    def test_referenced_memory_is_ordered_deduplicated_and_snapshotted(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        self.write_memory(project, [
            ("M001", "W", "First worker fact", "FIRST FULL BODY MUST NOT LEAK"),
            ("M1000", "B", "Four-digit shared fact", "SECOND FULL BODY MUST NOT LEAK"),
        ])
        task_id = self.create_task(project, "referenced memory", ["memory/**"])
        runtime = project / ".baton"
        spec_path = runtime / "tasks" / f"{task_id}.md"
        spec_path.write_text(spec_path.read_text().replace(
            "List the paths and facts the worker needs. Reference memory ids when useful.",
            "Use M1000, then M001, then M1000 again. Other sections do not count.",
        ))

        preview = self.baton(project, "task", "capsule", task_id, check=True)
        self.assertIn("- Referenced memory: ", preview.stdout)
        prospective = self.baton(
            project, "task", "capsule", task_id, "--raw", check=True,
        ).stdout
        expected_section = (
            "## Referenced memory\n"
            "Load full entries as needed with "
            "`python3 .baton/baton memory show ID`.\n"
            "- M1000: Four-digit shared fact\n"
            "- M001: First worker fact"
        )
        self.assertIn(expected_section, prospective)
        self.assertEqual(prospective.count("- M1000: Four-digit shared fact"), 1)
        self.assertGreater(
            prospective.index("## Referenced memory"),
            prospective.index("## Verification"),
        )
        for body in ("FIRST FULL BODY MUST NOT LEAK", "SECOND FULL BODY MUST NOT LEAK"):
            self.assertNotIn(body, prospective)
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_stored_memory_probe")
        stored_result = module["stored_context_capsule_components"](prospective)
        self.assertEqual(stored_result["text"], prospective)
        self.assertIn("Referenced memory", dict(stored_result["section_chars"]))

        self.baton(project, "run", task_id, check=True)
        work = runtime / "work" / task_id
        _digest, launch_capsule = (work / "attempt-1.brief.md").read_text().split(
            "\n\n", 1,
        )
        prompt = (work / "attempt-1.prompt.md").read_text()
        self.assertEqual(launch_capsule, prospective)
        self.assertTrue(prompt.startswith(launch_capsule))
        self.assertTrue(prompt.endswith(launch_capsule))
        self.assertEqual(prompt.count(launch_capsule), 2)

    def test_memory_reference_errors_fail_compile_preview_launch_and_validate(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        self.write_memory(project, [
            *[(f"M00{number}", "W", f"Worker fact {number}", "body")
              for number in range(1, 8)],
            ("M010", "O", "Orchestrator secret", "orchestrator body"),
        ])
        cases = [
            ("unknown reference", "M999", "referenced memory id M999 is missing from memory.md"),
            (
                "orchestrator reference", "M010",
                "referenced memory id M010 is orchestrator-only [O]; worker capsules "
                "may reference only [W] or [B]",
            ),
            (
                "too many references", "M001 M002 M003 M004 M005 M006 M007",
                "Context references 7 memory entries; maximum is 6; split the task "
                "or remove references",
            ),
        ]
        runtime = project / ".baton"
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_memory_error_probe")
        entries = module["memory_index_entries"]((runtime / "memory.md").read_text())
        task_cases = []
        for title, context, message in cases:
            task_id = self.create_task(project, title, [f"{title.replace(' ', '-')}/**"])
            spec_path = runtime / "tasks" / f"{task_id}.md"
            spec_path.write_text(spec_path.read_text().replace(
                "List the paths and facts the worker needs. Reference memory ids when useful.",
                context,
            ))
            task_cases.append((task_id, spec_path, message))
            with self.assertRaisesRegex(ValueError, re.escape(message)):
                module["compile_context_capsule"](
                    self.state(project, task_id), spec_path.read_text(), entries,
                )
            preview = self.baton(project, "task", "capsule", task_id)
            launch = self.baton(project, "run", task_id)
            self.assertNotEqual(preview.returncode, 0)
            self.assertNotEqual(launch.returncode, 0)
            self.assertIn(message, preview.stderr)
            self.assertIn(message, launch.stderr)

        validation = self.baton(project, "validate")
        self.assertNotEqual(validation.returncode, 0)
        for _task_id, _spec_path, message in task_cases:
            self.assertIn(message, validation.stdout)

    def test_memory_add_rejects_structural_values_and_show_requires_indexed_id(self):
        invalid_values = (
            ("   ", "body", "summary"),
            ("line one\nline two", "body", "summary"),
            (
                "valid summary",
                "real body\n### M999 [W] injected heading\nsynthetic body",
                "entry heading",
            ),
        )
        for index, (summary, body, message) in enumerate(invalid_values):
            with self.subTest(summary=summary, body=body):
                project = self.make_project(f"invalid-memory-add-{index}")
                memory = project / ".baton" / "memory.md"
                before = memory.read_bytes()
                rejected = self.baton(
                    project, "memory", "add", "--for", "worker", summary, body,
                )
                self.assertEqual(rejected.returncode, 1)
                self.assertIn(message, rejected.stderr)
                self.assertEqual(memory.read_bytes(), before)

        project = self.make_project("unindexed-memory-heading")
        self.write_memory(project, [
            (
                "M001", "W", "Real memory", "real body\n"
                "### M999 [W] injected heading\nsynthetic body",
            ),
        ])
        shown = self.baton(project, "memory", "show", "M999")
        self.assertEqual(shown.returncode, 1)
        self.assertIn("orphan full entry headings for ids: M999", shown.stderr)

    def test_memory_add_preserves_inline_entries_text_in_earlier_summary(self):
        project = self.make_project("inline-entries-memory-summary")
        first_summary = "Markdown may contain inline ## Entries text"
        first = self.baton(
            project, "memory", "add", "--for", "worker",
            first_summary, "first body", check=True,
        )
        second = self.baton(
            project, "memory", "add", "--for", "both",
            "Normal second summary", "second body", check=True,
        )

        self.assertEqual(first.stdout, "added M001\n")
        self.assertEqual(second.stdout, "added M002\n")
        indexed = self.baton(project, "memory", "index", check=True)
        self.assertEqual(indexed.stdout, (
            f"M001 [W] {first_summary}\n"
            "M002 [B] Normal second summary\n"
        ))
        first_shown = self.baton(project, "memory", "show", "M001", check=True)
        second_shown = self.baton(project, "memory", "show", "M002", check=True)
        self.assertEqual(
            first_shown.stdout,
            f"### M001 [W] {first_summary}\nfirst body\n",
        )
        self.assertEqual(
            second_shown.stdout,
            "### M002 [B] Normal second summary\nsecond body\n",
        )
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_added_memory_fresh_parse_probe",
        )
        entries, bodies = module["memory_records"](
            (project / ".baton" / "memory.md").read_text()
        )
        self.assertEqual([entry[0] for entry in entries], ["M001", "M002"])
        self.assertEqual(set(bodies), {"M001", "M002"})
        self.assertEqual(self.baton(project, "validate").returncode, 0)
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        task_id = self.create_task(project, "valid memory launch", ["memory/**"])
        spec_path = project / ".baton" / "tasks" / f"{task_id}.md"
        spec_path.write_text(spec_path.read_text().replace(
            "List the paths and facts the worker needs. Reference memory ids when useful.",
            "Use M001 and M002.",
        ))
        self.baton(project, "task", "capsule", task_id, check=True)
        self.baton(project, "run", task_id, check=True)

    def test_memory_add_rejects_duplicate_entries_heading_inside_body(self):
        project = self.make_project("entries-heading-memory-body")
        body = "Body introduction\n\n## Entries\n\nOrdinary body section"
        memory = project / ".baton" / "memory.md"
        before = memory.read_bytes()

        rejected = self.baton(
            project, "memory", "add", "--for", "worker",
            "Body uses an Entries heading", body,
        )

        self.assertEqual(rejected.returncode, 1)
        self.assertIn("memory structure", rejected.stderr)
        self.assertNotIn("Traceback", rejected.stderr)
        self.assertEqual(memory.read_bytes(), before)
        self.assertEqual(self.baton(project, "validate").returncode, 0)

    def test_memory_rejects_orphan_and_noncanonical_entries_layouts_without_mutation(self):
        cases = {
            "empty-index-intervening-section": (
                "# Memory\n\n## Index\n\n## Notes\nnot memory\n\n## Entries\n\n"
                "### M001 [W] Invisible fact\ninvisible body\n"
            ),
            "populated-index-intervening-section": (
                "# Memory\n\n## Index\n- M001 [W] Indexed fact\n\n"
                "## Notes\nnot memory\n\n## Entries\n\n"
                "### M001 [W] Indexed fact\nbody\n"
            ),
            "duplicate-entries": (
                "# Memory\n\n## Index\n- M001 [W] Indexed fact\n\n"
                "## Entries\n\n### M001 [W] Indexed fact\nbody\n\n"
                "## Entries\n"
            ),
            "entries-before-index": (
                "# Memory\n\n## Entries\n\n## Index\n\n## Entries\n"
            ),
            "orphan-before-entries": (
                "# Memory\n\n### M999 [W] Orphan fact\norphan body\n\n"
                "## Index\n\n## Entries\n"
            ),
            "orphan-after-entries": (
                "# Memory\n\n## Index\n\n## Entries\n\n"
                "### M999 [W] Orphan fact\norphan body\n"
            ),
            "malformed-orphan-heading": (
                "# Memory\n\n## Index\n\n## Entries\n\n"
                "### M01 [W] Invisible fact\ninvisible body\n"
            ),
        }
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_orphan_memory_parser_probe",
        )
        records = module["memory_records"]

        for name, malformed in cases.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "memory structure"):
                    records(malformed)
                project = self.make_project("malformed-memory-" + name)
                memory = project / ".baton" / "memory.md"
                memory.write_text(malformed)
                before = memory.read_bytes()

                rejected = self.baton(
                    project, "memory", "add", "--for", "worker",
                    "New fact", "new body",
                )

                self.assertEqual(rejected.returncode, 1)
                self.assertIn("memory structure", rejected.stderr)
                self.assertNotIn("Traceback", rejected.stderr)
                self.assertEqual(memory.read_bytes(), before)

    def test_memory_add_migrates_legacy_empty_layout_and_serializes_concurrent_ids(self):
        legacy = self.make_project("legacy-empty-memory")
        legacy_memory = legacy / ".baton" / "memory.md"
        legacy_memory.write_text("# Memory\n\n## Index\n")

        migrated = self.baton(
            legacy, "memory", "add", "--for", "worker",
            "Migrated fact", "migrated body", check=True,
        )

        self.assertEqual(migrated.stdout, "added M001\n")
        self.assertIn("\n## Entries\n\n### M001", legacy_memory.read_text())
        self.assertEqual(self.baton(legacy, "validate").returncode, 0)

        project = self.make_project("concurrent-memory-add")
        barrier = threading.Barrier(3)
        results = []

        def add_memory(number):
            barrier.wait()
            results.append(self.baton(
                project, "memory", "add", "--for", "both",
                f"Concurrent fact {number}", f"body {number}",
            ))

        threads = [threading.Thread(target=add_memory, args=(number,))
                   for number in (1, 2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual([result.returncode for result in results], [0, 0])
        self.assertEqual(
            {result.stdout for result in results}, {"added M001\n", "added M002\n"},
        )
        module = runpy.run_path(
            str(SOURCE_BATON), run_name="baton_concurrent_memory_parse_probe",
        )
        entries, bodies = module["memory_records"](
            (project / ".baton" / "memory.md").read_text()
        )
        self.assertEqual([entry[0] for entry in entries], ["M001", "M002"])
        self.assertEqual(set(bodies), {"M001", "M002"})
        self.assertEqual(self.baton(project, "validate").returncode, 0)

    def test_missing_memory_body_fails_validate_preview_and_launch_without_claim(self):
        project = self.make_project("missing-memory-body")
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        self.baton(
            project, "memory", "add", "--for", "worker",
            "Required worker fact", "full body", check=True,
        )
        task_id = self.create_task(project, "missing memory body", ["memory/**"])
        runtime = project / ".baton"
        spec_path = runtime / "tasks" / f"{task_id}.md"
        spec_path.write_text(spec_path.read_text().replace(
            "List the paths and facts the worker needs. Reference memory ids when useful.",
            "Use M001.",
        ))
        memory_path = runtime / "memory.md"
        memory_text = memory_path.read_text()
        heading = "### M001 [W] Required worker fact"
        memory_path.write_text(memory_text[:memory_text.index(heading)].rstrip() + "\n")
        before = (runtime / "tasks" / f"{task_id}.json").read_bytes()

        shown = self.baton(project, "memory", "show", "M001")
        validation = self.baton(project, "validate")
        preview = self.baton(project, "task", "capsule", task_id, "--raw")
        launch = self.baton(project, "run", task_id)

        for result in (shown, preview, launch):
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("M001", result.stdout + result.stderr)
        self.assertNotEqual(validation.returncode, 0)
        self.assertIn("M001", validation.stdout + validation.stderr)
        self.assertEqual((runtime / "tasks" / f"{task_id}.json").read_bytes(), before)

    def test_memory_add_rejects_unicode_line_separators_without_mutation(self):
        for separator in ("\u2028", "\u2029"):
            for variant, summary in (
                    ("embedded", f"safe{separator}unsafe"),
                    ("trailing", f"trailing{separator}")):
                with self.subTest(separator=hex(ord(separator)), variant=variant):
                    project = self.make_project(
                        f"unicode-memory-{ord(separator):x}-{variant}"
                    )
                    memory = project / ".baton" / "memory.md"
                    before = memory.read_bytes()
                    rejected = self.baton(
                        project, "memory", "add", "--for", "worker",
                        summary, "body",
                    )
                    self.assertEqual(rejected.returncode, 1)
                    self.assertIn("single-line", rejected.stderr)
                    self.assertNotIn("Traceback", rejected.stderr)
                    self.assertEqual(memory.read_bytes(), before)
                    indexed = self.baton(project, "memory", "index")
                    self.assertEqual(
                        indexed.returncode, 0, indexed.stdout + indexed.stderr,
                    )

    def test_memory_index_parser_is_strict_and_preserves_four_digit_ids(self):
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_memory_parser_probe")
        parse = module["memory_index_entries"]
        records = module["memory_records"]
        valid = (
            "# Memory\n\n## Index\n"
            "- M1000 [B] Shared fact\n"
            "- M001 [W] Worker fact\n\n"
            "## Entries\n\n"
            "### M1000 [B] Shared fact\nshared body\n\n## Body details\nlegacy markdown\n\n"
            "### M001 [W] Worker fact\nworker body\n"
        )
        self.assertEqual(
            parse(valid),
            [("M1000", "B", "Shared fact"), ("M001", "W", "Worker fact")],
        )
        with self.assertRaisesRegex(ValueError, "malformed"):
            parse(valid.replace("- M001 [W] Worker fact", "- M01 [W] Worker fact"))
        with self.assertRaisesRegex(ValueError, "duplicate id M1000"):
            parse(valid.replace("M001 [W] Worker fact", "M1000 [W] Worker fact"))
        entries, bodies = records(valid)
        self.assertEqual(entries, parse(valid))
        self.assertEqual(
            bodies["M1000"],
            "### M1000 [B] Shared fact\nshared body\n\n## Body details\nlegacy markdown",
        )
        damaged = {
            "missing": valid.replace(
                "\n### M001 [W] Worker fact\nworker body", "",
            ),
            "mismatched": valid.replace(
                "### M001 [W] Worker fact", "### M001 [O] Different fact",
            ),
            "duplicate": valid + "\n### M001 [W] Worker fact\nduplicate body\n",
            "orphan": valid + "\n### M999 [W] Orphan fact\norphan body\n",
        }
        for problem, text in damaged.items():
            with self.subTest(problem=problem):
                with self.assertRaisesRegex(
                        ValueError, rf"{problem}.*M001" if problem != "orphan"
                        else r"orphan.*M999"):
                    records(text)

        project = self.make_project()
        memory = project / ".baton" / "memory.md"
        memory.write_text(valid.replace("M001 [W] Worker fact", "M1000 [W] Worker fact"))
        validation = self.baton(project, "validate")
        self.assertNotEqual(validation.returncode, 0)
        self.assertIn("memory index has duplicate id M1000", validation.stdout)

    def test_no_reference_format_is_unchanged_and_memory_counts_toward_budget(self):
        project = self.make_project()
        self.write_memory(project, [
            ("M001", "W", "A deliberately long summary for capsule budgeting", "body"),
        ])
        task_id = self.create_task(project, "format stability", ["stable/**"])
        runtime = project / ".baton"
        module = runpy.run_path(str(SOURCE_BATON), run_name="baton_memory_budget_probe")
        task = self.state(project, task_id)
        spec_path = runtime / "tasks" / f"{task_id}.md"
        spec = spec_path.read_text().replace(
            "Complete the format stability task.",
            "Complete the format stability task while mentioning M999 outside Context.",
        )
        entries = module["memory_index_entries"]((runtime / "memory.md").read_text())
        capsule = module["compile_context_capsule"](task, spec, entries)
        expected = (
            "# Critical Context Capsule\n\n"
            f"Task: {task_id}: format stability\n"
            "Scope: stable/**\n\n"
            "## Objective\nComplete the format stability task while mentioning M999 "
            "outside Context.\n\n"
            "## Acceptance criteria\n- The targeted task behavior is verified.\n\n"
            "## Not allowed\n- No changes outside the task scope.\n"
            "- No unrelated cleanup or new dependencies.\n\n"
            "## Verification\n- Add exact, targeted commands."
        )
        self.assertEqual(capsule, expected)
        self.assertNotIn("Referenced memory", capsule)

        baseline = module["context_capsule_components"](task, spec, entries)
        referenced_spec = spec.replace(
            "List the paths and facts the worker needs. Reference memory ids when useful.",
            "Load M001.",
        )
        referenced = module["context_capsule_components"](
            task, referenced_spec, entries, baseline["chars"],
        )
        self.assertGreater(referenced["overflow"], 0)
        self.assertIn("Referenced memory", dict(referenced["section_chars"]))
        spec_path.write_text(referenced_spec)
        self.configure(
            project, self.write_worker(NO_CHANGE_WORKER),
            capsule_max_chars=baseline["chars"],
        )
        preview = self.baton(project, "task", "capsule", task_id)
        self.assertNotEqual(preview.returncode, 0)
        self.assertIn("- Referenced memory: ", preview.stdout)
        self.assertIn("capsule_max_chars=", preview.stderr)

    def test_review_brief_warns_on_memory_drift_and_shows_launch_capsule(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        self.write_memory(project, [
            ("M001", "W", "Original launch summary", "body"),
        ])
        task_id = self.create_task(project, "review memory drift", ["review/**"])
        runtime = project / ".baton"
        spec = runtime / "tasks" / f"{task_id}.md"
        spec.write_text(spec.read_text().replace(
            "List the paths and facts the worker needs. Reference memory ids when useful.",
            "Use M001.",
        ))
        self.baton(project, "run", task_id, check=True)
        _digest, stored_capsule = (
            runtime / "work" / task_id / "attempt-1.brief.md"
        ).read_text().split("\n\n", 1)
        memory = runtime / "memory.md"
        memory.write_text(memory.read_text().replace(
            "Original launch summary", "Edited after launch summary",
        ))

        review, _token = self.review_brief_token(project, task_id)
        warning = (
            "WARNING: capsule inputs drifted since launch (spec or memory changed); "
            "showing launch capsule"
        )
        self.assertTrue(review.stdout.startswith(stored_capsule + "\n"))
        self.assertEqual(review.stdout.count(warning), 1)
        self.assertIn("- M001: Original launch summary", review.stdout)
        self.assertNotIn("Edited after launch summary", review.stdout)

    def test_memory_archive_and_prompt_spec_alignment(self):
        project = self.make_project()
        self.baton(project, "memory", "add", "--for", "worker",
                   "Use the local environment", "Do not install global packages.", check=True)
        index = self.baton(project, "memory", "index", "--for", "worker", check=True)
        self.assertIn("M001", index.stdout)
        shown = self.baton(project, "memory", "show", "M001", check=True)
        self.assertIn("Do not install global packages.", shown.stdout)
        self.assertEqual(self.baton(project, "validate").returncode, 0)

        spec = (ROOT / "SPEC.md").read_text()
        prompt = (ROOT / "prompts" / "create-framework.md").read_text()
        embedded = prompt.split("<!-- BEGIN SPEC -->\n", 1)[1].split(
            "\n<!-- END SPEC -->", 1
        )[0]
        self.assertEqual(embedded, spec.rstrip())

        orchestrator = (ROOT / "framework" / "orchestrator.md").read_text()
        use_prompt = (ROOT / "prompts" / "use-framework.md").read_text()
        example_text = (ROOT / "framework" / "config.example.toml").read_text()
        question = (
            "Which model and reasoning level should Baton use for hard, medium, "
            "and easy tasks? You can specify each one or ask me to derive the "
            "settings from the current orchestrator."
        )
        ui_rule = (
            "Ask this as a persistent plain-text question that remains visible "
            "until answered. Never use a transient form; expiration or dismissal "
            "is not an answer and must not be treated as selecting any option."
        )
        self.assertEqual(orchestrator.count(question), 1)
        self.assertEqual(orchestrator.count(ui_rule), 1)
        self.assertLess(abs(orchestrator.index(question) - orchestrator.index(ui_rule)), 600)
        normalized_orchestrator = " ".join(orchestrator.split())
        for phrase in (
            "unsure", "continues the task without settings", "do not repeat the initial question",
            "reliable harness-provided context", "read-only local", "never infer",
            "next lower available reasoning", "with only two levels",
            "already the minimum", "permission before lowering", "Omission is not approval",
            "avoided an unapproved downgrade", "display metadata alone is insufficient",
        ):
            self.assertIn(phrase.lower(), normalized_orchestrator.lower())
        self.assertIn("internally and silently", orchestrator)
        self.assertIn("Never ask the user to run", orchestrator)
        for path in (
            ROOT / "framework" / "orchestrator.md",
            ROOT / "framework" / "config.example.toml",
            ROOT / "prompts" / "use-framework.md",
            ROOT / "prompts" / "improve-framework.md",
            ROOT / "skill" / "SKILL.md",
            ROOT / "SPEC.md",
            ROOT / "summary.md",
        ):
            text = path.read_text()
            for stale in (
                "Harness memory", "use the defaults", "documented defaults",
                "GPT 5.6", "Claude Code Opus 4.8", "Claude Opus 4.8",
            ):
                self.assertNotIn(stale, text, f"{stale!r} remains in {path}")
        for text in (orchestrator, use_prompt):
            normalized = " ".join(text.split())
            self.assertIn("0 workers for this request", normalized)
        self.assertIn("stats --task ID", " ".join(use_prompt.split()))
        self.assertIn(
            "`--task ID` once for every unique task",
            " ".join(orchestrator.split()),
        )
        self.assertIn("runtime-wide", orchestrator)
        self.assertIn("whole Baton runtime", use_prompt)
        self.assertIn("Never omit `--tier`", orchestrator)

        with (ROOT / "framework" / "config.example.toml").open("rb") as source:
            config = tomllib.load(source)
        self.assertNotIn("worker", config.get("commands", {}))
        self.assertNotIn("tiers", config)


if __name__ == "__main__":
    unittest.main(verbosity=2)
