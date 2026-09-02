"""The email invitation path, minus the database.

What is covered here is everything that decides whether a real person receives a link
that works: the URL the link is built from, the two rendered messages, the transport
choice, the delivery bookkeeping, and the rule that an invited account cannot be logged
into with a password.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.application.mailman import Mailman
from app.core.config import Settings
from app.core.security import has_usable_password, unusable_password, verify_password
from app.domain.ports import EmailMessage
from app.infrastructure.mail.console import ConsoleEmailSender
from app.infrastructure.mail.factory import build_email_sender
from app.modules.identity.service import display_name_from_email
from app.modules.notification.mail_templates import invitation_email, session_ready_email


@pytest.fixture
def mail_settings(tmp_path) -> Settings:
    return Settings(
        app_env="test",
        jwt_secret="test-secret-test-secret-test-secret",
        public_app_url="https://gd.example.com/",
        mail_outbox_dir=str(tmp_path / "outbox"),
    )


# ===================================================================== links
def test_the_invite_url_is_built_from_the_public_origin(mail_settings: Settings) -> None:
    # The trailing slash in the configured origin must not produce a double slash —
    # some mail clients silently mangle those, and the link 404s.
    assert mail_settings.invite_url("abc123") == "https://gd.example.com/invite/abc123"
    assert mail_settings.room_url("sess-1") == "https://gd.example.com/room/sess-1"


def test_email_is_off_by_default(mail_settings: Settings) -> None:
    # Invitations are delivered in-app. Mail is opt-in, and "off" is a real transport
    # rather than a branch somewhere in the enrollment service.
    assert mail_settings.mail_enabled is False
    assert mail_settings.mail_transport == "off"
    assert build_email_sender(mail_settings).name == "off"


def test_enabling_mail_without_a_host_selects_the_console_transport(
    mail_settings: Settings,
) -> None:
    enabled = mail_settings.model_copy(update={"mail_enabled": True})
    assert enabled.mail_transport == "console"
    assert build_email_sender(enabled).name == "console"


def test_configuring_a_host_selects_smtp(mail_settings: Settings) -> None:
    smtp = mail_settings.model_copy(update={"mail_enabled": True, "smtp_host": "smtp.gmail.com"})
    assert smtp.mail_transport == "smtp"


def test_the_sender_falls_back_to_the_smtp_username(mail_settings: Settings) -> None:
    configured = mail_settings.model_copy(update={"smtp_username": "host@example.com"})
    assert configured.mail_sender_address == "host@example.com"


# ===================================================================== rendering
def _invite(**overrides) -> EmailMessage:
    kwargs = {
        "to": "priya@example.com",
        "invitee_name": "Priya",
        "host_name": "Nadia",
        "topic_title": "Retrieval Augmented Generation",
        "topic_description": "Retrieval is the bottleneck.",
        "guiding_points": ["Chunking", "Evaluation"],
        "join_url": "https://gd.example.com/invite/tok-123",
        "expires_at": datetime.now(UTC) + timedelta(minutes=30),
    }
    return invitation_email(**{**kwargs, **overrides})


def test_the_join_link_appears_in_both_parts() -> None:
    message = _invite()
    # A client that strips HTML must still show a usable link, so the plain-text part
    # is not decoration — it is the fallback the whole invitation depends on.
    assert "https://gd.example.com/invite/tok-123" in message.html
    assert "https://gd.example.com/invite/tok-123" in message.text


def test_the_subject_names_the_host_and_the_topic() -> None:
    assert _invite().subject == "Nadia invited you to discuss Retrieval Augmented Generation"


def test_a_hostile_display_name_cannot_inject_markup() -> None:
    message = _invite(host_name='<script>alert("x")</script>')
    assert "<script>" not in message.html
    assert "&lt;script&gt;" in message.html


def test_the_ready_email_points_at_the_room() -> None:
    message = session_ready_email(
        to="priya@example.com",
        invitee_name="Priya",
        topic_title="MCP",
        room_url="https://gd.example.com/room/s1",
    )
    assert "https://gd.example.com/room/s1" in message.text
    assert "MCP" in message.subject


# ===================================================================== transport
def _outbox(settings: Settings) -> list[Path]:
    return sorted(Path(settings.mail_outbox_dir).glob("*.eml"))


async def test_the_console_transport_writes_a_readable_file(mail_settings: Settings) -> None:
    sender = ConsoleEmailSender(mail_settings)
    await sender.send(_invite())

    files = _outbox(mail_settings)
    assert len(files) == 1
    written = files[0].read_text(errors="replace")
    assert "priya@example.com" in written
    assert "invite/tok-123" in written


class _FlakySender:
    """Fails a fixed number of times, then succeeds."""

    name = "flaky"

    def __init__(self, failures: int) -> None:
        self.remaining = failures
        self.attempts = 0

    async def send(self, message: EmailMessage) -> None:
        self.attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("smtp said no")

    async def aclose(self) -> None:
        return None


async def test_a_transient_failure_is_retried(mail_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("app.application.mailman._BACKOFF_SECONDS", 0.0)
    sender = _FlakySender(failures=1)
    mailman = Mailman(sender=sender, settings=mail_settings)

    mailman.dispatch([_invite()])
    await asyncio.sleep(0.05)

    assert sender.attempts == 2


async def test_a_dead_transport_never_escapes_into_the_request(
    mail_settings: Settings, monkeypatch
) -> None:
    monkeypatch.setattr("app.application.mailman._BACKOFF_SECONDS", 0.0)
    sender = _FlakySender(failures=99)
    mailman = Mailman(sender=sender, settings=mail_settings)

    # dispatch() is called after the transaction has committed. If it could raise, a
    # classroom that was created successfully would report a 500 to its host.
    mailman.dispatch([_invite(), _invite()])
    await asyncio.sleep(0.05)

    assert sender.attempts == 4  # two messages, two attempts each


# ===================================================================== accounts
def test_an_invited_account_cannot_be_logged_into_with_a_password() -> None:
    sentinel = unusable_password()
    assert not has_usable_password(sentinel)
    # Not merely "wrong password" — there is no password that opens this account.
    assert verify_password("", sentinel) is False
    assert verify_password(sentinel, sentinel) is False


def test_every_invited_account_gets_its_own_sentinel() -> None:
    assert unusable_password() != unusable_password()


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("priya.sharma@example.com", "Priya Sharma"),
        ("arjun_menon@example.com", "Arjun Menon"),
        ("dev-patel@example.com", "Dev Patel"),
        ("sana@example.com", "Sana"),
    ],
)
def test_a_placeholder_name_is_guessed_from_the_address(email: str, expected: str) -> None:
    assert display_name_from_email(email) == expected


@pytest.mark.parametrize("email", ["@example.com", "...@example.com", "@", ""])
def test_the_guessed_name_is_never_empty(email: str) -> None:
    # The moderator says this name out loud. Nothing that reaches a seat may be blank.
    assert display_name_from_email(email).strip() != ""


def test_the_invitee_id_round_trips_through_the_mail_reference() -> None:
    from app.application.mailman import _invitation_ref

    invitation_id = uuid4()
    assert _invitation_ref(f"invitation:{invitation_id}") == invitation_id
    assert _invitation_ref("session-ready") is None
    assert _invitation_ref(None) is None
