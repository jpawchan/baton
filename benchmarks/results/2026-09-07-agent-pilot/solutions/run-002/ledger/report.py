"""Deterministic inventory reconciliation."""

from datetime import datetime, timezone


_MISSING = object()


def _as_utc(value, label):
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be an aware datetime")
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{label} must be an aware datetime")
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be an aware datetime") from exc


def summarize(events, since=None, until=None):
    """Deduplicate and aggregate an iterable of normalized stock events."""
    lower = None if since is None else _as_utc(since, "since")
    upper = None if until is None else _as_utc(until, "until")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("since must not be after until")

    unique = {}
    for event in events:
        try:
            event_id = event["event_id"]
            sku = event["sku"]
            quantity = event["quantity"]
            unit_cents = event["unit_cents"]
            timestamp = _as_utc(event["timestamp"], "event timestamp")
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid event") from exc

        signature = (sku, quantity, unit_cents, timestamp)
        previous = unique.get(event_id, _MISSING)
        if previous is not _MISSING:
            if previous[1:] != signature:
                raise ValueError("conflicting duplicate event_id")
            continue
        unique[event_id] = (event_id,) + signature

    aggregates = {}
    count = 0
    for _, sku, quantity, unit_cents, timestamp in unique.values():
        if lower is not None and timestamp < lower:
            continue
        if upper is not None and timestamp >= upper:
            continue
        count += 1
        quantity_total, value_total = aggregates.get(sku, (0, 0))
        quantity_total += quantity
        value_total += quantity * unit_cents
        aggregates[sku] = quantity_total, value_total

    items = [
        {"sku": sku, "quantity": quantity, "value_cents": value}
        for sku, (quantity, value) in sorted(aggregates.items())
    ]
    return {
        "events": count,
        "items": items,
        "total_value_cents": sum(item["value_cents"] for item in items),
    }
