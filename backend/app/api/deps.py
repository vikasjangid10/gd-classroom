"""Dependency injection.

Three layers of dependency, each built from the one below:

    AsyncSession → UnitOfWork + repositories → services

FastAPI resolves them per request, caches within the request, and tears them down in
reverse order. Authorisation is expressed the same way — as a dependency you can read
off the endpoint signature (``user: SuperUser``) rather than an ``if`` buried in a
handler.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.enrollment import EnrollmentService
from app.application.mailman import Mailman
from app.container import Container, get_container
from app.core.config import Settings
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.security import decode_token, read_ticket
from app.db.engine import get_sessionmaker
from app.db.uow import UnitOfWork
from app.domain.enums import Role
from app.modules.classroom.repository import (
    ClassroomParticipantRepository,
    ClassroomRepository,
    TopicRepository,
)
from app.modules.classroom.service import ClassroomService
from app.modules.identity.repository import RefreshTokenRepository, UserRepository
from app.modules.identity.schemas import SessionUser
from app.modules.identity.service import AuthService, UserService
from app.modules.invitation.repository import InvitationRepository
from app.modules.invitation.service import InvitationService
from app.modules.moderation.orchestrator import AIOrchestratorService
from app.modules.notification.event_bus import EventBus
from app.modules.session.repository import (
    SessionParticipantRepository,
    SessionRepository,
    SummaryRepository,
    TurnRepository,
)
from app.modules.session.service import SessionService

bearer_scheme = HTTPBearer(auto_error=False)


# ===================================================================== singletons
def container() -> Container:
    return get_container()


def settings_dep(c: Annotated[Container, Depends(container)]) -> Settings:
    return c.settings


def event_bus(c: Annotated[Container, Depends(container)]) -> EventBus:
    return c.bus


def orchestrator(c: Annotated[Container, Depends(container)]) -> AIOrchestratorService:
    return c.orchestrator


def mailman(c: Annotated[Container, Depends(container)]) -> Mailman:
    return c.mailman


Cfg = Annotated[Settings, Depends(settings_dep)]
Bus = Annotated[EventBus, Depends(event_bus)]
Orchestrator = Annotated[AIOrchestratorService, Depends(orchestrator)]
Post = Annotated[Mailman, Depends(mailman)]


# ===================================================================== database
async def db_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


Db = Annotated[AsyncSession, Depends(db_session)]


async def unit_of_work(db: Db, bus: Bus, post: Post) -> AsyncIterator[UnitOfWork]:
    """One transaction per request; events published and email sent only after it commits."""
    uow = UnitOfWork(db)
    try:
        yield uow
        await uow.commit()
    except Exception:
        await uow.rollback()
        raise
    else:
        bus.publish_all(uow.drain_events())
        post.dispatch(uow.drain_mail())


Uow = Annotated[UnitOfWork, Depends(unit_of_work)]


# ===================================================================== repositories
def users_repo(uow: Uow) -> UserRepository:
    return UserRepository(uow.session)


def refresh_repo(uow: Uow) -> RefreshTokenRepository:
    return RefreshTokenRepository(uow.session)


def topics_repo(uow: Uow) -> TopicRepository:
    return TopicRepository(uow.session)


def classrooms_repo(uow: Uow) -> ClassroomRepository:
    return ClassroomRepository(uow.session)


def classroom_participants_repo(uow: Uow) -> ClassroomParticipantRepository:
    return ClassroomParticipantRepository(uow.session)


def invitations_repo(uow: Uow) -> InvitationRepository:
    return InvitationRepository(uow.session)


def sessions_repo(uow: Uow) -> SessionRepository:
    return SessionRepository(uow.session)


def session_participants_repo(uow: Uow) -> SessionParticipantRepository:
    return SessionParticipantRepository(uow.session)


def turns_repo(uow: Uow) -> TurnRepository:
    return TurnRepository(uow.session)


def summaries_repo(uow: Uow) -> SummaryRepository:
    return SummaryRepository(uow.session)


# ===================================================================== services
def auth_service(
    uow: Uow,
    users: Annotated[UserRepository, Depends(users_repo)],
    refresh: Annotated[RefreshTokenRepository, Depends(refresh_repo)],
    cfg: Cfg,
) -> AuthService:
    return AuthService(uow=uow, users=users, refresh_tokens=refresh, settings=cfg)


def user_service(
    uow: Uow, users: Annotated[UserRepository, Depends(users_repo)]
) -> UserService:
    return UserService(uow=uow, users=users)


def classroom_service(
    uow: Uow,
    classrooms: Annotated[ClassroomRepository, Depends(classrooms_repo)],
    participants: Annotated[ClassroomParticipantRepository, Depends(classroom_participants_repo)],
    topics: Annotated[TopicRepository, Depends(topics_repo)],
    cfg: Cfg,
) -> ClassroomService:
    return ClassroomService(
        uow=uow, classrooms=classrooms, participants=participants, topics=topics, settings=cfg
    )


def invitation_service(
    uow: Uow,
    invitations: Annotated[InvitationRepository, Depends(invitations_repo)],
    users: Annotated[UserService, Depends(user_service)],
    cfg: Cfg,
) -> InvitationService:
    return InvitationService(uow=uow, invitations=invitations, users=users, settings=cfg)


def session_service(
    uow: Uow,
    sessions: Annotated[SessionRepository, Depends(sessions_repo)],
    participants: Annotated[SessionParticipantRepository, Depends(session_participants_repo)],
    turns: Annotated[TurnRepository, Depends(turns_repo)],
    summaries: Annotated[SummaryRepository, Depends(summaries_repo)],
    cfg: Cfg,
) -> SessionService:
    return SessionService(
        uow=uow,
        sessions=sessions,
        participants=participants,
        turns=turns,
        summaries=summaries,
        settings=cfg,
    )


def enrollment_service(
    uow: Uow,
    classrooms: Annotated[ClassroomService, Depends(classroom_service)],
    invitations: Annotated[InvitationService, Depends(invitation_service)],
    users: Annotated[UserService, Depends(user_service)],
    sessions: Annotated[SessionService, Depends(session_service)],
    cfg: Cfg,
) -> EnrollmentService:
    return EnrollmentService(
        uow=uow,
        classrooms=classrooms,
        invitations=invitations,
        users=users,
        sessions=sessions,
        settings=cfg,
    )


Auth = Annotated[AuthService, Depends(auth_service)]
Users = Annotated[UserService, Depends(user_service)]
Classrooms = Annotated[ClassroomService, Depends(classroom_service)]
Invitations = Annotated[InvitationService, Depends(invitation_service)]
Sessions = Annotated[SessionService, Depends(session_service)]
Enrollment = Annotated[EnrollmentService, Depends(enrollment_service)]


# ===================================================================== authn
async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    auth: Auth,
) -> SessionUser:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError()
    claims = decode_token(credentials.credentials, expected="access")
    return await auth.resolve(uuid.UUID(claims["sub"]))


CurrentUser = Annotated[SessionUser, Depends(current_user)]


async def current_super_user(user: CurrentUser) -> SessionUser:
    if user.role is not Role.SUPER_USER:
        raise AuthorizationError("This action is limited to super users.")
    return user


SuperUser = Annotated[SessionUser, Depends(current_super_user)]


# ===================================================================== tickets
class TicketPrincipal:
    """Identity proven by a short-lived, session-scoped ticket rather than a bearer token."""

    __slots__ = ("session_id", "user_id")

    def __init__(self, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
        self.user_id = user_id
        self.session_id = session_id


def _ticket_dependency(scope: str) -> Callable[..., Awaitable[TicketPrincipal]]:
    async def dependency(
        request: Request,
        session_id: uuid.UUID,
        ticket: Annotated[str | None, Query(description="Short-lived stream ticket")] = None,
    ) -> TicketPrincipal:
        raw = ticket or request.headers.get("X-Stream-Ticket")
        if not raw:
            raise AuthenticationError("A stream ticket is required.")
        user_id, ticket_session = read_ticket(raw, scope=scope)  # type: ignore[arg-type]
        if ticket_session != session_id:
            raise AuthorizationError("That ticket belongs to a different discussion.")
        return TicketPrincipal(user_id, session_id)

    return dependency


SseTicket = Annotated[TicketPrincipal, Depends(_ticket_dependency("sse"))]


async def rtc_principal(session_id: uuid.UUID, ticket: str) -> TicketPrincipal:
    user_id, ticket_session = read_ticket(ticket, scope="rtc")
    if ticket_session != session_id:
        raise AuthorizationError("That ticket belongs to a different discussion.")
    return TicketPrincipal(user_id, session_id)


# ===================================================================== misc
async def last_event_id(
    header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    query: Annotated[str | None, Query(alias="last_event_id")] = None,
) -> str | None:
    return header or query


LastEventId = Annotated[str | None, Depends(last_event_id)]
