"""Version 1 of the public API, assembled from the module routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, classrooms, health, invitations, sessions, users
from app.core.config import settings

api_router = APIRouter(prefix=settings.api_prefix)

api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(classrooms.router)
api_router.include_router(invitations.router)
api_router.include_router(sessions.router)
