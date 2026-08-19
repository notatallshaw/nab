"""Retry policy for index GETs, shared by the async transports.

An index GET is idempotent, and a 5xx, a 429, a 408, or a dropped connection
is usually a blip rather than the index's answer.

urllib3's default retries a connection error but retries a 413, 429, or 503
only when the response carries ``Retry-After``; httpx retries nothing. Both
transports take the policy here instead of their library's default.
"""

from __future__ import annotations

import random

import urllib3
from typing_extensions import override
from urllib3.exceptions import InvalidHeader

from .retry_limits import MAX_REDIRECTS, MAX_RETRIES, RETRY_STATUSES

__all__ = [
    "GET_RETRY",
    "next_delay",
]

_BACKOFF_FACTOR = 0.25

# Without a random spread, requests throttled together retry in lockstep and
# re-trigger the throttle.
_BACKOFF_JITTER = 0.25

# Unbounded, a "Retry-After: 3600" would park the resolve for an hour.
_RETRY_AFTER_MAX_SECONDS = 10.0

_RETRY_AFTER_PARSER = urllib3.Retry()


def _retry_after_seconds(value: str) -> float | None:
    """Bounded Retry-After in seconds; None when the header does not parse."""
    try:
        seconds = _RETRY_AFTER_PARSER.parse_retry_after(value)
    except (InvalidHeader, ValueError, OverflowError):
        # urllib3 raises InvalidHeader for what it rejects up front; the rest
        # raise out of its int or date conversion.
        return None
    return min(seconds, _RETRY_AFTER_MAX_SECONDS)


class _BoundedRetry(urllib3.Retry):
    """Retry that bounds the Retry-After wait and ignores a malformed one."""

    @override
    def get_retry_after(self, response: urllib3.BaseHTTPResponse) -> float | None:
        value = response.headers.get("Retry-After")
        return None if value is None else _retry_after_seconds(value)


GET_RETRY = _BoundedRetry(
    # A shared total would count redirects against the transient budget.
    total=None,
    connect=MAX_RETRIES,
    read=MAX_RETRIES,
    status=MAX_RETRIES,
    other=MAX_RETRIES,
    redirect=MAX_REDIRECTS,
    status_forcelist=RETRY_STATUSES,
    backoff_factor=_BACKOFF_FACTOR,
    backoff_jitter=_BACKOFF_JITTER,
    # Once the budget is spent, hand the response back so the caller sees the
    # status the index served rather than a retry error.
    raise_on_status=False,
)


def next_delay(failures: int, retry_after: str | None = None) -> float:
    """Seconds to wait after ``failures`` attempts, on urllib3's schedule."""
    if retry_after is not None:
        seconds = _retry_after_seconds(retry_after)
        if seconds is not None:
            return seconds
    if failures <= 1:
        return 0.0
    # Annotated because typeshed types int ** int as Any: a negative exponent
    # yields a float.
    backoff: float = _BACKOFF_FACTOR * 2 ** (failures - 1)
    return backoff + random.random() * _BACKOFF_JITTER  # noqa: S311
