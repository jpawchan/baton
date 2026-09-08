"""CSV ingestion for normalized stock events."""

import csv
from datetime import datetime, timezone
import io
import re


_COLUMNS = {"event_id", "sku", "quantity", "unit_price", "when"}
_QUANTITY = re.compile(r"[+-]?[0-9]+")
_PRICE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")


def _quantity(value: str) -> int:
    if not _QUANTITY.fullmatch(value):
        raise ValueError("quantity must be an ASCII integer")
    # Remove leading zeroes before conversion, including for very long fields.
    digits = value.lstrip("+-").lstrip("0")
    if not digits or len(digits) > 7 or int(digits) > 1_000_000:
        raise ValueError("quantity must be nonzero with absolute value <= 1000000")
    return -int(digits) if value.startswith("-") else int(digits)


def _unit_cents(value: str) -> int:
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


def _timestamp(value: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.utcoffset() is None:
            raise ValueError("missing timezone offset")
        return timestamp.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError(
            "when must be an ISO-8601 datetime with a timezone offset and a valid UTC equivalent"
        ) from exc


def read_events(text: str) -> list[dict]:
    """Parse and validate all CSV records, preserving order and duplicate IDs.

    Only physically empty records are ignored. Parsing is all-or-nothing;
    invalid headers, CSV syntax, widths, or field values raise ValueError.
    """
    reader = csv.reader(io.StringIO(text.removeprefix("\ufeff"), newline=""), strict=True)
    header = None
    events = []
    field_limit = csv.field_size_limit()
    try:
        # The schema imposes no field-length limit. Restore csv's global
        # setting afterward, including when validation fails.
        csv.field_size_limit(max(field_limit, len(text)))
        for row in reader:
            if not row:
                continue
            fields = [field.strip() for field in row]
            if header is None:
                if len(fields) != len(_COLUMNS) or set(fields) != _COLUMNS:
                    raise ValueError(
                        "header must contain event_id,sku,quantity,unit_price,when exactly once"
                    )
                header = fields
                continue
            if len(fields) != len(header):
                raise ValueError(f"expected {len(header)} fields, got {len(fields)}")
            values = dict(zip(header, fields))
            if not values["event_id"] or not values["sku"]:
                raise ValueError("event_id and sku must be nonblank")
            events.append({
                "event_id": values["event_id"],
                "sku": values["sku"],
                "quantity": _quantity(values["quantity"]),
                "unit_cents": _unit_cents(values["unit_price"]),
                "timestamp": _timestamp(values["when"]),
            })
    except (csv.Error, ValueError) as exc:
        raise ValueError(f"CSV record ending at line {reader.line_num}: {exc}") from exc
    finally:
        csv.field_size_limit(field_limit)
    return events
