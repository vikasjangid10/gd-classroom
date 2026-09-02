"""Tokens, tickets and password hashing."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.errors import AuthenticationError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    create_ticket,
    decode_token,
    hash_password,
    hash_token,
    read_ticket,
    verify_password,
)


def test_password_round_trip() -> None:
    hashed = hash_password("Password123!")
    assert hashed != "Password123!"
    assert verify_password("Password123!", hashed)
    assert not verify_password("Password123", hashed)


def test_a_corrupt_hash_fails_closed() -> None:
    assert verify_password("anything", "not-a-bcrypt-hash") is False


def test_token_kinds_are_not_interchangeable() -> None:
    user_id = uuid4()
    access = create_access_token(user_id, "PARTICIPANT")

    assert decode_token(access, expected="access")["sub"] == str(user_id)
    with pytest.raises(AuthenticationError):
        decode_token(access, expected="refresh")


def test_a_refresh_token_carries_its_family() -> None:
    token = create_refresh_token(uuid4(), "fam123")
    assert decode_token(token, expected="refresh")["fam"] == "fam123"


def test_tickets_are_bound_to_one_session_and_one_scope() -> None:
    user_id, session_id = uuid4(), uuid4()
    ticket = create_ticket(user_id, session_id, "sse")

    assert read_ticket(ticket, scope="sse") == (user_id, session_id)
    with pytest.raises(AuthenticationError):
        read_ticket(ticket, scope="rtc")


def test_stored_tokens_are_digests_not_plaintext() -> None:
    raw = "super-secret-refresh-token"
    digest = hash_token(raw)
    assert digest != raw and len(digest) == 64
    assert hash_token(raw) == digest


def test_a_tampered_token_is_rejected() -> None:
    token = create_access_token(uuid4(), "SUPER_USER")
    tampered = token[:-2] + ("ab" if not token.endswith("ab") else "cd")
    with pytest.raises(AuthenticationError):
        decode_token(tampered, expected="access")
