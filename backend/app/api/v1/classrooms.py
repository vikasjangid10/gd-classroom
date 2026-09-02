from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import (
    Cfg,
    Classrooms,
    CurrentUser,
    Enrollment,
    Invitations,
    Sessions,
    SuperUser,
    Users,
)
from app.core.responses import ok, page
from app.domain.enums import ClassroomStatus, InvitationStatus
from app.modules.classroom.schemas import (
    CancelIn,
    ClassroomDetailOut,
    ClassroomOut,
    CreateClassroomIn,
    InviteMoreIn,
    RosterEntry,
    TopicOut,
)

router = APIRouter(tags=["classrooms"])


@router.get("/topics")
async def list_topics(user: CurrentUser, classrooms: Classrooms) -> dict:
    topics = await classrooms.list_topics()
    return ok([TopicOut.model_validate(t).model_dump(mode="json") for t in topics])


@router.post("/classrooms", status_code=status.HTTP_201_CREATED)
async def create_classroom(
    payload: CreateClassroomIn,
    user: SuperUser,
    enrollment: Enrollment,
    classrooms: Classrooms,
) -> dict:
    """Create the classroom and invite the four named people, in one transaction.

    The emails go out after that transaction commits, so a failure here cannot leave a
    stranger holding a link to a classroom that was rolled back.
    """
    classroom = await enrollment.create_classroom(
        creator=user,
        topic_id=payload.topic_id,
        title=payload.title,
        persist_transcript=payload.persist_transcript,
        config=payload.config,
        invitee_emails=[str(email) for email in payload.invitee_emails],
    )
    detail = await classrooms.get(classroom.id)
    return ok(ClassroomOut.model_validate(detail).model_dump(mode="json"))


@router.get("/classrooms")
async def list_classrooms(
    user: CurrentUser,
    classrooms: Classrooms,
    status_filter: Annotated[ClassroomStatus | None, Query(alias="status")] = None,
    cursor: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict:
    rows = await classrooms.list_for(user, status=status_filter, cursor=cursor, limit=limit + 1)
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1].created_at.isoformat() if has_more and rows else None
    return page(
        [ClassroomOut.model_validate(row).model_dump(mode="json") for row in rows],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/classrooms/{classroom_id}")
async def get_classroom(
    classroom_id: uuid.UUID,
    user: CurrentUser,
    classrooms: Classrooms,
    invitations: Invitations,
    sessions: Sessions,
    users: Users,
    cfg: Cfg,
) -> dict:
    classroom = await classrooms.get(classroom_id)
    await classrooms.assert_member(classroom, user)

    invites = await invitations.for_classroom(classroom_id)
    seats = {p.user_id: p.seat_no for p in await classrooms.roster(classroom_id)}

    people = {u.id: u for u in await users.by_ids([i.user_id for i in invites])}
    roster = [
        RosterEntry(
            user_id=invite.user_id,
            display_name=people[invite.user_id].display_name if invite.user_id in people else "—",
            email=invite.invited_email
            or (people[invite.user_id].email if invite.user_id in people else ""),
            seat_no=seats.get(invite.user_id),
            invitation_id=invite.id,
            invitation_status=invite.status,
            responded_at=invite.responded_at,
            email_sent_at=invite.email_sent_at,
            email_error=invite.email_error,
        )
        for invite in invites
    ]
    accepted = sum(1 for r in roster if r.invitation_status is InvitationStatus.ACCEPTED)
    pending = sum(1 for r in roster if r.invitation_status is InvitationStatus.PENDING)
    session = await sessions.by_classroom(classroom_id)

    detail = ClassroomDetailOut(
        **ClassroomOut.model_validate(classroom).model_dump(),
        accepted_count=accepted,
        pending_count=pending,
        roster=roster,
        session_id=session.id if session else None,
        min_to_start=cfg.min_participants_to_start,
        # The host may begin as soon as the floor is met — waiting for a seat nobody
        # took is their choice, not a rule.
        can_start=accepted >= cfg.min_participants_to_start
        and classroom.status in (ClassroomStatus.INVITING, ClassroomStatus.READY),
    )
    return ok(detail.model_dump(mode="json"))


@router.post("/classrooms/{classroom_id}/invitations", status_code=status.HTTP_202_ACCEPTED)
async def invite_more(
    classroom_id: uuid.UUID,
    payload: InviteMoreIn,
    user: SuperUser,
    classrooms: Classrooms,
    enrollment: Enrollment,
) -> dict:
    """Invite additional people by email — a replacement for someone who declined."""
    classroom = await classrooms.get(classroom_id)
    issued = await enrollment.invite_more(
        classroom, user, [str(email) for email in payload.emails]
    )
    return ok({"invitations_sent": issued})


@router.post(
    "/classrooms/{classroom_id}/invitations/{invitation_id}/resend",
    status_code=status.HTTP_202_ACCEPTED,
)
async def resend_invitation(
    classroom_id: uuid.UUID,
    invitation_id: uuid.UUID,
    user: SuperUser,
    classrooms: Classrooms,
    enrollment: Enrollment,
) -> dict:
    """Re-issue and re-send one invitation. The previous link stops working."""
    classroom = await classrooms.get(classroom_id)
    email = await enrollment.resend(classroom, user, invitation_id)
    return ok({"resent_to": email})


@router.post("/classrooms/{classroom_id}/start")
async def start_discussion(
    classroom_id: uuid.UUID,
    user: SuperUser,
    classrooms: Classrooms,
    enrollment: Enrollment,
) -> dict:
    classroom = await classrooms.get(classroom_id)
    session = await enrollment.start(classroom, user)
    return ok({"session_id": str(session.id), "status": session.status.value})


@router.post("/classrooms/{classroom_id}/cancel")
async def cancel_classroom(
    classroom_id: uuid.UUID,
    payload: CancelIn,
    user: SuperUser,
    classrooms: Classrooms,
    enrollment: Enrollment,
) -> dict:
    classroom = await classrooms.get(classroom_id)
    await enrollment.cancel(classroom, user, payload.reason)
    return ok({"classroom_id": str(classroom_id), "status": classroom.status.value})
