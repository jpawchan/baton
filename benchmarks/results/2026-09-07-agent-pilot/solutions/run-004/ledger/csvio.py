"""CSV ingestion for normalized stock events."""

import csv
from datetime import datetime, timezone
from io import StringIO
import re


_COLUMNS = ("event_id", "sku", "quantity", "unit_price", "when")
_QUANTITY = re.compile(r"[+-]?[0-9]+")
_PRICE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")


def _quantity(value):
    if not _QUANTITY.fullmatch(value):
        raise ValueError("quantity must be an ASCII integer")
    # Remove leading zeros before int(), including for very long valid fields.
    digits = value.lstrip("+-").lstrip("0")
    if not digits or len(digits) > 7 or int(digits) > 1_000_000:
        raise ValueError("quantity must be nonzero with absolute value <= 1000000")
    number = int(digits)
    return -number if value.startswith("-") else number


def _unit_cents(value):
    if not _PRICE.fullmatch(value):
        raise ValueError("unit_price must be unsigned ASCII digits with at most two decimals")
    whole, _, fraction = value.partition(".")
    whole = whole.lstrip("0") or "0"
    if len(whole) > 7:
        raise ValueError("unit_price must be <= 1000000.00")
    cents = int(whole) * 100 + int(fraction.ljust(2, "0"))
    if cents > 100_000_000:
        raise ValueError("unit_price must be <= 1000000.00")
    return cents


def _timestamp(value):
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.utcoffset() is None:
            raise ValueError("when must include a timezone offset")
        return timestamp.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError("when is outside the UTC datetime range") from exc


def read_events(text) -> list[dict]:
    """Parse an entire CSV into ordered events, rejecting any malformed record."""
    reader = csv.reader(StringIO(text.removeprefix("\ufeff"), newline=""), strict=True)
    header = None
    events = []
    previous_limit = csv.field_size_limit()
    try:
        # No field-length restriction is part of the format. Restore the CSV
        # module's process-wide setting after parsing this already-buffered text.
        csv.field_size_limit(max(previous_limit, len(text)))
        for row in reader:
            # Unlike [""] or ["", ...], [] is a physically empty CSV record.
            if not row:
                continue
            fields = [field.strip() for field in row]
            if header is None:
                if len(fields) != len(_COLUMNS) or set(fields) != set(_COLUMNS):
                    raise ValueError("header must contain exactly: " + ",".join(_COLUMNS))
                header = fields
                continue
            if len(fields) != len(header):
                raise ValueError("wrong number of fields")
            record = dict(zip(header, fields))
            if not record["event_id"] or not record["sku"]:
                raise ValueError("event_id and sku must be nonblank")
            events.append({
                "event_id": record["event_id"],
                "sku": record["sku"],
                "quantity": _quantity(record["quantity"]),
                "unit_cents": _unit_cents(record["unit_price"]),
                "timestamp": _timestamp(record["when"]),
            })
    except (csv.Error, ValueError) as exc:
        raise ValueError(f"CSV line {reader.line_num}: {exc}") from exc
    finally:
        csv.field_size_limit(previous_limit)
    return events
