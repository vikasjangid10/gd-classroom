from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status
from sse_starlette.sse import EventSourceResponse

from app.api.deps import (
    Bus,
    Cfg,
    Classrooms,
    CurrentUser,
    LastEventId,
    Orchestrator,
    Sessions,
    SseTicket,
    Users,
    rtc_principal,
)
from app.core.errors import AuthorizationError, ConflictError
from app.core.responses import ok
from app.core.security import create_ticket, read_ticket
from app.domain.aliases import aliases_for
from app.domain.enums import EndReason
from app.domain.events import session_topic, user_topic
from app.modules.notification.sse import event_stream, parse_last_event_id
from app.modules.session.schemas import (
    EndSessionIn,
    IceCandidateIn,
    RtcAnswerOut,
    RtcOfferIn,
    SessionOut,
    SummaryOut,
    TextTurnIn,
    TicketsOut,
    TurnOut,
)

router = APIRouter(tags=["sessions"])


async def _viewer(
    session_id: uuid.UUID,
    user: CurrentUser,
    sessions: Sessions,
    classrooms: Classrooms,
) -> bool:
    """Who may watch a discussion: its four participants, and the host who convened it.

    Returns ``True`` for the host. Composed here in the API layer rather than inside
    either service, because answering it needs both modules and neither owns the other.
    """
    record = await sessions.get(session_id)
    classroom = await classrooms.get(record.classroom_id)
    if classroom.created_by == user.id:
        return True
    await sessions.assert_member(session_id, user.id)
    return False


async def _call_signs(
    session_id: uuid.UUID, sessions: Sessions, users: Users
) -> dict[uuid.UUID, str]:
    """The names this discussion goes by — the same ones the live session assigned.

    Composed here rather than inside either service because it needs the roster from one
    and self-declared gender from the other, and neither owns the other. Note what is
    *not* fetched: account names.
    """
    seats = await sessions.seats(session_id)
    genders = await users.genders(list(seats))
    return aliases_for(session_id, {uid: (seat, genders.get(uid)) for uid, seat in seats.items()})


# ===================================================================== read
@router.get("/sessions/{session_id}")
async def get_session(
    session_id: uuid.UUID,
    user: CurrentUser,
    sessions: Sessions,
    classrooms: Classrooms,
    orchestrator: Orchestrator,
    users: Users,
) -> dict:
    is_host = await _viewer(session_id, user, sessions, classrooms)
    record = await sessions.get(session_id)
    payload = SessionOut.model_validate(record).model_dump(mode="json")

    # Call-signs rather than account names — inside a discussion, nobody has one. The
    # host does not get an exception: they picked the invitees on the classroom page and
    # can see who they invited there, but who said what in the round is anonymous.
    names = await _call_signs(session_id, sessions, users)
    for row in payload["participants"]:
        row["display_name"] = names.get(uuid.UUID(row["user_id"]), "A participant")

    payload["live"] = orchestrator.snapshot(session_id)
    payload["is_host"] = is_host
    return ok(payload)


@router.get("/sessions/{session_id}/transcript")
async def get_transcript(
    session_id: uuid.UUID,
    user: CurrentUser,
    sessions: Sessions,
    classrooms: Classrooms,
    users: Users,
) -> dict:
    await _viewer(session_id, user, sessions, classrooms)
    turns = await sessions.transcript(session_id)

    # The same call-signs the room used, so a transcript read afterwards says exactly
    # what was on screen at the time — and still does not name anybody.
    names = await _call_signs(session_id, sessions, users)

    rows = []
    for turn in turns:
        row = TurnOut.model_validate(turn)
        row.speaker_name = (
            "Moderator"
            if turn.speaker_user_id is None
            else names.get(turn.speaker_user_id, "A participant")
        )
        rows.append(row.model_dump(mode="json"))
    return ok(rows)


@router.get("/sessions/{session_id}/summary")
async def get_summary(
    session_id: uuid.UUID, user: CurrentUser, sessions: Sessions, classrooms: Classrooms
) -> dict:
    await _viewer(session_id, user, sessions, classrooms)
    summary = await sessions.summary(session_id)
    return ok(SummaryOut.model_validate(summary).model_dump(mode="json"))


# ===================================================================== tickets
@router.post("/sessions/{session_id}/tickets", status_code=status.HTTP_201_CREATED)
async def issue_tickets(
    session_id: uuid.UUID, user: CurrentUser, sessions: Sessions, classrooms: Classrooms
) -> dict:
    """Exchange a bearer token for the short-lived credentials SSE and WebRTC need."""
    await _viewer(session_id, user, sessions, classrooms)
    return ok(TicketsOut(**sessions.issue_tickets(session_id, user.id)).model_dump(mode="json"))


@router.get(
    "/sessions/{session_id}/speech/{clip_id}",
    response_class=Response,
    responses={200: {"content": {"audio/wav": {}}}},
)
async def moderator_speech(
    session_id: uuid.UUID,
    clip_id: str,
    user: CurrentUser,
    sessions: Sessions,
    classrooms: Classrooms,
    orchestrator: Orchestrator,
) -> Response:
    """One synthesised moderator sentence, as a WAV.

    This is how someone who joined without a microphone still *hears* the moderator.
    Ordinary bearer auth rather than a stream ticket, because unlike ``EventSource`` a
    ``fetch`` can set headers — and unlike the media plane this is a plain download.
    """
    await _viewer(session_id, user, sessions, classrooms)
    clip = orchestrator.speech_clip(session_id, clip_id)
    return Response(
        content=clip.to_wav(),
        media_type="audio/wav",
        headers={
            # Clips are immutable and short-lived; caching one avoids a re-fetch when a
            # browser retries playback, and they never outlive the session anyway.
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": "inline",
        },
    )


