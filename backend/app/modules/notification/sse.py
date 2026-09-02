"""SSE stream generator.

Reconnection contract:

* every frame carries ``id:`` = the global sequence number;
* on reconnect the browser sends ``Last-Event-ID`` automatically, and the server
  replays everything still in the ring buffer before going live;
* if the client is further behind than the ring reaches, it is told to resync
  (``stream.resync``) rather than being handed a silently incomplete history;
* a comment heartbeat every 15 s keeps proxies and mobile radios from killing the
  connection.

A *first* connection is not a reconnection, and ``replay_on_open`` is where the two
streams part company. The room stream replays, because the discussion so far is exactly
what a late joiner needs. The lobby stream must not: its state is loaded over REST, and
a fresh subscriber replaying the ring would be handed invitations they answered
yesterday and a "your discussion is ready" for a room that has already ended.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable

from app.core.logging import get_logger
from app.domain.events import DomainEvent
from app.modules.notification.event_bus import EventBus, SubscriberOverrun

log = get_logger(__name__)

HEARTBEAT_SECONDS = 15.0


def parse_last_event_id(raw: str | None) -> int:
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _frame(event: DomainEvent) -> dict[str, str]:
    return {
        "id": str(event.seq),
        "event": event.type,
        "data": json.dumps(event.to_frame(), separators=(",", ":"), default=str),
    }


async def event_stream(
    bus: EventBus,
    topics: Iterable[str],
    *,
    last_event_id: int = 0,
    replay_on_open: bool = True,
    label: str = "",
) -> AsyncIterator[dict[str, str]]:
    topic_list = list(topics)
    sub = bus.subscribe(topic_list)
    try:
        oldest = bus.oldest_available(topic_list)
        if last_event_id and oldest and last_event_id < oldest - 1:
            yield {
                "event": "stream.resync",
                "data": json.dumps({"reason": "replay_window_exceeded", "from_seq": oldest}),
            }
            last_event_id = 0

        # No Last-Event-ID and no replay wanted means "start from now". Subscribing
        # happened above, so nothing published from here on can slip through the gap.
        if last_event_id or replay_on_open:
            for missed in bus.replay_since(topic_list, last_event_id):
                yield _frame(missed)

        yield {"event": "stream.open", "data": json.dumps({"topics": topic_list})}

        while True:
            try:
                event = await sub.next(timeout=HEARTBEAT_SECONDS)
            except SubscriberOverrun:
                log.warning("sse.overrun", label=label, topics=topic_list)
                yield {
                    "event": "error",
                    "data": json.dumps(
                        {
                            "code": "stream_overrun",
                            "message": "Reconnecting to catch up.",
                            "recoverable": True,
                        }
                    ),
                }
                return

            if event is None:
                yield {"comment": "heartbeat"}
            else:
                yield _frame(event)
    except asyncio.CancelledError:
        raise
    finally:
        bus.unsubscribe(sub)
