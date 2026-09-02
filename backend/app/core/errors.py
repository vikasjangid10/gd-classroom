"""Application exception hierarchy.

Every failure the API can produce is one of these. Handlers in ``app.main`` translate
them into the response envelope from ``app.core.responses`` — routers and services never
build an HTTP response by hand.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class. ``status`` and ``code`` are what the client actually sees."""

    status: int = 500
    code: str = "internal_error"
    message: str = "Something went wrong."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        super().__init__(self.message)


# --------------------------------------------------------------------- 4xx
class ValidationError(AppError):
    status = 422
    code = "validation_error"
    message = "The request body failed validation."


class AuthenticationError(AppError):
    status = 401
    code = "unauthenticated"
    message = "Authentication is required."


class TokenExpiredError(AuthenticationError):
    code = "token_expired"
    message = "The credential has expired."


class AuthorizationError(AppError):
    status = 403
    code = "forbidden"
    message = "You are not allowed to do that."


class NotFoundError(AppError):
    status = 404
    code = "not_found"
    message = "The resource does not exist."


class ConflictError(AppError):
    status = 409
    code = "conflict"
    message = "The resource is not in a state that allows this."


class RateLimitError(AppError):
    status = 429
    code = "rate_limited"
    message = "Too many requests."


# --------------------------------------------------------------------- domain
class DomainError(AppError):
    """A business rule said no. Almost always a 409."""

    status = 409
    code = "domain_error"


class IllegalTransitionError(DomainError):
    code = "illegal_transition"

    def __init__(self, entity: str, current: object, event: object) -> None:
        super().__init__(
            f"{entity} cannot handle '{event}' while in '{current}'.",
            details={"entity": entity, "current_state": str(current), "event": str(event)},
        )


class ClassroomNotReadyError(DomainError):
    code = "classroom_not_ready"
    message = "The discussion needs exactly four accepted participants."


class SeatsFullError(DomainError):
    code = "seats_full"
    message = "This classroom already has all of its participants."


class InvitationClosedError(DomainError):
    code = "invitation_closed"
    message = "This invitation has already been answered or has expired."


class FloorViolationError(DomainError):
    code = "floor_violation"
    message = "You do not currently hold the floor."


# --------------------------------------------------------------------- 5xx
class ExternalServiceError(AppError):
    status = 503
    code = "provider_unavailable"
    message = "An upstream AI provider is unavailable."

    def __init__(self, provider: str, message: str | None = None) -> None:
        super().__init__(message or f"{provider} is unavailable.", details={"provider": provider})
        self.provider = provider


class ProviderTimeoutError(ExternalServiceError):
    code = "provider_timeout"


class CapacityError(AppError):
    status = 503
    code = "at_capacity"
    message = "This node cannot host another live discussion right now."