# ===================================================================== SSE
@router.get("/sessions/{session_id}/events")
async def session_events(
    session_id: uuid.UUID,
    request: Request,
    principal: SseTicket,
    bus: Bus,
    last: LastEventId,
) -> EventSourceResponse:
    """One stream per open room. Resumable via ``Last-Event-ID``."""
    topics = [session_topic(session_id), user_topic(principal.user_id)]
    return EventSourceResponse(
        event_stream(
            bus,
            topics,
            last_event_id=parse_last_event_id(last),
            label=f"session:{session_id}",
        ),
        ping=15,
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/me/stream-ticket", status_code=status.HTTP_201_CREATED)
async def issue_user_ticket(user: CurrentUser, cfg: Cfg) -> dict:
    """A ticket for the lobby stream. ``EventSource`` cannot send an Authorization header."""
    return ok(
        {
            "ticket": create_ticket(user.id, user.id, "user"),
            "expires_in": cfg.ticket_ttl_seconds,
        }
    )


@router.get("/events")
async def user_events(
    request: Request,
    bus: Bus,
    last: LastEventId,
    ticket: Annotated[str, Query(description="Ticket from POST /me/stream-ticket")],
) -> EventSourceResponse:
    """Lobby stream: invitations, classroom status, "your discussion is ready".

    A first connection starts from *now*. Everything already true — the invitations
    waiting, the classrooms in flight — is loaded over REST when the page mounts, so
    replaying the ring here would only re-announce yesterday's news as though it had
    just happened. A genuine reconnect still replays, via ``Last-Event-ID``.
    """
    user_id, subject = read_ticket(ticket, scope="user")
    if user_id != subject:
        raise AuthorizationError("That ticket is not a lobby ticket.")
    return EventSourceResponse(
        event_stream(
            bus,
            [user_topic(user_id)],
            last_event_id=parse_last_event_id(last),
            replay_on_open=False,
            label="user",
        ),
        ping=15,
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


# ===================================================================== WebRTC
@router.post("/sessions/{session_id}/rtc/offer", status_code=status.HTTP_201_CREATED)
async def rtc_offer(
    session_id: uuid.UUID,
    payload: RtcOfferIn,
    orchestrator: Orchestrator,
    cfg: Cfg,
) -> dict:
    """Non-trickle signalling: the browser sends a complete offer, we answer once."""
    principal = await rtc_principal(session_id, payload.ticket)
    answer = await orchestrator.negotiate(session_id, principal.user_id, payload.sdp, payload.type)
    return ok(
        {
            **RtcAnswerOut(**answer).model_dump(mode="json"),
            "ice_servers": cfg.ice_servers(),
        }
    )


@router.post("/sessions/{session_id}/rtc/ice", status_code=status.HTTP_204_NO_CONTENT)
async def rtc_ice(session_id: uuid.UUID, payload: IceCandidateIn) -> None:
    """Accepted for client compatibility; this deployment negotiates without trickle."""
    await rtc_principal(session_id, payload.ticket)
    return None


@router.delete("/sessions/{session_id}/rtc", status_code=status.HTTP_204_NO_CONTENT)
async def rtc_leave(
    session_id: uuid.UUID, user: CurrentUser, sessions: Sessions, orchestrator: Orchestrator
) -> None:
    await sessions.assert_member(session_id, user.id)
    await orchestrator.leave(session_id, user.id)
    return None


# ===================================================================== control
@router.post("/sessions/{session_id}/floor/release", status_code=status.HTTP_202_ACCEPTED)
async def release_floor(
    session_id: uuid.UUID, user: CurrentUser, sessions: Sessions, orchestrator: Orchestrator
) -> dict:
    await sessions.assert_member(session_id, user.id)
    orchestrator.release_floor(session_id, user.id)
    return ok({"released": True})


@router.post("/sessions/{session_id}/join-text", status_code=status.HTTP_202_ACCEPTED)
async def join_without_audio(
    session_id: uuid.UUID,
    user: CurrentUser,
    sessions: Sessions,
    orchestrator: Orchestrator,
) -> dict:
    """Take part with a keyboard instead of a microphone."""
    await sessions.assert_member(session_id, user.id)
    await sessions.mark_connected(session_id, user.id)
    await orchestrator.join_without_audio(session_id, user.id)
    return ok({"joined": True, "mode": "text"})


@router.post("/sessions/{session_id}/turn-text", status_code=status.HTTP_202_ACCEPTED)
async def submit_text_turn(
    session_id: uuid.UUID,
    payload: TextTurnIn,
    user: CurrentUser,
    sessions: Sessions,
    orchestrator: Orchestrator,
    cfg: Cfg,
) -> dict:
    """Speak without a microphone. Development affordance, gated by ``ALLOW_TEXT_INPUT``."""
    if not cfg.allow_text_input:
        raise ConflictError("Text input is disabled on this deployment.")
    await sessions.assert_member(session_id, user.id)
    await orchestrator.submit_text_turn(session_id, user.id, payload.text)
    return ok({"accepted": True})


@router.post("/sessions/{session_id}/end", status_code=status.HTTP_202_ACCEPTED)
async def end_session(
    session_id: uuid.UUID,
    payload: EndSessionIn,
    user: CurrentUser,
    sessions: Sessions,
    classrooms: Classrooms,
    orchestrator: Orchestrator,
) -> dict:
    record = await sessions.get(session_id)
    classroom = await classrooms.get(record.classroom_id)
    if classroom.created_by != user.id:
        raise AuthorizationError("Only the host can end the discussion.")
    await orchestrator.end(session_id, EndReason.HOST_ENDED)
    return ok({"ending": True})
