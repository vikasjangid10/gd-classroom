"""Deepgram streaming speech-to-text over WebSocket.

One socket per *speaking* participant. Because the moderator enforces one speaker at a
time, only the floor-holder's socket is open — three fewer connections and three fewer
bills than transcribing everybody continuously.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any
from urllib.parse import urlencode

import websockets

from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.domain.ports import STT_SAMPLE_RATE, Transcript, TranscriptCallback

log = get_logger(__name__)

DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"


class DeepgramSttStream:
    def __init__(
        self,
        # ``websockets`` moved the client type between releases and the legacy alias is
        # not exported for type checking; the protocol we rely on is send/recv/close.
        socket: Any,
        on_transcript: TranscriptCallback,
    ) -> None:
        self._socket = socket
        self._on_transcript = on_transcript
        self._reader = asyncio.create_task(self._read_loop(), name="deepgram-reader")
        self._closed = False

    async def _read_loop(self) -> None:
        try:
            async for raw in self._socket:
                message = json.loads(raw)
                kind = message.get("type")

                if kind == "SpeechStarted":
                    self._on_transcript(Transcript("", is_final=False, speech_started=True))
                    continue

                if kind == "UtteranceEnd":
                    self._on_transcript(Transcript("", is_final=True, confidence=1.0))
                    continue

                if kind != "Results":
                    continue

                alternative = message["channel"]["alternatives"][0]
                text = alternative.get("transcript", "").strip()
                if not text:
                    continue
                # ``speech_final`` means Deepgram believes the utterance is over;
                # ``is_final`` only means this segment will not be revised.
                self._on_transcript(
                    Transcript(
                        text=text,
                        is_final=bool(message.get("speech_final")),
                        confidence=float(alternative.get("confidence", 0.0)),
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("stt.reader_failed", error=str(exc))

    async def send_audio(self, pcm: bytes) -> None:
        if self._closed:
            return
        try:
            await self._socket.send(pcm)
        except Exception as exc:
            log.warning("stt.send_failed", error=str(exc))
            self._closed = True

    async def finalize(self) -> None:
        if not self._closed:
            with suppress(Exception):
                await self._socket.send(json.dumps({"type": "Finalize"}))

    async def close(self) -> None:
        self._closed = True
        self._reader.cancel()
        with suppress(Exception):
            await self._socket.send(json.dumps({"type": "CloseStream"}))
        with suppress(Exception):
            await self._socket.close()


class DeepgramSttProvider:
    name = "deepgram"

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = api_key or settings.deepgram_api_key
        self._model = model or settings.deepgram_model

    async def open(
        self,
        *,
        on_transcript: TranscriptCallback,
        sample_rate: int = STT_SAMPLE_RATE,
        language: str = "en",
    ) -> DeepgramSttStream:
        if not self._api_key:
            raise ExternalServiceError(self.name, "DEEPGRAM_API_KEY is not set.")

        query = urlencode(
            {
                "model": self._model,
                "language": language,
                "encoding": "linear16",
                "sample_rate": sample_rate,
                "channels": 1,
                "interim_results": "true",
                "vad_events": "true",
                "punctuate": "true",
                "smart_format": "true",
                "endpointing": settings.silence_end_ms,
                "utterance_end_ms": max(1000, settings.silence_end_ms),
            }
        )
        try:
            socket = await websockets.connect(
                f"{DEEPGRAM_URL}?{query}",
                additional_headers={"Authorization": f"Token {self._api_key}"},
                open_timeout=5,
                ping_interval=5,
            )
        except Exception as exc:
            raise ExternalServiceError(self.name, str(exc)) from exc

        return DeepgramSttStream(socket, on_transcript)

    async def healthy(self) -> bool:
        return bool(self._api_key)
