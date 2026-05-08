"""Retry helper with exponential backoff.

Provider-agnostic — pattern-matches on exception type names and message
content rather than importing each SDK's exception classes. Retryable:
rate limits (429), transient 5xx (500/502/503/504), and timeouts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar


T = TypeVar("T")
_log = logging.getLogger(__name__)

_RETRYABLE_HTTP_STATUSES = {"429", "500", "502", "503", "504"}


def is_retryable(exc: BaseException) -> bool:
    msg = str(exc).lower()
    cls = exc.__class__.__name__.lower()

    if "ratelimit" in cls or "timeout" in cls:
        return True
    if "rate limit" in msg or "rate_limit" in msg or "rate-limit" in msg:
        return True
    if "timeout" in msg or "timed out" in msg:
        return True
    for code in _RETRYABLE_HTTP_STATUSES:
        if code in msg:
            return True
    return False


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 1.0,
    max_delay_s: float = 30.0,
    op_name: str = "llm call",
) -> T:
    """Run `fn` with exponential backoff on retryable failures."""
    delay = base_delay_s
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except Exception as exc:
            if attempt == max_attempts or not is_retryable(exc):
                raise
            _log.warning(
                "%s: retryable %s on attempt %d/%d; sleeping %.1fs (%s)",
                op_name,
                exc.__class__.__name__,
                attempt,
                max_attempts,
                delay,
                str(exc)[:200],
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay_s)
    raise RuntimeError("unreachable")  # for type-checker; loop always raises or returns
