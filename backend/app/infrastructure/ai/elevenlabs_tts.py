"""ElevenLabs streaming text-to-speech.

Called once per *sentence* rather than once per moderator turn. The orchestrator's
sentence chunker flushes on terminal punctuation, so synthesis starts while the language
model is still generating — roughly a second of dead air removed from every turn.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.infrastructure.ai.resilience import CircuitBreaker

log = get_logger(__name__)

BASE_URL = "https://api.elevenlabs.io/v1"
#: Requested PCM rate. The moderator track upsamples to 48 kHz for Opus.
OUTPUT_SAMPLE_RATE = 24_000


class ElevenLabsTtsProvider:
    name = "elevenlabs"
    sample_rate = OUTPUT_SAMPLE_RATE

    def __init__(self, *, api_key: str | None = None, voice_id: str | None = None) -> None:
        self._api_key = api_key or settings.elevenlabs_api_key
        self._voice_id = voice_id or settings.elevenlabs_voice_id
        self._breaker = CircuitBreaker(self.name)
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=httpx.Timeout(connect=3.0, read=20.0, write=5.0, pool=5.0),
            headers={"xi-api-key": self._api_key, "accept": "audio/pcm"},
        )

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        if not self._api_key:
            raise ExternalServiceError(self.name, "ELEVENLABS_API_KEY is not set.")
        if self._breaker.is_open:
            raise ExternalServiceError(self.name, "Circuit is open.")

        payload = {
            "text": text,
            "model_id": "eleven_flash_v2_5",  # lowest latency tier
            "voice_settings": {"stability": 0.45, "similarity_boost": 0.75},
        }
        try:
            async with self._client.stream(
                "POST",
                f"/text-to-speech/{self._voice_id}/stream",
                params={
                    "output_format": f"pcm_{OUTPUT_SAMPLE_RATE}",
                    "optimize_streaming_latency": 3,
                },
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode()[:300]
                    self._breaker.record_failure()
                    raise ExternalServiceError(self.name, f"{response.status_code}: {body}")

                async for chunk in response.aiter_bytes(chunk_size=1920):
                    if chunk:
                        yield chunk
            self._breaker.record_success()
        except httpx.HTTPError as exc:
            self._breaker.record_failure()
            raise ExternalServiceError(self.name, str(exc)) from exc

    async def healthy(self) -> bool:
        return bool(self._api_key) and not self._breaker.is_open

    async def aclose(self) -> None:
        await self._client.aclose()
