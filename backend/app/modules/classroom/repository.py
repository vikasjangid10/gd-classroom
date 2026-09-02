from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.db.repository import BaseRepository
from app.domain.enums import ClassroomStatus
from app.modules.classroom.models import Classroom, ClassroomParticipant, Topic


class TopicRepository(BaseRepository[Topic]):
    model = Topic

    async def active(self) -> list[Topic]:
        return await self.find_all(Topic.is_active.is_(True), order_by=Topic.title)

    async def by_slug(self, slug: str) -> Topic | None:
        return await self.find_one(Topic.slug == slug)


class ClassroomRepository(BaseRepository[Classroom]):
    model = Classroom

    async def detail(self, classroom_id: uuid.UUID) -> Classroom | None:
        return await self.find_one(
            Classroom.id == classroom_id,
            options=[selectinload(Classroom.topic), selectinload(Classroom.participants)],
        )

    async def for_creator(
        self,
        creator_id: uuid.UUID,
        *,
        status: ClassroomStatus | None,
        cursor: datetime | None,
        limit: int,
    ) -> list[Classroom]:
        """Keyset pagination on ``created_at`` — stable under inserts, unlike OFFSET."""
        conditions = [Classroom.created_by == creator_id]
        if status is not None:
            conditions.append(Classroom.status == status)
        if cursor is not None:
            conditions.append(Classroom.created_at < cursor)
        return await self.find_all(
            *conditions,
            order_by=Classroom.created_at.desc(),
            limit=limit,
            options=[selectinload(Classroom.topic)],
        )

    async def for_member(
        self,
        user_id: uuid.UUID,
        *,
        cursor: datetime | None,
        limit: int,
    ) -> list[Classroom]:
        stmt = (
            select(Classroom)
            .join(ClassroomParticipant, ClassroomParticipant.classroom_id == Classroom.id)
            .where(ClassroomParticipant.user_id == user_id)
            .options(selectinload(Classroom.topic))
            .order_by(Classroom.created_at.desc())
            .limit(limit)
        )
        if cursor is not None:
            stmt = stmt.where(Classroom.created_at < cursor)
        return list((await self.session.execute(stmt)).scalars().all())

    async def expiring(self, now: datetime, limit: int = 100) -> list[Classroom]:
        return await self.find_all(
            Classroom.status.in_([ClassroomStatus.INVITING, ClassroomStatus.READY]),
            Classroom.expires_at.is_not(None),
            Classroom.expires_at < now,
            limit=limit,
        )


class ClassroomParticipantRepository(BaseRepository[ClassroomParticipant]):
    model = ClassroomParticipant

    async def for_classroom(self, classroom_id: uuid.UUID) -> list[ClassroomParticipant]:
        return await self.find_all(
            ClassroomParticipant.classroom_id == classroom_id,
            order_by=ClassroomParticipant.seat_no,
        )

    async def is_member(self, classroom_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        return await self.exists(
            ClassroomParticipant.classroom_id == classroom_id,
            ClassroomParticipant.user_id == user_id,
        )

    async def next_free_seat(self, classroom_id: uuid.UUID) -> int | None:
        taken = set(
            (
                await self.session.execute(
                    select(ClassroomParticipant.seat_no).where(
                        ClassroomParticipant.classroom_id == classroom_id
                    )
                )
            )
            .scalars()
            .all()
        )
        return next((seat for seat in range(1, 5) if seat not in taken), None)

    async def seat_count(self, classroom_id: uuid.UUID) -> int:
        return int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(ClassroomParticipant)
                    .where(ClassroomParticipant.classroom_id == classroom_id)
                )
            ).scalar_one()
        )
