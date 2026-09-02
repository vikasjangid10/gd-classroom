"""SMTP transport.

Deliberately plain: one connection per message, no pooling. A classroom sends four
emails when it is created and four when it becomes ready, so the cost of a fresh
handshake is irrelevant next to the cost of debugging a stale pooled socket that a
provider silently closed twenty minutes ago.
"""

from __future__ import annotations

from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, make_msgid

import aiosmtplib

from app.core.config import Settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.domain.ports import EmailMessage

log = get_logger(__name__)


class SmtpEmailSender:
    name = "smtp"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _build(self, message: EmailMessage) -> MimeMessage:
        mime = MimeMessage()
        mime["From"] = formataddr(
            (self._settings.mail_from_name, self._settings.mail_sender_address)
        )
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime["Message-ID"] = make_msgid(domain="gd-classroom.app")
        # Gmail and Outlook both weight a plain-text alternative when scoring spam, and
        # the text part is the only thing a watch or a screen reader will read out.
        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")
        return mime

    async def send(self, message: EmailMessage) -> None:
        cfg = self._settings
        try:
            await aiosmtplib.send(
                self._build(message),
                hostname=cfg.smtp_host,
                port=cfg.smtp_port,
                username=cfg.smtp_username or None,
                password=cfg.smtp_password or None,
                use_tls=cfg.smtp_security == "ssl",
                start_tls=cfg.smtp_security == "starttls",
                timeout=cfg.smtp_timeout_seconds,
            )
        except aiosmtplib.SMTPAuthenticationError as exc:
            # By far the most common real failure: a Gmail account password used where
            # an app password is required. Say so, rather than logging "535".
            raise ExternalServiceError(
                "smtp",
                "The mail server rejected the credentials. For Gmail this must be a "
                "16-character app password, not the account password.",
            ) from exc
        except (aiosmtplib.SMTPException, OSError, TimeoutError) as exc:
            raise ExternalServiceError("smtp", f"Could not deliver the email: {exc}") from exc

        log.info("mail.sent", to=message.to, subject=message.subject, via="smtp")

    async def aclose(self) -> None:
        return None
