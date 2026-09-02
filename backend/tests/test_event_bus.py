"""Event bus: ordering, replay, and refusing to be slowed down by a slow client."""

from __future__ import annotations

import pytest

from app.domain.events import DomainEvent
from app.modules.notification.event_bus import (
    SUBSCRIBER_QUEUE_SIZE,
    EventBus,
    SubscriberOverrun,
)
from app.modules.notification.sse import event_stream


def _event(topic: str, type_: str = "test") -> DomainEvent:
    return DomainEvent(topic=topic, type=type_)


async def test_sequence_numbers_are_global_and_monotonic() -> None:
    bus = EventBus()
    first = bus.publish(_event("session:a"))
    second = bus.publish(_event("user:b"))
    third = bus.publish(_event("session:a"))
    assert [first.seq, second.seq, third.seq] == [1, 2, 3]


async def test_subscriber_only_receives_its_topics() -> None:
    bus = EventBus()
    sub = bus.subscribe(["session:a"])
    bus.publish(_event("session:a", "wanted"))
    bus.publish(_event("user:b", "ignored"))

    received = await sub.next(timeout=0.1)
    assert received is not None and received.type == "wanted"
    assert await sub.next(timeout=0.01) is None


async def test_replay_returns_only_what_was_missed_in_order() -> None:
    bus = EventBus()
    for index in range(5):
        bus.publish(_event("session:a", f"e{index}"))
    bus.publish(_event("user:b", "other"))

    missed = bus.replay_since(["session:a", "user:b"], last_seq=3)
    assert [e.type for e in missed] == ["e3", "e4", "other"]
    assert [e.seq for e in missed] == sorted(e.seq for e in missed)


async def test_replay_window_is_bounded() -> None:
    bus = EventBus(replay=3)
    for index in range(10):
        bus.publish(_event("session:a", f"e{index}"))
    assert len(bus.replay_since(["session:a"], last_seq=0)) == 3
    assert bus.oldest_available(["session:a"]) == 8


async def test_a_slow_subscriber_is_dropped_not_buffered() -> None:
    """The publisher may be the session runner; it must never block on a stalled client."""
    bus = EventBus()
    sub = bus.subscribe(["session:a"])
    for _ in range(SUBSCRIBER_QUEUE_SIZE + 20):
        bus.publish(_event("session:a"))  # never awaits, never raises

    with pytest.raises(SubscriberOverrun):
        await sub.next(timeout=0.01)


async def test_dropping_a_topic_clears_its_replay_ring() -> None:
    bus = EventBus()
    bus.subscribe(["session:a"])
    bus.publish(_event("session:a", "transcript.final"))
    bus.drop_topic("session:a")
    assert bus.replay_since(["session:a"], last_seq=0) == []
    assert bus.stats["subscribers"] == 0


# ===================================================================== stream open
async def _drain(stream, limit: int = 20) -> list[dict]:
    frames: list[dict] = []
    async for frame in stream:
        frames.append(frame)
        if frame.get("event") == "stream.open" or len(frames) >= limit:
            break
    return frames


async def test_a_first_lobby_connection_starts_from_now() -> None:
    """History must not be re-announced to someone who just opened the app.

    A replayed ``session.ready`` for a discussion that ended yesterday would put a live
    Join button in front of a user, and a replayed ``invitation.sent`` would raise a
    notification for an invitation they already answered.
    """
    bus = EventBus()
    bus.publish(_event("user:a", "invitation.sent"))
    bus.publish(_event("user:a", "session.ready"))

    frames = await _drain(event_stream(bus, ["user:a"], replay_on_open=False))
    assert [f.get("event") for f in frames] == ["stream.open"]


async def test_a_room_connection_replays_the_discussion_so_far() -> None:
    bus = EventBus()
    bus.publish(_event("session:a", "moderator.speaking"))
    bus.publish(_event("session:a", "floor.granted"))

    frames = await _drain(event_stream(bus, ["session:a"]))
    assert [f.get("event") for f in frames] == [
        "moderator.speaking",
        "floor.granted",
        "stream.open",
    ]


async def test_a_reconnect_replays_what_was_missed_even_on_the_lobby() -> None:
    bus = EventBus()
    first = bus.publish(_event("user:a", "invitation.sent"))
    bus.publish(_event("user:a", "classroom.updated"))

    frames = await _drain(
        event_stream(bus, ["user:a"], last_event_id=first.seq, replay_on_open=False)
    )
    assert [f.get("event") for f in frames] == ["classroom.updated", "stream.open"]
