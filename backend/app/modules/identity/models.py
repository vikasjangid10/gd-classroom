from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamped, UUIDPk
from app.domain.enums import Gender, Role


class User(Base, UUIDPk, Timestamped):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(80))
    role: Mapped[Role] = mapped_column(
        Enum(Role, native_enum=False, length=16), default=Role.PARTICIPANT, index=True
    )
    #: Self-declared at sign-up, optional, and read for exactly one purpose: choosing a
    #: call-sign for a discussion that fits the person using it.
    gender: Mapped[Gender] = mapped_column(
        Enum(Gender, native_enum=False, length=16), default=Gender.UNSPECIFIED
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Round-robin fairness input for the matcher: least-recently-invited goes first.
    last_invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    interests: Mapped[list[UserTopicInterest]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (Index("ix_users_role_active", "role", "is_active", "last_invited_at"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.email} {self.role}>"


class UserTopicInterest(Base):
    __tablename__ = "user_topic_interests"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True
    )
    proficiency: Mapped[int] = mapped_column(SmallInteger, default=3)

    user: Mapped[User] = relationship(back_populates="interests", lazy="raise")


class RefreshToken(Base, UUIDPk):
    """Rotating refresh tokens with family-wide revocation on reuse."""

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[str] = mapped_column(String(32), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(200))

    __table_args__ = (
        UniqueConstraint("token_hash", name="token_hash"),
        Index("ix_refresh_tokens_expiry", "expires_at"),
    )
