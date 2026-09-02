"""The live discussion aggregate.

This object is the *entire* memory of a discussion. It is created when the first
participant connects, mutated only by the session runner, and dereferenced when the
session ends. It has no ORM identity, no ``__table__`` and no way to persist itself —
which is how the "session memory only" rule is enforced structurally rather than by
convention.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from app.domain.enums import (
    ModeratorState,
    ParticipantConnection,
    SessionStatus,
    TurnKind,
)
from app.domain.ledger import SpeakingTimeLedger
from app.domain.ports import Assessment
from app.domain.prompts import RECENT_TURNS_KEPT, TopicBrief, TurnRecord
from app.domain.turn_policy import DiscussionBudget


@dataclass(slots=True)
class ParticipantState:
    user_id: UUID
    display_name: str
    seat_no: int
    connection: ParticipantConnection = ParticipantConnection.INVITED
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None

    @property
    def is_present(self) -> bool:
        return self.connection is ParticipantConnection.CONNECTED

    @property
    def is_removed(self) -> bool:
        return self.connection is ParticipantConnection.REMOVED


@dataclass(slots=True)
class BufferedTurn:
    """A turn held in RAM until the single flush at wrap-up."""

    turn_index: int
    kind: TurnKind
    is_moderator: bool
    speaker_user_id: UUID | None
    speaker_name: str
    text: str
    started_at: datetime
    duration_ms: int


@dataclass(slots=True)
class DiscussionSession:
    session_id: UUID
    classroom_id: UUID
    topic: TopicBrief
    budget: DiscussionBudget
    participants: dict[UUID, ParticipantState] = field(default_factory=dict)

    status: SessionStatus = SessionStatus.PENDING
    moderator_state: ModeratorState = ModeratorState.IDLE

    floor_holder: UUID | None = None
    floor_granted_at: datetime | None = None
    last_speaker: UUID | None = None
    turn_index: int = 0
    #: Turns in a row that produced no words. Resets the moment anybody speaks.
    consecutive_silent_turns: int = 0

    ledger: SpeakingTimeLedger = field(default_factory=SpeakingTimeLedger)
    rolling_summary: str = ""
    recent_turns: deque[TurnRecord] = field(
        default_factory=lambda: deque(maxlen=RECENT_TURNS_KEPT)
    )
    buffered_turns: list[BufferedTurn] = field(default_factory=list)
    #: The deep lane's verdict on each contribution, in the order they were made. Kept
    #: for the closing report only — nothing here is ever spoken, and a turn with no
    #: entry simply means the judge was off, slow, or unusable at that moment.
    assessments: list[tuple[UUID, Assessment]] = field(default_factory=list)

    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    ended_at: datetime | None = None

    # ---------------------------------------------------------------- roster
    def add_participant(self, user_id: UUID, display_name: str, seat_no: int) -> None:
        self.participants[user_id] = ParticipantState(user_id, display_name, seat_no)
        self.ledger.register(user_id, display_name)
        # `register` defaults a fresh tally to eligible — the right default for the
        # ledger's own tests, which register and select in the same breath, but wrong
        # here: this fires from the roster at session bootstrap, before anyone has
        # actually joined. Left at the ledger's default, someone who accepted an
        # invitation and then closed the tab without ever connecting is never marked
        # disconnected either — nothing ever ran to do it, because no peer connection
        # ever existed to drop — so they stay "eligible" forever and the moderator keeps
        # trying to hand them the floor. `mark_connected` is what earns eligibility now.
        self.ledger.set_connected(user_id, False)

    def mark_connected(self, user_id: UUID) -> None:
        if p := self.participants.get(user_id):
            # Removal is final for this round. Opening the room again must not undo it —
            # otherwise the sanction lasts exactly as long as it takes to press reload.
            if p.is_removed:
                return
            p.connection = ParticipantConnection.CONNECTED
            p.connected_at = datetime.now(UTC)
            self.ledger.set_connected(user_id, True)

    def mark_disconnected(self, user_id: UUID) -> None:
        if p := self.participants.get(user_id):
            if p.is_removed:
                return
            p.connection = ParticipantConnection.DISCONNECTED
            p.disconnected_at = datetime.now(UTC)
            self.ledger.set_connected(user_id, False)

    def mark_removed(self, user_id: UUID) -> None:
        """Take somebody out of the round for good."""
        if p := self.participants.get(user_id):
            p.connection = ParticipantConnection.REMOVED
            p.disconnected_at = datetime.now(UTC)
            self.ledger.set_connected(user_id, False)

    @property
    def connected_count(self) -> int:
        return sum(1 for p in self.participants.values() if p.is_present)

    @property
    def expected_count(self) -> int:
        return len(self.participants)

    def name_of(self, user_id: UUID | None) -> str:
        if user_id is None:
            return "Moderator"
        p = self.participants.get(user_id)
        return p.display_name if p else "Someone"

    # ---------------------------------------------------------------- timing
    @property
    def elapsed_seconds(self) -> int:
        if self.started_at is None:
            return 0
        return int((datetime.now(UTC) - self.started_at).total_seconds())

    def floor_held_ms(self) -> int:
        if self.floor_granted_at is None:
            return 0
        return int((datetime.now(UTC) - self.floor_granted_at).total_seconds() * 1000)

    # ---------------------------------------------------------------- turns
    def next_turn_index(self) -> int:
        self.turn_index += 1
        return self.turn_index

    def record_turn(
        self,
        *,
        kind: TurnKind,
        text: str,
        speaker_user_id: UUID | None,
        duration_ms: int = 0,
        started_at: datetime | None = None,
    ) -> BufferedTurn:
        turn = BufferedTurn(
            turn_index=self.next_turn_index(),
            kind=kind,
            is_moderator=speaker_user_id is None,
            speaker_user_id=speaker_user_id,
            speaker_name=self.name_of(speaker_user_id),
            text=text,
            started_at=started_at or datetime.now(UTC),
            duration_ms=duration_ms,
        )
        self.buffered_turns.append(turn)
        self.recent_turns.append(
            TurnRecord(
                index=turn.turn_index,
                speaker=turn.speaker_name,
                text=text,
                is_moderator=turn.is_moderator,
            )
        )
        return turn

    def transcript(self) -> list[TurnRecord]:
        return [
            TurnRecord(t.turn_index, t.speaker_name, t.text, t.is_moderator)
            for t in self.buffered_turns
        ]

    # ---------------------------------------------------------------- teardown
    def wipe(self) -> None:
        """Best-effort scrub before the object is dropped. See §24."""
        self.rolling_summary = ""
        self.recent_turns.clear()
        self.buffered_turns.clear()
        self.assessments.clear()
        self.participants.clear()
        self.ledger.tallies.clear()
        self.floor_holder = None
