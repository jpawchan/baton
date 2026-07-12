#!/usr/bin/env python3
"""End-to-end tests for Attention Relay."""

import hashlib
import json
import os
import re
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
brief = subprocess.run(
    [sys.executable, str(rd / "relay"), "task", "brief", tid, "--phase", "report"],
    cwd=root, check=True, capture_output=True, text=True,
)
token = next(line.removeprefix("Brief token: ") for line in brief.stdout.splitlines()
             if line.startswith("Brief token: "))
status = os.environ.get("SUBMIT_STATUS", "needs_review")
report.write_text(
    f"# {tid} report\n\n## Result\n{status}\n\n## Changes\n- updated task output\n\n"
    "## Verification\n- worker completed\n\n## Decisions and risks\n- none\n"
)
finish = [sys.executable, str(rd / "relay"), "task", "finish", tid,
          "--status", status, "--brief", token]
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
if os.environ.get("EXIT_CODE"):
    raise SystemExit(int(os.environ["EXIT_CODE"]))
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
brief = subprocess.run(
    [sys.executable, str(rd / "relay"), "task", "brief", tid, "--phase", "report"],
    cwd=root, check=True, capture_output=True, text=True,
)
token = next(line.removeprefix("Brief token: ") for line in brief.stdout.splitlines()
             if line.startswith("Brief token: "))
