#!/usr/bin/env python3
"""Evaluate operator-supplied paired direct/Baton measurements offline.

The evaluator deliberately does not run agents, inspect logs, or verify the
measurements.  It only validates the small JSON schema and applies the stated
pair-level rules.
"""

import argparse
import json
import math
import numbers
from pathlib import Path
import sys
import unicodedata


SCHEMA_VERSION = 1
MAX_ID_LENGTH = 160
VERDICTS = (
    "inconclusive",
    "quality_gate_failed",
    "quality_regression",
    "strict_win",
    "noninferior_saving",
    "quality_gain_only",
    "no_win",
)

_TOP_LEVEL_KEYS = frozenset(("schema_version", "pairs"))
_PAIR_KEYS = frozenset(("case_id", "rubric_id", "direct", "baton"))
_RUN_KEYS = frozenset(
    (
        "quality",
        "critical_checks_passed",
        "usage_complete",
        "logical_input_tokens",
        "output_tokens",
        "elapsed_seconds",
    )
)


class EvaluationError(ValueError):
    """Raised when the input does not satisfy the evaluator contract."""


def _error(path, reason):
    raise EvaluationError(f"{path}: {reason}")


def _require_keys(value, path, expected):
    if not isinstance(value, dict):
        _error(path, "must be an object")

    actual = set(value.keys())
    missing = expected - actual
    extra = actual - expected
    problems = []
    if missing:
        # These are schema names, not input values, so the diagnostic remains
        # useful without echoing an operator's document.
        problems.append(
            "missing required key(s): " + ", ".join(sorted(missing))
        )
    if extra:
        # Do not print arbitrary extra keys: they may contain private data.
        problems.append("unexpected key(s)")
    if problems:
        _error(path, "; ".join(problems))


def _validate_id(value, path):
    if not isinstance(value, str):
        _error(path, "must be a string")
    if not value.strip():
        _error(path, "must be nonblank")
    if len(value) > MAX_ID_LENGTH:
        _error(path, f"must be at most {MAX_ID_LENGTH} characters")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in ("Zl", "Zp")
        for character in value
    ):
        _error(path, "must not contain control or line-separator characters")
    return str(value)


