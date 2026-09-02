"""Invitation lifecycle: issue, deliver, accept, reject, revoke, expire.

The token is the whole security model of this module. It is generated once, shown to
nobody but the recipient's inbox, and stored only as a SHA-256 digest — so a database
dump does not let anyone take a seat, and a leaked link expires on its own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings
from app.core.errors import AuthorizationError, InvitationClosedError, NotFoundError
from app.core.logging import get_logger
from app.core.security import hash_token, random_token
from app.db.uow import UnitOfWork
from app.domain.enums import InvitationStatus
from app.domain.events import DomainEvent, EventType, user_topic
from app.modules.identity.service import UserService
from app.modules.invitation.models import Invitation
from app.modules.invitation.repository import InvitationRepository

log = get_logger(__name__)

_OPEN = InvitationStatus.PENDING

#: Prefix used to tie an outgoing email back to the row it belongs to.
MAIL_REF = "invitation"


@dataclass(slots=True)
class IssuedInvitation:
    """An invitation plus the one and only time its raw token exists in memory."""

    invitation: Invitation
    token: str
    email: str
    display_name: str


class InvitationService:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        invitations: InvitationRepository,
        users: UserService,
        settings: Settings,
    ) -> None:
        self._uow = uow
        self._invitations = invitations
        self._users = users
        self._settings = settings

    # ---------------------------------------------------------------- shared reads
    @staticmethod
    def for_session(db: AsyncSession) -> InvitationService:
        """Build the service outside the request cycle (the mail dispatcher's route in)."""
        return InvitationService(
            uow=UnitOfWork(db),
            invitations=InvitationRepository(db),
            users=UserService.for_session(db),
            settings=settings,
        )

    # ---------------------------------------------------------------- issue
    async def issue(
        self,
        *,
        classroom_id: uuid.UUID,
        classroom_title: str,
        topic_title: str,
        user_ids: list[uuid.UUID],
    ) -> list[IssuedInvitation]:
        """Create one pending invitation per person and hand back their raw tokens.

        Callers get ``IssuedInvitation`` rather than ``Invitation`` because the token is
        never readable again — the row holds only its digest. Whatever is going to put
        that token in front of a human has to do it with what is returned here.
        """
        expires_at = datetime.now(UTC) + timedelta(seconds=self._settings.invitation_ttl_seconds)
        people = {u.id: u for u in await self._users.by_ids(user_ids)}
        issued: list[IssuedInvitation] = []

        for user_id in user_ids:
            person = people.get(user_id)
            if person is None:
                raise NotFoundError("One of the invitees no longer exists.")
            if await self._invitations.open_for(classroom_id, user_id):
                continue

            token = random_token()
            invitation = Invitation(
                classroom_id=classroom_id,
                user_id=user_id,
                attempt_no=await self._invitations.next_attempt_no(classroom_id, user_id),
                status=_OPEN,
                token_hash=hash_token(token),
                invited_email=person.email,
                expires_at=expires_at,
            )
            self._invitations.add(invitation)
            issued.append(
                IssuedInvitation(
                    invitation=invitation,
                    token=token,
                    email=person.email,
                    display_name=person.display_name,
                )
            )

        await self._users.mark_invited([i.invitation.user_id for i in issued])
        await self._uow.flush()

        for item in issued:
            self._uow.collect(
                DomainEvent(
                    topic=user_topic(item.invitation.user_id),
                    type=EventType.INVITATION_SENT,
                    payload={
                        "invitation_id": str(item.invitation.id),
                        "classroom_id": str(classroom_id),
                        "classroom_title": classroom_title,
                        "topic_title": topic_title,
                        "expires_at": item.invitation.expires_at.isoformat(),
                    },
                )
            )
        log.info("invitations.issued", classroom=str(classroom_id), count=len(issued))
        return issued

    async def revoke(self, invitation: Invitation) -> None:
        """Close an invitation so a fresh one can be issued to the same person."""
        invitation.status = InvitationStatus.REVOKED
        invitation.responded_at = datetime.now(UTC)
        await self._uow.flush()

    # ---------------------------------------------------------------- respond
    async def load_open(self, invitation_id: uuid.UUID, user_id: uuid.UUID) -> Invitation:
        invitation = await self._invitations.get(invitation_id)
        if invitation is None:
            raise NotFoundError("That invitation does not exist.")
        if invitation.user_id != user_id:
            # 403 rather than 404: the caller proved identity, they just are not the invitee.
            raise AuthorizationError("That invitation was not sent to you.")
        return self._assert_open(invitation)

    async def load_by_token(self, raw_token: str) -> Invitation:
        """Resolve a link from an inbox. Possession of the token is the whole claim.

        Deliberately a 404 for anything unrecognised: a link that has been guessed,
        tampered with or already used must not be distinguishable from a link that
        never existed.
        """
        invitation = await self._invitations.by_token_hash(hash_token(raw_token))
        if invitation is None:
            raise NotFoundError("This invitation link is not valid.")
        return invitation

    async def load_open_by_token(self, raw_token: str) -> Invitation:
        return self._assert_open(await self.load_by_token(raw_token))

    def _assert_open(self, invitation: Invitation) -> Invitation:
        if invitation.status is not _OPEN:
            raise InvitationClosedError()
        if invitation.expires_at < datetime.now(UTC):
            invitation.status = InvitationStatus.EXPIRED
            invitation.responded_at = datetime.now(UTC)
            raise InvitationClosedError("That invitation has expired.")
        return invitation

    def mark(
        self, invitation: Invitation, status: InvitationStatus, *, reason: str | None = None
    ) -> None:
        invitation.status = status
        invitation.responded_at = datetime.now(UTC)
        if reason:
            invitation.reject_reason = reason[:280]

    # ---------------------------------------------------------------- delivery
    async def record_delivery(self, invitation_id: uuid.UUID, *, error: str | None) -> None:
        """Write the outcome of the send attempt back onto the invitation."""
        invitation = await self._invitations.get(invitation_id)
        if invitation is None:
            return
        if error is None:
            invitation.email_sent_at = datetime.now(UTC)
            invitation.email_error = None
        else:
            invitation.email_error = error[:500]
        await self._uow.commit()

    # ---------------------------------------------------------------- queries
    async def get(self, invitation_id: uuid.UUID) -> Invitation:
        invitation = await self._invitations.get(invitation_id)
        if invitation is None:
            raise NotFoundError("That invitation does not exist.")
        return invitation

    async def open_for_user(
        self, classroom_id: uuid.UUID, user_id: uuid.UUID
    ) -> Invitation | None:
        return await self._invitations.open_for(classroom_id, user_id)

    async def pending_for(self, user_id: uuid.UUID) -> list[Invitation]:
        return await self._invitations.pending_for_user(user_id)

    async def for_classroom(self, classroom_id: uuid.UUID) -> list[Invitation]:
        return await self._invitations.for_classroom(classroom_id)

    async def accepted_user_ids(self, classroom_id: uuid.UUID) -> list[uuid.UUID]:
        return await self._invitations.accepted_user_ids(classroom_id)

    async def already_involved(self, classroom_id: uuid.UUID) -> list[uuid.UUID]:
        return await self._invitations.involved_user_ids(classroom_id)

    async def counts(self, classroom_id: uuid.UUID) -> dict[InvitationStatus, int]:
        return await self._invitations.count_by_status(classroom_id)

    async def revoke_all(self, classroom_id: uuid.UUID) -> int:
        return await self._invitations.revoke_pending(classroom_id, datetime.now(UTC))

    async def expire_overdue(self) -> list[Invitation]:
        return await self._invitations.expire_overdue(datetime.now(UTC))
