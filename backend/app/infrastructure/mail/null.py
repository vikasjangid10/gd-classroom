"""The transport used when email is switched off.

It exists so that "no email" is a transport like any other rather than a branch inside
the enrollment service. Nothing reaches it in normal operation — the enrollment service
does not queue mail at all when mail is disabled — so a message arriving here means a
code path queued one without checking, and it says so loudly instead of vanishing.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.domain.ports import EmailMessage

log = get_logger(__name__)


class NullEmailSender:
    name = "off"

    async def send(self, message: EmailMessage) -> None:
        log.warning(
            "mail.dropped",
            to=message.to,
            subject=message.subject,
            reason="MAIL_ENABLED is false — invitations are delivered in-app",
        )

    async def aclose(self) -> None:
        return None
