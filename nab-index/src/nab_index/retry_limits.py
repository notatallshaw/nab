"""Retry budgets and the statuses that are retried.

Separate from :mod:`.retry` so :mod:`.transport` can classify a status without
importing urllib3.
"""

from __future__ import annotations

__all__ = [
    "MAX_REDIRECTS",
    "MAX_RETRIES",
    "RETRY_STATUSES",
]

MAX_RETRIES = 3
"""Retries after the first attempt: :data:`~.retry.GET_RETRY` counts them per
failure class; the transports' own retry loops count them across all failures."""

MAX_REDIRECTS = 20
"""Redirects followed per GET, matching httpx's default."""

# A 408 says the server gave up waiting for the request (RFC 9110 15.5.9).
# 520-524 and 527 are Cloudflare's transient origin errors, not the index's answer.
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 527})
"""Statuses that are retried; any other status is served to the caller."""
