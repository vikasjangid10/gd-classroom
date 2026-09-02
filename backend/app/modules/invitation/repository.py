from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update

from app.db.repository import BaseRepository
from app.domain.enums import InvitationStatus
from app.modules.invitation.models import Invitation


class InvitationRepository(BaseRepository[Invitation]):
    model = Invitation

    async def for_classroom(self, classroom_id: uuid.UUID) -> list[Invitation]:
        return await self.find_all(
            Invitation.classroom_id == classroom_id,
            order_by=Invitation.created_at,
        )

    async def pending_for_user(self, user_id: uuid.UUID) -> list[Invitation]:
        return await self.find_all(
            Invitation.user_id == user_id,
            Invitation.status == InvitationStatus.PENDING,
            order_by=Invitation.expires_at,
        )

    async def by_token_hash(self, token_hash: str) -> Invitation | None:
        return await self.find_one(Invitation.token_hash == token_hash)

    async def open_for(self, classroom_id: uuid.UUID, user_id: uuid.UUID) -> Invitation | None:
        return await self.find_one(
            Invitation.classroom_id == classroom_id,
            Invitation.user_id == user_id,
            Invitation.status == InvitationStatus.PENDING,
        )

    async def count_by_status(self, classroom_id: uuid.UUID) -> dict[InvitationStatus, int]:
        rows = await self.session.execute(
            select(Invitation.status, func.count())
            .where(Invitation.classroom_id == classroom_id)
            .group_by(Invitation.status)
        )
        # .tuples() is what makes the row type visible; without it the result is
        # Sequence[Row] and the dict() call cannot be checked.
        return dict(rows.tuples().all())

    async def accepted_user_ids(self, classroom_id: uuid.UUID) -> list[uuid.UUID]:
        rows = await self.session.execute(
            select(Invitation.user_id).where(
                Invitation.classroom_id == classroom_id,
                Invitation.status == InvitationStatus.ACCEPTED,
            )
        )
        return list(rows.scalars().all())

    async def involved_user_ids(self, classroom_id: uuid.UUID) -> list[uuid.UUID]:
        """Everyone already invited to this classroom, whatever they answered.

        The matcher uses this so a person who declined is not immediately re-invited.
        """
        rows = await self.session.execute(
            select(Invitation.user_id).where(Invitation.classroom_id == classroom_id).distinct()
        )
        return list(rows.scalars().all())

    async def next_attempt_no(self, classroom_id: uuid.UUID, user_id: uuid.UUID) -> int:
        current = (
            await self.session.execute(
                select(func.coalesce(func.max(Invitation.attempt_no), 0)).where(
                    Invitation.classroom_id == classroom_id,
                    Invitation.user_id == user_id,
                )
            )
        ).scalar_one()
        return int(current) + 1

    async def expire_overdue(self, now: datetime, limit: int = 500) -> list[Invitation]:
        overdue = await self.find_all(
            Invitation.status == InvitationStatus.PENDING,
            Invitation.expires_at < now,
            limit=limit,
        )
        if overdue:
            await self.session.execute(
                update(Invitation)
                .where(Invitation.id.in_([i.id for i in overdue]))
                .values(status=InvitationStatus.EXPIRED, responded_at=now)
            )
        return overdue

    async def revoke_pending(self, classroom_id: uuid.UUID, now: datetime) -> int:
        result = await self.session.execute(
            update(Invitation)
            .where(
                Invitation.classroom_id == classroom_id,
                Invitation.status == InvitationStatus.PENDING,
            )
            .values(status=InvitationStatus.REVOKED, responded_at=now)
        )
        # An UPDATE always produces a CursorResult; only that type exposes rowcount.
        return int(cast("CursorResult[Any]", result).rowcount or 0)