report.write_text(
    "# no-change report\n\n## Result\nneeds_review\n\n## Changes\n- no changes\n\n"
    "## Verification\n- worker completed\n\n## Decisions and risks\n- none\n"
)
subprocess.run([sys.executable, str(rd / "relay"), "task", "finish", tid,
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


TIER_TIMEOUT_WORKER = r'''
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
if task["tier"] == "short":
    time.sleep(10)

report = rd / "work" / tid / f"attempt-{attempt}.report.md"
report.parent.mkdir(parents=True, exist_ok=True)
brief = subprocess.run(
    [sys.executable, str(rd / "relay"), "task", "brief", tid, "--phase", "report"],
    cwd=root, check=True, capture_output=True, text=True,
)
token = next(line.removeprefix("Brief token: ") for line in brief.stdout.splitlines()
             if line.startswith("Brief token: "))
report.write_text(
    "# tier timeout report\n\n## Result\nneeds_review\n\n## Changes\n- no changes\n\n"
    "## Verification\n- worker completed\n\n## Decisions and risks\n- none\n"
)
subprocess.run([sys.executable, str(rd / "relay"), "task", "finish", tid,
                "--status", "needs_review", "--brief", token], cwd=root, check=True)
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


OVERSIZED_INTEGER_RESULT_WORKER = r'''
import json
import os
from pathlib import Path

rd = Path(os.environ["RELAY_DIR"])
tid = os.environ["RELAY_TASK_ID"]
attempt = os.environ["RELAY_ATTEMPT"]
path = rd / "work" / tid / f"attempt-{attempt}.result.json"
path.write_text(
    '{"status":"failed","note":"otherwise valid","at":"now","lease":'
    + json.dumps(os.environ["RELAY_LEASE"])
    + ',"changed_paths":[],"oversized":' + "9" * 5000 + "}\n"
)
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
brief = subprocess.run(
    [sys.executable, str(rd / "relay"), "task", "brief", tid, "--phase", "report"],
    cwd=root, check=True, capture_output=True, text=True,
)
token = next(line.removeprefix("Brief token: ") for line in brief.stdout.splitlines()
             if line.startswith("Brief token: "))
report.write_text(
    "# staged report\n\n## Result\nneeds_review\n\n## Changes\n- staged file\n\n"
    "## Verification\n- worker completed\n\n## Decisions and risks\n- none\n"
)
subprocess.run([sys.executable, str(rd / "relay"), "task", "finish", tid,
                "--status", "needs_review", "--brief", token,
                "--changed", "new/staged.txt"],
               cwd=root, check=True)
'''


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="attention-relay-test-")
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
            self.command([SOURCE_RELAY, "init", project], project, check=True)
        return project

    def relay(self, project, *args, env=None, check=False, timeout=15):
        return self.command(
            [project / ".attention-relay" / "relay", *args], project,
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
            "[limits]\n"
            f"max_parallel = {max_parallel}\n"
            f"capsule_max_chars = {capsule_max_chars}\n"
            f"worker_timeout_minutes = {timeout_minutes}\n"
        )
        (project / ".attention-relay" / "config.toml").write_text(config)

    def task_create_command(self, title, scope=None, depends_on=None, tier=None):
        args = ["task", "create", "--title", title]
        for item in scope or []:
            args += ["--scope", item]
        for item in depends_on or []:
            args += ["--depends-on", item]
        if tier is not None:
            args += ["--tier", tier]
        return args

    def create_task(self, project, title, scope=None, depends_on=None, tier=None):
        args = self.task_create_command(title, scope, depends_on, tier)
        result = self.relay(project, *args, check=True)
        task_id = result.stdout.split()[1]
        spec = project / ".attention-relay" / "tasks" / f"{task_id}.md"
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
        runtime = project / ".attention-relay"
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

    def try_create_task(self, project, title, scope=None, depends_on=None, tier=None):
        args = self.task_create_command(title, scope, depends_on, tier)
        return self.relay(project, *args)

    def state(self, project, task_id):
        path = project / ".attention-relay" / "tasks" / f"{task_id}.json"
        return json.loads(path.read_text())

    def lease_task(self, project, task_id, lease):
        runtime = project / ".attention-relay"
        state_path = runtime / "tasks" / f"{task_id}.json"
        task = json.loads(state_path.read_text())
        task["status"] = "running"
        task["runner"] = {"pid": None, "started_at": "now", "lease": lease}
        state_path.write_text(json.dumps(task))
        spec = (runtime / "tasks" / f"{task_id}.md").read_text()
        module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_brief_probe")
        memory_entries = module["memory_index_entries"](
            (runtime / "memory.md").read_text()
        )
        capsule = module["compile_context_capsule"](task, spec, memory_entries)
        digest = hashlib.sha256(capsule.encode()).hexdigest()
        brief = runtime / "work" / task_id / f"attempt-{task['attempt']}.brief.md"
        brief.parent.mkdir(parents=True, exist_ok=True)
        brief.write_text(f"Content digest: sha256:{digest}\n\n{capsule}")
        return {
            "RELAY_TASK_ID": task_id,
            "RELAY_ATTEMPT": str(task["attempt"]),
            "RELAY_LEASE": lease,
            "RELAY_DIR": runtime,
            "RELAY_ROOT": project,
        }

    def report_brief_token(self, project, task_id, env):
        brief = self.relay(
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
        work = project / ".attention-relay" / "work" / task_id
        return project, task_id, env, work / "attempt-1.report.md", token

    def review_brief_token(self, project, task_id, env=None, include_log_tail=False):
        args = ["orchestrator", "brief", "--phase", "review", task_id]
        if include_log_tail:
            args.append("--include-log-tail")
        brief = self.relay(
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
        return self.relay(
            project, "task", "accept", task_id, "--brief", token, check=True,
        )

    def test_init_requires_git_and_creates_only_runtime_files(self):
        plain = self.base / "plain"
        plain.mkdir()
        result = self.command([SOURCE_RELAY, "init", plain], plain)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((plain / ".attention-relay").exists())

        project = self.make_project()
        initialized = self.command([SOURCE_RELAY, "init", project], project, check=True)
        self.assertIn(
            ".attention-relay/relay orchestrator brief --phase start",
            initialized.stdout,
        )
        lines = (project / ".gitignore").read_text().splitlines()
        self.assertEqual(lines.count(".attention-relay/"), 1)
        runtime = project / ".attention-relay"
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
        self.assertFalse((nested / ".attention-relay").exists())

        symlink_project = self.make_project("symlink-project", initialize=False)
        external = self.base / "external"
        external.mkdir()
        (external / "sentinel").write_text("unchanged\n")
        (symlink_project / ".attention-relay").symlink_to(
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
        self.assertFalse((submodule_project / ".attention-relay").exists())

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
        truncated = self.create_task(project, "x" * 39 + " next", ["slug/**"])
        self.assertEqual(truncated, "T006-" + "x" * 39)
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
            work = project / ".attention-relay" / "work" / task_id
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
        self.relay(project, "run", check=True)
        self.accept_task(project, first)

        human = project / "same" / "human.txt"
        human.write_text("already here\n")
        second = self.create_task(project, "second", ["same/**"], [first])
        self.relay(project, "run", check=True)
        diff = (project / ".attention-relay" / "work" / second / "attempt-1.diff").read_text()
        self.assertIn(f"{second}.txt", diff)
        self.assertNotIn(f"{first}.txt", diff)
        self.assertNotIn("human.txt", diff)

        self.relay(project, "task", "return", second, "--reason", "retry", check=True)
        no_change = self.write_worker(NO_CHANGE_WORKER)
        self.configure(project, no_change)
        self.relay(project, "run", second, check=True)
        retry_diff = project / ".attention-relay" / "work" / second / "attempt-2.diff"
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
            project / ".attention-relay" / "work" / task_id
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
        diff = project / ".attention-relay" / "work" / task_id / "attempt-1.diff"
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
        spec = project / ".attention-relay" / "tasks" / f"{task_id}.md"
        self.assertIn("Use option A", spec.read_text())

    def test_decision_question_text_is_sanitized_flattened_and_exactly_bounded(self):
        module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_decision_text_probe")
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
        runtime = project / ".attention-relay"
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

        status = self.relay(project, "status", check=True)
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
            fallback_project / ".attention-relay" / "tasks" / f"{fallback}.json"
        )
        fallback_task = json.loads(fallback_path.read_text())
        fallback_task["status"] = "needs_decision"
        fallback_task["last_note"] = {"not": "text"}
        fallback_task["history"].append({
            "event": "worker_exited", "status": "needs_decision", "note": None,
        })
        fallback_path.write_text(json.dumps(fallback_task))
        fallback_status = self.relay(fallback_project, "status", check=True)
        self.assertIn(f"\n- decide {fallback}\n", fallback_status.stdout)
        self.assertNotIn("worker question:", fallback_status.stdout)

    def test_start_brief_bounds_questions_and_recommends_a_real_decision_id(self):
        project = self.make_project()
        decisions = [self.create_task(project, f"start decision {index}") for index in range(5)]
        runtime = project / ".attention-relay"
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

        started = self.relay(
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
            f"Recommended next command: relay task decide {decisions[0]} --answer ANSWER",
            started.stdout,
        )
        self.assertNotIn("relay task decide +1 more", started.stdout)

    def test_start_brief_difficulty_levels_are_missing_only_parseable_and_bounded(self):
        project = self.make_project()

        def difficulty_section(output):
            marker = "\nDifficulty levels:\n"
            self.assertEqual(output.count(marker), 1)
            body = output.split(marker, 1)[1].split("\n\n", 1)[0]
            return ["Difficulty levels:", *body.splitlines()]

        started = self.relay(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        ).stdout
        section = difficulty_section(started)
        self.assertLessEqual(len(section), 12)
        self.assertIn(
            "- Configured conventional levels: none; missing: hard, medium, easy.",
            section,
        )
        self.assertTrue(any(
            "ask the USER" in line and "model (and optionally provider)" in line
            for line in section
        ))
        snippet = "\n".join(
            line.removeprefix("# ") for line in section if line.startswith("# ")
        )
        parsed = tomllib.loads(snippet)
        self.assertEqual(list(parsed["tiers"]), ["hard", "medium", "easy"])
        for tier in parsed["tiers"].values():
            command = tier["command"]
            self.assertIn("--ignore-rules", command)
            self.assertIn("-m MODEL", command)
            self.assertIn("--provider PROVIDER", command)
            self.assertIn("-q {prompt}", command)
        self.assertTrue(any(
            "worker_timeout_minutes/capsule_max_chars are optional" in line
            for line in section
        ))
        self.assertTrue(any(
            "no per-invocation reasoning override" in line for line in section
        ))
        self.assertTrue(any("optional conventions" in line for line in section))

        config = project / ".attention-relay" / "config.toml"
        config.write_text(config.read_text() + (
            "\n[tiers.hard]\ncapsule_max_chars = 5000\n"
            "\n[tiers.custom]\ncapsule_max_chars = 4500\n"
        ))
        partial = self.relay(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        ).stdout
        partial_section = difficulty_section(partial)
        self.assertLessEqual(len(partial_section), 12)
        self.assertIn(
            "- Configured conventional levels: hard; missing: medium, easy.",
            partial_section,
        )
        partial_snippet = "\n".join(
            line.removeprefix("# ")
            for line in partial_section if line.startswith("# ")
        )
        self.assertEqual(
            list(tomllib.loads(partial_snippet)["tiers"]), ["medium", "easy"],
        )
        self.assertNotIn("[tiers.hard]", partial_snippet)
        self.assertNotIn("tiers.custom", "\n".join(partial_section))

        config.write_text(config.read_text() + (
            "\n[tiers.medium]\nworker_timeout_minutes = 45\n"
            "\n[tiers.easy]\nworker_timeout_minutes = 20\n"
        ))
        complete = self.relay(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        ).stdout
        self.assertNotIn("Difficulty levels:", complete)

    def test_worker_role_and_live_runner_guards(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "guarded", ["guarded/**"])
        marker = self.base / "finished"
        self_accept = self.base / "self-accept"
        proc = subprocess.Popen(
            [str(project / ".attention-relay" / "relay"), "run", task_id],
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
        diff = project / ".attention-relay" / "work" / task_id / "attempt-1.diff"
        self.assertIn("after-finish.txt", diff.read_text())

    def test_worker_phase_briefs_and_default_finish_gate(self):
        project = self.make_project()
        task_id = self.create_task(project, "brief gate", ["brief/**"])
        env = self.lease_task(project, task_id, "lease-one")
        runtime = project / ".attention-relay"
        token_path = runtime / "work" / task_id / "finish-brief-token.json"

        unleased = self.relay(project, "task", "brief", task_id, "--phase", "edit")
        self.assertNotEqual(unleased.returncode, 0)
        for phase, heading in (("edit", "Edit"), ("verify", "Verify")):
            output = self.relay(
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
            rejected = self.relay(project, *command, env=env)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("report-phase brief token is required", rejected.stderr)

        self.relay(project, *finish, "--brief", second_token, env=env, check=True)
        self.assertFalse(token_path.exists())
        result = runtime / "work" / task_id / "attempt-1.result.json"
        result.unlink()
        replay = self.relay(
            project, *finish, "--brief", second_token, env=env,
        )
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("report-phase brief token is required", replay.stderr)

    def test_phase_brief_receipts_are_bounded_and_malformed_files_are_replaced(self):
        project = self.make_project()
        task_id = self.create_task(project, "brief receipts", ["receipts/**"])
        env = self.lease_task(project, task_id, "receipt-lease")
        receipt_path = (
            project / ".attention-relay" / "work" / task_id
            / "attempt-1.briefs.json"
        )
        receipt_path.write_text('{"phases": ["malformed"], "token": "must-disappear"}\n')

        self.relay(
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

        self.relay(
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
        work = project / ".attention-relay" / "work" / task_id
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

        self.relay(
            project, "task", "brief", task_id, "--phase", "edit",
            env=env, check=True,
        )
        replaced = json.loads(receipt_path.read_text())
        self.assertEqual(set(replaced["phases"]), {"edit"})
        self.assertEqual(replaced["phases"]["edit"]["count"], 1)

    def test_phase_sequence_gate_enforces_order_and_edit_invalidates_report_token(self):
        project = self.make_project()
        config = project / ".attention-relay" / "config.toml"
        config.write_text(config.read_text().replace(
            "phase_sequence_requires_briefs = false",
            "phase_sequence_requires_briefs = true",
        ))
        task_id = self.create_task(project, "phase sequence", ["sequence/**"])
        env = self.lease_task(project, task_id, "sequence-lease")

        verify = self.relay(
            project, "task", "brief", task_id, "--phase", "verify", env=env,
        )
        self.assertEqual(
            verify.stderr,
            f"error: phase sequence requires an edit brief; run `relay task brief "
            f"{task_id} --phase edit`\n",
        )
        report = self.relay(
            project, "task", "brief", task_id, "--phase", "report", env=env,
        )
        self.assertEqual(report.stderr, verify.stderr)

        self.relay(
            project, "task", "brief", task_id, "--phase", "edit",
            env=env, check=True,
        )
        report = self.relay(
            project, "task", "brief", task_id, "--phase", "report", env=env,
        )
        self.assertEqual(
            report.stderr,
            f"error: phase sequence requires a verify brief; run `relay task brief "
            f"{task_id} --phase verify`\n",
        )
        self.relay(
            project, "task", "brief", task_id, "--phase", "verify",
            env=env, check=True,
        )
        _brief, stale_token = self.report_brief_token(project, task_id, env)
        token_path = (
            project / ".attention-relay" / "work" / task_id
            / "finish-brief-token.json"
        )
        self.assertTrue(token_path.exists())
        self.relay(
            project, "task", "brief", task_id, "--phase", "edit",
            env=env, check=True,
        )
        self.assertFalse(token_path.exists())
        stale = self.relay(
            project, "task", "finish", task_id, "--status", "failed",
            "--brief", stale_token, env=env,
        )
        self.assertIn("fresh report-phase brief token is required", stale.stderr)
        _brief, fresh_token = self.report_brief_token(project, task_id, env)
        self.relay(
            project, "task", "finish", task_id, "--status", "failed",
            "--brief", fresh_token, env=env, check=True,
        )

    def test_phase_sequence_gate_defaults_off_and_never_blocks_briefs(self):
        project = self.make_project()
        task_id = self.create_task(project, "phase sequence off", ["sequence-off/**"])
        env = self.lease_task(project, task_id, "sequence-off-lease")
        self.report_brief_token(project, task_id, env)
        self.relay(
            project, "task", "brief", task_id, "--phase", "verify",
            env=env, check=True,
        )
        receipt_path = (
            project / ".attention-relay" / "work" / task_id
            / "attempt-1.briefs.json"
        )
        self.assertEqual(
            set(json.loads(receipt_path.read_text())["phases"]), {"report", "verify"},
        )

    def test_finish_gate_can_be_disabled(self):
        project = self.make_project()
        config = project / ".attention-relay" / "config.toml"
        config.write_text(config.read_text().replace(
            "finish_requires_brief = true", "finish_requires_brief = false",
        ))
        task_id = self.create_task(project, "gate off", ["off/**"])
        env = self.lease_task(project, task_id, "gate-off-lease")
        report = (
            project / ".attention-relay" / "work" / task_id
            / "attempt-1.report.md"
        )
        report.write_text(self.report_text())
        self.relay(
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
        state_path = project / ".attention-relay" / "tasks" / f"{task_id}.json"
        state_before = state_path.read_bytes()
        token_before = token_path.read_bytes()
        command = [
            "task", "finish", task_id, "--status", "needs_review",
            "--brief", token,
        ]

        rejected = self.relay(project, *command, env=env)
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
        self.relay(project, *command, env=env, check=True)
        self.assertFalse(token_path.exists())
        self.assertEqual(json.loads(result_path.read_text())["status"], "needs_review")

    def test_report_empty_verification_body_is_rejected(self):
        project, task_id, env, report, token = self.prepare_finish("empty-verification")
        report.write_text(self.report_text().replace("- test verification", ""))
        rejected = self.relay(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", token, env=env,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("report section `## Verification` has an empty body", rejected.stderr)
        self.assertIn("same `--brief` token", rejected.stderr)

    def test_report_result_must_match_submitted_status(self):
        project, task_id, env, report, token = self.prepare_finish("result-mismatch")
        report.write_text(self.report_text(status="failed"))
        rejected = self.relay(
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
        rejected = self.relay(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", token, env=env,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("missing required report section `## Verification`", rejected.stderr)

    def test_report_heading_inside_three_space_indented_fence_does_not_count(self):
        project, task_id, env, report, token = self.prepare_finish("indented-fence")
        report.write_text(
            "# task report\n\n## Result\nneeds_review\n\n## Changes\n- changes\n\n"
            "   ```\n## Verification\n- fake verification\n   ````\n\n"
            "## Decisions and risks\n- none\n"
        )
        rejected = self.relay(
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
        rejected = self.relay(
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
        rejected = self.relay(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", token, env=env,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("missing required report section `## Verification`", rejected.stderr)
        self.assertNotIn("missing required report section `## Decisions", rejected.stderr)

    def test_crlf_structured_report_is_accepted(self):
        project, task_id, env, report, token = self.prepare_finish("crlf-report")
        report.write_bytes(self.report_text(newline="\r\n").encode("utf-8"))
        self.relay(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", token, env=env, check=True,
        )

    def test_report_section_gate_off_accepts_free_form_report(self):
        project, task_id, env, report, token = self.prepare_finish("section-gate-off")
        config = project / ".attention-relay" / "config.toml"
        config.write_text(config.read_text().replace(
            "report_requires_sections = true", "report_requires_sections = false",
        ))
        report.write_text("free-form review report\n")
        self.relay(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", token, env=env, check=True,
        )

    def test_non_review_statuses_skip_report_section_gate(self):
        for status in ("needs_decision", "blocked", "failed"):
            with self.subTest(status=status):
                project, task_id, env, _report, token = self.prepare_finish(
                    "unstructured-" + status.replace("_", "-"),
                )
                self.relay(
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
                rejected = self.relay(
                    project, "task", "finish", task_id, "--status", "needs_review",
                    "--brief", token, env=env,
                )
                self.assertEqual(rejected.returncode, 1)
                self.assertIn("report rejected: " + expected, rejected.stderr)
                self.assertTrue(token_path.exists())

    def test_non_boolean_report_section_gate_fails_validate(self):
        project = self.make_project("invalid-report-gate")
        config = project / ".attention-relay" / "config.toml"
        config.write_text(config.read_text().replace(
            "report_requires_sections = true", 'report_requires_sections = "yes"',
        ))
        validation = self.relay(project, "validate")
        self.assertEqual(validation.returncode, 1)
        self.assertIn(
            "config: report_requires_sections must be true or false",
            validation.stdout,
        )

    def test_non_boolean_phase_sequence_gate_fails_validate(self):
        project = self.make_project("invalid-phase-sequence-gate")
        config = project / ".attention-relay" / "config.toml"
        config.write_text(config.read_text().replace(
            "phase_sequence_requires_briefs = false",
            'phase_sequence_requires_briefs = "yes"',
        ))
        validation = self.relay(project, "validate")
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

        runtime = project / ".attention-relay"
        state_path = runtime / "tasks" / f"{task_id}.json"
        task = json.loads(state_path.read_text())
        task["status"] = "needs_review"
        task.pop("runner")
        state_path.write_text(json.dumps(task))
        self.relay(
            project, "task", "return", task_id, "--reason", "retry token",
            check=True,
        )

        second_env = self.lease_task(project, task_id, "second-lease")
        report = runtime / "work" / task_id / "attempt-2.report.md"
        report.write_text("# retry report\n")
        stale = self.relay(
            project, "task", "finish", task_id, "--status", "needs_review",
            "--brief", old_token, env=second_env,
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("report-phase brief token is required", stale.stderr)

    def test_orchestrator_review_brief_accept_gate_and_worker_denial(self):
        project = self.make_project()
        self.configure(project, self.write_worker(GOOD_WORKER))
        task_id = self.create_task(project, "review gate", ["review/**"])
        self.relay(project, "run", task_id, check=True)

        missing = self.relay(project, "task", "accept", task_id)
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
            project / ".attention-relay" / "work" / task_id
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
            rejected = self.relay(
                project, "task", "accept", task_id, "--brief", token,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("review-phase brief token is required", rejected.stderr)

        worker_env = {
            "RELAY_TASK_ID": task_id, "RELAY_ATTEMPT": "1", "RELAY_LEASE": "worker",
        }
        denied = self.relay(
            project, "orchestrator", "brief", "--phase", "review", task_id,
            env=worker_env,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("worker processes cannot run orchestrator commands", denied.stderr)

        self.relay(
            project, "task", "accept", task_id, "--brief", current_token, check=True,
        )
        token_path = (
            project / ".attention-relay" / "work" / task_id
            / "review-brief-token.json"
        )
        self.assertFalse(token_path.exists())
        state_path = project / ".attention-relay" / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "needs_review"
        state_path.write_text(json.dumps(state))
        replay = self.relay(
            project, "task", "accept", task_id, "--brief", current_token,
        )
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("review-phase brief token is required", replay.stderr)

    def test_attempt_diff_summary_uses_observed_paths_and_exact_patch_state(self):
        module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_diff_stat_probe")
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
        self.relay(project, "run", task_id, check=True)
        runtime = project / ".attention-relay"
        work = runtime / "work" / task_id
        state_path = runtime / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["attempt"] = 5
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
        rejected = self.relay(
            project, "orchestrator", "brief", "--phase", "start",
            "--include-log-tail",
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(
            "--include-log-tail is valid only for the review phase", rejected.stderr,
        )

    def test_sanitize_log_text_redacts_lowercase_and_mixed_case_labels(self):
        module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_log_sanitizer_probe")
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
        self.relay(project, "run", task_id, check=True)
        _brief, stale_token = self.review_brief_token(project, task_id)
        token_path = (
            project / ".attention-relay" / "work" / task_id
            / "review-brief-token.json"
        )
        self.relay(
            project, "task", "return", task_id, "--reason", "try again", check=True,
        )
        self.assertFalse(token_path.exists())
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        self.relay(project, "run", task_id, check=True)
        invalidated = self.relay(
            project, "task", "accept", task_id, "--brief", stale_token,
        )
        self.assertNotEqual(invalidated.returncode, 0)

        gate_off = self.make_project("gate-off-accept")
        self.configure(gate_off, self.write_worker(GOOD_WORKER))
        config = gate_off / ".attention-relay" / "config.toml"
        config.write_text(config.read_text() + "\n[gates]\naccept_requires_brief = false\n")
        gate_off_id = self.create_task(gate_off, "accept gate off", ["gate-off/**"])
        self.relay(gate_off, "run", gate_off_id, check=True)
        gate_off_report = (
            gate_off / ".attention-relay" / "work" / gate_off_id
            / "attempt-1.report.md"
        )
        gate_off_report.write_text(gate_off_report.read_text() + "changed without brief\n")
        self.relay(gate_off, "task", "accept", gate_off_id, check=True)

    def test_review_evidence_mutations_reject_without_consuming_token(self):
        for artifact in ("report.md", "result.json", "diff"):
            with self.subTest(artifact=artifact):
                project = self.make_project("mutated-" + artifact.replace(".", "-"))
                self.configure(project, self.write_worker(GOOD_WORKER))
                task_id = self.create_task(project, "mutate " + artifact, ["mutate/**"])
                self.relay(project, "run", task_id, check=True)
                _brief, token = self.review_brief_token(project, task_id)
                token_path = (
                    project / ".attention-relay" / "work" / task_id
                    / "review-brief-token.json"
                )
                evidence_path = (
                    token_path.parent / f"attempt-1.{artifact}"
                )
                evidence_path.write_text(evidence_path.read_text() + "\n")

                rejected = self.relay(
                    project, "task", "accept", task_id, "--brief", token,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn(
                    "review evidence changed; run a fresh review brief",
                    rejected.stderr,
                )
                self.assertEqual(json.loads(token_path.read_text())["token"], token)
                self.assertEqual(self.state(project, task_id)["status"], "needs_review")

                _fresh, fresh_token = self.review_brief_token(project, task_id)
                self.relay(
                    project, "task", "accept", task_id,
                    "--brief", fresh_token, check=True,
                )

    def test_review_manifest_uses_launch_capsule_and_accepts_empty_diff(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        task_id = self.create_task(project, "empty evidence diff", ["empty/**"])
        self.relay(project, "run", task_id, check=True)
        work = project / ".attention-relay" / "work" / task_id
        self.assertEqual((work / "attempt-1.diff").read_text(), "")
        (work / "attempt-1.briefs.json").unlink()
        brief, token = self.review_brief_token(project, task_id)
        self.assertIn("Diff stat: no changes", brief.stdout)
        self.assertIn("Phase briefs: none recorded", brief.stdout)

        spec = project / ".attention-relay" / "tasks" / f"{task_id}.md"
        spec.write_text(spec.read_text().replace(
            f"Complete the empty evidence diff task.",
            "Complete the edited specification task.",
        ))
        self.relay(
            project, "task", "accept", task_id, "--brief", token, check=True,
        )

    def test_review_manifest_fresh_compile_accepts_without_stored_attempt_brief(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        task_id = self.create_task(project, "fresh review capsule", ["fresh/**"])
        self.relay(project, "run", task_id, check=True)
        work = project / ".attention-relay" / "work" / task_id
        (work / "attempt-1.brief.md").unlink()

        brief, token = self.review_brief_token(project, task_id)
        self.assertTrue(brief.stdout.startswith("# Critical Context Capsule\n"))
        self.assertTrue((work / "review-brief-token.json").exists())
        self.relay(
            project, "task", "accept", task_id, "--brief", token, check=True,
        )
        self.assertEqual(self.state(project, task_id)["status"], "done")

    def test_review_brief_requires_regular_complete_evidence(self):
        module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_review_hash_probe")
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
                self.relay(project, "run", task_id, check=True)
                work = project / ".attention-relay" / "work" / task_id
                (work / f"attempt-1.{missing}").unlink()
                rejected = self.relay(
                    project, "orchestrator", "brief", "--phase", "review", task_id,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertFalse((work / "review-brief-token.json").exists())

        project = self.make_project("symlink-report")
        self.configure(project, self.write_worker(GOOD_WORKER))
        task_id = self.create_task(project, "symlink report", ["symlink/**"])
        self.relay(project, "run", task_id, check=True)
        work = project / ".attention-relay" / "work" / task_id
        report = work / "attempt-1.report.md"
        external = self.base / "external-report.md"
        external.write_text(report.read_text())
        report.unlink()
        report.symlink_to(external)
        rejected = self.relay(
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
        spec = project / ".attention-relay" / "tasks" / f"{task_id}.md"
        spec.write_text(spec.read_text().replace(
            "List the paths and facts the worker needs. Reference memory ids when useful.",
            "Memory: M001.",
        ))
        self.relay(project, "run", task_id, check=True)
        self.write_memory(project, [])

        brief, token = self.review_brief_token(project, task_id)
        self.assertIn("Launch-only fact", brief.stdout)
        self.assertEqual(brief.stdout.count("WARNING:"), 1)
        self.assertIn("referenced memory id M001 is missing", brief.stdout)
        self.assertLess(len(next(
            line for line in brief.stdout.splitlines() if line.startswith("WARNING:")
        )), 600)
        self.relay(
            project, "task", "accept", task_id, "--brief", token, check=True,
        )

        no_launch = self.make_project("no-launch-capsule")
        task_id = self.create_task(no_launch, "no launch compile", ["none/**"])
        runtime = no_launch / ".attention-relay"
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
        failed = self.relay(
            no_launch, "orchestrator", "brief", "--phase", "review", task_id,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("template placeholder", failed.stderr)
        self.assertFalse((work / "review-brief-token.json").exists())

    def test_orchestrator_phase_handoff_and_next_action_capsules(self):
        project = self.make_project()
        self.configure(project, self.write_worker(GOOD_WORKER))
        task_id = self.create_task(project, "phase output", ["phase/**"])
        started = self.relay(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        )
        harness = started.stdout.split("Harness memory:\n", 1)[1].split(
            "\n\nDifficulty levels:", 1,
        )[0]
        self.assertLessEqual(len(harness.splitlines()) + 1, 12)
        for control in (
            '"autoMemoryEnabled": false',
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
            "the /memory toggle",
            '"claudeMdExcludes"',
            "managed-policy CLAUDE.md cannot be excluded",
            "claude --bare",
            "ALSO disables hooks (conflicts with hook-based injection)",
            "--ignore-rules",
            "--safe-mode: it drops user config",
            "hermes memory reset` is destructive",
            "already clean by default via the config worker command",
        ):
            self.assertIn(control, harness)
        plan = self.relay(
            project, "orchestrator", "brief", "--phase", "plan", check=True,
        )
        self.assertIn("Task-spec quality checklist:", plan.stdout)
        self.assertIn(f"{task_id} [queued]", plan.stdout)
        run_brief = self.relay(
            project, "orchestrator", "brief", "--phase", "run", check=True,
        )
        self.assertIn(f"Would run: {task_id}", run_brief.stdout)

        status = self.relay(project, "status", check=True)
        self.assertIn("\nNext actions:\n", status.stdout)
        run = self.relay(project, "run", task_id, check=True)
        self.assertIn("\nNext actions:\n", run.stdout)
        self.assertIn("attempt-1.report.md", run.stdout.rsplit("Next actions:", 1)[1])
        shown = self.relay(project, "task", "show", task_id, check=True)
        self.assertIn("\nNext actions:\n", shown.stdout)

        closed = self.relay(
            project, "orchestrator", "brief", "--phase", "close",
            "--goal", "Finish\n\x1b[31mhandoff\x1b[0m context",
            "--avoid", "Do not\n\x1b[33minherit\x1b[0m old goals",
            "--avoid", "Keep locks unchanged",
            "--avoid", "Preserve same-second dedupe",
            "--avoid", "Avoid placeholder context",
            "--avoid", "\x1b]0;title\x07" + "x" * 201,
            check=True,
        )
        handoff = project / ".attention-relay" / "orchestrator-handoff.md"
        self.assertTrue(handoff.exists())
        handoff_text = handoff.read_text()
        self.assertIn("consumed_at: (not yet)", handoff_text)
        self.assertIn("goal: Finish handoff context\n", handoff_text)
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
        self.assertIn("Start a fresh session", closed.stdout)
        started = self.relay(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        )
        self.assertIn("Current handoff:", started.stdout)
        self.assertIn("goal: Finish handoff context", started.stdout)
        for note in expected_avoids:
            self.assertIn("- " + note, started.stdout)
        self.assertNotIn("consumed_at: (not yet)", handoff.read_text())

    def test_close_brief_validates_required_and_phase_scoped_context(self):
        project = self.make_project()
        remediation = (
            "error: `--phase close` requires a nonblank goal; "
            "add `--goal TEXT`\n"
        )
        for extra in ([], ["--goal", " \n\t "]):
            with self.subTest(extra=extra):
                rejected = self.relay(
                    project, "orchestrator", "brief", "--phase", "close", *extra,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(rejected.stderr, remediation)

        too_many_args = [
            item
            for number in range(6)
            for item in ("--avoid", f"note {number}")
        ]
        too_many = self.relay(
            project, "orchestrator", "brief", "--phase", "close",
            "--goal", "Continue the work", *too_many_args,
        )
        self.assertNotEqual(too_many.returncode, 0)
        self.assertEqual(
            too_many.stderr,
            "error: at most 5 `--avoid` notes are allowed; consolidate them\n",
        )

        for phase in ("start", "plan", "run", "review"):
            for flag, value in (("--goal", "next"), ("--avoid", "risk")):
                with self.subTest(phase=phase, flag=flag):
                    rejected = self.relay(
                        project, "orchestrator", "brief", "--phase", phase,
                        flag, value,
                    )
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertEqual(
                        rejected.stderr,
                        f"error: `{flag}` is valid only for the close phase\n",
                    )

        closed = self.relay(
            project, "orchestrator", "brief", "--phase", "close",
            "--goal", "g" * 201, check=True,
        )
        handoff = project / ".attention-relay" / "orchestrator-handoff.md"
        goal_line = next(
            line for line in handoff.read_text().splitlines() if line.startswith("goal: ")
        )
        self.assertEqual(goal_line, "goal: " + "g" * 199 + "…")
        self.assertIn("avoid:\n- (fill in)\n", handoff.read_text())
        self.assertIn(goal_line, closed.stdout)

    def test_handoff_start_and_close_lock_complete_update(self):
        module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_handoff_lock_probe")
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
        globals_["read_handoff"] = lambda _relay_dir: events.append("read") or handoff
        globals_["atomic_write"] = lambda *_args: events.append("write")
        globals_["load_archived_tasks"] = lambda _relay_dir: events.append("archive") or []
        globals_["task_lock"] = lambda *_args: self.fail(
            "task lock nested in handoff lock"
        )
        globals_["now"] = lambda: "2026-01-01T00:00:01Z"
        globals_["say"] = lambda *_args: None

        module["orchestrator_start_brief"](
            "/relay", [], consume_handoff=True, include_levels_ask=False,
        )
        self.assertEqual(events, [
            ("enter", "orchestrator-handoff.lock"), "read", "write",
            ("exit", "orchestrator-handoff.lock"),
        ])
        events.clear()
        module["orchestrator_close_brief"]("/relay", [], "next goal", [])
        self.assertEqual(events, [
            ("enter", "orchestrator-handoff.lock"), "read", "archive", "write",
            ("exit", "orchestrator-handoff.lock"),
        ])

    def test_start_brief_and_hooks_do_not_write_beyond_handoff_consumption(self):
        project = self.make_project()
        runtime = project / ".attention-relay"
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
        started = self.relay(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        )
        self.assertIn("Difficulty levels:", started.stdout)
        after_start = snapshot()
        changed = {
            path for path in before if before[path] != after_start.get(path)
        } | (set(after_start) - set(before))
        self.assertEqual(changed, {"orchestrator-handoff.md"})
        self.assertNotIn("consumed_at: (not yet)", handoff.read_text())

        hook_command = [
            runtime / "relay", "hook-event", "session-start",
        ]
        for hook_input in ('{"source":"startup"}', '{"source":"compact"}'):
            hooked = subprocess.run(
                hook_command, cwd=project, input=hook_input, text=True,
                capture_output=True,
            )
            self.assertEqual(hooked.returncode, 0)
            self.assertEqual(hooked.stderr, "")
        self.assertEqual(snapshot(), after_start)

    def test_handoff_same_second_acceptance_is_emitted_once(self):
        project = self.make_project()
        task_id = self.create_task(project, "same second", ["same-second/**"])
        runtime = project / ".attention-relay"
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
        task["history"].append({"event": "accepted", "at": boundary})
        module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_boundary_probe")
        globals_ = module["orchestrator_close_brief"].__globals__
        globals_["now"] = lambda: boundary
        globals_["say"] = lambda *_args: None

        module["orchestrator_close_brief"](runtime, [task], "next goal", [])
        first = handoff_path.read_text().split("done:\n", 1)[1].split(
            "decisions:\n", 1,
        )[0]
        module["orchestrator_close_brief"](runtime, [task], "next goal", [])
        second = handoff_path.read_text().split("done:\n", 1)[1].split(
            "decisions:\n", 1,
        )[0]
        self.assertEqual(first.count(task_id) + second.count(task_id), 1)
        self.assertIn(task_id, first)
        self.assertNotIn(task_id, second)

    def test_worker_manual_uses_installed_relay_commands(self):
        worker = (ROOT / "framework" / "worker.md").read_text()
        self.assertNotIn("`relay ", worker)
        self.assertNotIn("`task finish ", worker)
        self.assertIn("python3 .attention-relay/relay task brief", worker)
        self.assertIn("python3 .attention-relay/relay task finish", worker)

    def test_claude_code_hook_fragment_is_exactly_two_matcher_free_commands(self):
        project = self.make_project()
        printed = self.relay(project, "hooks", "claude-code", check=True)
        fragment_text = printed.stdout.rsplit("\n", 2)[0]
        fragment = json.loads(fragment_text)
        commands = {
            "SessionStart": (
                '"$CLAUDE_PROJECT_DIR"/.attention-relay/relay '
                "hook-event session-start"
            ),
            "UserPromptSubmit": (
                '"$CLAUDE_PROJECT_DIR"/.attention-relay/relay '
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
        printed = self.relay(project, "hooks", "claude-code", check=True)
        fragment_text, instruction = printed.stdout.rsplit("\n", 2)[:2]
        fragment = json.loads(fragment_text)
        self.assertEqual(set(fragment["hooks"]), {"SessionStart", "UserPromptSubmit"})
        self.assertIn(".attention-relay/relay hook-event", printed.stdout)
        self.assertIn("Merge this fragment into .claude/settings.json", instruction)
        for event in ("SessionStart", "UserPromptSubmit"):
            self.assertNotIn("matcher", fragment["hooks"][event][0])

        self.relay(project, "hooks", "claude-code", "--write", check=True)
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
            self.relay(
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
        rejected = self.relay(
            invalid_project, "hooks", "claude-code", "--write",
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("cannot parse .claude/settings.json", rejected.stderr)
        self.assertEqual(invalid_path.read_text(), before)

    def test_session_start_hook_marks_compaction_and_caps_reinjected_brief(self):
        project = self.make_project()
        brief = self.relay(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        ).stdout
        command = [
            project / ".attention-relay" / "relay", "hook-event", "session-start",
        ]

        for hook_input in ("", '{"source":"startup"}', "not json", "[]"):
            with self.subTest(hook_input=hook_input):
                session = subprocess.run(
                    command, cwd=project, input=hook_input, text=True,
                    capture_output=True,
                )
                self.assertEqual(
                    (session.returncode, session.stdout, session.stderr),
                    (0, brief, ""),
                )

        notice = "attention-relay: context was compacted; state re-injected below."
        difficulty_start = brief.index("\nDifficulty levels:\n")
        difficulty_end = brief.index("\n\n", difficulty_start + 1)
        compact_brief = brief[:difficulty_start] + brief[difficulty_end + 1:]
        compact = subprocess.run(
            command, cwd=project, input='{"source":"compact"}', text=True,
            capture_output=True,
        )
        self.assertEqual(compact.returncode, 0)
        self.assertEqual(compact.stderr, "")
        self.assertEqual(compact.stdout, notice + "\n" + compact_brief)
        self.assertNotIn("Difficulty levels:", compact.stdout)

        (project / ".attention-relay" / "orchestrator-handoff.md").write_text(
            "goal: " + "x" * 10000 + "\nlast handoff line\n"
        )
        capped = subprocess.run(
            command, cwd=project, input='{"source":"compact"}', text=True,
            capture_output=True,
        )
        self.assertEqual(capped.returncode, 0)
        self.assertEqual(capped.stderr, "")
        self.assertLessEqual(len(capped.stdout), 9000)
        self.assertTrue(capped.stdout.startswith(notice + "\n"))
        self.assertIn("\n(truncated)\n", capped.stdout)

    def test_claude_code_hook_events_match_brief_emit_json_and_fail_open(self):
        project = self.make_project()
        brief = self.relay(
            project, "orchestrator", "brief", "--phase", "start", check=True,
        )
        session = subprocess.run(
            [project / ".attention-relay" / "relay", "hook-event", "session-start"],
            cwd=project, input="not json", text=True, capture_output=True,
        )
        self.assertEqual(session.returncode, 0)
        self.assertEqual(session.stderr, "")
        self.assertEqual(session.stdout, brief.stdout)
        decision = self.create_task(project, "hook decision")
        decision_path = (
            project / ".attention-relay" / "tasks" / f"{decision}.json"
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
                project / ".attention-relay" / "relay", "hook-event",
                "user-prompt-submit",
            ],
            cwd=project, input="{malformed", text=True, capture_output=True,
        )
        self.assertEqual(prompt.returncode, 0)
        self.assertLessEqual(len(prompt.stdout), 9000)
        payload = json.loads(prompt.stdout)
        specific = payload["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
        self.assertTrue(specific["additionalContext"].startswith(
            "attention-relay state:\nNext actions:\n",
        ))
        self.assertIn(
            f"- decide {decision}: worker question: May we change the interface?",
            specific["additionalContext"],
        )

        module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_hook_cap_probe")
        capped = module["cap_hook_output"]("first\n" + "x" * 10000 + "\nlast\n")
        self.assertLessEqual(len(capped), 9000)
        self.assertTrue(capped.startswith("first\n"))
        self.assertTrue(capped.endswith("(truncated)\nlast\n"))
        bounded_json = module["claude_user_prompt_output"](
            "attention-relay state:\n" + "x" * 10000 + "\nlast",
        )
        self.assertLessEqual(len(bounded_json), 9000)
        self.assertIn("(truncated)\nlast", json.loads(bounded_json)[
            "hookSpecificOutput"
        ]["additionalContext"])

        broken_runtime = self.base / "empty-runtime"
        broken_runtime.mkdir()
        for name in ("session-start", "user-prompt-submit"):
            broken = self.relay(
                project, "hook-event", name, env={"RELAY_DIR": broken_runtime},
            )
            self.assertEqual((broken.returncode, broken.stdout, broken.stderr), (0, "", ""))
        outside = self.command(
            [SOURCE_RELAY, "hook-event", "session-start"], self.base,
        )
        self.assertEqual((outside.returncode, outside.stdout, outside.stderr), (0, "", ""))

        worker_env = {
            "RELAY_TASK_ID": "T999-worker", "RELAY_ATTEMPT": "1",
            "RELAY_LEASE": "worker",
        }
        for command in (
                ("hooks", "claude-code"),
                ("hook-event", "session-start")):
            denied = self.relay(project, *command, env=worker_env)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("worker processes cannot run orchestrator commands", denied.stderr)

    def test_hook_cap_handles_edge_lines_and_preserves_normal_input(self):
        module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_hook_edges_probe")
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
        env = dict(os.environ, STARTS=str(starts), FINISH_MARKER=str(self.base / "wait"))
        commands = [str(project / ".attention-relay" / "relay"), "run", task_id]
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
        relay = str(project / ".attention-relay" / "relay")
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
        task_id = self.create_task(project, "bad command", ["a/**"])
        config = project / ".attention-relay" / "config.toml"
        config.write_text(
            '[commands]\nworker = "true {prompt} embedded{prompt}"\n'
            '[limits]\nmax_parallel = 1\n'
        )
        self.assertNotEqual(self.relay(project, "validate").returncode, 0)
        result = self.relay(project, "run")
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

    def test_valid_submission_survives_nonzero_exit_with_review_warning(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "submitted before exit", ["src/**"])
        self.relay(project, "run", task_id, env={"EXIT_CODE": "1"}, check=True)

        state = self.state(project, task_id)
        warning = "worker_exit_1_after_submission"
        self.assertEqual(state["status"], "needs_review")
        self.assertEqual(state["warning"], warning)
        worker_exit = state["history"][-1]
        self.assertEqual(worker_exit["event"], "worker_exited")
        self.assertEqual(worker_exit["exit_code"], 1)
        self.assertEqual(worker_exit["warning"], warning)

        status = self.relay(project, "status", check=True)
        self.assertIn(f"WARNING: {warning}", status.stdout)
        brief, token = self.review_brief_token(project, task_id)
        self.assertIn("WARNING: Worker exited with code 1 after submission", brief.stdout)
        self.assertIn(
            f".attention-relay/work/{task_id}/attempt-1.log", brief.stdout,
        )
        self.relay(
            project, "task", "accept", task_id, "--brief", token, check=True,
        )
        self.assertEqual(self.state(project, task_id)["status"], "done")

    def test_nonzero_exit_without_result_keeps_worker_exit_failure(self):
        project = self.make_project()
        self.configure(project, self.write_worker("raise SystemExit(1)\n"))
        task_id = self.create_task(project, "exit without result", ["src/**"])
        self.relay(project, "run", task_id, check=True)
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "worker_exit_1")
        self.assertNotIn("warning", state)

    def test_nonzero_exit_with_malformed_result_is_invalid_output(self):
        project = self.make_project()
        worker = self.write_worker(MALFORMED_OUTPUT_WORKER + "\nraise SystemExit(1)\n")
        self.configure(project, worker)
        task_id = self.create_task(project, "malformed before exit", ["src/**"])
        self.relay(project, "run", task_id, check=True)
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "invalid_worker_output")
        self.assertNotIn("warning", state)

    def test_timeout_overrides_valid_submission(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker, max_parallel=1, timeout_minutes=0.02)
        task_id = self.create_task(project, "submitted before timeout", ["src/**"])
        self.relay(
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
        self.relay(project, "run", task_id, env={"EXIT_CODE": "1"}, check=True)
        state = self.state(project, task_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_note"], "changed_paths_mismatch")
        self.assertNotIn("warning", state)

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

    def test_oversized_integer_result_fails_without_stale_runner(self):
        project = self.make_project()
        worker = self.write_worker(OVERSIZED_INTEGER_RESULT_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "oversized integer output", ["src/**"])
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
        directory = project / ".attention-relay" / "work" / task_id
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
        runtime = project / ".attention-relay"
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
        (project / ".attention-relay" / "config.toml").write_text(config)
        marker = self.base / "injected"
        scope = f"safe/$(touch {marker})/**"
        task_id = self.create_task(project, "literal prompt", [scope])
        self.relay(project, "run", task_id, check=True)
        self.assertFalse(marker.exists())
        self.assertEqual(self.state(project, task_id)["status"], "needs_review")

    def test_tiers_are_strict_at_create_validate_preview_and_launch(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        config = project / ".attention-relay" / "config.toml"
        config.write_text(config.read_text() + "\n[tiers.premium]\ncapsule_max_chars = 5000\n")

        unknown = self.try_create_task(project, "unknown tier", tier="mystery")
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("unknown tier 'mystery'", unknown.stderr)
        self.assertIn("known tiers: default, premium", unknown.stderr)
        blank = self.try_create_task(project, "blank tier", tier="")
        self.assertNotEqual(blank.returncode, 0)
        self.assertIn("tier name must be non-blank", blank.stderr)

        task_id = self.create_task(
            project, "strict premium", ["premium/**"], tier="premium",
        )
        brief = self.relay(
            project, "orchestrator", "brief", "--phase", "run", check=True,
        )
        dry = self.relay(project, "run", "--dry-run", check=True)
        annotation = f"{task_id} [tier=premium]"
        self.assertIn("Would run: " + annotation, brief.stdout)
        self.assertIn("would run: " + annotation, dry.stdout)

        state_path = project / ".attention-relay" / "tasks" / f"{task_id}.json"
        state = json.loads(state_path.read_text())
        state["tier"] = "removed"
        state_path.write_text(json.dumps(state))
        validation = self.relay(project, "validate")
        preview = self.relay(project, "task", "capsule", task_id)
        launch = self.relay(project, "run", task_id)
        for result in (validation, preview, launch):
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unknown tier 'removed'", result.stdout + result.stderr)
        self.assertEqual(self.state(project, task_id)["status"], "queued")

    def test_per_tier_capsule_budget_controls_preview_validate_and_launch(self):
        roomy = self.make_project("roomy-tier")
        self.configure(
            roomy, self.write_worker(NO_CHANGE_WORKER), capsule_max_chars=100,
        )
        config = roomy / ".attention-relay" / "config.toml"
        config.write_text(config.read_text() + "\n[tiers.roomy]\ncapsule_max_chars = 4000\n")
        roomy_id = self.create_task(roomy, "roomy capsule", ["roomy/**"], tier="roomy")
        preview = self.relay(roomy, "task", "capsule", roomy_id, check=True)
        self.assertIn("of 4000 chars", preview.stdout)
        self.relay(roomy, "validate", check=True)
        self.relay(roomy, "run", roomy_id, check=True)
        self.assertEqual(self.state(roomy, roomy_id)["status"], "needs_review")

        tight = self.make_project("tight-tier")
        self.configure(tight, self.write_worker(NO_CHANGE_WORKER), capsule_max_chars=4000)
        config = tight / ".attention-relay" / "config.toml"
        config.write_text(config.read_text() + "\n[tiers.tight]\ncapsule_max_chars = 100\n")
        tight_id = self.create_task(tight, "tight capsule", ["tight/**"], tier="tight")
        results = (
            self.relay(tight, "task", "capsule", tight_id),
            self.relay(tight, "validate"),
            self.relay(tight, "run", tight_id),
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
        config = project / ".attention-relay" / "config.toml"
        config.write_text(config.read_text() + (
            "\n[tiers.short]\nworker_timeout_minutes = 0.005\n"
            "\n[tiers.long]\nworker_timeout_minutes = 0.1\n"
        ))
        short = self.create_task(project, "short timeout", ["short/**"], tier="short")
        long = self.create_task(project, "long timeout", ["long/**"], tier="long")
        self.relay(project, "run", short, long, check=True)
        self.assertEqual(self.state(project, short)["status"], "failed")
        self.assertEqual(self.state(project, short)["last_note"], "worker_timeout")
        self.assertEqual(self.state(project, long)["status"], "needs_review")

    def test_validate_reports_every_malformed_unused_tier_setting_and_name(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        config = project / ".attention-relay" / "config.toml"
        config.write_text(config.read_text() + (
            "\n[tiers.broken]\n"
            "command = \"\"\n"
            "worker_timeout_minutes = \"never\"\n"
            "capsule_max_chars = true\n"
            "\n[tiers.\"\"]\ncapsule_max_chars = 100\n"
            "\n[tiers.default]\ncapsule_max_chars = 200\n"
        ))
        validation = self.relay(project, "validate")
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
                    config = project / ".attention-relay" / "config.toml"
                    if location == "global":
                        config.write_text(config.read_text().replace(
                            "worker_timeout_minutes = 1",
                            f"worker_timeout_minutes = {value}",
                        ))
                    else:
                        config.write_text(config.read_text() + (
                            f"\n[tiers.bad]\nworker_timeout_minutes = {value}\n"
                        ))

                    validation = self.relay(project, "validate")
                    self.assertEqual(validation.returncode, 1)
                    self.assertIn(message, validation.stdout)
                    tiers = self.relay(project, "tiers")
                    self.assertEqual(tiers.returncode, 1)
                    self.assertIn(message, tiers.stderr)
                    self.assertNotIn(f"{value} minutes", tiers.stdout)

    def test_tiers_output_is_exact_read_only_redacted_and_worker_denied(self):
        project = self.make_project()
        worker = self.write_worker(NO_CHANGE_WORKER)
        self.configure(project, worker, timeout_minutes=2.5, capsule_max_chars=4000)
        config = project / ".attention-relay" / "config.toml"
        tier_command = f"{sys.executable} {worker} --secret do-not-print {{prompt_file}}"
        config.write_text(config.read_text() + (
            "\n[tiers.alpha]\ncapsule_max_chars = 5000\n"
            "\n[tiers.zeta]\n"
            f"command = {json.dumps(tier_command)}\n"
            "worker_timeout_minutes = 0\n"
        ))
        runtime = project / ".attention-relay"

        def snapshot():
            return {
                path.relative_to(runtime).as_posix(): path.read_bytes()
                for path in runtime.rglob("*") if path.is_file()
            }

        before = snapshot()
        tiers = self.relay(project, "tiers", check=True)
        executable = sys.executable
        tier_blocks = (
            f"Tier: default\nExecutable: {executable}\nCommand source: default\n"
            "Worker timeout: 2.5 minutes\nCapsule budget: 4000 characters\n\n"
            f"Tier: alpha\nExecutable: {executable}\nCommand source: default\n"
            "Worker timeout: 2.5 minutes\nCapsule budget: 5000 characters\n\n"
            f"Tier: zeta\nExecutable: {executable}\nCommand source: tier\n"
            "Worker timeout: 0 minutes\nCapsule budget: 4000 characters\n"
        )
        expected = tier_blocks + "Conventional levels missing: hard, medium, easy\n"
        self.assertEqual(tiers.stdout, expected)
        self.assertNotIn("--secret", tiers.stdout)
        self.assertNotIn("do-not-print", tiers.stdout)
        self.assertEqual(snapshot(), before)

        config.write_text(config.read_text() + "\n[tiers.hard]\ncapsule_max_chars = 4100\n")
        partial = self.relay(project, "tiers", check=True)
        self.assertTrue(partial.stdout.endswith(
            "Conventional levels missing: medium, easy\n",
        ))
        config.write_text(config.read_text() + (
            "\n[tiers.medium]\ncapsule_max_chars = 4200\n"
            "\n[tiers.easy]\ncapsule_max_chars = 4300\n"
        ))
        complete = self.relay(project, "tiers", check=True)
        self.assertNotIn("Conventional levels missing:", complete.stdout)
        denied = self.relay(
            project, "tiers", env={"RELAY_TASK_ID": "T999-worker"},
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("worker processes cannot run orchestrator commands", denied.stderr)

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
                    [str(project / ".attention-relay" / "relay"), "run", task_id],
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
            [str(project / ".attention-relay" / "relay"), "run", *task_ids],
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
        self.accept_task(project, first)
        second = self.create_task(project, "second", ["second/**"], [first])
        self.relay(project, "archive", check=True)
        dry = self.relay(project, "run", "--dry-run", check=True)
        self.assertIn(f"would run: {second}", dry.stdout)
        self.assertEqual(self.relay(project, "validate").returncode, 0)

        third = self.create_task(project, "third", ["third/**"])
        self.assertTrue(third.startswith("T003-"), third)
        second_path = project / ".attention-relay" / "tasks" / f"{second}.json"
        third_path = project / ".attention-relay" / "tasks" / f"{third}.json"
        second_state = json.loads(second_path.read_text())
        third_state = json.loads(third_path.read_text())
        second_state["depends_on"] = [third]
        third_state["depends_on"] = [second]
        second_path.write_text(json.dumps(second_state))
        third_path.write_text(json.dumps(third_state))
        validation = self.relay(project, "validate")
        self.assertNotEqual(validation.returncode, 0)
        self.assertIn("dependency cycle", validation.stdout)

    def test_stats_empty_runtime_is_read_only_and_denied_to_workers(self):
        project = self.make_project()
        runtime = project / ".attention-relay"

        def snapshot():
            return {
                path.relative_to(runtime).as_posix(): path.read_bytes()
                for path in runtime.rglob("*") if path.is_file()
            }

        before = snapshot()
        stats = self.relay(project, "stats", check=True)
        self.assertEqual(stats.stdout, "no task data\n")
        self.assertEqual(snapshot(), before)
        denied = self.relay(
            project, "stats",
            env={"RELAY_TASK_ID": "T999-worker", "RELAY_ATTEMPT": "1",
                 "RELAY_LEASE": "worker"},
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("worker processes cannot run orchestrator commands", denied.stderr)

    def test_stats_exact_mixed_outcomes_and_archived_receipt_coverage(self):
        project = self.make_project()
        runtime = project / ".attention-relay"
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
        self.relay(project, "archive", check=True)
        archived_receipt = (
            runtime / "archive" / f"{task_ids[3]}.work" / "attempt-1.briefs.json"
        )
        self.assertTrue(archived_receipt.exists())

        stats = self.relay(project, "stats", check=True)
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

    def test_archive_preflights_all_destinations_before_moving(self):
        project = self.make_project()
        task_ids = [
            self.create_task(project, "archive one", ["one/**"]),
            self.create_task(project, "archive two", ["two/**"]),
        ]
        runtime = project / ".attention-relay"
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
        runtime = project / ".attention-relay"
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
        diff = project / ".attention-relay" / "work" / task_id / "attempt-1.diff"
        self.assertIn("new/staged.txt", diff.read_text())

    def test_capsule_sandwich_brief_digest_and_retry_delta(self):
        project = self.make_project()
        worker = self.write_worker(GOOD_WORKER)
        self.configure(project, worker)
        task_id = self.create_task(project, "capsule", ["capsule/**"])
        self.relay(project, "run", task_id, check=True)

        work = project / ".attention-relay" / "work" / task_id
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

        module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_capsule_probe")
        task = self.state(project, task_id)
        spec = (project / ".attention-relay" / "tasks" / f"{task_id}.md").read_text()
        entries = module["memory_index_entries"](
            (project / ".attention-relay" / "memory.md").read_text()
        )
        self.assertEqual(module["compile_context_capsule"](task, spec, entries), capsule)
        self.assertEqual(module["compile_context_capsule"](task, spec, entries), capsule)

        self.relay(
            project, "task", "return", task_id,
            "--reason", "Preserve the capsule boundary", check=True,
        )
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        self.relay(project, "run", task_id, check=True)
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
        created = self.relay(
            project, "task", "create", "--title", "unfinished spec", check=True,
        )
        task_id = created.stdout.split()[1]
        run = self.relay(project, "run", task_id)
        validation = self.relay(project, "validate")
        self.assertNotEqual(run.returncode, 0)
        self.assertNotEqual(validation.returncode, 0)
        shared = "Objective still contains the template placeholder"
        self.assertIn(shared, run.stderr)
        self.assertIn(shared, validation.stdout)
        self.assertEqual(self.state(project, task_id)["status"], "queued")

        spec = project / ".attention-relay" / "tasks" / f"{task_id}.md"
        spec.write_text(spec.read_text().replace(
            "Replace this line with one clear outcome.", "",
        ))
        empty_run = self.relay(project, "run", task_id)
        empty_validation = self.relay(project, "validate")
        self.assertIn("Objective is empty", empty_run.stderr)
        self.assertIn("Objective is empty", empty_validation.stdout)

    def test_capsule_budget_overflow_is_rejected_by_run_and_validate(self):
        project = self.make_project()
        self.configure(
            project, self.write_worker(NO_CHANGE_WORKER), capsule_max_chars=100,
        )
        task_id = self.create_task(project, "over budget", ["budget/**"])
        run = self.relay(project, "run", task_id)
        validation = self.relay(project, "validate")
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
            project / ".attention-relay" / "work" / task_id / "attempt-1.brief.md"
        ).read_text()
        _digest_header, stored_capsule = brief.split("\n\n", 1)
        spec = project / ".attention-relay" / "tasks" / f"{task_id}.md"
        spec.write_text(spec.read_text().replace(
            "Complete the stored capsule task.", "This changed after launch.",
        ))

        raw = self.relay(project, "task", "capsule", task_id, "--raw", check=True)
        self.assertEqual(raw.stdout.encode(), stored_capsule.encode())
        shown = self.relay(project, "task", "capsule", task_id, check=True)
        self.assertTrue(shown.stdout.startswith(stored_capsule + "\n\nCapsule diagnostics:\n"))
        self.assertIn("Source: launch (attempt 1)\n", shown.stdout)
        self.assertIn(
            "Digest: sha256:" + hashlib.sha256(stored_capsule.encode()).hexdigest(),
            shown.stdout,
        )
        denied = self.relay(project, "task", "capsule", task_id, env=env)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("worker processes cannot run orchestrator commands", denied.stderr)

    def test_task_capsule_prospective_preview_matches_launch_and_writes_nothing(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        task_id = self.create_task(project, "prospective capsule", ["preview/**"])
        runtime = project / ".attention-relay"

        def snapshot():
            return {
                str(path.relative_to(runtime)): (
                    path.is_dir(), path.stat().st_mtime_ns,
                    b"" if path.is_dir() else path.read_bytes(),
                )
                for path in runtime.rglob("*")
            }

        before = snapshot()
        shown = self.relay(project, "task", "capsule", task_id, check=True)
        self.assertEqual(snapshot(), before)
        self.assertIn("Source: current spec (prospective)\n", shown.stdout)
        prospective = self.relay(
            project, "task", "capsule", task_id, "--raw", check=True,
        ).stdout
        self.assertEqual(snapshot(), before)

        self.relay(project, "run", task_id, check=True)
        brief = runtime / "work" / task_id / "attempt-1.brief.md"
        _digest_header, launched = brief.read_text().split("\n\n", 1)
        self.assertEqual(prospective, launched)

    def test_task_capsule_diagnostics_count_unicode_characters(self):
        project = self.make_project()
        title = "aperçu 😀 東京"
        task_id = self.create_task(project, title, ["café/**"])
        shown = self.relay(project, "task", "capsule", task_id, check=True)
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
        shown = self.relay(project, "task", "capsule", task_id)
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

        raw = self.relay(project, "task", "capsule", task_id, "--raw")
        self.assertNotEqual(raw.returncode, 0)
        self.assertEqual(raw.stdout, "")
        self.assertIn("capsule_max_chars=100", raw.stderr)

    def test_task_capsule_errors_pass_through_and_reject_unknown_or_archived(self):
        project = self.make_project()
        self.configure(project, self.write_worker(NO_CHANGE_WORKER))
        created = self.relay(
            project, "task", "create", "--title", "unfinished preview", check=True,
        )
        unfinished = created.stdout.split()[1]
        preview = self.relay(project, "task", "capsule", unfinished)
        launch = self.relay(project, "run", unfinished)
        self.assertNotEqual(preview.returncode, 0)
        self.assertEqual(preview.stderr, launch.stderr)
        self.assertIn("Objective still contains the template placeholder", preview.stderr)

        unknown = self.relay(project, "task", "capsule", "T999-not-here")
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("no such task: T999-not-here", unknown.stderr)

        archived = self.create_task(project, "archived preview", ["archive/**"])
        state_path = project / ".attention-relay" / "tasks" / f"{archived}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "done"
        state_path.write_text(json.dumps(state))
        self.relay(project, "archive", check=True)
        rejected = self.relay(project, "task", "capsule", archived)
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
        runtime = project / ".attention-relay"
        spec_path = runtime / "tasks" / f"{task_id}.md"
        spec_path.write_text(spec_path.read_text().replace(
            "List the paths and facts the worker needs. Reference memory ids when useful.",
            "Use M1000, then M001, then M1000 again. Other sections do not count.",
        ))

        preview = self.relay(project, "task", "capsule", task_id, check=True)
        self.assertIn("- Referenced memory: ", preview.stdout)
        prospective = self.relay(
            project, "task", "capsule", task_id, "--raw", check=True,
        ).stdout
        expected_section = (
            "## Referenced memory\n"
            "Load full entries as needed with "
            "`python3 .attention-relay/relay memory show ID`.\n"
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
        module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_stored_memory_probe")
        stored_result = module["stored_context_capsule_components"](prospective)
        self.assertEqual(stored_result["text"], prospective)
        self.assertIn("Referenced memory", dict(stored_result["section_chars"]))

        self.relay(project, "run", task_id, check=True)
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
        runtime = project / ".attention-relay"
        module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_memory_error_probe")
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
            preview = self.relay(project, "task", "capsule", task_id)
            launch = self.relay(project, "run", task_id)
            self.assertNotEqual(preview.returncode, 0)
            self.assertNotEqual(launch.returncode, 0)
            self.assertIn(message, preview.stderr)
            self.assertIn(message, launch.stderr)

        validation = self.relay(project, "validate")
        self.assertNotEqual(validation.returncode, 0)
        for _task_id, _spec_path, message in task_cases:
            self.assertIn(message, validation.stdout)

    def test_memory_index_parser_is_strict_and_preserves_four_digit_ids(self):
        module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_memory_parser_probe")
        parse = module["memory_index_entries"]
        valid = (
            "# Memory\n\n## Index\n"
            "- M1000 [B] Shared fact\n"
            "- M001 [W] Worker fact\n\n"
            "## Entries\n"
        )
        self.assertEqual(
            parse(valid),
            [("M1000", "B", "Shared fact"), ("M001", "W", "Worker fact")],
        )
        with self.assertRaisesRegex(ValueError, "malformed"):
            parse(valid.replace("- M001 [W] Worker fact", "- M01 [W] Worker fact"))
        with self.assertRaisesRegex(ValueError, "duplicate id M1000"):
            parse(valid.replace("M001 [W] Worker fact", "M1000 [W] Worker fact"))

        project = self.make_project()
        memory = project / ".attention-relay" / "memory.md"
        memory.write_text(valid.replace("M001 [W] Worker fact", "M1000 [W] Worker fact"))
        validation = self.relay(project, "validate")
        self.assertNotEqual(validation.returncode, 0)
        self.assertIn("memory index has duplicate id M1000", validation.stdout)

    def test_no_reference_format_is_unchanged_and_memory_counts_toward_budget(self):
        project = self.make_project()
        self.write_memory(project, [
            ("M001", "W", "A deliberately long summary for capsule budgeting", "body"),
        ])
        task_id = self.create_task(project, "format stability", ["stable/**"])
        runtime = project / ".attention-relay"
        module = runpy.run_path(str(SOURCE_RELAY), run_name="relay_memory_budget_probe")
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
        preview = self.relay(project, "task", "capsule", task_id)
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
        runtime = project / ".attention-relay"
        spec = runtime / "tasks" / f"{task_id}.md"
        spec.write_text(spec.read_text().replace(
            "List the paths and facts the worker needs. Reference memory ids when useful.",
            "Use M001.",
        ))
        self.relay(project, "run", task_id, check=True)
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
        self.assertEqual(command.count("--ignore-rules"), 1)
        self.assertIn("--ignore-rules", command)
        query = command.index("-q")
        self.assertEqual(command[query + 1], "{prompt}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
