"""Invitation endpoints.

Two audiences, two authentication models:

* ``/invitations`` and ``/invitations/{id}/…`` — someone already signed in, looking at
  their own invitations.
* ``/invitations/by-token/{token}`` — someone who has never used this application,
  clicking a link in an email. Possession of the token is the credential; accepting one
  signs them in. Every route in that group is unauthenticated by design.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response

from app.api.deps import (
    Auth,
    Cfg,
    Classrooms,
    CurrentUser,
    Enrollment,
    Invitations,
    Users,
    classrooms_repo,
    topics_repo,
)
from app.core.responses import ok
from app.domain.enums import InvitationStatus
from app.modules.classroom.repository import ClassroomRepository, TopicRepository
from app.modules.classroom.schemas import (
    AcceptByTokenIn,
    InvitationOut,
    RejectIn,
    TokenInvitationOut,
)
from app.modules.identity.schemas import UserOut

router = APIRouter(prefix="/invitations", tags=["invitations"])

REFRESH_COOKIE = "gd_refresh"


@router.get("")
async def my_invitations(
    user: CurrentUser,
    invitations: Invitations,
    classrooms_r: Annotated[ClassroomRepository, Depends(classrooms_repo)],
    topics_r: Annotated[TopicRepository, Depends(topics_repo)],
) -> dict:
    pending = await invitations.pending_for(user.id)
    rooms = {c.id: c for c in await classrooms_r.by_ids([i.classroom_id for i in pending])}
    topics = {
        t.id: t for t in await topics_r.by_ids([c.topic_id for c in rooms.values()])
    }

    out = []
    for invite in pending:
        classroom = rooms.get(invite.classroom_id)
        topic = topics.get(classroom.topic_id) if classroom else None
        out.append(
            InvitationOut(
                id=invite.id,
                classroom_id=invite.classroom_id,
                status=invite.status,
                expires_at=invite.expires_at,
                responded_at=invite.responded_at,
                classroom_title=classroom.title if classroom else None,
                topic_title=topic.title if topic else None,
            ).model_dump(mode="json")
        )
    return ok(out)


@router.post("/{invitation_id}/accept")
async def accept(
    invitation_id: uuid.UUID,
    user: CurrentUser,
    enrollment: Enrollment,
) -> dict:
    classroom, session = await enrollment.respond(
        invitation_id=invitation_id, user=user, accept=True
    )
    return ok(
        {
            "classroom_id": str(classroom.id),
            "classroom_status": classroom.status.value,
            "session_id": str(session.id) if session else None,
        }
    )


@router.post("/{invitation_id}/reject")
async def reject(
    invitation_id: uuid.UUID,
    payload: RejectIn,
    user: CurrentUser,
    enrollment: Enrollment,
) -> dict:
    classroom, _ = await enrollment.respond(
        invitation_id=invitation_id, user=user, accept=False, reason=payload.reason
    )
    return ok({"classroom_id": str(classroom.id), "classroom_status": classroom.status.value})


# ===================================================================== by token
@router.get("/by-token/{token}")
async def preview_by_token(
    token: str,
    invitations: Invitations,
    classrooms: Classrooms,
    users: Users,
) -> dict:
    """What the emailed link opens: enough to decide, and nothing more.

    An already-answered invitation is shown rather than refused, so someone reopening
    the link sees "you accepted this" instead of a dead end.
    """
    invitation = await invitations.load_by_token(token)
    classroom = await classrooms.get(invitation.classroom_id)
    topic = await classrooms.get_topic(classroom.topic_id)
    host = await users.session_user(classroom.created_by)
    invitee = await users.session_user(invitation.user_id)

    return ok(
        TokenInvitationOut(
            classroom_title=classroom.title,
            topic_title=topic.title,
            topic_description=topic.description,
            guiding_points=list(topic.guiding_points or []),
            host_name=host.display_name,
            invited_email=invitation.invited_email or invitee.email,
            invitee_name=invitee.display_name,
            expires_at=invitation.expires_at,
            status=invitation.status,
            seat_count=classroom.seat_count,
            accepted_count=await classrooms.seat_count(classroom.id),
        ).model_dump(mode="json")
    )


@router.post("/by-token/{token}/accept")
async def accept_by_token(
    token: str,
    payload: AcceptByTokenIn,
    enrollment: Enrollment,
    auth: Auth,
    users: Users,
    cfg: Cfg,
    response: Response,
    user_agent: Annotated[str | None, Header()] = None,
) -> dict:
    """Accept from the email and get signed in, in one round trip.

    The response carries a real access token and sets the refresh cookie, because the
    person is now a participant with a room to join and must not be asked for a password
    they were never given.
    """
    classroom, session, user_id = await enrollment.respond_by_token(token=token, accept=True)
    if payload.display_name:
        await users.rename(user_id, payload.display_name)

    access, refresh, user = await auth.issue_session_for(user_id, user_agent=user_agent)
    response.set_cookie(
        REFRESH_COOKIE,
        refresh,
        max_age=cfg.refresh_token_ttl_seconds,
        httponly=True,
        secure=cfg.is_production,
        samesite="lax",
        path="/api/v1/auth",
    )
    return ok(
        {
            "classroom_id": str(classroom.id),
            "classroom_status": classroom.status.value,
            "session_id": str(session.id) if session else None,
            "access_token": access,
            "token_type": "bearer",
            "expires_in": cfg.access_token_ttl_seconds,
            "user": UserOut.model_validate(user).model_dump(mode="json"),
        }
    )


@router.post("/by-token/{token}/reject")
async def reject_by_token(
    token: str,
    payload: RejectIn,
    enrollment: Enrollment,
) -> dict:
    """Decline from the email. No session is issued — they asked not to take part."""
    classroom, _, _ = await enrollment.respond_by_token(
        token=token, accept=False, reason=payload.reason
    )
    return ok(
        {
            "classroom_id": str(classroom.id),
            "status": InvitationStatus.REJECTED.value,
        }
    )
