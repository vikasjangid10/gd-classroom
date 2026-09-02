"""Groq Whisper speech-to-text.

Groq serves Whisper as a *file* endpoint, not a socket: you post audio, you get text.
The rest of this system expects a streaming provider that pushes transcripts as someone
speaks. This adapter is the bridge, and everything unusual about it follows from that
one mismatch.

Deepgram tells us where an utterance ends. Nobody tells us here, so this file has to
decide, from the audio itself:

* a short-time energy gate marks speech versus silence, frame by frame;
* speech beginning raises ``speech_started`` immediately, so the moderator stops
  talking the moment somebody starts;
* ``SILENCE_END_MS`` of quiet ends the utterance, which is when the buffered audio is
  posted and the final transcript is emitted;
* while somebody is still talking, the buffer so far is transcribed every
  ``STT_INTERIM_SECONDS`` so the room sees a live caption rather than a frozen panel.

The interim pass costs a request per few seconds of speech. Set ``STT_INTERIM_SECONDS=0``
to turn it off and pay only for finals — the discussion still works, the captions just
appear a sentence at a time.
"""

from __future__ import annotations

import asyncio
import io
import wave
from contextlib import suppress

import httpx

from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.domain.ports import STT_SAMPLE_RATE, Transcript, TranscriptCallback
from app.infrastructure.ai.resilience import CircuitBreaker
from app.modules.voice import _audioop_shim as audioop

log = get_logger(__name__)

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

#: RMS below this counts as silence for 16-bit audio. Low enough to survive a quiet
#: speaker and room tone, high enough that a fan does not hold the floor open.
SILENCE_RMS = 320
#: How often the watchdog re-examines the buffer.
_TICK_SECONDS = 0.1
#: Whisper hallucinates confidently on very short clips; below this, say nothing.
_MIN_UTTERANCE_MS = 400
#: Groq rejects oversized uploads, and a turn is capped long before this anyway.
_MAX_UTTERANCE_SECONDS = 300


def _to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw PCM16 mono in a WAV container — the format the endpoint accepts."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


