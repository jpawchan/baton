"""Inventory-ledger command-line interface."""

import argparse
from datetime import datetime
import json
import os
import sys
import tempfile

from .csvio import read_events
from .report import summarize


def _publish(path, text):
    """Stage a regular file beside the destination and replace it atomically."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=os.path.dirname(path) or ".", prefix=".ledger-", suffix=".tmp",
        ) as stream:
            temporary = stream.name
            stream.write(text)
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


def _write_stdout(text):
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except OSError:
        # Prevent Python's shutdown flush from retrying a failed buffered write
        # and turning our status 2 into status 120 with an extra diagnostic.
        try:
            sys.stdout.close()
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    """Run the CLI without raising SystemExit, including for argument errors."""
    parser = argparse.ArgumentParser(
        description="Reconcile an inventory CSV into JSON.", allow_abbrev=False,
    )
    parser.add_argument("input", metavar="INPUT", help="UTF-8 event CSV")
    parser.add_argument("--output", metavar="PATH", help="atomically replace this output file")
    parser.add_argument("--since", metavar="ISO", help="inclusive aware datetime")
    parser.add_argument("--until", metavar="ISO", help="exclusive aware datetime")
    try:
        try:
            args = parser.parse_args(argv)
        except SystemExit as exc:
            if exc.code == 0:
                _write_stdout("")  # Also detect buffered I/O failures for --help.
            return exc.code

        since = datetime.fromisoformat(args.since) if args.since is not None else None
        until = datetime.fromisoformat(args.until) if args.until is not None else None
        with open(args.input, encoding="utf-8", newline="") as stream:
            events = read_events(stream.read())
        text = json.dumps(summarize(events, since=since, until=until), sort_keys=True) + "\n"
        if args.output is None:
            _write_stdout(text)
        else:
            _publish(args.output, text)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0
