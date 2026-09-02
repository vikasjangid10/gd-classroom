"""OpenAI-compatible chat completions over streaming HTTP.

Written against the wire format rather than the vendor SDK so that any OpenAI-compatible
endpoint — Azure, vLLM, Ollama, OpenRouter — works by changing ``OPENAI_BASE_URL``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.domain.ports import ChatMessage
from app.infrastructure.ai.resilience import CircuitBreaker, retry

log = get_logger(__name__)


class OpenAiLlmProvider:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._api_key = api_key or settings.openai_api_key
        self._model = model or settings.openai_model
        self._base_url = (base_url or settings.openai_base_url).rstrip("/")
        self._breaker = CircuitBreaker(self.name)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=3.0, read=30.0, write=5.0, pool=5.0),
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    def _body(
        self, messages: list[ChatMessage], temperature: float, max_tokens: int, stream: bool
    ) -> dict:
        return {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 300,
    ) -> AsyncIterator[str]:
        if self._breaker.is_open:
            raise ExternalServiceError(self.name, "Circuit is open.")
        try:
            async with self._client.stream(
                "POST",
                "/chat/completions",
                json=self._body(messages, temperature, max_tokens, stream=True),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode()[:300]
                    self._breaker.record_failure()
                    raise ExternalServiceError(self.name, f"{response.status_code}: {body}")

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        delta = json.loads(payload)["choices"][0]["delta"].get("content")
                    except (KeyError, IndexError, json.JSONDecodeError):
                        continue
                    if delta:
                        yield delta
            self._breaker.record_success()
        except httpx.HTTPError as exc:
            self._breaker.record_failure()
            raise ExternalServiceError(self.name, str(exc)) from exc

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int = 800,
    ) -> str:
        async def _call() -> str:
            response = await self._client.post(
                "/chat/completions",
                json=self._body(messages, temperature, max_tokens, stream=False),
            )
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"])

        return await self._breaker.call(lambda: retry(_call, provider=self.name))

    async def healthy(self) -> bool:
        return bool(self._api_key) and not self._breaker.is_open

    async def aclose(self) -> None:
        await self._client.aclose()
