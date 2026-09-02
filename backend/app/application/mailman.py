"""Post-commit email delivery.

Sending an email inside the request transaction would be wrong twice over: it would
hold a database connection open across a network call to a third party, and it would
send a message describing a row that a later rollback deletes. So the unit of work
collects messages and hands them here after it commits, and this class does the slow,
failure-prone part on its own time.

Delivery is at-most-once and its outcome is written back to the invitation row. That is
a deliberate ceiling: a retry queue would need a broker, the brief rules those out, and
a host who can see "this address bounced" and press resend is a better answer than
silent machinery. What must never happen is a bounce nobody hears about.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.engine import session_scope
from app.domain.ports import EmailMessage, EmailSender
from app.modules.invitation.service import MAIL_REF, InvitationService

log = get_logger(__name__)

#: Two quick attempts absorb the common transient failure — a provider throttling a
#: burst of four messages — without turning a dead SMTP host into a slow request.
_ATTEMPTS = 2
_BACKOFF_SECONDS = 2.0


class Mailman:
    def __init__(self, *, sender: EmailSender, settings: Settings) -> None:
        self._sender = sender
        self._settings = settings
        self._in_flight: set[asyncio.Task[None]] = set()

    @property
    def transport(self) -> str:
        return self._sender.name

    # ---------------------------------------------------------------- dispatch
    def dispatch(self, messages: list[EmailMessage]) -> None:
        """Hand messages to the event loop. Never awaits, never raises."""
        for message in messages:
            task = asyncio.create_task(self._deliver(message))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)

    async def send_now(self, message: EmailMessage) -> None:
        """Deliver synchronously and propagate the failure — used by the health probe."""
        await self._sender.send(message)

    # ---------------------------------------------------------------- internals
    async def _deliver(self, message: EmailMessage) -> None:
        error: str | None = None
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                await self._sender.send(message)
                error = None
                break
            # Broad on purpose: whatever the transport raises, the outcome is data to be
            # recorded against the invitation, not an exception to propagate into a
            # background task nobody is awaiting.
            except Exception as exc:
                error = str(exc)
                log.warning(
                    "mail.attempt_failed",
                    to=message.to,
                    attempt=attempt,
                    of=_ATTEMPTS,
                    error=error,
                )
                if attempt < _ATTEMPTS:
                    await asyncio.sleep(_BACKOFF_SECONDS)

        if error is not None:
            log.error("mail.failed", to=message.to, subject=message.subject, error=error)
        await self._record(message, error)

    async def _record(self, message: EmailMessage, error: str | None) -> None:
        invitation_id = _invitation_ref(message.reference)
        if invitation_id is None:
            return
        try:
            async with session_scope() as db:
                await InvitationService.for_session(db).record_delivery(
                    invitation_id, error=error
                )
        except Exception as exc:  # bookkeeping must never mask the send itself
            log.warning("mail.record_failed", invitation=str(invitation_id), error=str(exc))

    # ---------------------------------------------------------------- lifecycle
    async def aclose(self) -> None:
        """Give in-flight sends a moment to finish so shutdown does not eat an invite."""
        if not self._in_flight:
            return
        pending = list(self._in_flight)
        done, still_running = await asyncio.wait(pending, timeout=10)
        for task in still_running:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        log.info("mail.drained", delivered=len(done), cancelled=len(still_running))
        await self._sender.aclose()


def _invitation_ref(reference: str | None) -> uuid.UUID | None:
    if not reference or not reference.startswith(f"{MAIL_REF}:"):
        return None
    try:
        return uuid.UUID(reference.split(":", 1)[1])
    except ValueError:  # pragma: no cover - a malformed reference is a programming error
        return None
