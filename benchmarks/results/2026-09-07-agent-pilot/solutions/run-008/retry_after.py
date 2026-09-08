"""Parse HTTP Retry-After headers into bounded delays in seconds."""

from datetime import datetime
from email.utils import parsedate_to_datetime
from math import isfinite


def _nonnegative_finite_float(value, name):
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
    """Return a capped float delay, or a capped default for unusable headers.

    ``now`` must be an aware datetime; ``default`` and ``cap`` must be finite,
    nonnegative int/float values (excluding bool). Invalid arguments raise
    ValueError even when the header is missing or the cap is zero.
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    try:
        now_offset = now.utcoffset()
    except (TypeError, ValueError, OverflowError, NotImplementedError) as exc:
        raise ValueError("now must be a timezone-aware datetime") from exc
    if now_offset is None:
        raise ValueError("now must be a timezone-aware datetime")

    default = _nonnegative_finite_float(default, "default")
    cap = _nonnegative_finite_float(cap, "cap")
    fallback = min(default, cap)
    if not isinstance(value, str):
        return fallback
    header = value.strip()
    if not header:
        return fallback

    if header.isascii() and header.isdigit():
        # Float parsing has no integer-string digit limit. Huge values become
        # infinity, which is safely clamped to the already-validated finite cap.
        return min(float(header), cap)

    try:
        target = parsedate_to_datetime(header)
        target_offset = target.utcoffset()
    except (TypeError, ValueError, OverflowError):
        return fallback
    if target_offset is None:
        return fallback

    # Subtract wall times and offsets separately to compare absolute instants
    # without overflowing UTC datetimes near datetime.min or datetime.max.
    difference = (
        target.replace(tzinfo=None) - now.replace(tzinfo=None)
        - target_offset + now_offset
    )
    return min(max(difference.total_seconds(), 0.0), cap)
