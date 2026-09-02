from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.domain.enums import ClassroomStatus, InvitationStatus


class TopicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    title: str
    description: str
    guiding_points: list[str] = Field(default_factory=list)
    difficulty: int


class ClassroomConfig(BaseModel):
    """Per-classroom overrides of the discussion defaults."""

    target_seconds: int | None = Field(default=None, ge=120, le=5400)
    max_seconds: int | None = Field(default=None, ge=180, le=7200)
    turn_max_seconds: int | None = Field(default=None, ge=20, le=300)
    min_turns_per_participant: int | None = Field(default=None, ge=1, le=6)


class CreateClassroomIn(BaseModel):
    topic_id: uuid.UUID
    title: str | None = Field(default=None, max_length=160)
    persist_transcript: bool = True
    config: ClassroomConfig = Field(default_factory=ClassroomConfig)
    #: The four real people the host wants in the room. Bounds are generous here and the
    #: exact-four rule is enforced in the service, so the API can say "you gave three
    #: addresses" instead of pydantic saying "List should have at least 4 items".
    invitee_emails: list[EmailStr] = Field(default_factory=list, max_length=12)


class InviteMoreIn(BaseModel):
    """Fill seats left empty by a decline or a mistyped address."""

    emails: list[EmailStr] = Field(min_length=1, max_length=8)


class RosterEntry(BaseModel):
    user_id: uuid.UUID
    display_name: str
    email: str
    seat_no: int | None = None
    invitation_id: uuid.UUID | None = None
    invitation_status: InvitationStatus | None = None
    responded_at: datetime | None = None
    #: Delivery, as three distinct states the host can act on: queued (both null),
    #: delivered (sent_at set), or bounced (error set).
    email_sent_at: datetime | None = None
    email_error: str | None = None


class ClassroomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    status: ClassroomStatus
    seat_count: int
    persist_transcript: bool
    created_at: datetime
    expires_at: datetime | None
    topic: TopicOut


class ClassroomDetailOut(ClassroomOut):
    accepted_count: int = 0
    pending_count: int = 0
    roster: list[RosterEntry] = Field(default_factory=list)
    session_id: uuid.UUID | None = None
    #: How many acceptances a discussion needs before it can run at all.
    min_to_start: int = 2
    can_start: bool = False


class InvitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    classroom_id: uuid.UUID
    status: InvitationStatus
    expires_at: datetime
    responded_at: datetime | None
    classroom_title: str | None = None
    topic_title: str | None = None
    host_name: str | None = None


class RejectIn(BaseModel):
    reason: str | None = Field(default=None, max_length=280)


class TokenInvitationOut(BaseModel):
    """What the emailed link shows before the recipient has decided anything.

    No identifiers, no roster, no host email — only what someone deciding whether to
    accept actually needs. The token is a bearer credential that will sit in browser
    history, so the page behind it must not be worth stealing.
    """

    classroom_title: str
    topic_title: str
    topic_description: str
    guiding_points: list[str] = Field(default_factory=list)
    host_name: str
    invited_email: str
    invitee_name: str
    expires_at: datetime
    status: InvitationStatus
    seat_count: int
    accepted_count: int
    #: Set once the person has accepted and all four seats are full.
    session_id: uuid.UUID | None = None


class AcceptByTokenIn(BaseModel):
    #: An invitee may correct the name guessed from their address before joining.
    display_name: str | None = Field(default=None, max_length=80)


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=280)
