"""Offline answer-key sanity checks for the grader; never copied to solver runs."""

RETRY = '''import math
import re
from datetime import datetime
from email.utils import parsedate_to_datetime


def retry_delay(value, now, default=1.0, cap=300.0):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware now required")
    bounds = []
    for item in (default, cap):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("numeric bounds required")
        try:
            number = float(item)
        except (ValueError, OverflowError):
            raise ValueError("finite bounds required") from None
        if not math.isfinite(number) or number < 0:
            raise ValueError("finite nonnegative bounds required")
        bounds.append(number)
    default, cap = bounds
    fallback = min(default, cap)
    if not isinstance(value, str):
        return fallback
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        seconds = 0
        for digit in value:
            seconds = seconds * 10 + ord(digit) - ord("0")
            if seconds >= cap:
                return cap
        return float(seconds)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None or date.utcoffset() is None:
            return fallback
        return float(min(cap, max(0, (date - now).total_seconds())))
    except (ValueError, TypeError, OverflowError):
        return fallback
'''

CSVIO = '''import csv
from datetime import datetime, timezone
from decimal import Decimal
import io
import re


def read_events(text):
    rows = iter(csv.reader(io.StringIO(text.removeprefix("\\ufeff")), strict=True))
    header = None
    output = []
    try:
        for row in rows:
            if not row:
                continue
            fields = [item.strip() for item in row]
            if header is None:
                header = fields
                if len(header) != 5 or set(header) != {"event_id", "sku", "quantity", "unit_price", "when"}:
                    raise ValueError("invalid header")
                continue
            if len(fields) != 5:
                raise ValueError("invalid width")
            record = dict(zip(header, fields))
            if not record["event_id"] or not record["sku"]:
                raise ValueError("blank identity")
            if not re.fullmatch(r"[+-]?[0-9]+", record["quantity"]):
                raise ValueError("invalid quantity")
            quantity = int(record["quantity"])
            if quantity == 0 or abs(quantity) > 1000000:
                raise ValueError("invalid quantity")
            if not re.fullmatch(r"[0-9]+(?:\\.[0-9]{1,2})?", record["unit_price"]):
                raise ValueError("invalid price")
            price = Decimal(record["unit_price"])
            if price > 1000000:
                raise ValueError("invalid price")
            when = datetime.fromisoformat(record["when"])
            if when.tzinfo is None or when.utcoffset() is None:
                raise ValueError("aware timestamp required")
            output.append(dict(event_id=record["event_id"], sku=record["sku"],
                               quantity=quantity, unit_cents=int(price * 100),
                               timestamp=when.astimezone(timezone.utc)))
    except csv.Error as error:
        raise ValueError("invalid CSV") from error
    return output
'''

REPORT = '''from datetime import datetime


def summarize(events, since=None, until=None):
    for bound in (since, until):
        if bound is not None and (not isinstance(bound, datetime) or bound.utcoffset() is None):
            raise ValueError("aware window required")
    if since is not None and until is not None and since > until:
        raise ValueError("reversed window")
    seen = {}
    for event in events:
        signature = tuple(event[key] for key in ("sku", "quantity", "unit_cents", "timestamp"))
        identifier = event["event_id"]
        if identifier in seen and seen[identifier] != signature:
            raise ValueError("conflicting event")
        seen[identifier] = signature
    items = {}
    count = 0
    for sku, quantity, cents, when in seen.values():
        if (since is not None and when < since) or (until is not None and when >= until):
            continue
        count += 1
        item = items.setdefault(sku, dict(sku=sku, quantity=0, value_cents=0))
        item["quantity"] += quantity
        item["value_cents"] += quantity * cents
    ordered = [items[key] for key in sorted(items)]
    return dict(events=count, items=ordered,
                total_value_cents=sum(item["value_cents"] for item in ordered))
'''

CLI = '''import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from .csvio import read_events
from .report import summarize


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("--output")
    parser.add_argument("--since")
    parser.add_argument("--until")
    args = parser.parse_args(argv)
    temporary = None
    try:
        since = datetime.fromisoformat(args.since) if args.since is not None else None
        until = datetime.fromisoformat(args.until) if args.until is not None else None
        events = read_events(Path(args.input).read_text(encoding="utf-8"))
        data = json.dumps(summarize(events, since, until), sort_keys=True, allow_nan=False) + "\\n"
        if args.output is None:
            sys.stdout.write(data)
        else:
            destination = Path(args.output)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        return 0
    except (ValueError, OSError, UnicodeError) as error:
        print("error: " + str(error), file=sys.stderr)
        return 2
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
'''


SOLUTIONS = {
    "retry_after": {"retry_after.py": RETRY},
    "ledger": {"ledger/csvio.py": CSVIO, "ledger/report.py": REPORT, "ledger/cli.py": CLI},
}
