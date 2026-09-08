"""Parse HTTP Retry-After headers into bounded delays."""

from datetime import datetime
from email.utils import parsedate_to_datetime
from math import isfinite


def _nonnegative_float(value, name):
    message = f"{name} must be an int/float representable as a finite nonnegative float"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    try:
        result = float(value)
    except Exception as exc:
        # Numeric subclasses can also fail while converting to float.
        raise ValueError(message) from exc
    if not isfinite(result) or result < 0:
        raise ValueError(message)
    return result


def retry_delay(value, now, default=1.0, cap=300.0):
    """Return a capped delay as a float; invalid arguments raise ValueError.

    Missing or malformed headers use min(default, cap). Dates must include
    timezone information, and numeric delays must contain only ASCII digits.
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    try:
        now_offset = now.utcoffset()
    except Exception as exc:
        # An unusable tzinfo cannot establish an aware instant.
        raise ValueError("now must be a timezone-aware datetime") from exc
    if now_offset is None:
        raise ValueError("now must be a timezone-aware datetime")

    default = _nonnegative_float(default, "default")
    cap = _nonnegative_float(cap, "cap")
    fallback = min(default, cap)
    if not isinstance(value, str):
        return fallback
    header = value.strip()
    if not header:
        return fallback
    if header.isascii() and header.isdigit():
        # Float parsing has no integer-string digit limit; overflow becomes
        # infinity, which is then bounded by the validated finite cap.
        return min(float(header), cap)

    try:
        retry_at = parsedate_to_datetime(header)
        retry_offset = retry_at.utcoffset()
    except (TypeError, ValueError, OverflowError, IndexError):
        return fallback
    if retry_offset is None:
        return fallback

    # Compare absolute instants without converting either datetime to UTC,
    # which could overflow near datetime.min or datetime.max.
    delay = (
        retry_at.replace(tzinfo=None) - now.replace(tzinfo=None)
        - retry_offset + now_offset
    ).total_seconds()
    return min(max(0.0, delay), cap)
