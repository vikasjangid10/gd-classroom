"""Domain events — the payloads that travel over SSE.

Events are facts, in the past tense, and they carry *absolute* state rather than deltas
(``seconds=41``, never ``seconds_delta=3``). That is what makes the browser reducer
idempotent, which is what makes at-least-once replay after a reconnect harmless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


class EventType:
    CLASSROOM_UPDATED = "classroom.updated"
    INVITATION_SENT = "invitation.sent"
    INVITATION_RESPONDED = "invitation.responded"
    SESSION_READY = "session.ready"
    SESSION_STATE = "session.state"
    PARTICIPANT_CONNECTED = "participant.connected"
    PARTICIPANT_DISCONNECTED = "participant.disconnected"
    #: Taken out of the round. The payload names the *kind* of rule that was broken,
    #: never the words that broke it.
    PARTICIPANT_REMOVED = "participant.removed"
    MODERATOR_SPEAKING = "moderator.speaking"
    #: A synthesised sentence is ready to fetch — for browsers that are not on WebRTC.
    MODERATOR_AUDIO = "moderator.audio"
    #: The moderator is weighing what was just said. Shown, so the pause reads as
    #: attention rather than as the application having frozen.
    MODERATOR_THINKING = "moderator.thinking"
    MODERATOR_INTERRUPTED = "moderator.interrupted"
    FLOOR_GRANTED = "floor.granted"
    FLOOR_RELEASED = "floor.released"
    TRANSCRIPT_PARTIAL = "transcript.partial"
    TRANSCRIPT_FINAL = "transcript.final"
    SPEAKING_TIME_UPDATED = "speaking_time.updated"
    SESSION_SUMMARY_READY = "session.summary_ready"
    SESSION_ENDED = "session.ended"
    ERROR = "error"


ENVELOPE_VERSION = 1


@dataclass(slots=True)
class DomainEvent:
    """A single SSE frame before it is serialised."""

    topic: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))
    seq: int = 0  # assigned by the EventBus, monotonic per topic

    def to_frame(self) -> dict[str, Any]:
        return {
            "v": ENVELOPE_VERSION,
            "seq": self.seq,
            "ts": self.ts.isoformat(),
            "type": self.type,
            "topic": self.topic,
            "payload": self.payload,
        }


def classroom_topic(classroom_id: UUID) -> str:
    return f"classroom:{classroom_id}"


def session_topic(session_id: UUID) -> str:
    return f"session:{session_id}"


def user_topic(user_id: UUID) -> str:
    return f"user:{user_id}"
