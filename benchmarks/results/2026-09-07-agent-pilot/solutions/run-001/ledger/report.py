"""Deterministic inventory reconciliation."""

from collections.abc import Iterable
from datetime import datetime, timedelta


def _instant(value: datetime, name: str) -> timedelta:
    """Compare absolute instants, including folds and datetime's endpoints."""
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be an aware datetime")
    offset = value.utcoffset()
    if offset is None:
        raise ValueError(f"{name} must be an aware datetime")
    # Unlike astimezone(), this also works when a boundary's UTC equivalent
    # would fall outside datetime's representable years.
    return value.replace(tzinfo=None) - datetime.min - offset


def summarize(
    events: Iterable[dict], since: datetime | None = None, until: datetime | None = None
) -> dict:
    """Deduplicate globally, then aggregate unique events in [since, until)."""
    lower = _instant(since, "since") if since is not None else None
    upper = _instant(until, "until") if until is not None else None
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("since must not be after until")

    unique = {}
    for event in events:
        event_id = event["event_id"]
        record = (
            event["sku"], event["quantity"], event["unit_cents"],
            _instant(event["timestamp"], "timestamp"),
        )
        if event_id in unique:
            if unique[event_id] != record:
                raise ValueError(f"conflicting duplicate event_id {event_id!r}")
        else:
            unique[event_id] = record

    # No time filtering occurs until every duplicate has been checked.
    totals = {}
    count = 0
    for sku, quantity, unit_cents, timestamp in unique.values():
        if lower is not None and timestamp < lower:
            continue
        if upper is not None and timestamp >= upper:
            continue
        count += 1
        item = totals.setdefault(sku, {"sku": sku, "quantity": 0, "value_cents": 0})
        item["quantity"] += quantity
        item["value_cents"] += quantity * unit_cents

    items = [totals[sku] for sku in sorted(totals)]
    return {
        "events": count,
        "items": items,
        "total_value_cents": sum(item["value_cents"] for item in items),
    }
