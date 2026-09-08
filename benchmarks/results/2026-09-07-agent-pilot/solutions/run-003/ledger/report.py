"""Deterministic inventory reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone



def _as_utc(value, label):
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime")
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{label} must be timezone-aware")
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        if isinstance(exc, ValueError) and str(exc) == f"{label} must be timezone-aware":
            raise
        raise ValueError(f"invalid {label} datetime") from exc


def summarize(events, since=None, until=None):
    """Deduplicate and aggregate an iterable of normalized inventory events."""
    since_utc = _as_utc(since, "since") if since is not None else None
    until_utc = _as_utc(until, "until") if until is not None else None
    if since_utc is not None and until_utc is not None and since_utc > until_utc:
        raise ValueError("since must not be later than until")

    # First reconcile the complete input.  Filtering while iterating would let
    # a conflicting duplicate outside the requested window go unnoticed.
    unique = {}
    for event in events:
        try:
            event_id = event["event_id"]
            sku = event["sku"]
            quantity = event["quantity"]
            unit_cents = event["unit_cents"]
            timestamp = _as_utc(event["timestamp"], "timestamp")
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid event") from exc

        record = (sku, quantity, unit_cents, timestamp)
        try:
            previous = unique.get(event_id)
        except TypeError as exc:
            raise ValueError("event_id must be hashable") from exc
        if previous is not None:
            if previous != record:
                raise ValueError(f"conflicting duplicate event_id: {event_id}")
            continue
        unique[event_id] = record

    totals = {}
    event_count = 0
    for sku, quantity, unit_cents, timestamp in unique.values():
        if since_utc is not None and timestamp < since_utc:
            continue
        if until_utc is not None and timestamp >= until_utc:
            continue

        event_count += 1
        quantity_total, value_total = totals.get(sku, (0, 0))
        totals[sku] = (
            quantity_total + quantity,
            value_total + quantity * unit_cents,
        )

    items = [
        {"sku": sku, "quantity": quantity, "value_cents": value}
        for sku, (quantity, value) in sorted(totals.items())
    ]
    total_value_cents = sum(item["value_cents"] for item in items)
    return {
        "events": event_count,
        "items": items,
        "total_value_cents": total_value_cents,
    }