def _finite_real(value, path, *, bounded=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        _error(path, "must be a real number")

    # math.isfinite converts integers to float, which can overflow for a very
    # large but otherwise finite Python int.  Integers are finite by nature.
    if not isinstance(value, int):
        try:
            finite = math.isfinite(value)
        except (OverflowError, TypeError, ValueError):
            finite = False
        if not finite:
            _error(path, "must be finite")

    try:
        if nonnegative and value < 0:
            _error(path, "must be nonnegative")
        if bounded and (value < 0 or value > 1):
            _error(path, "must be between 0 and 1")
    except (TypeError, ValueError, OverflowError):
        _error(path, "must be a finite real number")

    # Keep the returned value JSON-serializable for evaluate() callers using a
    # Real implementation other than the built-in int/float.
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    try:
        return float(value)
    except (OverflowError, TypeError, ValueError):
        _error(path, "must be a finite real number")


def _token_count(value, path):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        _error(path, "must be a nonnegative integer or null")
    if value < 0:
        _error(path, "must be nonnegative")
    return int(value)


def _validate_run(value, path):
    _require_keys(value, path, _RUN_KEYS)

    quality = _finite_real(value["quality"], f"{path}.quality", bounded=True)
    critical_checks_passed = value["critical_checks_passed"]
    if not isinstance(critical_checks_passed, bool):
        _error(f"{path}.critical_checks_passed", "must be a boolean")

    usage_complete = value["usage_complete"]
    if not isinstance(usage_complete, bool):
        _error(f"{path}.usage_complete", "must be a boolean")

    logical_input_tokens = _token_count(
        value["logical_input_tokens"], f"{path}.logical_input_tokens"
    )
    output_tokens = _token_count(
        value["output_tokens"], f"{path}.output_tokens"
    )
    elapsed_seconds = value["elapsed_seconds"]
    if elapsed_seconds is not None:
        _finite_real(
            elapsed_seconds,
            f"{path}.elapsed_seconds",
            nonnegative=True,
        )

    total_tokens = None
    if (
        usage_complete
        and logical_input_tokens is not None
        and output_tokens is not None
    ):
        total_tokens = logical_input_tokens + output_tokens

    return {
        "quality": quality,
        "critical_checks_passed": critical_checks_passed,
        "total_tokens": total_tokens,
    }


def _verdict(direct, baton):
    direct_total = direct["total_tokens"]
    baton_total = baton["total_tokens"]
    if direct_total is None or baton_total is None:
        return "inconclusive"
    if not direct["critical_checks_passed"] or not baton["critical_checks_passed"]:
        return "quality_gate_failed"
    if baton["quality"] < direct["quality"]:
        return "quality_regression"
    if baton["quality"] > direct["quality"] and baton_total < direct_total:
        return "strict_win"
    if baton["quality"] == direct["quality"] and baton_total < direct_total:
        return "noninferior_saving"
    if baton["quality"] > direct["quality"]:
        return "quality_gain_only"
    return "no_win"


def evaluate(data):
    """Validate *data* and return the deterministic paired evaluation report.

    ``data`` is not modified.  It must be a Python representation of the JSON
    input schema; callers that need duplicate-key detection should parse JSON
    with :func:`load_json` (the command-line interface does this).
    """
    _require_keys(data, "input", _TOP_LEVEL_KEYS)

    schema_version = data["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        _error("schema_version", "must be integer 1")
    if schema_version != SCHEMA_VERSION:
        _error("schema_version", "unknown schema version")

    pairs = data["pairs"]
    if not isinstance(pairs, list):
        _error("pairs", "must be a nonempty list")
    if not pairs:
        _error("pairs", "must be a nonempty list")

    seen_case_ids = set()
    output_pairs = []
    verdict_counts = {verdict: 0 for verdict in VERDICTS}

    for index, pair in enumerate(pairs):
        path = f"pairs[{index}]"
        _require_keys(pair, path, _PAIR_KEYS)
        case_id = _validate_id(pair["case_id"], f"{path}.case_id")
        if case_id in seen_case_ids:
            _error(f"{path}.case_id", "duplicate case_id")
        seen_case_ids.add(case_id)
        rubric_id = _validate_id(pair["rubric_id"], f"{path}.rubric_id")
        direct = _validate_run(pair["direct"], f"{path}.direct")
        baton = _validate_run(pair["baton"], f"{path}.baton")

        direct_total = direct["total_tokens"]
        baton_total = baton["total_tokens"]
        token_delta = (
            baton_total - direct_total
            if direct_total is not None and baton_total is not None
            else None
        )
        quality_delta = baton["quality"] - direct["quality"]
        verdict = _verdict(direct, baton)
        verdict_counts[verdict] += 1
        output_pairs.append(
            {
                "case_id": case_id,
                "rubric_id": rubric_id,
                "verdict": verdict,
                "direct_total_tokens": direct_total,
                "baton_total_tokens": baton_total,
                "token_delta": token_delta,
                "quality_delta": quality_delta,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "pairs": output_pairs,
        "summary": {
            "pairs": len(output_pairs),
            "verdicts": verdict_counts,
        },
    }


def _reject_constant(_value):
    raise EvaluationError("input: non-finite JSON number")


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationError("input: duplicate JSON object key")
        result[key] = value
    return result


def load_json(path):
    """Read and parse *path*, rejecting duplicate and non-finite JSON values."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise EvaluationError("input: could not read UTF-8 JSON file")

    try:
        return json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except EvaluationError:
        raise
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise EvaluationError("input: invalid JSON")


def _metric_text(value):
    if value is None:
        return "null"
    return repr(value) if isinstance(value, float) else str(value)


def render_text(result):
    """Render a compact human-readable report for a validated result."""
    lines = []
    for pair in result["pairs"]:
        lines.append(
            "case_id={} rubric_id={} verdict={} token_delta={} quality_delta={}".format(
                pair["case_id"],
                pair["rubric_id"],
                pair["verdict"],
                _metric_text(pair["token_delta"]),
                _metric_text(pair["quality_delta"]),
            )
        )
    lines.append("pairs={}".format(result["summary"]["pairs"]))
    lines.append("verdict counts:")
    for verdict in VERDICTS:
        lines.append(
            "{}: {}".format(verdict, result["summary"]["verdicts"][verdict])
        )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Evaluate operator-supplied paired direct/Baton measurements."
    )
    parser.add_argument("file", metavar="FILE", help="input JSON measurement file")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="write the deterministic report as JSON",
    )
    args = parser.parse_args(argv)

    try:
        result = evaluate(load_json(args.file))
        if args.as_json:
            output = json.dumps(result, sort_keys=True, allow_nan=False)
        else:
            output = render_text(result)
        print(output)
    except EvaluationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        # Keep malformed operator input a concise data error rather than a
        # traceback.  Validation normally turns these into EvaluationError.
        print("error: invalid input", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
