"""Offline provider implementations.

These are not stubs that return empty strings — they are the reason the whole product
can be run, demonstrated and tested with no API keys, no network and no microphone.
Every one of them honours the same Protocol as its live counterpart, including
streaming and cancellation, so switching to ``AI_PROVIDER=live`` changes nothing but the
constructor.
"""

from __future__ import annotations

import array
import asyncio
import json
import math
import random
import re
import struct
from collections.abc import AsyncIterator

try:  # ``audioop`` is a C module removed in Python 3.13
    import audioop  # type: ignore[import-not-found,unused-ignore]
except ImportError:  # pragma: no cover - exercised only on 3.13+
    from app.modules.voice import _audioop_shim as audioop  # type: ignore[no-redef]

from app.core.logging import get_logger
from app.domain.ports import (
    STT_SAMPLE_RATE,
    TTS_SAMPLE_RATE,
    ChatMessage,
    Transcript,
    TranscriptCallback,
)

log = get_logger(__name__)


# ===================================================================== LLM
_OPENERS = (
    "Welcome, everyone.",
    "Good to have you all here.",
    "Let's get started.",
)

_ANSWER_SEEDS = (
    "I'd start from the retrieval side, because that's where most of the failure modes live.",
    "In production the bottleneck is usually the boundary between components, not the model.",
    "My experience is that the simple version works until you hit concurrency, then it doesn't.",
    "There's a trade-off here between latency and correctness that nobody likes talking about.",
    "I'd push back slightly on that — it depends heavily on the size of the workload.",
)


class FakeLlmProvider:
    """A rule-based moderator.

    It reads the instruction the ``PromptBuilder`` produced and answers in character.
    Deterministic given a seed, which is what makes moderator behaviour assertable in
    tests without mocking a network call.
    """

    name = "fake-llm"

    def __init__(self, *, seed: int | None = None, delay: float = 0.02) -> None:
        self._rng = random.Random(seed)
        self._delay = delay

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _last_user(messages: list[ChatMessage]) -> str:
        for message in reversed(messages):
            if message.role == "user":
                return message.content
        return ""

    @staticmethod
    def _target_name(instruction: str) -> str:
        match = re.search(
            r"(?:floor to|addressed to|Hand the floor to) ([A-Z][\w'-]*)", instruction
        )
        if match:
            return match.group(1)
        match = re.search(r"([A-Z][\w'-]*) (?:just said|has gone quiet)", instruction)
        return match.group(1) if match else "everyone"

    @staticmethod
    def _topic_title(instruction: str) -> str:
        match = re.search(r"Topic: (.+)", instruction)
        return match.group(1).strip() if match else "today's topic"

    def _compose(self, messages: list[ChatMessage]) -> str:
        full = self._last_user(messages)
        system = self._last_system(messages)
        topic = self._topic_title(full)

        # Route on the system message first. The user message can contain a transcript,
        # and a transcript quotes the moderator's own earlier lines — matching against
        # the whole body would make the fake answer the wrong prompt.
        if "post-discussion reports" in system:
            return self._report(full)
        if "running summary" in system:
            return self._fold(full)

        # The actual instruction is always the tail of the user message.
        instruction = full[-500:]
        who = self._target_name(instruction)

        if "Open the session" in instruction:
            # Search the whole message: the roster line sits above the instruction tail.
            names = re.search(r"Participants: (.+?)\.\s", full)
            roster = names.group(1) if names else "everyone"
            return (
                f"{self._rng.choice(_OPENERS)} {roster} — today we are discussing {topic}. "
                "It is worth getting into because almost every team runs into it, and almost "
                "every team solves it differently."
            )

        if "ground rules" in instruction:
            return (
                "A few ground rules. One person speaks at a time, and I will say whose turn "
                "it is. You have up to ninety seconds, and I will make sure everybody gets a "
                "comparable share. Let's begin with our first speaker."
            )

        if "one short follow-up" in instruction or "Ask them" in instruction:
            return f"{who}, could you make that concrete — what would that look like in practice?"

        if "has gone quiet" in instruction:
            return f"{who}, would you like a moment, or shall I move on and come back to you?"

        if "Close the discussion" in instruction:
            return (
                "That is where we will stop. Thank you all — the strongest idea today was that "
                f"{topic} is really a question of trade-offs rather than of tooling."
            )

        # Default: a question to the named participant.
        return (
            f"{who}, let's bring you in. Where do you think the real difficulty in {topic} "
            "actually sits, and why?"
        )

    @staticmethod
    def _last_system(messages: list[ChatMessage]) -> str:
        for message in messages:
            if message.role == "system":
                return message.content
        return ""

    @staticmethod
    def _fold(instruction: str) -> str:
        lines = [
            line.strip()
            for line in instruction.splitlines()
            if ":" in line and not line.startswith(("Existing", "New"))
        ]
        joined = " ".join(lines[-6:])
        return (f"So far: {joined}")[:1500]

    #: Line prefixes in the prompt that look like a speaker but are not one.
    _NOT_SPEAKERS = frozenset({"Moderator", "Topic", "Participants", "Angles", "Summary"})

    def _report(self, instruction: str) -> str:
        names = sorted(set(re.findall(r"^([A-Z][\w'-]*):", instruction, flags=re.MULTILINE)))
        names = [n for n in names if n not in self._NOT_SPEAKERS] or ["The group"]
        return json.dumps(
            {
                "headline": "A practical discussion about trade-offs rather than tools.",
                "key_points": [
                    "The group agreed the hard part is the boundary between components.",
                    "Latency and correctness were repeatedly framed as a trade-off.",
                    "Simple designs were preferred until concurrency forces a change.",
                ],
                "per_participant": [
                    {
                        "name": name,
                        "contribution": f"{name} argued from concrete production experience.",
                        "strength": "Grounded examples",
                    }
                    for name in names
                ],
                "open_questions": [
                    "What does the failure mode look like at ten times the load?",
                ],
            }
        )

    # ---------------------------------------------------------------- port
    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 300,
    ) -> AsyncIterator[str]:
        text = self._compose(messages)
        for word in text.split(" "):
            await asyncio.sleep(self._delay)
            yield word + " "

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int = 800,
    ) -> str:
        await asyncio.sleep(self._delay)
        return self._compose(messages)


# ===================================================================== TTS
def _voiced_frame(sample_rate: int, chunk_ms: int, frequency: float) -> bytes:
    """One whole cycle-aligned frame of a hum, built once at import."""
    samples = sample_rate * chunk_ms // 1000
    step = 2 * math.pi * frequency / sample_rate
    return array.array(
        "h", (int(12000 * math.sin(step * n)) for n in range(samples))
    ).tobytes()


class FakeTtsProvider:
    """Synthesises a speech-shaped waveform.

    Not intelligible — deliberately. It carries the right *duration* and the right
    envelope so that timing, barge-in, jitter buffering and the AI audio track can all
    be exercised end to end without a synthesis vendor.

    The base frame is generated once and only scaled per chunk. Building it sample by
    sample in the loop cost tens of milliseconds of event loop time per chunk, which was
    enough to stall every other request on the node while the moderator was talking.
    """

    name = "fake-tts"
    sample_rate = TTS_SAMPLE_RATE

    #: Roughly natural speech pace.
    WORDS_PER_SECOND = 2.8
    CHUNK_MS = 20
    _BASE = _voiced_frame(TTS_SAMPLE_RATE, 20, 150.0)

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        words = max(1, len(text.split()))
        total_ms = int(words / self.WORDS_PER_SECOND * 1000)
        chunks = max(1, total_ms // self.CHUNK_MS)

        for index in range(chunks):
            # A slow syllabic envelope over a fixed hum. audioop.mul is C-speed.
            envelope = 0.35 + 0.3 * math.sin(index / 6.0)
            await asyncio.sleep(self.CHUNK_MS / 1000 * 0.25)  # ahead of real time
            yield audioop.mul(self._BASE, 2, envelope)


# ===================================================================== STT
class FakeSttStream:
    """Energy-gated transcription.

    Real audio in, canned sentences out. It watches RMS energy so ``speech_started`` and
    the end-of-utterance timing are genuinely driven by the microphone; only the words
    are invented. That is enough to drive every state transition in the moderator.
    """

    SPEECH_RMS = 900
    SILENCE_FRAMES_TO_END = 40  # ~800 ms at 20 ms frames

    def __init__(self, on_transcript: TranscriptCallback, sample_rate: int) -> None:
        self._on_transcript = on_transcript
        self._sample_rate = sample_rate
        self._speaking = False
        self._silent_frames = 0
        self._voiced_frames = 0
        self._closed = False
        self._rng = random.Random()

    @staticmethod
    def _rms(pcm: bytes) -> float:
        if len(pcm) < 2:
            return 0.0
        count = len(pcm) // 2
        samples = struct.unpack(f"<{count}h", pcm[: count * 2])
        return math.sqrt(sum(s * s for s in samples) / count)

    async def send_audio(self, pcm: bytes) -> None:
        if self._closed:
            return
        energy = self._rms(pcm)

        if energy >= self.SPEECH_RMS:
            self._silent_frames = 0
            self._voiced_frames += 1
            if not self._speaking:
                self._speaking = True
                self._on_transcript(Transcript(text="", is_final=False, speech_started=True))
            elif self._voiced_frames % 25 == 0:
                self._on_transcript(Transcript(text="…", is_final=False))
            return

        if self._speaking:
            self._silent_frames += 1
            if self._silent_frames >= self.SILENCE_FRAMES_TO_END:
                await self.finalize()

    async def finalize(self) -> None:
        if not self._speaking:
            return
        self._speaking = False
        self._silent_frames = 0
        spoken_frames, self._voiced_frames = self._voiced_frames, 0
        # Longer speech gets a longer canned answer, so word-count policies behave.
        sentences = 1 + min(3, spoken_frames // 100)
        text = " ".join(self._rng.sample(_ANSWER_SEEDS, k=min(sentences, len(_ANSWER_SEEDS))))
        self._on_transcript(Transcript(text=text, is_final=True, confidence=0.5))

    async def close(self) -> None:
        self._closed = True


class FakeSttProvider:
    name = "fake-stt"

    async def open(
        self,
        *,
        on_transcript: TranscriptCallback,
        sample_rate: int = STT_SAMPLE_RATE,
        language: str = "en",
    ) -> FakeSttStream:
        return FakeSttStream(on_transcript, sample_rate)
