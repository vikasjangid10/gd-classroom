"""Unit of Work.

One request, one transaction, one place where ``commit`` is allowed to be called.
Services declare *what* changes; the unit of work decides *when* it becomes durable —
and holds the domain events that must not be published until after the commit.
"""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.events import DomainEvent
from app.domain.ports import EmailMessage


class UnitOfWork:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._pending_events: list[DomainEvent] = []
        self._pending_mail: list[EmailMessage] = []
        self._committed = False

    # ---------------------------------------------------------------- events
    def collect(self, *events: DomainEvent) -> None:
        """Queue an event to publish *after* the transaction commits.

        Publishing inside the transaction would let a client observe a fact that a
        later rollback erases — the classic read-your-own-phantom-write bug.
        """
        self._pending_events.extend(events)

    def drain_events(self) -> list[DomainEvent]:
        events, self._pending_events = self._pending_events, []
        return events

    # ---------------------------------------------------------------- email
    def collect_mail(self, *messages: EmailMessage) -> None:
        """Queue an email to send after the transaction commits.

        Same reasoning as events, with higher stakes: an event a rollback erases is a
        stale browser, but an email a rollback erases is a stranger holding a link to a
        classroom that does not exist.
        """
        self._pending_mail.extend(messages)

    def drain_mail(self) -> list[EmailMessage]:
        mail, self._pending_mail = self._pending_mail, []
        return mail

    # ---------------------------------------------------------------- txn
    async def flush(self) -> None:
        await self.session.flush()

    async def commit(self) -> None:
        await self.session.commit()
        self._committed = True

    async def rollback(self) -> None:
        await self.session.rollback()
        self._pending_events.clear()
        self._pending_mail.clear()

    async def __aenter__(self) -> UnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            await self.rollback()
        elif not self._committed:
            await self.commit()
