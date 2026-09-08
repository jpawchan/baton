"""Parse HTTP Retry-After headers into bounded delays in seconds."""

from datetime import datetime
from email.utils import parsedate_to_datetime
from math import isfinite


def _nonnegative_float(value, name):
    message = f"{name} must be an int or float representable as a finite nonnegative float"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(message) from exc
    if not isfinite(result) or result < 0:
        raise ValueError(message)
    return result


def retry_delay(value, now, default=1.0, cap=300.0):
    """Return a float delay, using min(default, cap) for unusable headers.

    Always validate the aware datetime ``now`` and finite, nonnegative numeric
    ``default`` and ``cap`` arguments; invalid arguments raise ValueError.
    """
    if not isinstance(now, datetime):
        raise ValueError("now must be a timezone-aware datetime")
    try:
        now_offset = now.utcoffset()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("now must be a timezone-aware datetime") from exc
    if now_offset is None:
        raise ValueError("now must be a timezone-aware datetime")
    default = _nonnegative_float(default, "default")
    cap = _nonnegative_float(cap, "cap")
    fallback = min(default, cap)

    if not isinstance(value, str):
        return fallback
    value = value.strip()
    if not value:
        return fallback
    if value.isascii() and value.isdigit():
        # Float parsing bypasses integer digit limits; infinity clamps to cap.
        return min(float(value), cap)

    try:
        retry_at = parsedate_to_datetime(value)
        retry_offset = retry_at.utcoffset()
    except (TypeError, ValueError, OverflowError, IndexError):
        return fallback
    if retry_offset is None:
        return fallback

    # Apply offsets to the timedelta, not the datetimes: this compares absolute
    # instants without overflowing at datetime.min/max when converting to UTC.
    delay = (
        retry_at.replace(tzinfo=None) - now.replace(tzinfo=None)
        - retry_offset + now_offset
    ).total_seconds()
    return min(max(0.0, delay), cap)
