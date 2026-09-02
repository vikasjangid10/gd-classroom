"""Password hashing and JWT minting / verification.

Three token kinds share one signer but never share a purpose:

* ``access``  — 15 min bearer, sent in ``Authorization``.
* ``refresh`` — 7 day, httpOnly cookie, rotating with reuse detection.
* ``ticket``  — 60 s, single-purpose, session-scoped. Exists because ``EventSource``
  cannot set headers and an SDP offer arrives before any media does; a ticket in a URL
  is far less dangerous than an access token in a URL.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
import jwt

from app.core.config import settings
from app.core.errors import AuthenticationError, TokenExpiredError

TokenKind = Literal["access", "refresh", "ticket"]
#: ``sse``/``rtc`` are session-scoped; ``user`` is the lobby stream, scoped to the caller.
TicketScope = Literal["sse", "rtc", "user"]


# --------------------------------------------------------------------- passwords
#: Marks an account that has no password at all — someone the host invited by email, who
#: authenticates by magic link. A sentinel rather than a nullable column: every password
#: check goes through ``verify_password``, so there is exactly one place to get it right.
UNUSABLE_PASSWORD_PREFIX = "!"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def unusable_password() -> str:
    return f"{UNUSABLE_PASSWORD_PREFIX}invited-{secrets.token_hex(8)}"


def has_usable_password(hashed: str) -> bool:
    return not hashed.startswith(UNUSABLE_PASSWORD_PREFIX)


def verify_password(plain: str, hashed: str) -> bool:
    if not has_usable_password(hashed):
        return False
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


def hash_token(raw: str) -> str:
    """Refresh tokens and invitation tokens are stored as digests, never in the clear."""
    return hashlib.sha256(raw.encode()).hexdigest()


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# --------------------------------------------------------------------- jwt
def _encode(payload: dict[str, Any], ttl_seconds: int, kind: TokenKind) -> str:
    now = datetime.now(UTC)
    claims = {
        **payload,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "jti": uuid.uuid4().hex,
        "typ": kind,
        "iss": settings.app_name,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, *, expected: TokenKind) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.app_name,
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError() from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("The credential could not be verified.") from exc

    if claims.get("typ") != expected:
        raise AuthenticationError(f"Expected a {expected} token.")
    return claims


def create_access_token(user_id: uuid.UUID, role: str) -> str:
    return _encode({"sub": str(user_id), "role": role}, settings.access_token_ttl_seconds, "access")


def create_refresh_token(user_id: uuid.UUID, family_id: str) -> str:
    return _encode(
        {"sub": str(user_id), "fam": family_id},
        settings.refresh_token_ttl_seconds,
        "refresh",
    )


def create_ticket(user_id: uuid.UUID, session_id: uuid.UUID, scope: TicketScope) -> str:
    return _encode(
        {"sub": str(user_id), "sid": str(session_id), "scope": scope},
        settings.ticket_ttl_seconds,
        "ticket",
    )


def read_ticket(token: str, *, scope: TicketScope) -> tuple[uuid.UUID, uuid.UUID]:
    """Return ``(user_id, session_id)`` for a ticket, or raise."""
    claims = decode_token(token, expected="ticket")
    if claims.get("scope") != scope:
        raise AuthenticationError("This ticket is not valid for that stream.")
    return uuid.UUID(claims["sub"]), uuid.UUID(claims["sid"])
