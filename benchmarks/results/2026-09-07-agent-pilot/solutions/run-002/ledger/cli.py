"""Inventory-ledger command-line interface."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Sequence

from .csvio import read_events
from .report import summarize


class _ParserExit(Exception):
    """Turn argparse's process exit into a return value from ``main``."""

    def __init__(self, status: int) -> None:
        self.status = status


class _ArgumentParser(argparse.ArgumentParser):
    def exit(self, status: int = 0, message: str | None = None) -> None:
        if message:
            self._print_message(message, sys.stderr)
        raise _ParserExit(status)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="ledger",
        usage="%(prog)s INPUT [--output PATH] [--since ISO] [--until ISO]",
        description="Summarize inventory events from a UTF-8 CSV file.",
    )
    parser.add_argument("input", metavar="INPUT")
    parser.add_argument("--output", metavar="PATH")
    parser.add_argument("--since", metavar="ISO")
    parser.add_argument("--until", metavar="ISO")
    return parser


def _bound(value: str | None, option: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{option} must be a valid ISO datetime") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{option} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _remove_created_temp(path: str, identity: tuple[int, int]) -> None:
    """Remove path only if it is still the regular file created by this run."""

    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(current.st_mode)
        and (current.st_dev, current.st_ino) == identity
    ):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _publish_atomic(destination: Path, text: str) -> None:
    fd, temporary = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    created = os.fstat(fd)
    identity = created.st_dev, created.st_ino
    raw_fd: int | None = fd
    try:
        stream = os.fdopen(fd, "w", encoding="utf-8", newline="")
        raw_fd = None
        with stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        if raw_fd is not None:
            try:
                os.close(raw_fd)
            except OSError:
                pass
        _remove_created_temp(temporary, identity)
        raise


def _error_message(exc: BaseException) -> str:
    message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return f"error: {message}\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process-compatible status code."""

    try:
        args = _parser().parse_args(argv)
    except _ParserExit as exc:
        return exc.status

    try:
        since = _bound(args.since, "--since")
        until = _bound(args.until, "--until")
        if since is not None and until is not None and since > until:
            raise ValueError("--since must not be later than --until")

        text = Path(args.input).read_text(encoding="utf-8")
        events = read_events(text)
        report = summarize(events, since=since, until=until)
        result = json.dumps(report, sort_keys=True) + "\n"

        if args.output is None:
            sys.stdout.write(result)
        else:
            _publish_atomic(Path(args.output), result)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        sys.stderr.write(_error_message(exc))
        return 2

    return 0
