"""Database access for the session runner.

The runner lives outside the request cycle, so it cannot borrow a request-scoped
``AsyncSession``. This gateway opens its own short transaction for each of the handful
of writes a session performs — two at the start, two at the end, and nothing in between.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import update

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.db.engine import session_scope
from app.domain.aliases import aliases_for
from app.domain.enums import (
    ClassroomStatus,
    EndReason,
    SessionStatus,
    SpeakerType,
    SummaryStatus,
)
from app.domain.prompts import TopicBrief
from app.domain.session import DiscussionSession
from app.domain.turn_policy import DiscussionBudget
from app.modules.classroom.service import ClassroomService
from app.modules.identity.service import UserService
from app.modules.session.models import SessionParticipant, SessionRecord, SessionSummary, Turn
from app.modules.session.repository import (
    SessionParticipantRepository,
    SessionRepository,
    SummaryRepository,
)

log = get_logger(__name__)


class SessionDataGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ================================================================ bootstrap
    async def load_live_session(self, session_id: UUID) -> DiscussionSession:
        """Build the in-memory aggregate from durable rows. Called once per session."""
        async with session_scope() as db:
            sessions = SessionRepository(db)
            record = await sessions.get(session_id)
            if record is None:
                raise NotFoundError(f"Session {session_id} does not exist.")

            topic, _ = await ClassroomService.for_session(db).brief(record.classroom_id)

            roster = await SessionParticipantRepository(db).for_session(session_id)
            # Call-signs, not account names. This is the only place a live session is
            # populated, so not looking the real names up here is what guarantees they
            # never reach the moderator's context, the transcript or anyone's screen.
            # Gender is fetched — the name it belongs to is not.
            genders = await UserService.for_session(db).genders([p.user_id for p in roster])
            names = aliases_for(
                session_id,
                {p.user_id: (p.seat_no, genders.get(p.user_id)) for p in roster},
            )

            config = record.config_snapshot or {}
            live = DiscussionSession(
                session_id=session_id,
                classroom_id=record.classroom_id,
                topic=TopicBrief(
                    title=topic.title,
                    description=topic.description,
                    guiding_points=tuple(topic.guiding_points or ()),
                ),
                budget=DiscussionBudget(
                    target_seconds=int(
                        config.get("target_seconds", self.settings.discussion_target_seconds)
                    ),
                    max_seconds=int(
                        config.get("max_seconds", self.settings.discussion_max_seconds)
                    ),
                    min_turns_per_participant=int(
                        config.get(
                            "min_turns_per_participant", self.settings.min_turns_per_participant
                        )
                    ),
                    turn_max_seconds=int(
                        config.get("turn_max_seconds", self.settings.turn_max_seconds)
                    ),
                    max_silent_turns=self.settings.max_silent_turns,
                ),
            )
            for participant in roster:
                live.add_participant(
                    participant.user_id,
                    names.get(participant.user_id, "Participant"),
                    participant.seat_no,
                )
            return live

    # ================================================================ persistence
    async def mark_active(self, session_id: UUID) -> None:
        async with session_scope() as db:
            await db.execute(
                update(SessionRecord)
                .where(SessionRecord.id == session_id)
                .values(status=SessionStatus.ACTIVE, started_at=datetime.now(UTC))
            )

    async def mark_status(
        self,
        session_id: UUID,
        status: SessionStatus,
        *,
        end_reason: EndReason | None = None,
    ) -> None:
        now = datetime.now(UTC)
        values: dict[str, Any] = {"status": status}
        if end_reason is not None:
            values["end_reason"] = end_reason

        terminal = status in (SessionStatus.ENDED, SessionStatus.ABORTED)
        if terminal:
            values["ended_at"] = now

        async with session_scope() as db:
            await db.execute(
                update(SessionRecord).where(SessionRecord.id == session_id).values(**values)
            )
            if terminal:
                # ``left_at IS NULL`` is what enforces "one live discussion per person".
                # A session that ends without ever running still has to release its
                # participants, or they can never be matched into another classroom.
                await db.execute(
                    update(SessionParticipant)
                    .where(
                        SessionParticipant.session_id == session_id,
                        SessionParticipant.left_at.is_(None),
                    )
                    .values(left_at=now)
                )

    async def flush_transcript(self, session: DiscussionSession) -> None:
        """The single write of the whole discussion.

        Everything is inserted in one transaction, so a crash leaves either the complete
        transcript or none of it — never half a conversation.
        """
        async with session_scope() as db:
            _, persist = await ClassroomService.for_session(db).brief(session.classroom_id)

            if persist and session.buffered_turns:
                db.add_all(
                    [
                        Turn(
                            session_id=session.session_id,
                            turn_index=turn.turn_index,
                            speaker_type=(
                                SpeakerType.MODERATOR
                                if turn.is_moderator
                                else SpeakerType.PARTICIPANT
                            ),
                            speaker_user_id=turn.speaker_user_id,
                            kind=turn.kind,
                            text=turn.text,
                            started_at=turn.started_at,
                            duration_ms=turn.duration_ms,
                        )
                        for turn in session.buffered_turns
                    ]
                )

            for tally in session.ledger.tallies.values():
                await db.execute(
                    update(SessionParticipant)
                    .where(
                        SessionParticipant.session_id == session.session_id,
                        SessionParticipant.user_id == tally.user_id,
                    )
                    .values(
                        spoken_ms=tally.spoken_ms,
                        turns_taken=tally.turns_taken,
                        left_at=datetime.now(UTC),
                    )
                )
            log.info(
                "session.transcript_flushed",
                session=str(session.session_id),
                turns=len(session.buffered_turns),
                persisted=persist,
            )

    async def save_summary(
        self,
        session_id: UUID,
        *,
        summary: dict[str, Any] | None,
        model: str,
        error: str | None = None,
    ) -> None:
        async with session_scope() as db:
            existing = await SummaryRepository(db).for_session(session_id)
            row = existing or SessionSummary(session_id=session_id)
            if summary:
                row.status = SummaryStatus.READY
                row.headline = str(summary.get("headline", ""))[:500]
                row.key_points = list(summary.get("key_points", []))
                row.per_participant = list(summary.get("per_participant", []))
                row.open_questions = list(summary.get("open_questions", []))
                row.error = None
            else:
                row.status = SummaryStatus.FAILED
                row.error = error or "Summary generation failed."
            row.model = model
            if existing is None:
                db.add(row)

    async def complete_classroom(self, session_id: UUID, *, aborted: bool) -> None:
        async with session_scope() as db:
            record = await db.get(SessionRecord, session_id)
            if record is None:
                return
            await ClassroomService.for_session(db).set_status(
                record.classroom_id,
                ClassroomStatus.READY if aborted else ClassroomStatus.COMPLETED,
            )
