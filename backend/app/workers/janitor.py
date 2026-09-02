"""The janitor: a single periodic asyncio task, not a queue and not a scheduler service.

Four sweeps, each idempotent and each cheap enough to run every minute:

1. expire invitations nobody answered;
2. expire classrooms that never filled;
3. abort sessions whose owning process died (crash recovery);
4. delete transcripts past the retention window.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.engine import session_scope
from app.domain.enums import ClassroomStatus, SessionStatus
from app.domain.events import DomainEvent, EventType, user_topic
from app.modules.classroom.repository import ClassroomRepository
from app.modules.invitation.repository import InvitationRepository
from app.modules.moderation.orchestrator import AIOrchestratorService
from app.modules.notification.event_bus import EventBus
from app.modules.session.repository import SessionRepository, TurnRepository

log = get_logger(__name__)

#: A live session touches its row only at the start and the end, so "stale" has to be
#: generous — 90 minutes is well past the 45-minute hard cap on a discussion.
STALE_SESSION_MINUTES = 90
#: A provisioned session nobody ever joined has no runner to time it out, and it holds
#: its four participants out of every other classroom until it is swept.
UNJOINED_SESSION_MINUTES = 15


class Janitor:
    def __init__(
        self,
        *,
        settings: Settings,
        bus: EventBus,
        orchestrator: AIOrchestratorService,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self._orchestrator = orchestrator
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="janitor")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        # Stagger the first run so a rolling restart does not have every node sweep at once.
        await asyncio.sleep(10)
        while not self._stopping.is_set():
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("janitor.sweep_failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._settings.janitor_interval_seconds
                )

    async def sweep(self) -> dict[str, int]:
        now = datetime.now(UTC)
        result = {
            "invitations_expired": await self._expire_invitations(now),
            "classrooms_expired": await self._expire_classrooms(now),
            "sessions_recovered": await self._recover_sessions(now),
            "turns_purged": await self._purge_transcripts(now),
        }
        if any(result.values()):
            log.info("janitor.swept", **result)
        return result

    # ---------------------------------------------------------------- sweeps
    async def _expire_invitations(self, now: datetime) -> int:
        async with session_scope() as db:
            expired = await InvitationRepository(db).expire_overdue(now)
        for invitation in expired:
            self._bus.publish(
                DomainEvent(
                    topic=user_topic(invitation.user_id),
                    type=EventType.INVITATION_RESPONDED,
                    payload={
                        "invitation_id": str(invitation.id),
                        "classroom_id": str(invitation.classroom_id),
                        "status": "EXPIRED",
                    },
                )
            )
        return len(expired)

    async def _expire_classrooms(self, now: datetime) -> int:
        async with session_scope() as db:
            classrooms = await ClassroomRepository(db).expiring(now)
            for classroom in classrooms:
                classroom.status = ClassroomStatus.EXPIRED
        return len(classrooms)

    async def _recover_sessions(self, now: datetime) -> int:
        """A session in a running state that no process owns can only be abandoned."""
        running_cutoff = now - timedelta(minutes=STALE_SESSION_MINUTES)
        unjoined_cutoff = now - timedelta(minutes=UNJOINED_SESSION_MINUTES)

        async with session_scope() as db:
            # Query with the looser cutoff, then apply the stricter one per status.
            candidates = await SessionRepository(db).stale(unjoined_cutoff)
            ids = [
                record.id
                for record in candidates
                if self._orchestrator.get(record.id) is None
                and (
                    record.updated_at < running_cutoff
                    if record.status is not SessionStatus.PENDING
                    else True
                )
            ]

        for session_id in ids:
            await self._orchestrator.abort_stale(session_id)
            log.warning("janitor.session_recovered", session=str(session_id))
        return len(ids)

    async def _purge_transcripts(self, now: datetime) -> int:
        if self._settings.transcript_retention_days <= 0:
            return 0
        cutoff = now - timedelta(days=self._settings.transcript_retention_days)
        async with session_scope() as db:
            return await TurnRepository(db).purge_before(cutoff)
