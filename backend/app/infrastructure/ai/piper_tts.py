"""Piper text-to-speech, running locally.

No API key, no per-word cost, no round trip — the voice model is an ONNX file on disk
and synthesis happens in this process. That makes it the cheapest possible moderator
voice, and the only one that still works with the network unplugged.

The catch is that it is CPU-bound and synchronous, and this process is running a live
audio mixer on a single event loop. Blocking that loop to synthesise a sentence would
stutter every participant's audio at once. So synthesis runs in a worker thread and
hands PCM back through a queue, chunk by chunk, which also preserves the property the
orchestrator depends on: the first chunk of a sentence reaches the mixer long before the
last one has been generated.

Piper changed its Python API between 1.2 and 1.3. Both are handled below, because
pinning the older one only moves the problem to whoever next runs ``pip install -U``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger

log = get_logger(__name__)

#: Sentinels passed through the queue: end of stream, and a failure to re-raise.
_DONE = object()
#: Piper emits far larger blocks than the mixer wants; 20 ms at 22.05 kHz is ~882 B.
_CHUNK_BYTES = 4096


class PiperTtsProvider:
    name = "piper"

    def __init__(self, *, model_path: str | None = None) -> None:
        self._model_path = model_path or settings.piper_model_path
        self._voice: Any | None = None
        self._lock = threading.Lock()
        # A real value is only known once the model is loaded; 22.05 kHz is the rate
        # every published Piper voice uses, and _load() corrects it from the config.
        self.sample_rate = 22_050

    # ---------------------------------------------------------------- model
    def _load(self) -> Any:
        """Load the ONNX voice once, on the thread that first needs it."""
        with self._lock:
            if self._voice is not None:
                return self._voice
            try:
                from piper.voice import PiperVoice
            except ImportError as exc:  # pragma: no cover - missing optional dependency
                raise ExternalServiceError(
                    self.name, "piper-tts is not installed in this image."
                ) from exc

            model = Path(self._model_path)
            if not model.is_file():
                raise ExternalServiceError(
                    self.name,
                    f"Piper voice model not found at {model}. "
                    "Run `python -m scripts.fetch_piper_voice` to download it.",
                )

            voice = PiperVoice.load(str(model))
            rate = getattr(getattr(voice, "config", None), "sample_rate", None)
            if rate:
                self.sample_rate = int(rate)
            self._voice = voice
            log.info("tts.piper_loaded", model=model.name, sample_rate=self.sample_rate)
            return voice

    def _synthesize(self, text: str) -> Iterator[bytes]:
        """Yield raw PCM16 mono for one sentence. Runs on a worker thread."""
        voice = self._load()

        # piper-tts >= 1.3
        if hasattr(voice, "synthesize"):
            for chunk in voice.synthesize(text):
                audio = getattr(chunk, "audio_int16_bytes", None)
                if audio is None:
                    audio = getattr(chunk, "audio_int16_array", None)
                    audio = audio.tobytes() if audio is not None else None
                if audio:
                    yield audio
            return

        # piper-tts < 1.3
        if hasattr(voice, "synthesize_stream_raw"):
            yield from voice.synthesize_stream_raw(text)
            return

        raise ExternalServiceError(  # pragma: no cover - an API we have not seen
            self.name,
            "This piper-tts build has neither synthesize() nor synthesize_stream_raw().",
        )

    # ---------------------------------------------------------------- stream
    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """Synthesise one sentence on a worker thread, yielding PCM as it is produced.

        The queue is unbounded, which is safe for exactly the reason the orchestrator
        calls this per *sentence*: a long sentence is a few hundred kilobytes of PCM,
        and the mixer drains far faster than Piper produces.
        """
        loop = asyncio.get_running_loop()
        outbox: asyncio.Queue[Any] = asyncio.Queue()
        cancelled = threading.Event()

        def emit(item: Any) -> None:
            loop.call_soon_threadsafe(outbox.put_nowait, item)

        def worker() -> None:
            try:
                for block in self._synthesize(text):
                    if cancelled.is_set():
                        break
                    for offset in range(0, len(block), _CHUNK_BYTES):
                        emit(block[offset : offset + _CHUNK_BYTES])
            except Exception as exc:  # surfaced on the event loop, not swallowed here
                emit(exc)
            finally:
                emit(_DONE)

        worker_done = loop.run_in_executor(None, worker)
        try:
            while True:
                item = await outbox.get()
                if item is _DONE:
                    break
                if isinstance(item, Exception):
                    raise ExternalServiceError(self.name, str(item)) from item
                yield item
        finally:
            # Barge-in cancels this generator mid-sentence; tell the worker to stop
            # rather than leaving it synthesising audio nobody will ever hear.
            cancelled.set()
            with suppress(Exception):
                await worker_done

    async def warm(self) -> None:
        """Load the ONNX voice before anyone needs it.

        The first synthesis otherwise pays ~1.7 s to load the model, and the sentence
        that pays it is the moderator's opening line — the one moment in a discussion
        where four people are sitting in silence waiting to hear something.
        """
        try:
            await asyncio.to_thread(self._load)
        except Exception as exc:  # a missing model must not stop the app from booting
            log.warning("tts.warm_failed", error=str(exc))

    async def healthy(self) -> bool:
        # A stat is cheap, but /readyz runs it on the event loop that is mixing audio,
        # and the model may live on a network volume. Off-thread, like the synthesis.
        return await asyncio.to_thread(Path(self._model_path).is_file)

    async def aclose(self) -> None:
        self._voice = None
