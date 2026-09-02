"""Time as an injectable dependency.

The moderator is full of deadlines — silence windows, turn caps, join windows. Tests
that had to wait 90 real seconds to prove the hard cap works would be useless, so time
is a port like anything else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        import time

        return time.monotonic()


class FrozenClock:
    """Test double. ``advance()`` moves both wall clock and monotonic clock together."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._now += timedelta(seconds=seconds)
        self._mono += seconds


system_clock = SystemClock()
