"""Inventory-ledger command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

from .csvio import read_events
from .report import summarize


def _parser():
    parser = argparse.ArgumentParser(prog="python3 -m ledger")
    parser.add_argument("input", metavar="INPUT")
    parser.add_argument("--output", metavar="PATH")
    parser.add_argument("--since", metavar="ISO")
    parser.add_argument("--until", metavar="ISO")
    return parser


def _parse_bound(value, name):
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        if isinstance(exc, ValueError) and str(exc) == f"{name} must be timezone-aware":
            raise
        raise ValueError(f"invalid {name} timestamp") from exc


def _atomic_write(destination, payload):
    directory = os.path.dirname(os.path.abspath(destination))
    basename = os.path.basename(destination) or "ledger"
    temporary_path = None
    file_descriptor = None
    try:
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{basename}.", dir=directory
        )
        with os.fdopen(file_descriptor, "wb") as stream:
            file_descriptor = None
            stream.write(payload.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    except Exception:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
        raise


def _discard_failed_stdout():
    """Keep interpreter shutdown from retrying a failed buffered flush."""
    try:
        replacement = open(os.devnull, "w")
        replacement.close()
        sys.stdout = replacement
    except Exception:
        # A closed/null stdout is preferable to an unraisable flush diagnostic
        # if even opening the null device is unavailable.
        sys.stdout = None


def main(argv=None):
    """Run the ledger pipeline, returning a process-style status code."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        # argparse normally exits the process.  Keep the library entry point
        # callable while preserving its conventional 0/2 status codes.
        return exc.code if isinstance(exc.code, int) else 2

    try:
        with open(args.input, "r", encoding="utf-8", newline="") as stream:
            text = stream.read()

        since = _parse_bound(args.since, "since") if args.since is not None else None
        until = _parse_bound(args.until, "until") if args.until is not None else None
        result = summarize(read_events(text), since=since, until=until)
        payload = json.dumps(result, sort_keys=True) + "\n"

        if args.output is None:
            try:
                sys.stdout.write(payload)
                sys.stdout.flush()
            except Exception:
                _discard_failed_stdout()
                raise
        else:
            _atomic_write(args.output, payload)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0
