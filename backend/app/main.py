"""ASGI application: middleware, error translation, lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import install_error_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.router import api_router
from app.container import build_container, get_container, set_container
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.engine import dispose_engine
from app.workers.janitor import Janitor

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    container = build_container(settings)
    set_container(container)

    janitor = Janitor(settings=settings, bus=container.bus, orchestrator=container.orchestrator)
    janitor.start()
    await container.providers.warm()
    log.info("app.started", env=settings.app_env, api=settings.api_prefix)

    try:
        yield
    finally:
        # Order matters: stop new sweeps, drain live discussions, then close the pool.
        await janitor.stop()
        await container.aclose()
        await dispose_engine()
        set_container(None)
        log.info("app.stopped")


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="Voice-first, AI-moderated group discussions.",
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


app.add_middleware(RequestContextMiddleware, expose_internals=not settings.is_production)

install_error_handlers(app)
app.include_router(api_router)


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "name": settings.app_name,
        "docs": "/docs",
        "api": settings.api_prefix,
        "live_sessions": get_container().orchestrator.live_count,
    }
