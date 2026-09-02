from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
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
from app.domain.enums import EndReason, SessionStatus, SpeakerType, SummaryStatus, TurnKind


class SessionRecord(Base, UUIDPk, Timestamped):
    """The durable record of a discussion. Not the live session — see ``domain.session``."""

    __tablename__ = "sessions"

    classroom_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("classrooms.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, native_enum=False, length=16), default=SessionStatus.PENDING, index=True
    )
    end_reason: Mapped[EndReason | None] = mapped_column(
        Enum(EndReason, native_enum=False, length=32)
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Frozen copy of the classroom config so a later edit cannot rewrite history.
    config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Which process owns this session; used by crash recovery, not for routing.
    owner_node: Mapped[str | None] = mapped_column(String(64))

    participants: Mapped[list[SessionParticipant]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="raise"
    )
    turns: Mapped[list[Turn]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Turn.turn_index",
        lazy="raise",
    )
    summary: Mapped[SessionSummary | None] = relationship(
        back_populates="session", cascade="all, delete-orphan", uselist=False, lazy="raise"
    )

    __table_args__ = (
        Index(
            "ix_sessions_stale",
            "status",
            "started_at",
            postgresql_where=text(
                "status IN ('PENDING','CONNECTING','ACTIVE','SUMMARIZING')"
            ),
        ),
    )


class SessionParticipant(Base, UUIDPk):
    __tablename__ = "session_participants"

    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    seat_no: Mapped[int] = mapped_column(SmallInteger)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    spoken_ms: Mapped[int] = mapped_column(Integer, default=0)
    turns_taken: Mapped[int] = mapped_column(SmallInteger, default=0)

    session: Mapped[SessionRecord] = relationship(back_populates="participants", lazy="raise")

    __table_args__ = (UniqueConstraint("session_id", "user_id", name="session_user"),)


class Turn(Base, UUIDPk):
    __tablename__ = "turns"

    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    turn_index: Mapped[int] = mapped_column(SmallInteger)
    speaker_type: Mapped[SpeakerType] = mapped_column(
        Enum(SpeakerType, native_enum=False, length=16)
    )
    speaker_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    kind: Mapped[TurnKind] = mapped_column(Enum(TurnKind, native_enum=False, length=16))
    text: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    session: Mapped[SessionRecord] = relationship(back_populates="turns", lazy="raise")

    __table_args__ = (
        UniqueConstraint("session_id", "turn_index", name="session_turn_index"),
        CheckConstraint(
            "(speaker_type = 'MODERATOR' AND speaker_user_id IS NULL) OR "
            "(speaker_type = 'PARTICIPANT' AND speaker_user_id IS NOT NULL)",
            name="speaker_identity",
        ),
        Index("ix_turns_session_index", "session_id", "turn_index"),
    )


class SessionSummary(Base, UUIDPk, Timestamped):
    __tablename__ = "session_summaries"

    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[SummaryStatus] = mapped_column(
        Enum(SummaryStatus, native_enum=False, length=16), default=SummaryStatus.PENDING
    )
    headline: Mapped[str | None] = mapped_column(Text)
    key_points: Mapped[list] = mapped_column(JSONB, default=list)
    per_participant: Mapped[list] = mapped_column(JSONB, default=list)
    open_questions: Mapped[list] = mapped_column(JSONB, default=list)
    model: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)

    session: Mapped[SessionRecord] = relationship(back_populates="summary", lazy="raise")
