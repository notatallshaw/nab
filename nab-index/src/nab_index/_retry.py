"""Shared HTTP retry policy for the nab-index transports.

All transports issue only idempotent GETs, so retrying is safe.
urllib3-future's default retry has ``backoff_factor=0``, so its attempts
fire within milliseconds and cannot outlast a brief blip; this adds
exponential backoff and retries transient server statuses too.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar

from urllib3.util.retry import Retry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

TOTAL = 3
BACKOFF_FACTOR = 0.5
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

_ResponseT = TypeVar("_ResponseT")


def urllib3_retry() -> Retry:
    """Retry policy for the urllib3 and niquests transports.

    Both run on urllib3-future, so one :class:`~urllib3.util.retry.Retry`
    covers connection, read, and status retries with backoff.  Scoped to
    GET because that is all the transports send.
    """
    return Retry(
        total=TOTAL,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )


def _backoff(attempt: int) -> float:
    return BACKOFF_FACTOR * (2.0**attempt)


async def get_with_retry(
    do_get: Callable[[], Awaitable[_ResponseT]],
    *,
    transient: type[BaseException] | tuple[type[BaseException], ...],
    retry_status: Callable[[_ResponseT], bool],
) -> _ResponseT:
    """Retry an async GET on transient errors or statuses with backoff.

    For transports whose library has no status-aware retry of its own
    (httpx).  Makes up to ``TOTAL`` retries; the final attempt's result
    or exception is returned or propagated unchanged.
    """
    for attempt in range(TOTAL):
        try:
            response = await do_get()
        except transient:
            await asyncio.sleep(_backoff(attempt))
            continue
        if not retry_status(response):
            return response
        await asyncio.sleep(_backoff(attempt))
    return await do_get()
