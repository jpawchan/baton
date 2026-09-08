"""Deterministic inventory reconciliation."""

from datetime import datetime


def _instant(value, name):
    offset = value.utcoffset() if isinstance(value, datetime) else None
    if offset is None:
        raise ValueError(f"{name} must be an aware datetime")
    # Compare absolute instants, including folded local times and offsets at
    # datetime.min/max where converting to a UTC datetime could overflow.
    return value.replace(tzinfo=None) - datetime.min - offset


def summarize(events, since=None, until=None) -> dict:
    """Deduplicate all events, then aggregate the half-open [since, until) window."""
    start = _instant(since, "since") if since is not None else None
    stop = _instant(until, "until") if until is not None else None
    if start is not None and stop is not None and start > stop:
        raise ValueError("since must not be after until")

    unique = {}
    for event in events:
        event_id = event["event_id"]
        signature = (
            event["sku"], event["quantity"], event["unit_cents"],
            _instant(event["timestamp"], "timestamp"),
        )
        if event_id in unique and unique[event_id] != signature:
            raise ValueError(f"conflicting event_id: {event_id!r}")
        unique[event_id] = signature

    # Do not discard anything until every duplicate has been checked.
    grouped = {}
    count = 0
    for sku, quantity, unit_cents, timestamp in unique.values():
        if start is not None and timestamp < start:
            continue
        if stop is not None and timestamp >= stop:
            continue
        count += 1
        item = grouped.setdefault(sku, {"sku": sku, "quantity": 0, "value_cents": 0})
        item["quantity"] += quantity
        item["value_cents"] += quantity * unit_cents

    items = [grouped[sku] for sku in sorted(grouped)]
    return {
        "events": count,
        "items": items,
        "total_value_cents": sum(item["value_cents"] for item in items),
    }
