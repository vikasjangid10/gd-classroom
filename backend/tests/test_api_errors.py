"""Every failure a client can see comes back in the same envelope — and comes back.

The last test here is the one that matters most: an exception nobody predicted must
still produce a response. A hung request is worse than a failed one.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from app.api.errors import install_error_handlers
from app.api.middleware import RequestContextMiddleware
from app.core.errors import (
    AuthorizationError,
    ClassroomNotReadyError,
    ExternalServiceError,
    NotFoundError,
)


class Body(BaseModel):
    count: int


def build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware, expose_internals=False)
    install_error_handlers(app)

    @app.get("/missing")
    async def missing() -> None:
        raise NotFoundError("No such classroom.")

    @app.get("/forbidden")
    async def forbidden() -> None:
        raise AuthorizationError()

    @app.get("/not-ready")
    async def not_ready() -> None:
        raise ClassroomNotReadyError("2 of 4 participants have accepted so far.")

    @app.get("/provider-down")
    async def provider_down() -> None:
        raise ExternalServiceError("deepgram")

    @app.post("/validated")
    async def validated(body: Body) -> dict:
        return {"data": body.count}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("a bug nobody predicted")

    return app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=build_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_not_found_uses_the_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/missing")
    assert response.status_code == 404
    body = response.json()["error"]
    assert body["code"] == "not_found"
    assert body["message"] == "No such classroom."


async def test_authorization_failures_are_403_not_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/forbidden")).status_code == 403


async def test_domain_rules_answer_409_with_a_usable_message(client: httpx.AsyncClient) -> None:
    response = await client.get("/not-ready")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "classroom_not_ready"
    assert "2 of 4" in response.json()["error"]["message"]


async def test_a_dead_provider_is_503_and_names_itself(client: httpx.AsyncClient) -> None:
    response = await client.get("/provider-down")
    assert response.status_code == 503
    assert response.json()["error"]["details"]["provider"] == "deepgram"


async def test_validation_errors_name_the_field(client: httpx.AsyncClient) -> None:
    response = await client.post("/validated", json={"count": "twelve"})
    assert response.status_code == 422
    fields = response.json()["error"]["details"]["fields"]
    assert fields[0]["field"] == "count"


async def test_an_unexpected_exception_still_answers_and_leaks_nothing(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/boom")
    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "internal_error"
    assert "a bug nobody predicted" not in body["message"]


async def test_unknown_routes_use_the_envelope_too(client: httpx.AsyncClient) -> None:
    response = await client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
