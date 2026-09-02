"""Structured logging.

Every log line carries whatever is bound to the context — ``request_id``,
``session_id``, ``user_id`` — so a single discussion can be pulled out of a busy node
with one filter. In development the renderer is human-readable; in production it is JSON.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.core.config import settings

__all__ = ["bind_contextvars", "clear_contextvars", "configure_logging", "get_logger"]


def configure_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    for noisy in ("aioice", "aiortc", "sqlalchemy.engine", "httpx", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    processors.append(structlog.processors.format_exc_info)
    if settings.log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        # Plain tracebacks, deliberately. ConsoleRenderer's default pretty formatter
        # renders every frame's locals, which on a deep ASGI stack took over a minute
        # per exception — long enough that the request looked hung rather than failed.
        processors.append(
            structlog.dev.ConsoleRenderer(
                colors=False, exception_formatter=structlog.dev.plain_traceback
            )
        )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
