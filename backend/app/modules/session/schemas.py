from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import EndReason, SessionStatus, SpeakerType, SummaryStatus, TurnKind


class SessionParticipantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    seat_no: int
    spoken_ms: int
    turns_taken: int
    connected_at: datetime | None
    #: Filled in by the API layer. Without it the room renders four cards that all say
    #: "Participant" until somebody happens to speak.
    display_name: str = ""


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    classroom_id: uuid.UUID
    status: SessionStatus
    end_reason: EndReason | None
    started_at: datetime | None
    ended_at: datetime | None
    participants: list[SessionParticipantOut] = Field(default_factory=list)
    live: dict[str, Any] | None = None
    #: True when the caller convened this classroom — they may end the discussion.
    is_host: bool = False


class TicketsOut(BaseModel):
    sse_ticket: str
    rtc_ticket: str
    expires_in: int
    ice_servers: list[dict[str, Any]]


class RtcOfferIn(BaseModel):
    sdp: str = Field(min_length=1, max_length=200_000)
    type: str = Field(default="offer", pattern="^(offer|answer)$")
    ticket: str


class RtcAnswerOut(BaseModel):
    sdp: str
    type: str


class IceCandidateIn(BaseModel):
    ticket: str
    candidate: str | None = None
    sdp_mid: str | None = None
    sdp_m_line_index: int | None = None


class TextTurnIn(BaseModel):
    """Development affordance — inject a turn without a microphone."""

    text: str = Field(min_length=1, max_length=2000)


class TurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    turn_index: int
    speaker_type: SpeakerType
    speaker_user_id: uuid.UUID | None
    speaker_name: str | None = None
    kind: TurnKind
    text: str
    started_at: datetime
    duration_ms: int


class SummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: SummaryStatus
    headline: str | None
    key_points: list[Any] = Field(default_factory=list)
    per_participant: list[Any] = Field(default_factory=list)
    open_questions: list[Any] = Field(default_factory=list)
    model: str | None
    error: str | None


class EndSessionIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)
