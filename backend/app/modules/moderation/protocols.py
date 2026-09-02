"""What the session runner is allowed to ask the database for.

Deliberately tiny: two writes at the edges of the session and nothing in between. The
runner has no repository, no ``AsyncSession`` and no way to write a row mid-discussion.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from app.domain.enums import EndReason, SessionStatus
from app.domain.session import DiscussionSession


class SessionPersistence(Protocol):
    async def mark_active(self, session_id: UUID) -> None:
        """Session moved from CONNECTING to ACTIVE."""

    async def mark_status(
        self,
        session_id: UUID,
        status: SessionStatus,
        *,
        end_reason: EndReason | None = None,
    ) -> None: ...

    async def flush_transcript(self, session: DiscussionSession) -> None:
        """The single write of the whole discussion — turns plus per-participant totals."""

    async def save_summary(
        self,
        session_id: UUID,
        *,
        summary: dict[str, Any] | None,
        model: str,
        error: str | None = None,
    ) -> None: ...

    async def complete_classroom(self, session_id: UUID, *, aborted: bool) -> None:
        """Move the owning classroom to COMPLETED (or back to READY on abort)."""


class SessionStore(SessionPersistence, Protocol):
    """Persistence plus the one read the orchestrator needs.

    The orchestrator depends on this Protocol, not on the concrete gateway. That keeps
    the dependency pointing inward — a module must never import the application layer
    that wires it up — and lets a test drive a whole discussion from a dict.
    """

    async def load_live_session(self, session_id: UUID) -> DiscussionSession:
        """Build the in-memory aggregate from durable rows. Called once per session."""
