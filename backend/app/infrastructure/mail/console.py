"""The transport used when no SMTP host is configured.

It writes each message to disk as a .eml file and logs the join link at INFO. That
keeps the flow runnable — and testable — without credentials, while making it obvious
in the log that nothing left the machine.
"""

from __future__ import annotations

import asyncio
import re
from email.message import EmailMessage as MimeMessage
from pathlib import Path

from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.ports import EmailMessage

log = get_logger(__name__)

_LINK = re.compile(r"https?://\S+/invite/[\w\-]+")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class ConsoleEmailSender:
    name = "console"

    def __init__(self, settings: Settings) -> None:
        self._dir = Path(settings.mail_outbox_dir)
        self._settings = settings
        self._seq = 0

    async def send(self, message: EmailMessage) -> None:
        self._seq += 1
        mime = MimeMessage()
        mime["From"] = self._settings.mail_sender_address
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")

        path = self._dir / f"{self._seq:04d}-{_UNSAFE.sub('_', message.to)}.eml"
        try:
            # Off the event loop. Small file, fast disk, usually — but "usually" is how
            # you end up with a stalled discussion because someone's volume was slow.
            await asyncio.to_thread(self._write, path, mime.as_bytes())
        except OSError as exc:  # pragma: no cover - a full or read-only disk
            log.warning("mail.outbox_write_failed", path=str(path), error=str(exc))

        link = _LINK.search(message.text)
        log.info(
            "mail.sent",
            to=message.to,
            subject=message.subject,
            via="console",
            file=str(path),
            join_link=link.group(0) if link else None,
        )

    def _write(self, path: Path, payload: bytes) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    async def aclose(self) -> None:
        return None
