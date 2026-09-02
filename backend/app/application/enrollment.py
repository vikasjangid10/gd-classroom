"""The use cases that span classroom, identity, invitation and session.

Keeping this coordination in one place is what allows the modules underneath it to stay
unaware of each other. It is the only service that composes several modules, and it owns
the rules that matter most in the product:

* the host names four real email addresses, and those four people — nobody else — are
  invited;
* a classroom becomes ``READY`` **only** when all four have accepted;
* every invitation carries a link to an inbox, and every link is a working credential
  for the person who received it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.errors import (
    ClassroomNotReadyError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.db.uow import UnitOfWork
from app.domain.enums import ClassroomStatus, InvitationStatus
from app.domain.events import EventType
from app.domain.ports import EmailMessage
from app.domain.state_machines import CE, CLASSROOM_FSM
from app.modules.classroom.models import Classroom, Topic
from app.modules.classroom.schemas import ClassroomConfig
from app.modules.classroom.service import ClassroomService
from app.modules.identity.schemas import SessionUser
from app.modules.identity.service import UserService
from app.modules.invitation.models import Invitation
from app.modules.invitation.service import MAIL_REF, InvitationService, IssuedInvitation
from app.modules.notification.mail_templates import invitation_email, session_ready_email
from app.modules.session.models import SessionRecord
from app.modules.session.service import SessionService

log = get_logger(__name__)


class EnrollmentService:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        classrooms: ClassroomService,
        invitations: InvitationService,
        users: UserService,
        sessions: SessionService,
        settings: Settings,
    ) -> None:
        self._uow = uow
        self._classrooms = classrooms
        self._invitations = invitations
        self._users = users
        self._sessions = sessions
        self._settings = settings

    # ================================================================ create
    async def create_classroom(
        self,
        *,
        creator: SessionUser,
        topic_id: uuid.UUID,
        title: str | None,
        persist_transcript: bool,
        config: ClassroomConfig,
        invitee_emails: list[str],
    ) -> Classroom:
        emails = self._clean_emails(invitee_emails, host=creator)
        seats = self._settings.participants_per_classroom
        floor = self._settings.min_participants_to_start
        # A range, not a quota. The seat count is a ceiling on how many may be invited;
        # the floor is what a discussion actually needs. Demanding exactly four here
        # while running happily on two was the same rigidity in a different place.
        if not floor <= len(emails) <= seats:
            raise ValidationError(
                f"Invite between {floor} and {seats} people; "
                f"{len(emails)} distinct address(es) were given.",
                details={"min": floor, "max": seats, "received": len(emails)},
            )

        classroom = await self._classrooms.create(
            creator=creator,
            topic_id=topic_id,
            title=title,
            persist_transcript=persist_transcript,
            config=config,
        )
        await self._invite(classroom, emails=emails, host=creator)
        self._classrooms.transition(classroom, CE.INVITATIONS_SENT)
        self._classrooms.publish_update(classroom)
        return classroom

    async def invite_more(
        self, classroom: Classroom, user: SessionUser, emails: list[str]
    ) -> int:
        """Fill seats emptied by a decline, an expiry, or an address typed wrong."""
        self._classrooms.assert_owner(classroom, user)
        if classroom.status not in (ClassroomStatus.INVITING, ClassroomStatus.READY):
            raise ConflictError("This classroom is no longer accepting participants.")

        cleaned = self._clean_emails(emails, host=user)
        free = classroom.seat_count - await self._held_seats(classroom.id)
        if free <= 0:
            raise ConflictError("Every seat in this classroom is already taken or pending.")
        if len(cleaned) > free:
            raise ValidationError(
                f"There {'is' if free == 1 else 'are'} only {free} free "
                f"seat{'' if free == 1 else 's'} left.",
                details={"free_seats": free, "received": len(cleaned)},
            )

        issued = await self._invite(classroom, emails=cleaned, host=user)
        if issued:
            self._classrooms.publish_update(classroom)
        return len(issued)

    async def resend(
        self, classroom: Classroom, user: SessionUser, invitation_id: uuid.UUID
    ) -> str:
        """Issue a brand new link to the same person and email it again.

        The original token cannot be resent — only its digest was kept — so a resend is
        genuinely a re-issue. That also means a forwarded copy of the first email stops
        working, which is the behaviour you want from a seat that is one person's.
        """
        self._classrooms.assert_owner(classroom, user)
        invitation = await self._invitations.get(invitation_id)
        if invitation.classroom_id != classroom.id:
            raise NotFoundError("That invitation belongs to another classroom.")
        if invitation.status not in (InvitationStatus.PENDING, InvitationStatus.EXPIRED):
            raise ConflictError("That invitation has already been answered.")

        email = invitation.invited_email
        await self._invitations.revoke(invitation)
        issued = await self._invite(classroom, emails=[email], host=user)
        if not issued:
            raise ConflictError("That invitation could not be re-sent.")
        self._classrooms.publish_update(classroom)
        return email

    # ---------------------------------------------------------------- shared
    async def _invite(
        self, classroom: Classroom, *, emails: list[str], host: SessionUser
    ) -> list[IssuedInvitation]:
        people = await self._users.ensure_invitees(
            emails, create_missing=self._settings.mail_enabled
        )
        fresh = []
        for person in people:
            if await self._reinvitable(classroom, person.id):
                fresh.append(person)
        if not fresh:
            return []

        topic = await self._classrooms.get_topic(classroom.topic_id)
        issued = await self._invitations.issue(
            classroom_id=classroom.id,
            classroom_title=classroom.title,
            topic_title=topic.title,
            user_ids=[person.id for person in fresh],
        )
        # The invitation itself already reached everyone: ``issue`` collected an
        # ``invitation.sent`` event per invitee, which their open tab receives over SSE.
        # Email is an optional second copy for people who are not currently looking.
        if self._settings.mail_enabled:
            self._uow.collect_mail(*[self._compose_invite(item, topic, host) for item in issued])
        return issued

    async def _reinvitable(self, classroom: Classroom, user_id: uuid.UUID) -> bool:
        """True when this person has no open or accepted invitation left to honour."""
        if await self._invitations.open_for_user(classroom.id, user_id):
            return False
        return user_id not in set(await self._invitations.accepted_user_ids(classroom.id))

    def _compose_invite(
        self, item: IssuedInvitation, topic: Topic, host: SessionUser
    ) -> EmailMessage:
        return invitation_email(
            to=item.email,
            invitee_name=item.display_name,
            host_name=host.display_name,
            topic_title=topic.title,
            topic_description=topic.description,
            guiding_points=list(topic.guiding_points or []),
            join_url=self._settings.invite_url(item.token),
            expires_at=item.invitation.expires_at,
            reference=f"{MAIL_REF}:{item.invitation.id}",
        )

    def _clean_emails(self, emails: list[str], *, host: SessionUser) -> list[str]:
        seen: list[str] = []
        for raw in emails:
            email = raw.lower().strip()
            if not email:
                continue
            if email == host.email.lower():
                raise ValidationError(
                    "You are hosting this discussion, so you cannot also be a participant.",
                    details={"email": email},
                )
            if email not in seen:
                seen.append(email)
        return seen

    async def _held_seats(self, classroom_id: uuid.UUID) -> int:
        counts = await self._invitations.counts(classroom_id)
        return counts.get(InvitationStatus.ACCEPTED, 0) + counts.get(InvitationStatus.PENDING, 0)

    # ================================================================ respond
    async def respond(
        self,
        *,
        invitation_id: uuid.UUID,
        user: SessionUser,
        accept: bool,
        reason: str | None = None,
    ) -> tuple[Classroom, SessionRecord | None]:
        invitation = await self._invitations.load_open(invitation_id, user.id)
        return await self._apply_response(invitation, user, accept=accept, reason=reason)

    async def respond_by_token(
        self, *, token: str, accept: bool, reason: str | None = None
    ) -> tuple[Classroom, SessionRecord | None, uuid.UUID]:
        """Answer straight from the emailed link, with no prior account or login.

        The token proves the caller controls the invited mailbox, which is precisely the
        claim an invitation makes. The user id it resolves to is returned so the caller
        can mint a real session for them.
        """
        invitation = await self._invitations.load_open_by_token(token)
        user = await self._users.session_user(invitation.user_id)
        classroom, session = await self._apply_response(
            invitation, user, accept=accept, reason=reason
        )
        return classroom, session, invitation.user_id

    async def _apply_response(
        self,
        invitation: Invitation,
        user: SessionUser,
        *,
        accept: bool,
        reason: str | None,
    ) -> tuple[Classroom, SessionRecord | None]:
        classroom = await self._classrooms.get(invitation.classroom_id)

        if classroom.status not in (ClassroomStatus.INVITING, ClassroomStatus.READY):
            raise ConflictError("This classroom is no longer open.")

        if not accept:
            self._invitations.mark(invitation, InvitationStatus.REJECTED, reason=reason)
            await self._uow.flush()
            self._notify_response(classroom, user, InvitationStatus.REJECTED)
            # No auto-backfill: the host chose these four people by name, so replacing
            # one is their decision to make, not the matcher's.
            self._classrooms.publish_update(classroom)
            return classroom, None

        self._invitations.mark(invitation, InvitationStatus.ACCEPTED)
        await self._classrooms.seat(classroom, user.id)
        await self._uow.flush()
        self._notify_response(classroom, user, InvitationStatus.ACCEPTED)

        session = await self._maybe_reach_quorum(classroom)
        self._classrooms.publish_update(
            classroom, accepted_count=await self._classrooms.seat_count(classroom.id)
        )
        return classroom, session

    async def _maybe_reach_quorum(self, classroom: Classroom) -> SessionRecord | None:
        """Open the room once everyone invited has made up their mind.

        The old rule was "all four accepted, or nothing". That let one person who never
        opened the app hold three others hostage until the invitation expired. The rule
        now is: wait until nobody is still deciding, then run with whoever said yes,
        provided there are at least two of them — a discussion needs someone to disagree
        with, and that is the only real floor.

        The host does not have to wait for the last holdout either; ``start`` will run
        the discussion as soon as the floor is met.
        """
        seated = await self._classrooms.seat_count(classroom.id)
        if seated < self._settings.min_participants_to_start:
            return None
        if classroom.status is ClassroomStatus.READY:
            return await self._sessions.by_classroom(classroom.id)

        counts = await self._invitations.counts(classroom.id)
        still_deciding = counts.get(InvitationStatus.PENDING, 0)
        room_for_more = seated < classroom.seat_count
        if still_deciding and room_for_more:
            return None

        self._classrooms.transition(classroom, CE.QUORUM_REACHED)
        await self._invitations.revoke_all(classroom.id)
        roster = await self._classrooms.roster(classroom.id)
        session = await self._sessions.provision(classroom, roster)
        await self._announce_ready(classroom, session, [p.user_id for p in roster])
        log.info("classroom.ready", classroom=str(classroom.id), session=str(session.id))
        return session

    async def _announce_ready(
        self, classroom: Classroom, session: SessionRecord, user_ids: list[uuid.UUID]
    ) -> None:
        """Tell all four the room is open.

        The ``session.ready`` event published alongside this is what actually opens the
        Join prompt in each participant's tab. The email, when enabled, is the copy for
        someone who closed that tab an hour ago.
        """
        if not self._settings.mail_enabled:
            return
        topic = await self._classrooms.get_topic(classroom.topic_id)
        room_url = self._settings.room_url(str(session.id))
        people = await self._users.by_ids(user_ids)
        self._uow.collect_mail(
            *[
                session_ready_email(
                    to=person.email,
                    invitee_name=person.display_name,
                    topic_title=topic.title,
                    room_url=room_url,
                )
                for person in people
            ]
        )

    def _notify_response(
        self, classroom: Classroom, user: SessionUser, status: InvitationStatus
    ) -> None:
        self._classrooms.notify_users(
            [classroom.created_by],
            EventType.INVITATION_RESPONDED,
            {
                "classroom_id": str(classroom.id),
                "user_id": str(user.id),
                "display_name": user.display_name,
                "status": status.value,
                "at": datetime.now(UTC).isoformat(),
            },
        )

    # ================================================================ start
    async def start(self, classroom: Classroom, user: SessionUser) -> SessionRecord:
        """Begin the discussion with whoever has accepted so far.

        The host may do this without waiting for the remaining invitations: three people
        who showed up should not be kept waiting on a fourth who never will.
        """
        self._classrooms.assert_owner(classroom, user)

        seated = await self._classrooms.seat_count(classroom.id)
        floor = self._settings.min_participants_to_start
        if seated < floor:
            raise ClassroomNotReadyError(
                f"{seated} participant{'' if seated == 1 else 's'} accepted so far. "
                f"A discussion needs at least {floor}."
            )

        session = await self._sessions.by_classroom(classroom.id)
        if session is None:
            # Starting early closes the door: an invitation accepted after this would
            # seat somebody the session was never provisioned with.
            await self._invitations.revoke_all(classroom.id)
            if CLASSROOM_FSM.can(classroom.status, CE.QUORUM_REACHED):
                self._classrooms.transition(classroom, CE.QUORUM_REACHED)
            roster = await self._classrooms.roster(classroom.id)
            session = await self._sessions.provision(classroom, roster)
            await self._announce_ready(classroom, session, [p.user_id for p in roster])

        if CLASSROOM_FSM.can(classroom.status, CE.STARTED):
            self._classrooms.transition(classroom, CE.STARTED)
            self._classrooms.publish_update(classroom, session_id=str(session.id))
        return session

    async def cancel(self, classroom: Classroom, user: SessionUser, reason: str | None) -> None:
        await self._classrooms.cancel(classroom, user, reason)
        await self._invitations.revoke_all(classroom.id)
        # A provisioned-but-never-started session would otherwise hold its four
        # participants out of every future classroom.
        if session := await self._sessions.by_classroom(classroom.id):
            await self._sessions.abort(session.id, reason or "CLASSROOM_CANCELLED")
