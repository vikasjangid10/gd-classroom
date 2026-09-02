"""Mail transport selection — the only file that knows SMTP exists."""

from __future__ import annotations

from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.ports import EmailSender

log = get_logger(__name__)


def build_email_sender(settings: Settings) -> EmailSender:
    if settings.mail_transport == "off":
        from app.infrastructure.mail.null import NullEmailSender

        log.info(
            "mail.transport",
            transport="off",
            reason="invitations are delivered in-app over the lobby event stream",
        )
        return NullEmailSender()

    if settings.mail_transport == "smtp":
        from app.infrastructure.mail.smtp import SmtpEmailSender

        log.info(
            "mail.transport",
            transport="smtp",
            host=settings.smtp_host,
            port=settings.smtp_port,
            security=settings.smtp_security,
            sender=settings.mail_sender_address,
        )
        return SmtpEmailSender(settings)

    from app.infrastructure.mail.console import ConsoleEmailSender

    log.warning(
        "mail.transport",
        transport="console",
        reason="SMTP_HOST is not set — invitations will be written to disk, not sent",
        outbox=settings.mail_outbox_dir,
    )
    return ConsoleEmailSender(settings)
