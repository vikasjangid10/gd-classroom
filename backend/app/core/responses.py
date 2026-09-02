"""The single response envelope used by every endpoint.

Success:  {"data": ..., "meta": {...}}
Failure:  {"error": {"code", "message", "details", "trace_id"}}

Keeping this in one place means a client only ever writes one unwrapping function and
one error handler.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Meta(BaseModel):
    request_id: str | None = None
    next_cursor: str | None = None
    has_more: bool | None = None
    total: int | None = None


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: Meta = Field(default_factory=Meta)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


def ok(data: Any, **meta: Any) -> dict[str, Any]:
    return {"data": data, "meta": {k: v for k, v in meta.items() if v is not None}}


def page(items: list[Any], *, next_cursor: str | None, has_more: bool) -> dict[str, Any]:
    return ok(items, next_cursor=next_cursor, has_more=has_more)
