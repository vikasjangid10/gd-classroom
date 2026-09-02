from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamped, UUIDPk
from app.domain.enums import ClassroomStatus


class Topic(Base, UUIDPk, Timestamped):
    __tablename__ = "topics"

    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    guiding_points: Mapped[list[str]] = mapped_column(JSONB, default=list)
    difficulty: Mapped[int] = mapped_column(SmallInteger, default=2)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class Classroom(Base, UUIDPk, Timestamped):
    __tablename__ = "classrooms"

    topic_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("topics.id", ondelete="RESTRICT")
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    title: Mapped[str] = mapped_column(String(160))
    status: Mapped[ClassroomStatus] = mapped_column(
        Enum(ClassroomStatus, native_enum=False, length=16),
        default=ClassroomStatus.DRAFT,
        index=True,
    )
    seat_count: Mapped[int] = mapped_column(SmallInteger, default=4)
    persist_transcript: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    topic: Mapped[Topic] = relationship(lazy="raise")
    participants: Mapped[list[ClassroomParticipant]] = relationship(
        back_populates="classroom", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        CheckConstraint("seat_count = 4", name="seat_count_is_four"),
        Index("ix_classrooms_creator_status", "created_by", "status", text("created_at DESC")),
    )


class ClassroomParticipant(Base, UUIDPk):
    __tablename__ = "classroom_participants"

    classroom_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("classrooms.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    seat_no: Mapped[int] = mapped_column(SmallInteger)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    classroom: Mapped[Classroom] = relationship(back_populates="participants", lazy="raise")

    __table_args__ = (
        UniqueConstraint("classroom_id", "user_id", name="classroom_user"),
        UniqueConstraint("classroom_id", "seat_no", name="classroom_seat"),
        CheckConstraint("seat_no BETWEEN 1 AND 4", name="seat_in_range"),
    )
