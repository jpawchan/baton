"""CSV ingestion for normalized stock events."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone


_FIELDS = ("event_id", "sku", "quantity", "unit_price", "when")
_QUANTITY = re.compile(r"[+-]?[0-9]+")
_UNIT_PRICE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
_MAX_QUANTITY = 1_000_000
_MAX_UNIT_CENTS = 100_000_000


def _parse_quantity(value: str) -> int:
    if _QUANTITY.fullmatch(value) is None:
        raise ValueError("invalid quantity")

    negative = value.startswith("-")
    digits = value[1:] if value[:1] in ("+", "-") else value
    normalized = digits.lstrip("0") or "0"
    if normalized == "0":
        raise ValueError("quantity must be nonzero")
    if len(normalized) > 7 or (
        len(normalized) == 7 and normalized > str(_MAX_QUANTITY)
    ):
        raise ValueError("quantity is out of range")

    parsed = int(normalized)
    return -parsed if negative else parsed


def _parse_unit_price(value: str) -> int:
    if _UNIT_PRICE.fullmatch(value) is None:
        raise ValueError("invalid unit_price")

    whole, separator, fraction = value.partition(".")
    normalized_whole = whole.lstrip("0") or "0"
    if len(normalized_whole) > 7 or (
        len(normalized_whole) == 7 and normalized_whole > "1000000"
    ):
        raise ValueError("unit_price is out of range")

    cents = int(normalized_whole) * 100
    if separator:
        cents += int(fraction.ljust(2, "0"))
    if cents > _MAX_UNIT_CENTS:
        raise ValueError("unit_price is out of range")
    return cents


def _parse_timestamp(value: str) -> datetime:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "timestamp must be timezone-aware":
            raise
        raise ValueError("invalid timestamp") from exc


def read_events(text):
    """Parse CSV text into normalized inventory events.

    The parser deliberately keeps all validation local until the complete CSV
    has been read, so a malformed later row cannot produce partial results.
    """
    if not isinstance(text, str):
        raise ValueError("CSV input must be text")

    if text.startswith("\ufeff"):
        text = text[1:]

    events = []
    positions = None
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        for raw_row in reader:
            # csv.reader represents a physically blank record as [].  Do not
            # discard rows containing empty fields: those are data errors.
            if raw_row == []:
                continue

            row = [field.strip() for field in raw_row]
            if positions is None:
                if len(row) != len(_FIELDS) or set(row) != set(_FIELDS):
                    raise ValueError("invalid CSV header")
                positions = {field: index for index, field in enumerate(row)}
                continue

            if len(row) != len(_FIELDS):
                raise ValueError("invalid CSV row width")
            if all(field == "" for field in row):
                raise ValueError("empty CSV row")

            event_id = row[positions["event_id"]]
            sku = row[positions["sku"]]
            if not event_id:
                raise ValueError("event_id must be nonblank")
            if not sku:
                raise ValueError("sku must be nonblank")

            quantity = _parse_quantity(row[positions["quantity"]])
            unit_cents = _parse_unit_price(row[positions["unit_price"]])
            timestamp = _parse_timestamp(row[positions["when"]])
            events.append(
                {
                    "event_id": event_id,
                    "sku": sku,
                    "quantity": quantity,
                    "unit_cents": unit_cents,
                    "timestamp": timestamp,
                }
            )
    except csv.Error as exc:
        raise ValueError("invalid CSV") from exc

    return events
