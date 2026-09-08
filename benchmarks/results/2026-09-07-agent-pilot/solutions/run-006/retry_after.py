"""Parse HTTP Retry-After headers into bounded delays in seconds."""

from datetime import datetime
from email.utils import parsedate_to_datetime
from math import isfinite


def _finite_nonnegative_float(value, name):
    message = f"{name} must be a finite nonnegative int or float (not bool)"
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
    """Return a float delay for ASCII seconds or an aware email-style date.

    Invalid headers use min(default, cap); invalid arguments raise ValueError,
    even when the header is missing. Date delays use absolute instants.
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    try:
        now_offset = now.utcoffset()
    except Exception as exc:
        # An unusable tzinfo makes now invalid, rather than the header malformed.
        raise ValueError("now must have a valid UTC offset") from exc
    if now_offset is None:
        raise ValueError("now must be a timezone-aware datetime")
    default = _finite_nonnegative_float(default, "default")
    cap = _finite_nonnegative_float(cap, "cap")
    fallback = min(default, cap)

    if not isinstance(value, str):
        return fallback
    header = value.strip()
    if not header:
        return fallback
    if header.isascii() and header.isdigit():
        # float has no integer-string digit limit; overflow becomes inf and caps.
        return min(float(header), cap)

    try:
        parsed = parsedate_to_datetime(header)
    except (TypeError, ValueError, IndexError, OverflowError):
        return fallback
    parsed_offset = parsed.utcoffset()
    if parsed_offset is None:
        return fallback

    # Apply offsets without converting to UTC datetimes, which can overflow at
    # datetime.min/max. Timedelta arithmetic also retains fractional seconds.
    delay = (
        parsed.replace(tzinfo=None) - now.replace(tzinfo=None)
        - parsed_offset + now_offset
    ).total_seconds()
    return min(max(delay, 0.0), cap)
