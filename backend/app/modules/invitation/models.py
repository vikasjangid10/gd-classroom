from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamped, UUIDPk
from app.domain.enums import InvitationStatus


class Invitation(Base, UUIDPk, Timestamped):
    __tablename__ = "invitations"

    classroom_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("classrooms.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # A declined invitation may be superseded by a fresh one for the same person.
    attempt_no: Mapped[int] = mapped_column(SmallInteger, default=1)
    status: Mapped[InvitationStatus] = mapped_column(
        Enum(InvitationStatus, native_enum=False, length=16),
        default=InvitationStatus.PENDING,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)

    # The address the invitation was actually sent to, snapshotted at issue time. The
    # user row could be renamed or reused later; what the host typed must not change.
    invited_email: Mapped[str] = mapped_column(String(254), default="")
    #: Delivery outcome. Null/null means "queued, not yet attempted" — the host UI shows
    #: all three states, because an invitation nobody received is not an invitation.
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("classroom_id", "user_id", "attempt_no", name="classroom_user_attempt"),
        # Only one *open* invitation per person per classroom; re-invites are allowed.
        Index(
            "uq_invitations_open",
            "classroom_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index(
            "ix_invitations_user_pending",
            "user_id",
            "status",
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index(
            "ix_invitations_expiry",
            "expires_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )
