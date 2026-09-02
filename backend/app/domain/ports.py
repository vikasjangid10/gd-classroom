"""Ports — the abstract seams the inner layers own and the adapters implement.

Nothing in this file imports an SDK, a driver or a web framework. That is what allows a
test to run an entire discussion with three fakes and no network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

# --------------------------------------------------------------------- audio
#: Every provider in this system speaks the same audio dialect: signed 16-bit
#: little-endian PCM, mono. Sample rates differ by direction and are stated explicitly.
STT_SAMPLE_RATE = 16_000
TTS_SAMPLE_RATE = 48_000


# --------------------------------------------------------------------- STT
@dataclass(slots=True)
class Transcript:
    text: str
    is_final: bool
    confidence: float = 1.0
    speech_started: bool = False


TranscriptCallback = Callable[[Transcript], None]


@runtime_checkable
class SttStream(Protocol):
    """One live transcription socket, bound to one speaker."""

    async def send_audio(self, pcm: bytes) -> None: ...

    async def finalize(self) -> None:
        """Ask the provider to flush and emit a final transcript now."""

    async def close(self) -> None: ...


@runtime_checkable
class SttProvider(Protocol):
    name: str

    async def open(
        self,
        *,
        on_transcript: TranscriptCallback,
        sample_rate: int = STT_SAMPLE_RATE,
        language: str = "en",
    ) -> SttStream: ...


# --------------------------------------------------------------------- LLM
@dataclass(slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@runtime_checkable
class LlmProvider(Protocol):
    name: str

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 300,
    ) -> AsyncIterator[str]:
        """Yield token deltas. Cancelling the iterator must abort the upstream call."""
        ...

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int = 800,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class Assessment:
    """What the deep lane made of one contribution.

    A judgement, never a line anybody hears. It exists so the moderator's next move is
    informed by what was *said* rather than by how many words it took to say it — and it
    is deliberately small, because everything on it has to survive a model that is having
    an off day.
    """

    #: 0 = nothing was contributed, 5 = a full, supported point.
    substance: int
    #: Whether they built on the discussion or restarted it.
    engaged_with_prior: bool
    needs_follow_up: bool
    #: One of the reasons ``app.domain.turn_policy`` knows how to phrase a follow-up for.
    follow_up_reason: str
    #: One clause, for the closing report. Never read out during the discussion.
    note: str
    #: The rung that judged it, so a strange verdict can be traced to a model.
    tier: str = ""


@runtime_checkable
class DeepLane(Protocol):
    """The deep lane, as the moderator sees it: the two jobs that are judgement.

    Both go through a *structured* call rather than a plain completion. That is not a
    detail — the models worth putting on this lane are reasoning models, and asked for
    JSON in prose they will fence it, preface it, or think until the budget is gone.

    ``assess`` returns ``None`` rather than raising when it cannot answer in time or at
    all: a verdict is an *improvement* on the word-count heuristic, never a dependency of
    it, and a room must not fall silent because a judge was slow.
    """

    name: str

    async def assess(self, messages: list[ChatMessage]) -> Assessment | None: ...

    async def write_report(
        self, messages: list[ChatMessage], *, schema: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """The closing report. Returns ``(parsed, tier)``; ``None`` if it came back
        unusable, which is a bad answer rather than a bad provider."""
        ...


# --------------------------------------------------------------------- TTS
@runtime_checkable
class TtsProvider(Protocol):
    name: str
    sample_rate: int

    def stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield PCM16 mono chunks at ``sample_rate`` as they are synthesised."""
        ...


# --------------------------------------------------------------------- email
@dataclass(slots=True)
class EmailMessage:
    """One outgoing message, already rendered. Adapters only transport it."""

    to: str
    subject: str
    html: str
    text: str
    #: Groups a message with the thing it is about, so a failure can be reported
    #: against that row rather than only into the log.
    reference: str | None = None


@runtime_checkable
class EmailSender(Protocol):
    name: str

    async def send(self, message: EmailMessage) -> None:
        """Deliver, or raise. Returning normally means the server accepted the message."""
        ...

    async def aclose(self) -> None: ...


# --------------------------------------------------------------------- health
@runtime_checkable
class HealthCheckable(Protocol):
    async def healthy(self) -> bool: ...
