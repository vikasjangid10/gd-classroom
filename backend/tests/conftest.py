"""Test doubles.

The point of the ports in ``app.domain.ports`` is that a whole discussion can be driven
here with no network, no database and no audio device — in milliseconds.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.config import Settings
from app.domain.enums import EndReason, SessionStatus
from app.domain.prompts import TopicBrief
from app.domain.session import DiscussionSession
from app.domain.turn_policy import DiscussionBudget
from app.infrastructure.ai.factory import AiProviders
from app.infrastructure.ai.fake import FakeLlmProvider, FakeSttProvider, FakeTtsProvider
from app.modules.notification.event_bus import EventBus


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        ai_provider="fake",
        allow_text_input=True,
        session_join_window_seconds=2,
        # The host's pause before replying is real behaviour, not a detail — but it is
        # wall-clock, so it is switched off here and tested on its own. Same for the
        # windows a participant gets to start speaking.
        moderator_think_seconds=0.0,
        # Long enough not to fire during a test that is about something else: `settle()`
        # advances well under a second. Tests that are *about* the silence path shorten
        # these themselves.
        silence_before_speaking_seconds=5.0,
        silence_after_nudge_seconds=5.0,
        turn_max_seconds=5,
        discussion_target_seconds=1,
        discussion_max_seconds=30,
        min_turns_per_participant=1,
        jwt_secret="test-secret-test-secret-test-secret",
    )


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def providers() -> AiProviders:
    return AiProviders(
        stt=FakeSttProvider(),
        llm=FakeLlmProvider(seed=7, delay=0.0),
        tts=FakeTtsProvider(),
    )


# ===================================================================== doubles
@dataclass
class FakeMixer:
    floor_holder: UUID | None = None
    moderator: bytearray = field(default_factory=bytearray)

    def clear_moderator(self) -> None:
        self.moderator.clear()

    def moderator_backlog_ms(self) -> int:
        return 0


@dataclass
class FakeVoicePlane:
    """Records what the runner asked the audio layer to do."""

    session_id: UUID
    mixer: FakeMixer = field(default_factory=FakeMixer)
    granted: list[UUID] = field(default_factory=list)
    releases: int = 0
    spoken: list[str] = field(default_factory=list)
    flushes: int = 0
    closed: bool = False

    async def grant_floor(self, user_id: UUID) -> None:
        self.mixer.floor_holder = user_id
        self.granted.append(user_id)

    async def release_floor(self) -> None:
        self.mixer.floor_holder = None
        self.releases += 1

    async def flush_stt(self) -> None:
        self.flushes += 1

    async def speak(self, sentences: AsyncIterator[str], on_started=None) -> None:
        async for sentence in sentences:
            self.spoken.append(sentence)

    async def wait_until_silent(self, *, timeout: float = 60.0) -> None:
        await asyncio.sleep(0)

    def interrupt(self) -> None:
        self.mixer.clear_moderator()

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakePersistence:
    """Captures the handful of writes a session is allowed to make."""

    active: list[UUID] = field(default_factory=list)
    statuses: list[tuple[UUID, SessionStatus, EndReason | None]] = field(default_factory=list)
    flushed: list[int] = field(default_factory=list)
    summaries: list[dict[str, Any] | None] = field(default_factory=list)
    classrooms_completed: list[bool] = field(default_factory=list)

    async def mark_active(self, session_id: UUID) -> None:
        self.active.append(session_id)

    async def mark_status(
        self, session_id: UUID, status: SessionStatus, *, end_reason: EndReason | None = None
    ) -> None:
        self.statuses.append((session_id, status, end_reason))

    async def flush_transcript(self, session: DiscussionSession) -> None:
        self.flushed.append(len(session.buffered_turns))

    async def save_summary(
        self, session_id: UUID, *, summary: dict | None, model: str, error: str | None = None
    ) -> None:
        self.summaries.append(summary)

    async def complete_classroom(self, session_id: UUID, *, aborted: bool) -> None:
        self.classrooms_completed.append(aborted)


@pytest.fixture
def live_session() -> DiscussionSession:
    session = DiscussionSession(
        session_id=uuid4(),
        classroom_id=uuid4(),
        topic=TopicBrief(
            title="Retrieval Augmented Generation",
            description="Retrieval is the bottleneck.",
            guiding_points=("Chunking", "Evaluation"),
        ),
        budget=DiscussionBudget(
            target_seconds=1,
            max_seconds=30,
            min_turns_per_participant=1,
            turn_max_seconds=5,
        ),
    )
    for index, name in enumerate(["Priya", "Arjun", "Meera", "Dev"], start=1):
        session.add_participant(uuid4(), name, index)
    return session


@pytest.fixture
def plane(live_session: DiscussionSession) -> FakeVoicePlane:
    return FakeVoicePlane(session_id=live_session.session_id)


@pytest.fixture
def persistence() -> FakePersistence:
    return FakePersistence()
