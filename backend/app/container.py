"""Composition root.

Singletons — the things whose identity matters and which must live for the process —
are built once here at startup. Everything else (repositories, services, the unit of
work) is per-request and is assembled by FastAPI's ``Depends`` in ``app.api.deps``.

Keeping construction in one place is what makes the wiring reviewable: if you want to
know what the application is made of, this file is the answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.application.mailman import Mailman
from app.application.session_gateway import SessionDataGateway
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.infrastructure.ai.factory import AiProviders, build_providers
from app.infrastructure.mail.factory import build_email_sender
from app.modules.moderation.orchestrator import AIOrchestratorService
from app.modules.notification.event_bus import EventBus

log = get_logger(__name__)


@dataclass(slots=True)
class Container:
    settings: Settings
    bus: EventBus
    providers: AiProviders
    gateway: SessionDataGateway
    orchestrator: AIOrchestratorService
    mailman: Mailman

    async def aclose(self) -> None:
        await self.orchestrator.shutdown()
        # Mail last: a discussion ending can queue a final message, and shutting the
        # transport before it drains would drop it silently.
        await self.mailman.aclose()
        await self.providers.aclose()


_container: Container | None = None


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or get_settings()
    bus = EventBus()
    providers = build_providers(settings)
    gateway = SessionDataGateway(settings)
    orchestrator = AIOrchestratorService(
        providers=providers, bus=bus, gateway=gateway, settings=settings
    )
    mailman = Mailman(sender=build_email_sender(settings), settings=settings)
    log.info(
        "container.built",
        ai=providers.describe(),
        mail=mailman.transport,
        public_url=settings.public_app_url,
        env=settings.app_env,
    )
    return Container(
        settings=settings,
        bus=bus,
        providers=providers,
        gateway=gateway,
        orchestrator=orchestrator,
        mailman=mailman,
    )


def set_container(container: Container | None) -> None:
    global _container
    _container = container


def get_container() -> Container:
    if _container is None:  # pragma: no cover - a programming error, not a runtime state
        raise RuntimeError("The container has not been initialised.")
    return _container
