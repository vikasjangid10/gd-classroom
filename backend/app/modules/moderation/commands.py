"""Commands — the only way anything reaches the session runner.

Producers (WebRTC callbacks, STT callbacks, timers, REST handlers) build one of these
and put it on the queue. They never touch session state themselves, which is what
removes every lock from the hot path.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.domain.enums import EndReason, TurnKind


@dataclass(slots=True, frozen=True)
class Command:
    """Marker base class."""


@dataclass(slots=True, frozen=True)
class ParticipantConnected(Command):
    user_id: UUID


@dataclass(slots=True, frozen=True)
class ParticipantDisconnected(Command):
    user_id: UUID


@dataclass(slots=True, frozen=True)
class JoinWindowExpired(Command):
    pass


@dataclass(slots=True, frozen=True)
class SpeechStarted(Command):
    user_id: UUID


@dataclass(slots=True, frozen=True)
class TranscriptPartial(Command):
    user_id: UUID
    text: str


@dataclass(slots=True, frozen=True)
class UtteranceFinal(Command):
    user_id: UUID
    text: str
    #: True when the moderator asked for this — the turn cap expired, or the speaker
    #: pressed "I'm done". An *unforced* empty transcript only means the recogniser had
    #: nothing usable yet, which is not a reason to take somebody's turn away.
    forced: bool = False


@dataclass(slots=True, frozen=True)
class FloorReleased(Command):
    """The speaker pressed "I'm done"."""

    user_id: UUID


@dataclass(slots=True, frozen=True)
class SilenceTimeout(Command):
    """Nobody said anything after the question was asked."""

    turn_index: int


@dataclass(slots=True, frozen=True)
class TurnHardCap(Command):
    """The speaker has used the whole turn budget."""

    turn_index: int


@dataclass(slots=True, frozen=True)
class ModeratorFinishedSpeaking(Command):
    kind: TurnKind
    text: str
    duration_ms: int


@dataclass(slots=True, frozen=True)
class ModeratorSpeechFailed(Command):
    kind: TurnKind
    error: str


@dataclass(slots=True, frozen=True)
class EndSessionRequested(Command):
    reason: EndReason = EndReason.HOST_ENDED
