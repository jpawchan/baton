"""CSV ingestion for normalized stock events."""

import csv
import io
import re
from datetime import datetime, timezone


_HEADER_NAMES = ("event_id", "sku", "quantity", "unit_price", "when")
_HEADER_SET = set(_HEADER_NAMES)
_QUANTITY_RE = re.compile(r"[+-]?[0-9]+")
_PRICE_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")


def _parse_quantity(value):
    if _QUANTITY_RE.fullmatch(value) is None:
        raise ValueError("invalid quantity")
    quantity = int(value)
    if quantity == 0 or abs(quantity) > 1_000_000:
        raise ValueError("quantity out of range")
    return quantity


def _parse_unit_cents(value):
    if _PRICE_RE.fullmatch(value) is None:
        raise ValueError("invalid unit price")
    whole, separator, fraction = value.partition(".")
    cents = int(whole) * 100
    if separator:
        cents += int(fraction.ljust(2, "0"))
    if cents > 100_000_000:
        raise ValueError("unit price out of range")
    return cents


def _parse_timestamp(value):
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timestamp must include an offset")
        return timestamp.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid timestamp") from exc


def read_events(text):
    """Parse CSV text into validated, normalized stock events."""
    if not isinstance(text, str):
        raise ValueError("CSV input must be text")
    if text.startswith("\ufeff"):
        text = text[1:]

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    positions = None
    events = []
    try:
        for raw_record in reader:
            if not raw_record:
                continue
            record = [field.strip() for field in raw_record]
            if positions is None:
                if (len(record) != len(_HEADER_NAMES) or
                        set(record) != _HEADER_SET):
                    raise ValueError("invalid CSV header")
                positions = {name: record.index(name) for name in _HEADER_NAMES}
                continue

            if len(record) != len(_HEADER_NAMES):
                raise ValueError("invalid CSV row width")

            event_id = record[positions["event_id"]]
            sku = record[positions["sku"]]
            if not event_id or not sku:
                raise ValueError("event_id and sku must be nonblank")
            events.append({
                "event_id": event_id,
                "sku": sku,
                "quantity": _parse_quantity(record[positions["quantity"]]),
                "unit_cents": _parse_unit_cents(record[positions["unit_price"]]),
                "timestamp": _parse_timestamp(record[positions["when"]]),
            })
    except csv.Error as exc:
        raise ValueError("malformed CSV") from exc

    return events
