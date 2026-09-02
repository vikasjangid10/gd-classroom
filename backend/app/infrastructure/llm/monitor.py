"""What the chain records, and why it records so little.

Only *state transitions* — a tier being taken out of rotation, a tier coming back, the
chain running out. Logging every call would bury those events under one line per turn per
tier, and the events are the entire reason anyone reads these logs.

Every event goes to two places, because they answer different questions:

* the **structured log** is durable — it is what you grep tomorrow when somebody asks
  why the moderator sounded scripted for twenty minutes last Tuesday;
* a **bounded ring buffer** is live — it is what the status endpoint renders, and being
  bounded is what stops a month of uptime from becoming a memory leak.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)


class Event:
    """The transitions worth a line. Anything not here happens too often to record."""

    SELECTED = "tier_selected"
    SKIPPED = "tier_skipped"
    EXHAUSTED = "tier_exhausted"
    THROTTLED = "tier_throttled"
    MISCONFIGURED = "tier_misconfigured"
    FAILING = "tier_failing"
    RECOVERED = "tier_recovered"
    CHAIN_EXHAUSTED = "chain_exhausted"


@dataclass(frozen=True, slots=True)
class ChainEvent:
    at: float
    kind: str
    tier: str
    purpose: str = ""
    detail: str = ""
    #: On an exhaustion, the rung picking up the slack — so one line answers both
    #: "what died" and "what took over" without needing the next line to arrive.
    successor: str = ""
    clears_at: float | None = None
    #: Which chain this happened on — "fast" (what the room hears) or "deep" (what judges
    #: what was said). Stamped by the lane view, never passed at the call site.
    lane: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class ChainMonitor:
    def __init__(self, *, buffer_size: int = 200) -> None:
        self._events: deque[ChainEvent] = deque(maxlen=buffer_size)
        self._lock = threading.Lock()
        #: Tiers currently known to be out. Kept so that "recovered" is only emitted for
        #: something that was actually down — otherwise every successful call after a
        #: single blip would claim a recovery.
        self._down: set[str] = set()
        self._lane = ""

    def for_lane(self, lane: str) -> ChainMonitor:
        """A view that stamps its lane on every event and *shares* this one's storage.

        Shared deliberately. With two chains running, the question anyone actually asks
        is "what happened, in what order" — and two separate buffers cannot answer it,
        because interleaving is exactly the information they threw away. The router still
        knows nothing about lanes: it holds a monitor and records what it always did.
        """
        view = ChainMonitor.__new__(ChainMonitor)
        view._events = self._events
        view._lock = self._lock
        view._down = self._down
        view._lane = lane
        return view

    def _record(self, event: ChainEvent, level: str = "info") -> None:
        event = replace(event, lane=self._lane)
        with self._lock:
            self._events.append(event)
        payload = {k: v for k, v in asdict(event).items() if v not in ("", None, {})}
        payload.pop("at", None)
        getattr(log, level)(f"llm.{event.kind}", **payload)

    # ================================================================ transitions
    def selected(self, tier: str, purpose: str, *, after_failures: int = 0) -> None:
        # Only interesting when it followed a failure. The happy path is one line per
        # call, which is exactly the noise this module exists to avoid.
        if after_failures:
            self._record(
                ChainEvent(time.time(), Event.SELECTED, tier, purpose, extra={
                    "after_failures": after_failures
                })
            )
        if tier in self._down:
            self._down.discard(tier)
            self._record(ChainEvent(time.time(), Event.RECOVERED, tier, purpose))

    def skipped(self, tier: str, purpose: str, reason: str) -> None:
        self._record(ChainEvent(time.time(), Event.SKIPPED, tier, purpose, detail=reason))

    def exhausted(
        self, tier: str, purpose: str, detail: str, *, successor: str, clears_at: float | None
    ) -> None:
        self._down.add(tier)
        self._record(
            ChainEvent(
                time.time(), Event.EXHAUSTED, tier, purpose, detail,
                successor=successor, clears_at=clears_at,
            ),
            level="warning",
        )

    def throttled(self, tier: str, purpose: str, detail: str, *, successor: str) -> None:
        # Not added to ``_down``: a throttle never takes a tier out of rotation, so it
        # has nothing to recover from.
        self._record(
            ChainEvent(time.time(), Event.THROTTLED, tier, purpose, detail, successor=successor)
        )

    def misconfigured(
        self, tier: str, purpose: str, detail: str, *, successor: str, clears_at: float | None
    ) -> None:
        self._down.add(tier)
        self._record(
            ChainEvent(
                time.time(), Event.MISCONFIGURED, tier, purpose, detail,
                successor=successor, clears_at=clears_at,
            ),
            level="error",
        )

    def failing(
        self,
        tier: str,
        purpose: str,
        detail: str,
        *,
        streak: int,
        successor: str,
        clears_at: float | None,
    ) -> None:
        if clears_at:
            self._down.add(tier)
        self._record(
            ChainEvent(
                time.time(), Event.FAILING, tier, purpose, detail,
                successor=successor, clears_at=clears_at, extra={"streak": streak},
            ),
            level="warning",
        )

    def chain_exhausted(self, purpose: str, detail: str) -> None:
        self._record(
            ChainEvent(time.time(), Event.CHAIN_EXHAUSTED, "", purpose, detail), level="error"
        )

    # ================================================================ reading
    def recent(self, limit: int = 50, *, lane: str = "") -> list[ChainEvent]:
        """Newest first, across both lanes unless one is named."""
        with self._lock:
            events = [e for e in self._events if not lane or e.lane == lane]
        return events[-limit:][::-1]
