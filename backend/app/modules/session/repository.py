from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import selectinload

from app.db.repository import BaseRepository
from app.domain.enums import SessionStatus
from app.modules.session.models import SessionParticipant, SessionRecord, SessionSummary, Turn


class SessionRepository(BaseRepository[SessionRecord]):
    model = SessionRecord

    async def by_classroom(self, classroom_id: uuid.UUID) -> SessionRecord | None:
        return await self.find_one(SessionRecord.classroom_id == classroom_id)

    async def detail(self, session_id: uuid.UUID) -> SessionRecord | None:
        return await self.find_one(
            SessionRecord.id == session_id,
            options=[selectinload(SessionRecord.participants)],
        )

    async def with_summary(self, session_id: uuid.UUID) -> SessionRecord | None:
        return await self.find_one(
            SessionRecord.id == session_id, options=[selectinload(SessionRecord.summary)]
        )

    async def stale(self, older_than: datetime, limit: int = 100) -> list[SessionRecord]:
        """Sessions nothing in RAM owns any more.

        ``PENDING`` is included deliberately: a discussion nobody ever joined still holds
        its participants through the "one live session per person" index, so it has to be
        swept like a crashed one.
        """
        return await self.find_all(
            SessionRecord.status.in_(
                [
                    SessionStatus.PENDING,
                    SessionStatus.CONNECTING,
                    SessionStatus.ACTIVE,
                    SessionStatus.SUMMARIZING,
                ]
            ),
            SessionRecord.updated_at < older_than,
            limit=limit,
        )


class SessionParticipantRepository(BaseRepository[SessionParticipant]):
    model = SessionParticipant

    async def for_session(self, session_id: uuid.UUID) -> list[SessionParticipant]:
        return await self.find_all(
            SessionParticipant.session_id == session_id, order_by=SessionParticipant.seat_no
        )

    async def is_member(self, session_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        return await self.exists(
            SessionParticipant.session_id == session_id,
            SessionParticipant.user_id == user_id,
        )

    async def get_for(
        self, session_id: uuid.UUID, user_id: uuid.UUID
    ) -> SessionParticipant | None:
        return await self.find_one(
            SessionParticipant.session_id == session_id,
            SessionParticipant.user_id == user_id,
        )


class TurnRepository(BaseRepository[Turn]):
    model = Turn

    async def for_session(self, session_id: uuid.UUID) -> list[Turn]:
        return await self.find_all(Turn.session_id == session_id, order_by=Turn.turn_index)

    async def purge_before(self, cutoff: datetime) -> int:
        """Transcript retention. Turns older than the window are deleted outright."""
        return await self.delete_where(Turn.started_at < cutoff)


class SummaryRepository(BaseRepository[SessionSummary]):
    model = SessionSummary

    async def for_session(self, session_id: uuid.UUID) -> SessionSummary | None:
        return await self.find_one(SessionSummary.session_id == session_id)
