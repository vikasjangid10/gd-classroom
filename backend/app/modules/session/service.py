"""Session records, membership checks and short-lived stream tickets."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.errors import AuthorizationError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.security import create_ticket
from app.db.uow import UnitOfWork
from app.domain.enums import SessionStatus
from app.domain.events import DomainEvent, EventType, session_topic, user_topic
from app.modules.classroom.models import Classroom, ClassroomParticipant
from app.modules.session.models import SessionParticipant, SessionRecord, SessionSummary
from app.modules.session.repository import (
    SessionParticipantRepository,
    SessionRepository,
    SummaryRepository,
    TurnRepository,
)

log = get_logger(__name__)


class SessionService:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        sessions: SessionRepository,
        participants: SessionParticipantRepository,
        turns: TurnRepository,
        summaries: SummaryRepository,
        settings: Settings,
    ) -> None:
        self._uow = uow
        self._sessions = sessions
        self._participants = participants
        self._turns = turns
        self._summaries = summaries
        self._settings = settings

    # ---------------------------------------------------------------- provision
    async def provision(
        self, classroom: Classroom, roster: list[ClassroomParticipant]
    ) -> SessionRecord:
        """Create the durable session record once the classroom has its four people."""
        if existing := await self._sessions.by_classroom(classroom.id):
            if existing.status in (SessionStatus.ENDED, SessionStatus.ABORTED):
                raise ConflictError("This classroom has already held its discussion.")
            return existing

        # A person can only be in one discussion at a time. The database enforces it, but
        # checking here turns a 500 into a message the host can act on.
        busy = await self._participants.find_all(
            SessionParticipant.user_id.in_([seat.user_id for seat in roster]),
            SessionParticipant.left_at.is_(None),
        )
        if busy:
            raise ConflictError(
                "One of the participants is already in another live discussion.",
                details={"user_ids": [str(row.user_id) for row in busy]},
            )

        record = SessionRecord(
            classroom_id=classroom.id,
            status=SessionStatus.PENDING,
            config_snapshot=dict(classroom.config or {}),
        )
        self._sessions.add(record)
        await self._uow.flush()

        self._participants.add_all(
            [
                SessionParticipant(
                    session_id=record.id, user_id=seat.user_id, seat_no=seat.seat_no
                )
                for seat in roster
            ]
        )
        await self._uow.flush()

        deadline = self._settings.session_join_window_seconds
        payload = {
            "session_id": str(record.id),
            "classroom_id": str(classroom.id),
            "classroom_title": classroom.title,
            "join_window_seconds": deadline,
            # However many actually accepted — a discussion no longer needs all four,
            # so the prompt cannot claim it does.
            "participants": len(roster),
        }
        self._uow.collect(
            *[
                DomainEvent(
                    topic=user_topic(seat.user_id),
                    type=EventType.SESSION_READY,
                    payload=payload,
                )
                for seat in roster
            ],
            DomainEvent(
                topic=user_topic(classroom.created_by),
                type=EventType.SESSION_READY,
                payload=payload,
            ),
        )
        log.info("session.provisioned", session=str(record.id), classroom=str(classroom.id))
        return record

    # ---------------------------------------------------------------- read
    async def get(self, session_id: uuid.UUID) -> SessionRecord:
        record = await self._sessions.detail(session_id)
        if record is None:
            raise NotFoundError("That session does not exist.")
        return record

    async def by_classroom(self, classroom_id: uuid.UUID) -> SessionRecord | None:
        return await self._sessions.by_classroom(classroom_id)

    async def assert_member(self, session_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Taking part: speaking, releasing the floor, joining the audio room."""
        if not await self._participants.is_member(session_id, user_id):
            raise AuthorizationError("You are not in this discussion.")

    async def is_member(self, session_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        return await self._participants.is_member(session_id, user_id)

    async def transcript(self, session_id: uuid.UUID) -> list:
        return await self._turns.for_session(session_id)

    async def seats(self, session_id: uuid.UUID) -> dict[uuid.UUID, int]:
        """``{user_id: seat_no}`` — what a call-sign is derived from."""
        return {p.user_id: p.seat_no for p in await self._participants.for_session(session_id)}

    async def summary(self, session_id: uuid.UUID) -> SessionSummary:
        summary = await self._summaries.for_session(session_id)
        if summary is None:
            raise NotFoundError("No summary has been generated for this discussion.")
        return summary

    # ---------------------------------------------------------------- tickets
    def issue_tickets(self, session_id: uuid.UUID, user_id: uuid.UUID) -> dict:
        """Short-lived, single-scope credentials for the two header-less transports."""
        return {
            "sse_ticket": create_ticket(user_id, session_id, "sse"),
            "rtc_ticket": create_ticket(user_id, session_id, "rtc"),
            "expires_in": self._settings.ticket_ttl_seconds,
            "ice_servers": self._settings.ice_servers(),
        }

    # ---------------------------------------------------------------- write
    async def mark_connected(self, session_id: uuid.UUID, user_id: uuid.UUID) -> None:
        participant = await self._participants.get_for(session_id, user_id)
        if participant is not None and participant.connected_at is None:
            participant.connected_at = datetime.now(UTC)

    async def abort(self, session_id: uuid.UUID, reason: str) -> None:
        record = await self._sessions.get(session_id)
        if record is None or record.status in (SessionStatus.ENDED, SessionStatus.ABORTED):
            return
        now = datetime.now(UTC)
        record.status = SessionStatus.ABORTED
        record.ended_at = now
        # Release the participants, or the "one live session per person" index keeps
        # them out of every future classroom.
        for participant in await self._participants.for_session(session_id):
            if participant.left_at is None:
                participant.left_at = now
        self._uow.collect(
            DomainEvent(
                topic=session_topic(session_id),
                type=EventType.SESSION_ENDED,
                payload={"reason": reason, "aborted": True},
            )
        )
