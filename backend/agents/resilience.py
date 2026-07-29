"""Shared retry-with-backoff and circuit-breaker primitives for the Failure Containment /
Blast Radius hardening pattern — used by both the LLM client and the MCP client so a
misbehaving external dependency degrades gracefully instead of cascading into every ticket.
"""
import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class CircuitBreaker:
    """Trips open after `failure_threshold` consecutive failures and stays open for
    `cooldown_seconds` before allowing calls through again."""

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._open_until = 0.0

    @property
    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._open_until = 0.0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._open_until = time.monotonic() + self.cooldown_seconds


def call_with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    should_retry: Callable[[Exception], bool] = lambda e: True,
) -> T:
    """Retry a synchronous callable with exponential backoff, only for exceptions where
    should_retry(e) is True — everything else propagates immediately."""
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            if not should_retry(e):
                raise
            last_error = e
            if attempt < max_attempts - 1:
                time.sleep(base_delay * (2**attempt))
    assert last_error is not None
    raise last_error


async def acall_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    should_retry: Callable[[Exception], bool] = lambda e: True,
) -> T:
    """Async version of call_with_retry."""
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except Exception as e:
            if not should_retry(e):
                raise
            last_error = e
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2**attempt))
    assert last_error is not None
    raise last_error
