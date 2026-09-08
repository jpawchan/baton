#!/usr/bin/env python3
"""Opt-in live Pi direct/Baton pilot. Prepare freezes inputs; run invokes agents."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import runpy
import shlex
import shutil
import signal
import subprocess
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "benchmarks" / "agent_pilot"
sys.path.insert(0, str(PILOT))
from cases import CASES, prompt
from usage import collect_usage, digest

COMMON_FLAGS = ["--offline", "--print", "--mode", "json", "--no-extensions",
                "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files"]
FROZEN = ["tools/benchmark_agents.py", "tools/evaluate_delegation.py", "benchmarks/agent_pilot/cases.py",
          "benchmarks/agent_pilot/holdout.py", "benchmarks/agent_pilot/usage.py",
          "benchmarks/agent_pilot/reference.py", "benchmarks/agent_pilot/README.md",
          "tests/test_agent_benchmarks.py", "framework/baton", "framework/orchestrator.md", "framework/worker.md",
          "prompts/use-framework.md"]


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    os.replace(temporary, path)


def environment(base):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
    env["PATH"] = str(base / "bin") + os.pathsep + env.get("PATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PI_OFFLINE"] = "1"
    return env


def command(argv, cwd, env, **kwargs):
    return subprocess.run([str(value) for value in argv], cwd=cwd, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          check=True, timeout=60, **kwargs).stdout.strip()


def routes_from_config(text):
    config = tomllib.loads(text)
    routes = {}
    for tier in ("hard", "medium", "easy"):
        tokens = shlex.split(config["tiers"][tier]["command"])
        if Path(tokens[0]).name != "pi" or any(flag in tokens for flag in
                ("--no-session", "--session", "--session-dir", "--continue", "--resume", "--fork", "-c", "-r")):
            raise ValueError("pilot requires fresh Pi commands with inherited session directories")
        if "--mode" not in tokens or tokens[tokens.index("--mode") + 1] != "json":
            raise ValueError("pilot requires JSON worker streams")
        routes[tier] = {key: tokens[tokens.index(flag) + 1] for key, flag in
                        (("provider", "--provider"), ("model", "--model"), ("effort", "--thinking"))}
    return routes


def prepare(args):
    base = args.directory.resolve()
    if base.exists():
        raise ValueError("refusing to overwrite an existing pilot directory")
    config = args.config.read_text()
    routes = routes_from_config(config)
    if args.repeats < 1 or args.repeats > 5 or args.timeout_seconds < 60:
        raise ValueError("repeats must be 1..5; timeout must be at least 60 seconds")
    if not all((args.provider, args.model, args.effort)):
        raise ValueError("explicit or PI_* provider/model/effort settings are required")
    base.mkdir(parents=True)
    (base / "bin").mkdir()
    (base / "bin" / "python3").symlink_to(sys.executable)
    env = environment(base)
    (base / "worker-config.toml").write_text(config)
    activation = (ROOT / "prompts" / "use-framework.md").read_text()
    blocks = [(name, repeat) for name in CASES for repeat in range(1, args.repeats + 1)]
    rng = random.Random(args.seed)
    rng.shuffle(blocks)
    # Alternate which arm starts each case across repetitions, randomizing its first arm.
    first = {name: rng.choice(["direct", "baton"]) for name in CASES}
    jobs = []
    for name, repeat in blocks:
        order = [first[name], "baton" if first[name] == "direct" else "direct"]
        if repeat % 2 == 0:
            order.reverse()
        for arm in order:
            job_id = f"run-{len(jobs) + 1:03d}"
            folder = base / job_id
            project = folder / "project"
            project.mkdir(parents=True)
            (folder / "sessions").mkdir()
            for relative, content in CASES[name]["files"].items():
                path = project / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            command(["git", "init", "-q"], project, env)
            command(["git", "config", "user.name", "Benchmark"], project, env)
            command(["git", "config", "user.email", "benchmark@example.invalid"], project, env)
            command(["git", "add", "."], project, env)
            fixed = dict(env, GIT_AUTHOR_DATE="2026-09-07T00:00:00+00:00",
                         GIT_COMMITTER_DATE="2026-09-07T00:00:00+00:00")
            command(["git", "commit", "-qm", "frozen fixture"], project, fixed)
            initial_commit = command(["git", "rev-parse", "HEAD"], project, env)
            if arm == "baton":
                command([sys.executable, ROOT / "framework" / "baton", "init", project], project, env)
                (project / ".baton" / "config.toml").write_text(config)
                command([project / ".baton" / "baton", "validate"], project, env)
            text = prompt(name, arm, activation)
            (folder / "prompt.txt").write_text(text)
            jobs.append({"id": job_id, "case": name, "repeat": repeat, "arm": arm,
                         "initial_commit": initial_commit,
                         "runtime_hashes": {name: digest(project / ".baton" / name)
                                            for name in ("baton", "orchestrator.md", "worker.md", "config.toml")}
                                           if arm == "baton" else {},
                         "prompt_sha256": digest(folder / "prompt.txt")})
    protocol = {
        "schema_version": 1, "prepared_at": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed, "repeats": args.repeats, "timeout_seconds": args.timeout_seconds,
        "solver": {"provider": args.provider, "model": args.model, "effort": args.effort},
        "worker_routes": routes, "worker_config_sha256": digest(base / "worker-config.toml"),
        "framework_revision": command(["git", "log", "-1", "--format=%H", "--", "framework/baton"], ROOT, env),
        "source_hashes": {name: digest(ROOT / name) for name in FROZEN},
        "python": sys.version.split()[0], "pi": command(["pi", "--version"], ROOT, env),
        "platform": sys.platform, "jobs": jobs,
        "quality_definition": "equal-weight held-out test-group pass fraction; functional proxy, not all code quality",
        "boundary": "fresh solver invocation through exit, all child workers and internal review/retries; fixture setup and external grading excluded",
        "cache": "provider cache not controllable; cached input included in logical totals; no billing inference",
    }
    save(base / "protocol.json", protocol)
    print(json.dumps({"directory": str(base), "jobs": jobs}, indent=2))


def stop_descendants(pid):
    """Stop only the launched run's process tree, including nested worker sessions."""
    output = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid="],
                            capture_output=True, text=True, check=True).stdout
    rows = [tuple(map(int, line.split())) for line in output.splitlines() if line.strip()]
    descendants = {pid}
    while True:
        expanded = descendants | {child for child, parent, _group in rows if parent in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    groups = {group for child, _parent, group in rows if child in descendants}
    groups.discard(os.getpgrp())
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for group in groups:
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                pass
        if sig == signal.SIGTERM:
            time.sleep(3)


def task_evidence(project):
    tasks, issues = [], []
    runtime = project / ".baton"
    for location in ("tasks", "archive"):
        for path in sorted((runtime / location).glob("T*.json")):
            try:
                value = json.loads(path.read_text())
                if not isinstance(value, dict) or not isinstance(value.get("history"), list):
                    raise ValueError("bad task")
                tasks.append(value)
            except (OSError, ValueError):
                issues.append("malformed_baton_state")
    logs = [("worker:" + path.parent.name + "/" + path.name, path)
            for location in ("work", "archive")
            for path in sorted((runtime / location).glob("*/attempt-*.log"))]
    return tasks, logs, issues


def allowed_path(case, name):
    if name == "README.md" or re.fullmatch(r"tests/test_[^/]+\.py", name):
        return True
    return name == "retry_after.py" if case == "retry_after" else bool(re.fullmatch(r"ledger/[^/]+\.py", name))


def analyze(base, protocol, job, code, timed_out, elapsed):
    folder, env = base / job["id"], environment(base)
    project = folder / "project"
    tasks, logs, issues = task_evidence(project)
    usage = collect_usage(folder / "sessions", [("solver", folder / "solver.jsonl"), *logs])
    module = runpy.run_path(str(ROOT / "framework" / "baton"), run_name="benchmark_baton")
    workers = dict.fromkeys(("hard", "medium", "easy", "other"), 0)
    launch_routes = {}
    for task in tasks:
        for entry, tier in module["recorded_launch_tiers"](task):
            workers[module["routing_tier_bucket"](tier)] += 1
            launch_routes[(task["id"], entry.get("attempt"))] = protocol["worker_routes"].get(tier)
    if sum(workers.values()) != len(logs):
        issues.append("worker_launch_log_count_mismatch")
    for relative in (".gitignore", "tests/test_smoke.py"):
        path = project / relative
        if not path.is_file() or path.read_text() != CASES[job["case"]]["files"][relative]:
            issues.append("modified_frozen_file:" + relative)
    if command(["git", "rev-parse", "HEAD"], project, env) != job["initial_commit"]:
        issues.append("solver_changed_git_head")
    changed = command(["git", "diff", "--name-only", "HEAD"], project, env).splitlines()
    untracked = command(["git", "ls-files", "--others", "--exclude-standard"], project, env).splitlines()
    for name in changed + untracked:
        if not allowed_path(job["case"], name):
            issues.append("out_of_scope_path:" + name)
    if job["arm"] == "direct" and (tasks or logs):
        issues.append("direct_arm_delegated")
    statuses = dict(Counter(task.get("status", "unknown") for task in tasks))
    if job["arm"] == "baton":
        intact = True
        for name, expected in job["runtime_hashes"].items():
            path = project / ".baton" / name
            if not path.is_file() or digest(path) != expected:
                issues.append("solver_changed_runtime:" + name)
                intact = False
        if intact:
            validation = subprocess.run([str(project / ".baton" / "baton"), "validate"],
                                        cwd=project, env=env, capture_output=True, text=True,
                                        stdin=subprocess.DEVNULL, timeout=30)
            if validation.returncode:
                issues.append("invalid_baton_state")
        if any(task.get("status") not in ("done", "cancelled") for task in tasks):
            issues.append("unfinished_baton_tasks")
        if CASES[job["case"]]["baton_policy"] == "delegated" and not statuses.get("done"):
            issues.append("required_delegation_not_completed")
    if code != 0 or timed_out:
        issues.append("solver_timeout_or_nonzero_exit")
    for call in usage["ledger"]:
        actual = {key: call[key] for key in ("provider", "model", "effort")}
        expected = protocol["solver"] if call["stream"] == "solver" else None
        if call["stream"].startswith("worker:"):
            task_id, log_name = call["stream"].removeprefix("worker:").split("/")
            match = re.fullmatch(r"attempt-(\d+)\.log", log_name)
            if match:
                expected = launch_routes.get((task_id.removesuffix(".work"), int(match[1])))
        if actual != expected:
            issues.append("unexpected_model_or_effort")
    # Preserve candidate code independently of Git index tricks, and a convenient patch.
    solution = folder / "solution"
    solution.mkdir()
    for path in sorted(project.rglob("*.py")):
        relative = path.relative_to(project).as_posix()
        if ".baton" not in path.relative_to(project).parts and allowed_path(job["case"], relative):
            target = solution / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    (folder / "solution.patch").write_text(command(["git", "diff", "HEAD", "--binary"], project, env) + "\n")
    graded = subprocess.run([sys.executable, str(PILOT / "holdout.py"), job["case"], str(project)],
                            cwd=folder, env=env, capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=90)
    (folder / "grading.stdout").write_text(graded.stdout)
    (folder / "grading.stderr").write_text(graded.stderr)
    try:
        quality = json.loads(graded.stdout)
    except ValueError:
        quality = {"quality": 0.0, "passed": 0, "total": 0, "critical_checks_passed": False,
                   "failed_checks": [], "grading_error": True}
        issues.append("grader_failed")
    if graded.returncode:
        issues.append("grader_failed")
    if timed_out or code != 0:
        usage["complete"] = False
        usage["issues"] = sorted(set(usage["issues"] + ["process_did_not_complete"]))
    return {**job, "returncode": code, "timed_out": timed_out,
            "elapsed_seconds": elapsed, "usage": usage, "quality": quality,
            "protocol_issues": sorted(set(issues)), "workflow_complete": not issues,
            "workers": workers, "task_statuses": statuses,
            "solution_hashes": {str(p.relative_to(solution)): digest(p)
                                for p in sorted(solution.rglob("*.py"))}}


def load_protocol(base):
    protocol = json.loads((base / "protocol.json").read_text())
    for relative, expected in protocol["source_hashes"].items():
        if digest(ROOT / relative) != expected:
            raise ValueError("frozen benchmark source changed: " + relative)
    if digest(base / "worker-config.toml") != protocol["worker_config_sha256"]:
        raise ValueError("frozen worker config changed")
    return protocol


def run(args):
    base = args.directory.resolve()
    protocol = load_protocol(base)
    completed = 0
    for job in protocol["jobs"]:
        folder = base / job["id"]
        if (folder / "result.json").exists():
            continue
        if args.limit is not None and completed >= args.limit:
            break
        if (folder / "started.json").exists():
            raise ValueError("interrupted run needs inspection; refusing to overwrite " + job["id"])
        if digest(folder / "prompt.txt") != job["prompt_sha256"]:
            raise ValueError("frozen prompt changed")
        env = environment(base)
        project = folder / "project"
        if command(["git", "rev-parse", "HEAD"], project, env) != job["initial_commit"]:
            raise ValueError("initial fixture commit changed")
        for relative, content in CASES[job["case"]]["files"].items():
            if (project / relative).read_text() != content:
                raise ValueError("initial fixture bytes changed")
        for name, expected in job["runtime_hashes"].items():
            if digest(project / ".baton" / name) != expected:
                raise ValueError("initial runtime bytes changed")
        env["PI_CODING_AGENT_SESSION_DIR"] = str(folder / "sessions")
        profile = protocol["solver"]
        argv = ["pi", *COMMON_FLAGS, "--session-dir", str(folder / "sessions"),
                "--provider", profile["provider"], "--model", profile["model"],
                "--thinking", profile["effort"], "--", (folder / "prompt.txt").read_text()]
        print(f"START {job['id']} {job['case']} repeat={job['repeat']} {job['arm']}", flush=True)
        timed_out = False
        started = time.monotonic()
        with (folder / "solver.jsonl").open("w") as stdout, (folder / "solver.stderr").open("w") as stderr:
            process = subprocess.Popen(argv, cwd=folder / "project", env=env, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, start_new_session=True)
            save(folder / "started.json", {"pid": process.pid, "at": datetime.now(timezone.utc).isoformat()})
            try:
                process.wait(timeout=protocol["timeout_seconds"])
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_descendants(process.pid)
                process.wait(timeout=15)
            except BaseException:
                stop_descendants(process.pid)
                process.wait(timeout=15)
                raise
        elapsed = time.monotonic() - started
        result = analyze(base, protocol, job, process.returncode, timed_out, elapsed)
        save(folder / "result.json", result)
        completed += 1
        print(f"END {job['id']} checks={result['quality']['passed']}/{result['quality']['total']} "
              f"tokens={result['usage']['totalTokens']} complete={result['usage']['complete']} "
              f"workflow={result['workflow_complete']} seconds={elapsed:.1f}", flush=True)
        if ("provider_error_or_incomplete_response" in result["usage"]["issues"]
                or "grader_failed" in result["protocol_issues"]):
            print("STOP: provider/grader failure observed; inspect before further paid runs", flush=True)
            break


def summarize(args):
    base = args.directory.resolve()
    protocol = load_protocol(base)
    results = [json.loads((base / job["id"] / "result.json").read_text())
               for job in protocol["jobs"] if (base / job["id"] / "result.json").exists()]
    pairs = []
    for name in CASES:
        for repeat in range(1, protocol["repeats"] + 1):
            selected = {r["arm"]: r for r in results if r["case"] == name and r["repeat"] == repeat}
            if len(selected) != 2:
                continue
            pair = {"case_id": f"{name}-{repeat}", "rubric_id": f"agent-pilot-v1:{name}"}
            for arm, result in selected.items():
                usage, quality = result["usage"], result["quality"]
                pair[arm] = {"quality": quality["quality"],
                             "critical_checks_passed": quality["critical_checks_passed"] and result["workflow_complete"],
                             "usage_complete": usage["complete"],
                             "logical_input_tokens": usage["logical_input_tokens"],
                             "output_tokens": usage["output"], "elapsed_seconds": result["elapsed_seconds"]}
            pairs.append(pair)
    publication = args.publish.resolve() if args.publish else base / "summary"
    if publication.exists():
        raise ValueError("refusing to overwrite a published summary")
    publication.mkdir(parents=True)
    save(publication / "protocol.json", protocol)
    save(publication / "results.json", {"schema_version": 1, "results": results,
                                       "pending": len(protocol["jobs"]) - len(results)})
    if pairs:
        spec = importlib.util.spec_from_file_location("evaluator", ROOT / "tools" / "evaluate_delegation.py")
        evaluator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(evaluator)
        measurements = {"schema_version": 1, "pairs": pairs}
        save(publication / "measurements.json", measurements)
        save(publication / "evaluation.json", evaluator.evaluate(measurements))
    for result in results:
        target = publication / "solutions" / result["id"]
        shutil.copytree(base / result["id"] / "solution", target)
    print(f"wrote {publication}; {len(results)} completed runs, {len(pairs)} paired comparisons")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("prepare", help="freeze fixtures/protocol without calling models")
    setup.add_argument("directory", type=Path)
    setup.add_argument("--config", type=Path, required=True)
    setup.add_argument("--repeats", type=int, default=2)
    setup.add_argument("--seed", type=int, default=20260907)
    setup.add_argument("--timeout-seconds", type=int, default=1500)
    setup.add_argument("--provider", default=os.environ.get("PI_PROVIDER"))
    setup.add_argument("--model", default=os.environ.get("PI_MODEL"))
    setup.add_argument("--effort", default=os.environ.get("PI_REASONING_LEVEL"))
    setup.set_defaults(function=prepare)
    live = commands.add_parser("run", help="LIVE: invoke fresh agents; may consume paid usage")
    live.add_argument("directory", type=Path)
    live.add_argument("--limit", type=int)
    live.set_defaults(function=run)
    summary = commands.add_parser("summarize", help="publish redacted measurements, not raw transcripts")
    summary.add_argument("directory", type=Path)
    summary.add_argument("--publish", type=Path)
    summary.set_defaults(function=summarize)
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11+ required")
    try:
        args.function(args)
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as error:
        parser.exit(2, f"benchmark error: {error}\n")


if __name__ == "__main__":
    main()
