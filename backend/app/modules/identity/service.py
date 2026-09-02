"""Authentication and user profile use cases."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AuthenticationError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.security import (
    UNUSABLE_PASSWORD_PREFIX,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    random_token,
    unusable_password,
    verify_password,
)
from app.db.uow import UnitOfWork
from app.domain.enums import Gender, Role
from app.modules.identity.models import RefreshToken, User
from app.modules.identity.repository import RefreshTokenRepository, UserRepository
from app.modules.identity.schemas import RegisterIn, SessionUser

log = get_logger(__name__)


class AuthService:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        users: UserRepository,
        refresh_tokens: RefreshTokenRepository,
        settings: Settings,
    ) -> None:
        self._uow = uow
        self._users = users
        self._refresh = refresh_tokens
        self._settings = settings

    # ---------------------------------------------------------------- register
    async def register(self, payload: RegisterIn) -> User:
        email = payload.email.lower().strip()
        if await self._users.by_email(email):
            raise ConflictError("An account with that email already exists.")

        user = User(
            email=email,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name.strip(),
            role=payload.role,
            gender=payload.gender,
        )
        self._users.add(user)
        await self._uow.flush()
        log.info("auth.registered", user_id=str(user.id), role=user.role.value)
        return user

    # ---------------------------------------------------------------- login
    async def login(
        self, email: str, password: str, *, user_agent: str | None
    ) -> tuple[str, str, User]:
        user = await self._users.by_email(email)
        # Constant-ish work whether or not the account exists, so timing does not leak.
        if user is None or not verify_password(password, user.password_hash):
            raise AuthenticationError("Those credentials did not match.")
        if not user.is_active:
            raise AuthenticationError("This account has been deactivated.")

        user.last_seen_at = datetime.now(UTC)
        access, refresh = await self._issue_pair(user, family_id=random_token(12), ua=user_agent)
        return access, refresh, user

    # ---------------------------------------------------------------- magic link
    async def issue_session_for(
        self, user_id: uuid.UUID, *, user_agent: str | None
    ) -> tuple[str, str, User]:
        """Sign someone in because they proved possession of an invitation token.

        The proof happened in the invitation module — a single-use, hashed, expiring
        token delivered to an address only that person controls. That is the same class
        of evidence as a password reset link, and it is why an invitee never needs an
        account before the host invites them.
        """
        user = await self._users.get(user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("This account is no longer active.")
        user.last_seen_at = datetime.now(UTC)
        access, refresh = await self._issue_pair(user, family_id=random_token(12), ua=user_agent)
        log.info("auth.magic_link", user_id=str(user.id))
        return access, refresh, user

    # ---------------------------------------------------------------- refresh
    async def refresh(self, raw_token: str, *, user_agent: str | None) -> tuple[str, str, User]:
        claims = decode_token(raw_token, expected="refresh")
        stored = await self._refresh.by_hash(hash_token(raw_token))

        if stored is None or stored.revoked_at is not None:
            # A replayed or unknown refresh token means the family is compromised.
            family = str(claims.get("fam", ""))
            if family:
                await self._refresh.revoke_family(family)
                log.warning("auth.refresh_reuse", family=family)
            raise AuthenticationError("Please sign in again.")

        if stored.expires_at < datetime.now(UTC):
            raise AuthenticationError("Your session has expired.")

        user = await self._users.get(stored.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("Please sign in again.")

        stored.revoked_at = datetime.now(UTC)
        access, new_refresh = await self._issue_pair(
            user, family_id=stored.family_id, ua=user_agent
        )
        return access, new_refresh, user

    async def logout(self, raw_token: str | None, user_id: uuid.UUID) -> None:
        if raw_token and (stored := await self._refresh.by_hash(hash_token(raw_token))):
            await self._refresh.revoke_family(stored.family_id)
            return
        await self._refresh.revoke_for_user(user_id)

    # ---------------------------------------------------------------- helpers
    async def _issue_pair(self, user: User, *, family_id: str, ua: str | None) -> tuple[str, str]:
        access = create_access_token(user.id, user.role.value)
        refresh = create_refresh_token(user.id, family_id)
        now = datetime.now(UTC)
        self._refresh.add(
            RefreshToken(
                user_id=user.id,
                family_id=family_id,
                token_hash=hash_token(refresh),
                issued_at=now,
                expires_at=now + timedelta(seconds=self._settings.refresh_token_ttl_seconds),
                user_agent=(ua or "")[:200] or None,
            )
        )
        await self._uow.flush()
        return access, refresh

    async def resolve(self, user_id: uuid.UUID) -> SessionUser:
        user = await self._users.get(user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("This account is no longer active.")
        return SessionUser(
            id=user.id, email=user.email, display_name=user.display_name, role=user.role
        )


class UserService:
    def __init__(self, *, uow: UnitOfWork, users: UserRepository) -> None:
        self._uow = uow
        self._users = users

    async def get_profile(self, user_id: uuid.UUID) -> tuple[User, list[tuple[uuid.UUID, int]]]:
        user = await self._users.with_interests(user_id)
        if user is None:
            raise NotFoundError("User not found.")
        return user, [(i.topic_id, i.proficiency) for i in user.interests]

    async def set_interests(
        self, user_id: uuid.UUID, interests: list[tuple[uuid.UUID, int]]
    ) -> None:
        await self._users.replace_interests(user_id, interests)

    async def rename(self, user_id: uuid.UUID, display_name: str) -> None:
        """Let an invitee replace the name guessed from their email address."""
        name = display_name.strip()[:80]
        if not name:
            return
        user = await self._users.get(user_id)
        if user is not None:
            user.display_name = name

    async def list_participants(self, limit: int = 100) -> list[User]:
        """Everyone a host can actually invite.

        Passwordless accounts are excluded. They exist only when email invitations are
        enabled — the emailed token is their credential — so with in-app invitations
        such a person could be given a seat they have no way of reaching. Offering them
        in the picker would let a host fill a classroom with people who can never accept.
        """
        return await self._users.find_all(
            User.role == Role.PARTICIPANT,
            User.is_active.is_(True),
            User.password_hash.notlike(f"{UNUSABLE_PASSWORD_PREFIX}%"),
            order_by=User.display_name,
            limit=limit,
        )

    # ---------------------------------------------------------------- shared reads
    # Everything below is the identity module's public surface for the other modules.
    # They must not touch UserRepository directly — a rule the import-linter contract
    # in pyproject.toml enforces, because that is exactly how a modular monolith rots.
    @staticmethod
    def for_session(db: AsyncSession) -> UserService:
        """Build the service outside the request cycle (the session runner's gateway)."""
        return UserService(uow=UnitOfWork(db), users=UserRepository(db))

    async def display_names(self, user_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        return {u.id: u.display_name for u in await self._users.by_ids(list(user_ids))}

    async def genders(self, user_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Gender]:
        """Gender only — deliberately not the names.

        A discussion needs this to choose a call-sign that fits the person. It must not
        need, and must not be given, the name behind it: what a session never loads, it
        can never leak.
        """
        return {u.id: u.gender for u in await self._users.by_ids(list(user_ids))}

    async def by_ids(self, user_ids: Sequence[uuid.UUID]) -> list[User]:
        return await self._users.by_ids(list(user_ids))

    async def session_user(self, user_id: uuid.UUID) -> SessionUser:
        """The caller-shaped view of a user, for code holding only an id."""
        user = await self._users.get(user_id)
        if user is None or not user.is_active:
            raise NotFoundError("That account no longer exists.")
        return SessionUser(
            id=user.id, email=user.email, display_name=user.display_name, role=user.role
        )

    async def mark_invited(self, user_ids: Sequence[uuid.UUID]) -> None:
        """Records when someone was last invited; shown to the host, not used to rank."""
        await self._users.touch_invited(list(user_ids))

    async def ensure_invitees(
        self, emails: Sequence[str], *, create_missing: bool = False
    ) -> list[User]:
        """Resolve the host's chosen addresses to user rows.

        ``create_missing`` follows the delivery model, and the two must agree:

        * **In-app invitations (the default).** The invitation is delivered over the
          invitee's lobby event stream, so they must already have an account to receive
          it on. Creating a passwordless row for a stranger would produce an account
          nobody can ever log into — a seat permanently held by a ghost. So an unknown
          address is refused, by name, and the host picks somebody real.
        * **Email invitations.** The emailed token *is* the credential, so a stranger
          with no account is exactly the expected case and one is created for them.

        Returned in the order the addresses were given, so seat order matches the
        host's choice.
        """
        wanted = [e.lower().strip() for e in emails]
        created: list[User] = []
        resolved: dict[str, User] = {}

        for email in wanted:
            if email in resolved:
                continue
            existing = await self._users.by_email(email)
            if existing is not None:
                if not existing.is_active:
                    raise ConflictError(f"The account for {email} has been deactivated.")
                resolved[email] = existing
                continue

            if not create_missing:
                raise NotFoundError(
                    f"Nobody is registered here with the address {email}. "
                    "Ask them to sign up first, then invite them."
                )

            user = User(
                email=email,
                password_hash=unusable_password(),
                display_name=display_name_from_email(email),
                role=Role.PARTICIPANT,
            )
            self._users.add(user)
            resolved[email] = user
            created.append(user)

        if created:
            await self._uow.flush()
            log.info("identity.invitees_created", count=len(created))
        return [resolved[email] for email in dict.fromkeys(wanted)]


def display_name_from_email(email: str) -> str:
    """``priya.sharma@x.com`` → ``Priya Sharma``. A placeholder the invitee can change.

    Never returns an empty string: this name is what the moderator says out loud when it
    calls on someone, and a blank there is worse than an ugly one.
    """
    local = email.split("@", 1)[0]
    words = [part for part in local.replace("_", ".").replace("-", ".").split(".") if part]
    name = " ".join(word.capitalize() for word in words)
    return (name or local or email or "Guest")[:80]

