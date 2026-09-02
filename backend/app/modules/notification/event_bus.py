"""In-process publish/subscribe with bounded replay.

This is the whole of the "message broker" in this system. It is roughly eighty lines
because it only has to do three things: fan an event out to the subscribers of a topic,
number events so a reconnecting client can ask for what it missed, and refuse to let a
stalled browser apply backpressure to the moderator.

Sequence numbers are **global**, not per topic, so a subscriber listening to several
topics still receives one monotonic stream and ``Last-Event-ID`` stays unambiguous.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Iterable

from app.core.logging import get_logger
from app.domain.events import DomainEvent

log = get_logger(__name__)

REPLAY_BUFFER = 200
SUBSCRIBER_QUEUE_SIZE = 100


class SubscriberOverrun(Exception):
    """The client fell too far behind; it must reconnect and replay."""


class Subscriber:
    __slots__ = ("overrun", "queue", "topics")

    def __init__(self, topics: frozenset[str]) -> None:
        self.topics = topics
        self.queue: asyncio.Queue[DomainEvent] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self.overrun = False

    def offer(self, event: DomainEvent) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Never await here: the publisher may be the session runner, and a slow
            # laptop must not be able to stall the discussion.
            self.overrun = True

    async def next(self, timeout: float) -> DomainEvent | None:
        if self.overrun:
            raise SubscriberOverrun
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except TimeoutError:
            return None


class EventBus:
    def __init__(self, replay: int = REPLAY_BUFFER) -> None:
        self._seq = 0
        self._replay = replay
        self._rings: dict[str, deque[DomainEvent]] = defaultdict(lambda: deque(maxlen=replay))
        self._subs: dict[str, set[Subscriber]] = defaultdict(set)

    # ---------------------------------------------------------------- publish
    def publish(self, event: DomainEvent) -> DomainEvent:
        self._seq += 1
        event.seq = self._seq
        self._rings[event.topic].append(event)
        for sub in self._subs.get(event.topic, ()):
            sub.offer(event)
        return event

    def publish_all(self, events: Iterable[DomainEvent]) -> None:
        for event in events:
            self.publish(event)

    # ---------------------------------------------------------------- subscribe
    def subscribe(self, topics: Iterable[str]) -> Subscriber:
        sub = Subscriber(frozenset(topics))
        for topic in sub.topics:
            self._subs[topic].add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        for topic in sub.topics:
            self._subs[topic].discard(sub)
            if not self._subs[topic]:
                self._subs.pop(topic, None)

    def replay_since(self, topics: Iterable[str], last_seq: int) -> list[DomainEvent]:
        """Everything the client missed, in global sequence order."""
        missed = [
            event
            for topic in topics
            for event in self._rings.get(topic, ())
            if event.seq > last_seq
        ]
        missed.sort(key=lambda e: e.seq)
        return missed

    def oldest_available(self, topics: Iterable[str]) -> int:
        """Lowest sequence still replayable. Below this the client must do a full resync."""
        firsts = [ring[0].seq for topic in topics if (ring := self._rings.get(topic))]
        return min(firsts) if firsts else 0

    # ---------------------------------------------------------------- lifecycle
    def drop_topic(self, topic: str) -> None:
        """Called at session cleanup — the replay ring is conversation data too."""
        self._rings.pop(topic, None)
        for sub in list(self._subs.get(topic, ())):
            sub.offer(
                DomainEvent(topic=topic, type="session.ended", payload={"reason": "CLEANUP"})
            )
        self._subs.pop(topic, None)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "topics": len(self._rings),
            "subscribers": sum(len(s) for s in self._subs.values()),
            "seq": self._seq,
        }
