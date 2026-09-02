"""Classroom lifecycle.

This service owns the classroom aggregate and nothing else. It does not know that
invitations exist, and it does not know how a session starts — those belong to the
invitation module and the session module. The coordination between all three lives in
``app/application/enrollment.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings
from app.core.errors import AuthorizationError, ConflictError, NotFoundError, SeatsFullError
from app.core.logging import get_logger
from app.db.uow import UnitOfWork
from app.domain.enums import ClassroomStatus, Role
from app.domain.events import DomainEvent, EventType, classroom_topic, user_topic
from app.domain.state_machines import CLASSROOM_FSM
from app.modules.classroom.models import Classroom, ClassroomParticipant, Topic
from app.modules.classroom.repository import (
    ClassroomParticipantRepository,
    ClassroomRepository,
    TopicRepository,
)
from app.modules.classroom.schemas import ClassroomConfig
from app.modules.identity.schemas import SessionUser

log = get_logger(__name__)


class ClassroomService:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        classrooms: ClassroomRepository,
        participants: ClassroomParticipantRepository,
        topics: TopicRepository,
        settings: Settings,
    ) -> None:
        self._uow = uow
        self._classrooms = classrooms
        self._participants = participants
        self._topics = topics
        self._settings = settings

    # ---------------------------------------------------------------- shared reads
    # The classroom module's public surface for callers outside the request cycle.
    @staticmethod
    def for_session(db: AsyncSession) -> ClassroomService:
        return ClassroomService(
            uow=UnitOfWork(db),
            classrooms=ClassroomRepository(db),
            participants=ClassroomParticipantRepository(db),
            topics=TopicRepository(db),
            settings=settings,
        )

    async def brief(self, classroom_id: uuid.UUID) -> tuple[Topic, bool]:
        """The topic a session is about, and whether its transcript may be kept."""
        classroom = await self.get(classroom_id)
        return classroom.topic, bool(classroom.persist_transcript)

    async def set_status(self, classroom_id: uuid.UUID, status: ClassroomStatus) -> None:
        classroom = await self._classrooms.get(classroom_id)
        if classroom is not None:
            classroom.status = status

    # ---------------------------------------------------------------- read
    async def list_topics(self) -> list:
        return await self._topics.active()

    async def get_topic(self, topic_id: uuid.UUID) -> Topic:
        """Load a topic by identity.

        Callers must never reach through ``classroom.topic``: relationships are
        ``lazy="raise"``, and a classroom that was just created has nothing loaded.
        """
        topic = await self._topics.get(topic_id)
        if topic is None:
            raise NotFoundError("That topic does not exist.")
        return topic

    async def get(self, classroom_id: uuid.UUID) -> Classroom:
        classroom = await self._classrooms.detail(classroom_id)
        if classroom is None:
            raise NotFoundError("That classroom does not exist.")
        return classroom

    async def list_for(
        self,
        user: SessionUser,
        *,
        status: ClassroomStatus | None,
        cursor: datetime | None,
        limit: int,
    ) -> list[Classroom]:
        if user.role is Role.SUPER_USER:
            return await self._classrooms.for_creator(
                user.id, status=status, cursor=cursor, limit=limit
            )
        return await self._classrooms.for_member(user.id, cursor=cursor, limit=limit)

    async def assert_member(self, classroom: Classroom, user: SessionUser) -> None:
        if classroom.created_by == user.id:
            return
        if await self._participants.is_member(classroom.id, user.id):
            return
        raise AuthorizationError("You are not part of this classroom.")

    def assert_owner(self, classroom: Classroom, user: SessionUser) -> None:
        if classroom.created_by != user.id:
            raise AuthorizationError("Only the host can do that.")

    # ---------------------------------------------------------------- write
    async def create(
        self,
        *,
        creator: SessionUser,
        topic_id: uuid.UUID,
        title: str | None,
        persist_transcript: bool,
        config: ClassroomConfig,
    ) -> Classroom:
        if creator.role is not Role.SUPER_USER:
            raise AuthorizationError("Only a super user can create a classroom.")

        topic = await self._topics.get(topic_id)
        if topic is None or not topic.is_active:
            raise NotFoundError("That topic is not available.")

        classroom = Classroom(
            topic_id=topic.id,
            created_by=creator.id,
            title=(title or f"{topic.title} — group discussion").strip()[:160],
            status=ClassroomStatus.DRAFT,
            seat_count=self._settings.participants_per_classroom,
            persist_transcript=persist_transcript,
            config=config.model_dump(exclude_none=True),
            expires_at=datetime.now(UTC)
            + timedelta(seconds=self._settings.invitation_ttl_seconds * 2),
        )
        self._classrooms.add(classroom)
        await self._uow.flush()
        log.info("classroom.created", classroom=str(classroom.id), topic=topic.slug)
        return classroom

    def transition(self, classroom: Classroom, event: str) -> None:
        classroom.status = CLASSROOM_FSM.next(classroom.status, event)

    async def seat(self, classroom: Classroom, user_id: uuid.UUID) -> ClassroomParticipant:
        if await self._participants.is_member(classroom.id, user_id):
            existing = await self._participants.find_one(
                ClassroomParticipant.classroom_id == classroom.id,
                ClassroomParticipant.user_id == user_id,
            )
            if existing is not None:
                return existing

        seat_no = await self._participants.next_free_seat(classroom.id)
        if seat_no is None:
            raise SeatsFullError()

        participant = ClassroomParticipant(
            classroom_id=classroom.id,
            user_id=user_id,
            seat_no=seat_no,
            joined_at=datetime.now(UTC),
        )
        self._participants.add(participant)
        await self._uow.flush()
        return participant

    async def seat_count(self, classroom_id: uuid.UUID) -> int:
        return await self._participants.seat_count(classroom_id)

    async def roster(self, classroom_id: uuid.UUID) -> list[ClassroomParticipant]:
        return await self._participants.for_classroom(classroom_id)

    async def cancel(self, classroom: Classroom, user: SessionUser, reason: str | None) -> None:
        self.assert_owner(classroom, user)
        if CLASSROOM_FSM.is_terminal(classroom.status):
            raise ConflictError("This classroom is already closed.")
        self.transition(classroom, "cancelled")
        self.publish_update(classroom, reason=reason)
        log.info("classroom.cancelled", classroom=str(classroom.id))

    # ---------------------------------------------------------------- events
    def publish_update(self, classroom: Classroom, **extra: object) -> None:
        payload = {
            "classroom_id": str(classroom.id),
            "status": classroom.status.value,
            "title": classroom.title,
            **extra,
        }
        self._uow.collect(
            DomainEvent(
                topic=classroom_topic(classroom.id),
                type=EventType.CLASSROOM_UPDATED,
                payload=payload,
            ),
            DomainEvent(
                topic=user_topic(classroom.created_by),
                type=EventType.CLASSROOM_UPDATED,
                payload=payload,
            ),
        )

    def notify_users(self, user_ids: list[uuid.UUID], event_type: str, payload: dict) -> None:
        self._uow.collect(
            *[
                DomainEvent(topic=user_topic(user_id), type=event_type, payload=payload)
                for user_id in user_ids
            ]
        )
