"""Retry with jitter, and a circuit breaker.

An AI provider is the least reliable part of this system and sits directly on the
moderator's critical path. Two rules keep a provider incident from becoming a session
incident: never retry more than the latency budget allows, and stop calling a provider
that is clearly down instead of adding a timeout to every turn.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.core.errors import ExternalServiceError
from app.core.logging import get_logger

log = get_logger(__name__)
T = TypeVar("T")


async def retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.25,
    max_delay: float = 2.0,
    provider: str = "provider",
) -> T:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last = exc
            if attempt == attempts:
                break
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay = delay * (0.5 + random.random())  # full jitter
            log.warning("provider.retry", provider=provider, attempt=attempt, error=str(exc))
            await asyncio.sleep(delay)
    raise ExternalServiceError(provider, str(last))


class CircuitBreaker:
    """Closed → open after N consecutive failures → half-open after a cool-down."""

    def __init__(self, name: str, *, threshold: int = 5, cooldown: float = 30.0) -> None:
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.cooldown:
            self._opened_at = None  # half-open: let one call through
            self._failures = self.threshold - 1
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold and self._opened_at is None:
            self._opened_at = time.monotonic()
            log.error("circuit.open", provider=self.name, failures=self._failures)

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        if self.is_open:
            raise ExternalServiceError(self.name, "Circuit is open.")
        try:
            result = await fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result
