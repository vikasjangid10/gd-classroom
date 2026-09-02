"""Translation of every exception into the error envelope.

Kept out of ``main`` so the whole ladder can be exercised in a test against a throwaway
app — including the "something we never predicted" branch, which is exactly the one that
never gets tested otherwise.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError
from app.core.logging import get_logger

log = get_logger(__name__)

_HTTP_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    429: "rate_limited",
}


def error_response(
    request: Request,
    *,
    status: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "trace_id": getattr(request.state, "request_id", None),
            }
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    """Handlers for the failures we predicted.

    There is deliberately no handler for bare ``Exception`` here. Anything unforeseen is
    caught by ``RequestContextMiddleware``, which owns the guarantee that every request
    gets an answer — see the note there.
    """

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status >= 500:
            log.error("app_error", code=exc.code, message=exc.message, details=exc.details)
        else:
            log.info("app_error", code=exc.code, message=exc.message)
        return error_response(
            request, status=exc.status, code=exc.code, message=exc.message, details=exc.details
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"field": ".".join(str(part) for part in err["loc"][1:]), "problem": err["msg"]}
            for err in exc.errors()
        ]
        return error_response(
            request,
            status=422,
            code="validation_error",
            message="Some fields need attention.",
            details={"fields": fields},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return error_response(
            request,
            status=exc.status_code,
            code=_HTTP_CODES.get(exc.status_code, "http_error"),
            message=str(exc.detail),
        )
