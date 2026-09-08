"""Inventory-ledger command-line interface."""

import argparse
from datetime import datetime
import json
import os
import sys
import tempfile

from .csvio import read_events
from .report import summarize


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _write_atomic(path: str, payload: str) -> None:
    """Publish a fully flushed regular file without touching the old output."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=os.path.dirname(os.path.abspath(path)),
            prefix=".ledger-", suffix=".tmp",
        ) as stream:
            temporary = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def main(argv=None) -> int:
    """Return 0 on success or 2 for invalid arguments/data or I/O failures."""
    parser = _ArgumentParser(
        prog="ledger", description="Reconcile inventory events from UTF-8 CSV.",
        allow_abbrev=False,
    )
    parser.add_argument("input", metavar="INPUT")
    parser.add_argument("--output", metavar="PATH")
    parser.add_argument("--since", metavar="ISO")
    parser.add_argument("--until", metavar="ISO")
    try:
        args = parser.parse_args(argv)
        since = datetime.fromisoformat(args.since) if args.since is not None else None
        until = datetime.fromisoformat(args.until) if args.until is not None else None
        with open(args.input, encoding="utf-8", newline="") as stream:
            events = read_events(stream.read())
        payload = json.dumps(summarize(events, since, until), sort_keys=True) + "\n"
        if args.output is None:
            sys.stdout.write(payload)
            sys.stdout.flush()
        else:
            _write_atomic(args.output, payload)
    except SystemExit as exc:
        # argparse's --help is successful, including for direct main() callers.
        return int(exc.code)
    except (OSError, ValueError) as exc:
        print(f"ledger: error: {exc}", file=sys.stderr)
        return 2
    return 0
