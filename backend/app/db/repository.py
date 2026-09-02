"""Generic repository.

A repository is a *collection of aggregates*, not a thin wrapper over SQL. It is the
only place that knows about SQLAlchemy, so a service reads like business language and a
test can substitute an in-memory list.

It deliberately does **not** expose ``commit`` — that belongs to the unit of work.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Generic, TypeVar, cast

from sqlalchemy import CursorResult, Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------------------------------------------------------------- write
    def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        return entity

    def add_all(self, entities: Sequence[ModelT]) -> None:
        self.session.add_all(list(entities))

    async def delete(self, entity: ModelT) -> None:
        await self.session.delete(entity)

    async def delete_where(self, *conditions: Any) -> int:
        result = await self.session.execute(delete(self.model).where(*conditions))
        # execute() is typed as returning Result, but a DML statement always yields a
        # CursorResult, which is the only kind that carries a row count.
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    # ---------------------------------------------------------------- read
    async def get(self, entity_id: uuid.UUID, *options: Any) -> ModelT | None:
        stmt: Select[tuple[ModelT]] = select(self.model).where(self.model.id == entity_id)  # type: ignore[attr-defined]
        if options:
            stmt = stmt.options(*options)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_404(self, entity_id: uuid.UUID, *options: Any) -> ModelT:
        entity = await self.get(entity_id, *options)
        if entity is None:
            raise NotFoundError(f"{self.model.__name__} {entity_id} does not exist.")
        return entity

    async def by_ids(self, ids: Sequence[uuid.UUID]) -> list[ModelT]:
        if not ids:
            return []
        return await self.find_all(self.model.id.in_(list(ids)))  # type: ignore[attr-defined]

    async def find_one(self, *conditions: Any, options: Sequence[Any] = ()) -> ModelT | None:
        stmt = select(self.model).where(*conditions)
        if options:
            stmt = stmt.options(*options)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find_all(
        self,
        *conditions: Any,
        order_by: Any = None,
        limit: int | None = None,
        options: Sequence[Any] = (),
    ) -> list[ModelT]:
        stmt = select(self.model)
        if conditions:
            stmt = stmt.where(*conditions)
        if options:
            stmt = stmt.options(*options)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count(self, *conditions: Any) -> int:
        stmt = select(func.count()).select_from(self.model)
        if conditions:
            stmt = stmt.where(*conditions)
        return int((await self.session.execute(stmt)).scalar_one())

    async def exists(self, *conditions: Any) -> bool:
        return await self.count(*conditions) > 0
