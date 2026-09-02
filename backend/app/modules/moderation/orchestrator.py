"""The AI Orchestrator — the central coordinator.

It owns the registry of live sessions, and it is the only component that holds both the
voice plane and the moderator policy at the same time. Everything else in the
application talks to a discussion through this object.

A session lives on exactly one node. There is no shared session store, and there is no
attempt to migrate a session between processes: if a node dies its discussions die with
it, which is an acceptable failure for a twenty-minute conversation and removes an
enormous amount of machinery.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.core.errors import CapacityError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.domain.enums import EndReason, SessionStatus
from app.domain.events import DomainEvent, EventType, session_topic
from app.domain.ports import Transcript
from app.infrastructure.ai.factory import AiProviders
from app.modules.moderation.commands import (
    EndSessionRequested,
    FloorReleased,
    ParticipantConnected,
    ParticipantDisconnected,
    SpeechStarted,
    TranscriptPartial,
    UtteranceFinal,
)
from app.modules.moderation.protocols import SessionStore
from app.modules.moderation.runner import SessionRunner
from app.modules.notification.event_bus import EventBus
from app.modules.voice.plane import VoicePlane
from app.modules.voice.speech import SpeechClip

log = get_logger(__name__)

MAX_LIVE_SESSIONS = 25


class LiveSession:
    __slots__ = ("plane", "runner", "task")

    def __init__(self, runner: SessionRunner, plane: VoicePlane, task: asyncio.Task) -> None:
        self.runner = runner
        self.plane = plane
        self.task = task


class AIOrchestratorService:
    def __init__(
        self,
        *,
        providers: AiProviders,
        bus: EventBus,
        gateway: SessionStore,
        settings: Settings,
    ) -> None:
        self._providers = providers
        self._bus = bus
        self._gateway = gateway
        self._settings = settings
        self._live: dict[UUID, LiveSession] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}

    # ================================================================ lifecycle
    def _lock_for(self, session_id: UUID) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def ensure(self, session_id: UUID) -> LiveSession:
        """Materialise the session on first contact. Idempotent and race-free."""
        if (live := self._live.get(session_id)) is not None:
            return live

        async with self._lock_for(session_id):
            if (live := self._live.get(session_id)) is not None:
                return live
            if len(self._live) >= MAX_LIVE_SESSIONS:
                raise CapacityError()

            session = await self._gateway.load_live_session(session_id)

            plane = VoicePlane(
                session_id=session_id,
                stt_provider=self._providers.stt,
                tts_provider=self._providers.tts,
                on_transcript=lambda user_id, t: self._on_transcript(session_id, user_id, t),
                on_connection=lambda user_id, up: self._on_connection(session_id, user_id, up),
                on_clip=lambda clip: self._on_clip(session_id, clip),
            )
            runner = SessionRunner(
                session=session,
                plane=plane,
                providers=self._providers,
                bus=self._bus,
                persistence=self._gateway,
                settings=self._settings,
            )
            task = runner.start()
            task.add_done_callback(lambda _: self._live.pop(session_id, None))

            live = LiveSession(runner, plane, task)
            self._live[session_id] = live
            log.info("orchestrator.session_started", session=str(session_id), live=len(self._live))
            return live

    def get(self, session_id: UUID) -> LiveSession | None:
        return self._live.get(session_id)

    def require(self, session_id: UUID) -> LiveSession:
        live = self._live.get(session_id)
        if live is None:
            raise NotFoundError("That discussion is not running.")
        return live

    @property
    def live_count(self) -> int:
        return len(self._live)

    # ================================================================ producers
    def _on_transcript(self, session_id: UUID, user_id: UUID, transcript: Transcript) -> None:
        live = self._live.get(session_id)
        if live is None:
            return
        if transcript.speech_started:
            live.runner.submit(SpeechStarted(user_id))
        elif transcript.is_final:
            live.runner.submit(UtteranceFinal(user_id, transcript.text))
        elif transcript.text:
            live.runner.submit(TranscriptPartial(user_id, transcript.text))

    def _on_clip(self, session_id: UUID, clip: SpeechClip) -> None:
        """Announce a synthesised sentence so browsers without a microphone can play it."""
        self._bus.publish(
            DomainEvent(
                topic=session_topic(session_id),
                type=EventType.MODERATOR_AUDIO,
                payload={
                    "clip_id": clip.id,
                    "duration_ms": clip.duration_ms,
                    "text": clip.text,
                },
            )
        )

    def speech_clip(self, session_id: UUID, clip_id: str) -> SpeechClip:
        live = self._live.get(session_id)
        clip = live.plane.speech.get(clip_id) if live else None
        if clip is None:
            # Also the normal outcome for a clip that has aged out of the cache, which
            # is why the client treats a 404 here as "skip it", not as an error.
            raise NotFoundError("That audio is no longer available.")
        return clip

    def _on_connection(self, session_id: UUID, user_id: UUID, connected: bool) -> None:
        live = self._live.get(session_id)
        if live is None:
            return
        live.runner.submit(
            ParticipantConnected(user_id) if connected else ParticipantDisconnected(user_id)
        )

    # ================================================================ commands in
    async def negotiate(
        self, session_id: UUID, user_id: UUID, sdp: str, sdp_type: str
    ) -> dict[str, str]:
        live = await self.ensure(session_id)
        return await live.plane.add_peer(user_id, sdp, sdp_type)

    async def leave(self, session_id: UUID, user_id: UUID) -> None:
        """The explicit "I am leaving" action — not a rejoin, a genuine departure.

        ``plane.remove_peer`` only reports a disconnect when a WebRTC peer existed and
        was connected, so it is a silent no-op for anyone who joined by text, or who
        accepted their invitation and left before ever connecting anything. Left
        unreported, they stay "eligible" forever: nothing else ever runs to mark them
        absent, because no peer connection ever existed for anything to notice dropping
        — which is how the moderator ends up asking questions to an empty seat.

        Submitted unconditionally, even when ``remove_peer`` also reports one moments
        earlier for a WebRTC leaver: ``mark_disconnected`` is idempotent, and a
        redundant notice costs a repeated state update, never a wrong one.
        """
        live = self._live.get(session_id)
        if live is None:
            return
        await live.plane.remove_peer(user_id)
        live.runner.submit(ParticipantDisconnected(user_id))

    def release_floor(self, session_id: UUID, user_id: UUID) -> None:
        self.require(session_id).runner.submit(FloorReleased(user_id))

    async def join_without_audio(self, session_id: UUID, user_id: UUID) -> None:
        """Take part with a keyboard instead of a microphone.

        Same command the WebRTC connection-state handler produces, so the moderator
        cannot tell the difference — which is exactly the point. It keeps the product
        usable without a working mic, and it lets the whole discussion be tested
        headlessly.
        """
        if not self._settings.allow_text_input:
            raise ConflictError("Text participation is disabled on this deployment.")
        live = await self.ensure(session_id)
        live.runner.submit(ParticipantConnected(user_id))

    async def submit_text_turn(self, session_id: UUID, user_id: UUID, text: str) -> None:
        """Development affordance: inject a turn as though it had been spoken.

        Enabled by ``ALLOW_TEXT_INPUT``. It is the same command the STT callback
        produces, so it exercises the real moderator path — not a parallel one.
        """
        if not self._settings.allow_text_input:
            raise ConflictError("Text input is disabled on this deployment.")
        live = await self.ensure(session_id)
        live.runner.submit(SpeechStarted(user_id))
        await asyncio.sleep(0)
        live.runner.submit(UtteranceFinal(user_id, text))

    async def end(self, session_id: UUID, reason: EndReason = EndReason.HOST_ENDED) -> None:
        """End a discussion. Works whether or not a runner is still holding it.

        A session with no runner is not a no-op to be ignored: it is provisioned but
        never joined, or its process restarted underneath it, and either way it is still
        holding every one of its participants out of every other classroom. The janitor
        does eventually sweep it — after fifteen minutes for an unjoined session and
        ninety for one that was running — which is a long time to tell a host that the
        End button did nothing. Ending it here is the same operation the janitor does,
        just now and on purpose.
        """
        live = self._live.get(session_id)
        if live is not None:
            live.runner.submit(EndSessionRequested(reason))
            return

        await self._gateway.mark_status(session_id, SessionStatus.ABORTED, end_reason=reason)
        await self._gateway.complete_classroom(session_id, aborted=True)
        log.info("orchestrator.ended_without_runner", session=str(session_id), reason=reason.value)

    # ================================================================ read model
    def snapshot(self, session_id: UUID) -> dict[str, Any] | None:
        """The live view a client needs when it (re)opens the room."""
        live = self._live.get(session_id)
        if live is None:
            return None
        session = live.runner.session
        return {
            "status": session.status.value,
            "moderator_state": session.moderator_state.value,
            "floor_holder": str(session.floor_holder) if session.floor_holder else None,
            "turn_index": session.turn_index,
            "elapsed_seconds": session.elapsed_seconds,
            "connected": [
                str(p.user_id) for p in session.participants.values() if p.is_present
            ],
            "speaking_time": session.ledger.snapshot(),
        }

    # ================================================================ shutdown
    async def shutdown(self) -> None:
        """Graceful drain: ask every discussion to close, then stop waiting."""
        if not self._live:
            return
        log.info("orchestrator.shutdown", live=len(self._live))
        for live in list(self._live.values()):
            live.runner.submit(EndSessionRequested(EndReason.HOST_ENDED))

        tasks = [live.task for live in list(self._live.values())]
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=25
            )
        for live in list(self._live.values()):
            live.task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await live.task
        self._live.clear()

    async def abort_stale(self, session_id: UUID) -> None:
        """Called by the janitor for sessions this node no longer has in memory."""
        if session_id in self._live:
            return
        await self._gateway.mark_status(
            session_id, SessionStatus.ABORTED, end_reason=EndReason.FATAL_ERROR
        )
        await self._gateway.complete_classroom(session_id, aborted=True)