class GroqSttStream:
    """One utterance detector plus transcriber, bound to the current floor-holder."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_key: str,
        model: str,
        language: str,
        sample_rate: int,
        on_transcript: TranscriptCallback,
        breaker: CircuitBreaker,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._model = model
        self._language = language
        self._rate = sample_rate
        self._on_transcript = on_transcript
        self._breaker = breaker

        self._buffer = bytearray()
        self._speaking = False
        self._silence_ms = 0
        self._since_interim_ms = 0
        self._closed = False
        self._lock = asyncio.Lock()
        self._watchdog = asyncio.create_task(self._watch(), name="groq-stt-watchdog")

    # ---------------------------------------------------------------- ingest
    async def send_audio(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return

        loud = audioop.rms(pcm, 2) >= SILENCE_RMS
        frame_ms = (len(pcm) // 2) * 1000 // max(self._rate, 1)

        if loud:
            if not self._speaking:
                self._speaking = True
                self._since_interim_ms = 0
                self._on_transcript(Transcript("", is_final=False, speech_started=True))
            self._silence_ms = 0
        elif self._speaking:
            self._silence_ms += frame_ms

        # Leading silence is dropped; once speech starts, everything is kept — including
        # the pauses inside a sentence, which Whisper needs to punctuate properly.
        if self._speaking:
            self._buffer.extend(pcm)
            self._since_interim_ms += frame_ms

    # ---------------------------------------------------------------- timing
    async def _watch(self) -> None:
        """Decide when an utterance is over. The audio stream will not tell us."""
        interim_ms = int(settings.stt_interim_seconds * 1000)
        try:
            while not self._closed:
                await asyncio.sleep(_TICK_SECONDS)
                if not self._speaking:
                    continue

                if self._silence_ms >= settings.silence_end_ms:
                    await self._emit(final=True)
                    continue

                if self._buffered_ms() >= _MAX_UTTERANCE_SECONDS * 1000:
                    log.warning("stt.utterance_too_long", session_rate=self._rate)
                    await self._emit(final=True)
                    continue

                if interim_ms and self._since_interim_ms >= interim_ms:
                    self._since_interim_ms = 0
                    await self._emit(final=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - the watchdog must never kill a turn
            log.error("stt.watchdog_failed", error=str(exc))

    def _buffered_ms(self) -> int:
        return (len(self._buffer) // 2) * 1000 // max(self._rate, 1)

    async def finalize(self) -> None:
        """The moderator wants the answer now — turn cap reached, or the host ended it."""
        await self._emit(final=True)

    # ---------------------------------------------------------------- transcribe
    async def _emit(self, *, final: bool) -> None:
        async with self._lock:
            if final:
                pcm, self._buffer = bytes(self._buffer), bytearray()
                self._speaking = False
                self._silence_ms = 0
                self._since_interim_ms = 0
            else:
                # An interim pass must not consume the buffer: the final transcript is
                # taken from the whole utterance, not from whatever was left over.
                pcm = bytes(self._buffer)

            if self._buffered_duration_ms(pcm) < _MIN_UTTERANCE_MS:
                if final and pcm:
                    # Someone held the floor and said nothing usable. The moderator has
                    # to hear *something* or the turn hangs until the silence timer.
                    self._on_transcript(Transcript("", is_final=True, confidence=0.0))
                return

        text = await self._transcribe(pcm)
        if text:
            self._on_transcript(Transcript(text=text, is_final=final, confidence=1.0))
        elif final:
            self._on_transcript(Transcript("", is_final=True, confidence=0.0))

    def _buffered_duration_ms(self, pcm: bytes) -> int:
        return (len(pcm) // 2) * 1000 // max(self._rate, 1)

    async def _transcribe(self, pcm: bytes) -> str:
        if self._breaker.is_open:
            log.warning("stt.circuit_open", provider="groq")
            return ""
        files = {
            "file": ("utterance.wav", _to_wav(pcm, self._rate), "audio/wav"),
        }
        data = {
            "model": self._model,
            "language": self._language,
            "response_format": "text",
            # Whisper's most common failure on silence is to invent a caption. A prompt
            # that describes the setting keeps it anchored to what was actually said.
            "prompt": "A participant speaking in a moderated technical group discussion.",
            "temperature": "0",
        }
        try:
            response = await self._client.post(
                GROQ_TRANSCRIBE_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                files=files,
                data=data,
            )
        except httpx.HTTPError as exc:
            self._breaker.record_failure()
            log.warning("stt.request_failed", provider="groq", error=str(exc))
            return ""

        if response.status_code >= 400:
            self._breaker.record_failure()
            log.warning(
                "stt.rejected",
                provider="groq",
                status=response.status_code,
                body=response.text[:200],
            )
            return ""

        self._breaker.record_success()
        return _clean(response.text)

    async def close(self) -> None:
        self._closed = True
        self._watchdog.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await self._watchdog
        self._buffer.clear()


#: Whisper's stock hallucinations on near-silence. Emitting these as a participant's
#: answer would put words in someone's mouth, so they are dropped outright.
_HALLUCINATIONS = {
    "you",
    "thank you.",
    "thanks for watching!",
    "thank you for watching.",
    "thank you for watching!",
    "subscribe.",
    "bye.",
    ".",
}


def _clean(raw: str) -> str:
    text = raw.strip().strip('"')
    return "" if text.lower() in _HALLUCINATIONS else text


class GroqSttProvider:
    name = "groq-whisper"

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = api_key or settings.groq_api_key
        self._model = model or settings.groq_stt_model
        self._breaker = CircuitBreaker(self.name)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=25.0, write=15.0, pool=5.0),
        )

    async def open(
        self,
        *,
        on_transcript: TranscriptCallback,
        sample_rate: int = STT_SAMPLE_RATE,
        language: str = "en",
    ) -> GroqSttStream:
        if not self._api_key:
            raise ExternalServiceError(self.name, "GROQ_API_KEY is not set.")
        return GroqSttStream(
            client=self._client,
            api_key=self._api_key,
            model=self._model,
            language=language,
            sample_rate=sample_rate,
            on_transcript=on_transcript,
            breaker=self._breaker,
        )

    async def healthy(self) -> bool:
        return bool(self._api_key) and not self._breaker.is_open

    async def aclose(self) -> None:
        await self._client.aclose()
