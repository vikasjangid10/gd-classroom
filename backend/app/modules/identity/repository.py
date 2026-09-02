from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import and_, delete, or_, update
from sqlalchemy.orm import selectinload

from app.db.repository import BaseRepository
from app.modules.identity.models import RefreshToken, User, UserTopicInterest


class UserRepository(BaseRepository[User]):
    model = User

    async def by_email(self, email: str) -> User | None:
        return await self.find_one(User.email == email.lower().strip())

    async def with_interests(self, user_id: uuid.UUID) -> User | None:
        return await self.find_one(User.id == user_id, options=[selectinload(User.interests)])

    async def by_ids(self, ids: Sequence[uuid.UUID]) -> list[User]:
        if not ids:
            return []
        return await self.find_all(User.id.in_(ids))

    async def touch_invited(self, user_ids: list[uuid.UUID]) -> None:
        if not user_ids:
            return
        await self.session.execute(
            update(User).where(User.id.in_(user_ids)).values(last_invited_at=datetime.now(UTC))
        )

    async def replace_interests(
        self, user_id: uuid.UUID, interests: list[tuple[uuid.UUID, int]]
    ) -> None:
        await self.session.execute(
            delete(UserTopicInterest).where(UserTopicInterest.user_id == user_id)
        )
        self.session.add_all(
            [
                UserTopicInterest(user_id=user_id, topic_id=topic_id, proficiency=level)
                for topic_id, level in interests
            ]
        )


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    model = RefreshToken

    async def by_hash(self, token_hash: str) -> RefreshToken | None:
        return await self.find_one(RefreshToken.token_hash == token_hash)

    async def revoke_family(self, family_id: str) -> None:
        """Reuse detection: one replayed token kills every token in its family."""
        await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def revoke_for_user(self, user_id: uuid.UUID) -> None:
        await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def purge_expired(self, now: datetime) -> int:
        return await self.delete_where(
            or_(
                RefreshToken.expires_at < now,
                and_(RefreshToken.revoked_at.is_not(None), RefreshToken.revoked_at < now),
            )
        )
