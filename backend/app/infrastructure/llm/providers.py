"""One spec per provider, and the lazily-built client behind it.

Everything that differs between providers is *data* on ``TierSpec``: the model, both
timeouts, how it does structured output, its daily limits, the timezone its quota day
rolls over in. The router reads those fields; it never learns which vendor it is talking
to. That is the whole reason this file exists — the alternative is a branch per provider
in the walk, and the walk is the part that must stay simple.

**Clients are built on first use, never at import.** A local daemon that is not running,
or an SDK that is not installed, must not stop the application from booting: it is one
rung of a chain whose entire purpose is surviving a rung being gone.

**A tier that cannot work stays in the chain.** It is marked ``available=False`` with a
reason a person can read, and skipped. Dropping it silently would make "no key
configured" and "healthy but never needed" look identical on the status page, which is
exactly the question someone opens that page to answer.
"""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Literal, Protocol, runtime_checkable

from app.core.config import Settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.domain.ports import ChatMessage

log = get_logger(__name__)

StructuredMethod = Literal["json_schema", "json_object", "prompt"]


@dataclass(slots=True)
class Usage:
    """What one call actually cost, as the provider counted it.

    Mutable and passed *in* rather than returned, because a streamed call has already
    handed its text to the caller by the time the number arrives — it comes in the final
    frame, after the last token. A sink the client fills is the only shape that works for
    both call styles without a different return type for each.

    Input and output are kept apart because they are priced apart, usually by a factor of
    four or more. A single total cannot be turned back into money.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def absorb(self, payload: dict[str, Any] | None) -> None:
        """Read a provider's ``usage`` block. Missing or malformed leaves it at zero,
        which is the honest answer: unknown, not free."""
        if not isinstance(payload, dict):
            return
        for field_name in ("prompt_tokens", "completion_tokens"):
            value = payload.get(field_name)
            if isinstance(value, int) and value >= 0:
                setattr(self, field_name, value)


@runtime_checkable
class TierClient(Protocol):
    """The uniform surface every tier presents to the router."""

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
        usage: Usage | None = None,
    ) -> str: ...

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
        usage: Usage | None = None,
    ) -> AsyncIterator[str]: ...

    async def complete_structured(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        temperature: float,
        max_tokens: int,
        usage: Usage | None = None,
    ) -> str: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class TierSpec:
    name: str
    model: str
    #: What the HTTP client itself waits for. Below ``hard_timeout_seconds`` so the
    #: client's own error — which carries a readable reason — usually wins the race
    #: against the router's timeout, which carries none.
    request_timeout_seconds: float
    #: The ceiling the router enforces. Per tier, because one global value either aborts
    #: a local model before it has finished loading or lets a hosted stall run for
    #: minutes.
    hard_timeout_seconds: float
    structured_output: StructuredMethod
    daily_request_limit: int | None
    daily_token_limit: int | None
    #: Hours from UTC at which this provider's quota day rolls over. Providers differ,
    #: and a single global "today" un-benches some tiers hours early.
    quota_day_utc_offset_hours: float
    #: No quota, no network in front of it. The chain must end on one.
    is_local: bool
    available: bool
    unavailable_reason: str
    #: Which chain this spec was built for. Stamped by ``build_chain`` rather than by the
    #: builders, because the two local rungs are shared between the lanes and a builder
    #: has no way to know which one is asking.
    lane: str = ""
    #: Extra fields merged into the request body, for knobs that exist on one provider
    #: and not the others. Data rather than a branch, for the same reason the timeouts
    #: are: the router must not learn which vendor it is talking to.
    extra_body: dict[str, Any] = field(default_factory=dict, compare=False)
    #: Called at most once, on first use.
    factory: Callable[[], TierClient] | None = field(default=None, compare=False, repr=False)


# ===================================================================== adapters
class OpenAiCompatibleClient:
    """OpenAI's chat-completions wire format, which four of these providers speak.

    Written against the protocol rather than a vendor SDK, so Groq, Ollama, vLLM and
    OpenAI itself are the same adapter with a different base URL — and so an optional
    provider never becomes an import-time dependency.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        structured_output: StructuredMethod,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        import httpx

        self._model = model
        self._structured = structured_output
        self._extra = dict(extra_body or {})
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(
                connect=min(3.0, timeout_seconds),
                read=timeout_seconds,
                write=5.0,
                pool=5.0,
            ),
            # Transport retries are off, and this is not a preference.
            #
            # An SDK or transport that retries does it *inside* the router's hard
            # timeout, so the router never gets to try the next rung — the whole chain
            # collapses into one provider retried until the ceiling. Measured on a bad
            # link: httpx with retries=5 turned a p50 of 400 ms into 100 seconds.
            transport=httpx.AsyncHTTPTransport(retries=0),
        )

    def _body(
        self, messages: list[ChatMessage], temperature: float, max_tokens: int, stream: bool
    ) -> dict[str, Any]:
        return {
            **self._extra,
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
            # Without this the moderator's utterances — most of the traffic — report no
            # usage at all, and the cost log would only ever see the quiet calls.
            **({"stream_options": {"include_usage": True}} if stream else {}),
            # Belt and braces with the transport setting above: providers that honour a
            # request-level retry budget must not spend it inside our ceiling either.
            "max_retries": 0,
        }

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        body.pop("max_retries", None)  # not a wire field; kept out of the JSON
        response = await self._client.post("/chat/completions", json=body)
        if response.status_code >= 400:
            # Raised with the body attached: ``failures.classify`` reads the text, and a
            # bare status code is exactly the case it cannot route correctly.
            raise httpx.HTTPStatusError(
                f"{response.status_code}: {response.text[:500]}",
                request=response.request,
                response=response,
            )
        return dict(response.json())

    def _content(self, payload: dict[str, Any]) -> str:
        """The answer, or an exception — never the empty string.

        A reasoning model spends ``max_tokens`` on thinking and returns ``content: ""``
        with ``finish_reason: length``, which is a *200*. Returned as-is it becomes a
        moderator who takes their turn and says nothing, in a room where the only sign
        of trouble is silence — and the chain, whose whole job is to have a rung that
        can speak, records it as a success and never moves down.
        """
        choice = payload["choices"][0]
        content = str(choice.get("message", {}).get("content") or "").strip()
        if content:
            return content
        usage = payload.get("usage") or {}
        thought = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        raise ExternalServiceError(
            self._model,
            f"answered with no content (finish_reason={choice.get('finish_reason')}, "
            f"completion_tokens={usage.get('completion_tokens')}"
            + (f", of which {thought} were reasoning" if thought else "")
            + "). A model that spends the whole budget thinking cannot serve this rung.",
        )

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
        usage: Usage | None = None,
    ) -> str:
        payload = await self._post(self._body(messages, temperature, max_tokens, stream=False))
        if usage is not None:
            usage.absorb(payload.get("usage"))
        return self._content(payload)

    async def complete_structured(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        temperature: float,
        max_tokens: int,
        usage: Usage | None = None,
    ) -> str:
        body = self._body(messages, temperature, max_tokens, stream=False)
        if self._structured == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "reply", "schema": schema, "strict": False},
            }
        elif self._structured == "json_object":
            body["response_format"] = {"type": "json_object"}
        # "prompt" asks for nothing: the caller's own instructions carry the shape, which
        # is all a model without a structured-output mode can be given.
        payload = await self._post(body)
        if usage is not None:
            usage.absorb(payload.get("usage"))
        # Unusable JSON is a bad answer and stays the caller's problem; *no* answer is a
        # rung that cannot serve, and is the chain's.
        return self._content(payload)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
        usage: Usage | None = None,
    ) -> AsyncIterator[str]:
        import httpx

        body = self._body(messages, temperature, max_tokens, stream=True)
        body.pop("max_retries", None)
        async with self._client.stream("POST", "/chat/completions", json=body) as response:
            if response.status_code >= 400:
                text = (await response.aread()).decode(errors="replace")[:500]
                raise httpx.HTTPStatusError(
                    f"{response.status_code}: {text}",
                    request=response.request,
                    response=response,
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    frame = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if usage is not None:
                    # Arrives in its own final frame, after the last token — which is why
                    # ``Usage`` is a sink rather than a return value.
                    usage.absorb(frame.get("usage"))
                try:
                    delta = frame["choices"][0]["delta"].get("content")
                except (KeyError, IndexError):
                    continue
                if delta:
                    yield delta

    async def aclose(self) -> None:
        await self._client.aclose()


class ScriptedClient:
    """The terminal rung: no key, no quota, no network.

    Reuses the offline moderator the project already ships, so the bottom of the chain is
    a thing that has been exercised in every test run rather than a special case written
    for the unhappy path.
    """

    def __init__(self) -> None:
        from app.infrastructure.ai.fake import FakeLlmProvider

        self._inner = FakeLlmProvider()

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
        usage: Usage | None = None,
    ) -> str:
        # ``usage`` is left at zero, and that is the true number: no model ran.
        return await self._inner.complete(
            messages, temperature=temperature, max_tokens=max_tokens
        )

    async def complete_structured(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        temperature: float,
        max_tokens: int,
        usage: Usage | None = None,
    ) -> str:
        return await self._inner.complete(
            messages, temperature=temperature, max_tokens=max_tokens
        )

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
        usage: Usage | None = None,
    ) -> AsyncIterator[str]:
        return self._inner.stream(messages, temperature=temperature, max_tokens=max_tokens)

    async def aclose(self) -> None:
        return None


# ===================================================================== specs
#: A rung's name carries its lane, and so does its row in the ledger. That is not
#: cosmetic: the two lanes run *different models* on the same key, and providers count a
#: free-tier quota per model. One shared row would bench a healthy rung the moment its
#: sibling ran out.
def _lane_name(provider: str, strong: bool) -> str:
    return f"{provider}-strong" if strong else provider


def _openai_spec(settings: Settings, *, strong: bool = False) -> TierSpec:
    key = settings.openai_api_key
    model = settings.openai_strong_model if strong else settings.openai_model
    return TierSpec(
        name=_lane_name("openai", strong),
        model=model,
        request_timeout_seconds=25.0,
        hard_timeout_seconds=30.0,
        structured_output="json_schema",
        daily_request_limit=settings.openai_daily_request_limit or None,
        daily_token_limit=settings.openai_daily_token_limit or None,
        # OpenAI's usage windows are UTC.
        quota_day_utc_offset_hours=0.0,
        is_local=False,
        available=bool(key),
        unavailable_reason="" if key else "OPENAI_API_KEY is not set",
        factory=lambda: OpenAiCompatibleClient(
            base_url=settings.openai_base_url,
            api_key=key,
            model=model,
            timeout_seconds=25.0,
            structured_output="json_schema",
        ),
    )


def _groq_spec(settings: Settings, *, strong: bool = False) -> TierSpec:
    key = settings.groq_api_key
    model = settings.groq_strong_llm_model if strong else settings.groq_llm_model
    effort = (
        settings.groq_strong_reasoning_effort if strong else settings.groq_reasoning_effort
    )
    # Groq's default catalogue is reasoning models, and a moderator's question is a
    # sentence, not a proof. Left at the default, gpt-oss-20b spent 218 of its 220 tokens
    # thinking and returned empty content — measured, not supposed. `low` is the floor
    # the endpoint accepts for gpt-oss; qwen also takes `none`.
    reasoning = {"reasoning_effort": effort} if effort else {}
    return TierSpec(
        name=_lane_name("groq", strong),
        model=model,
        # Groq is fast enough that a long ceiling only ever buys a longer stall.
        request_timeout_seconds=15.0,
        hard_timeout_seconds=20.0,
        # Groq's OpenAI-compatible endpoint accepts json_object but is inconsistent about
        # full json_schema across models, and a refused request looks like a
        # misconfiguration to the classifier.
        structured_output="json_object",
        daily_request_limit=settings.groq_daily_request_limit or None,
        daily_token_limit=settings.groq_daily_token_limit or None,
        quota_day_utc_offset_hours=0.0,
        is_local=False,
        available=bool(key),
        unavailable_reason="" if key else "GROQ_API_KEY is not set",
        extra_body=reasoning,
        factory=lambda: OpenAiCompatibleClient(
            base_url=settings.groq_base_url,
            api_key=key,
            model=model,
            timeout_seconds=15.0,
            structured_output="json_object",
            extra_body=reasoning,
        ),
    )


def _gemini_spec(settings: Settings, *, strong: bool = False) -> TierSpec:
    """Google's OpenAI-compatible endpoint, not the ``google-genai`` SDK.

    Same wire format as every other hosted rung, so it is a spec and no adapter at all —
    which is the point of writing the client against the protocol.
    """
    key = settings.gemini_api_key
    model = settings.gemini_strong_model if strong else settings.gemini_model
    return TierSpec(
        name=_lane_name("gemini", strong),
        model=model,
        # A ceiling this rung can actually meet. Measured on the real moderator prompt:
        # the lite model answers in ~1.5 s, while the reasoning flash models took 3.8 s
        # and 44 s for a sentence they then truncated. See GEMINI_MODEL in .env.example.
        request_timeout_seconds=22.0 if strong else 14.0,
        hard_timeout_seconds=26.0 if strong else 18.0,
        # The compatibility layer takes ``json_object`` on every model. Full json_schema
        # support varies by model, and a refused request reads as MISCONFIGURED to the
        # classifier — a thirty-minute bench for a field we do not need.
        structured_output="json_object",
        daily_request_limit=settings.gemini_daily_request_limit or None,
        daily_token_limit=settings.gemini_daily_token_limit or None,
        # Free-tier quotas roll over at midnight Pacific. Stated as -8 rather than
        # tracking DST: in summer this un-benches an hour late, which costs one hour of a
        # lower rung. The other direction re-probes a spent tier and pays a failure for
        # every call until the real rollover.
        quota_day_utc_offset_hours=-8.0,
        is_local=False,
        available=bool(key),
        unavailable_reason="" if key else "GEMINI_API_KEY is not set",
        factory=lambda: OpenAiCompatibleClient(
            base_url=settings.gemini_base_url,
            api_key=key,
            model=model,
            timeout_seconds=22.0 if strong else 14.0,
            structured_output="json_object",
        ),
    )


def _huggingface_spec(settings: Settings, *, strong: bool = False) -> TierSpec:
    """The HF router, which fronts several inference providers behind one key.

    Which provider actually serves a model is HF's decision and can change between
    calls, so this rung is the least predictable one in the chain: it gets a longer
    ceiling for a cold start and asks for no structured-output mode at all.
    """
    key = settings.huggingface_api_key
    model = settings.huggingface_strong_model if strong else settings.huggingface_model
    return TierSpec(
        name=_lane_name("huggingface", strong),
        model=model,
        # A model that has to be spun up on the serving provider is legitimately slow on
        # the first call of the day.
        request_timeout_seconds=20.0,
        hard_timeout_seconds=25.0,
        # No ``response_format``: support depends on whichever provider HF routes to, and
        # the prompt already carries the shape. See ``StructuredMethod``.
        structured_output="prompt",
        daily_request_limit=settings.huggingface_daily_request_limit or None,
        daily_token_limit=settings.huggingface_daily_token_limit or None,
        # The free allowance is monthly credits, so there is no daily boundary to align
        # to; exhaustion arrives as a 402 and benches the rung until it is topped up.
        quota_day_utc_offset_hours=0.0,
        is_local=False,
        available=bool(key),
        unavailable_reason="" if key else "HUGGINGFACE_API_KEY is not set",
        factory=lambda: OpenAiCompatibleClient(
            base_url=settings.huggingface_base_url,
            api_key=key,
            model=model,
            timeout_seconds=20.0,
            structured_output="prompt",
        ),
    )


def _ollama_spec(settings: Settings) -> TierSpec:
    """A local model, if one happens to be running.

    Availability is not probed here — that would be a network call at boot, and the
    daemon being down is precisely the condition this tier is allowed to be in. It is
    marked available and simply fails its first call, which the ledger then benches.
    """
    return TierSpec(
        name="ollama",
        model=settings.ollama_model,
        # A local model that has to load weights is legitimately slow on the first call,
        # and killing it at a hosted provider's ceiling means it never gets to be ready.
        request_timeout_seconds=110.0,
        hard_timeout_seconds=120.0,
        structured_output="json_object",
        daily_request_limit=None,
        daily_token_limit=None,
        quota_day_utc_offset_hours=0.0,
        is_local=True,
        available=bool(settings.ollama_base_url),
        unavailable_reason="" if settings.ollama_base_url else "OLLAMA_BASE_URL is not set",
        factory=lambda: OpenAiCompatibleClient(
            base_url=settings.ollama_base_url,
            api_key="",
            model=settings.ollama_model,
            timeout_seconds=110.0,
            structured_output="json_object",
        ),
    )


def _scripted_spec(_: Settings) -> TierSpec:
    return TierSpec(
        name="scripted",
        model="scripted-moderator",
        request_timeout_seconds=5.0,
        hard_timeout_seconds=10.0,
        structured_output="prompt",
        daily_request_limit=None,
        daily_token_limit=None,
        quota_day_utc_offset_hours=0.0,
        is_local=True,
        available=True,
        unavailable_reason="",
        factory=ScriptedClient,
    )


#: Every hosted provider appears twice — once per lane — from the same builder, so a
#: provider is added in one place and both lanes get it.
_HOSTED: dict[str, Callable[..., TierSpec]] = {
    "gemini": _gemini_spec,
    "groq": _groq_spec,
    "huggingface": _huggingface_spec,
    "openai": _openai_spec,
}

BUILDERS: dict[str, Callable[[Settings], TierSpec]] = {
    **{name: partial(build, strong=False) for name, build in _HOSTED.items()},
    **{f"{name}-strong": partial(build, strong=True) for name, build in _HOSTED.items()},
    # The two local rungs have nothing to make stronger: one is whatever model happens to
    # be running, the other is a fixed script. Both lanes share them, and their ledger row.
    "ollama": _ollama_spec,
    "scripted": _scripted_spec,
}


class ChainConfigurationError(RuntimeError):
    """Raised at boot. A malformed chain is a deployment mistake, not a runtime state."""


def build_chain(
    settings: Settings, raw: list[str], *, variable: str, lane: str = ""
) -> list[TierSpec]:
    """Validate one lane's chain and turn it into specs, in order.

    ``variable`` is the environment variable the list came from, and it appears in every
    message here: with two lanes, "the chain must end on a local tier" is only actionable
    if it says *which* chain.

    Every check fails the boot rather than the first discussion. A chain that ends on a
    hosted provider works perfectly until the day every key is spent, and then fails in
    front of four people who are waiting to speak.
    """
    names = [name.strip().lower() for name in raw if name.strip()]
    if not names:
        raise ChainConfigurationError(f"{variable} is empty; it needs at least one tier.")

    unknown = [name for name in names if name not in BUILDERS]
    if unknown:
        raise ChainConfigurationError(
            f"{variable} names unknown tiers: {', '.join(unknown)}. "
            f"Known tiers: {', '.join(sorted(BUILDERS))}."
        )

    duplicates = [name for index, name in enumerate(names) if name in names[:index]]
    if duplicates:
        raise ChainConfigurationError(
            f"{variable} repeats {', '.join(sorted(set(duplicates)))}. "
            "A tier appears once: the chain is the retry, so a repeat is a retry in "
            "disguise and would double that provider's timeout."
        )

    specs = [replace(BUILDERS[name](settings), lane=lane) for name in names]
    if not specs[-1].is_local:
        raise ChainConfigurationError(
            f"{variable} must end on a local tier; it ends on '{specs[-1].name}'. "
            "Without one there is no rung with no quota and no network in front of it, "
            "so a bad day upstream becomes an outage here."
        )
    return specs


# ===================================================================== clients
_clients: dict[str, TierClient] = {}
_clients_lock = threading.Lock()


def client_for(spec: TierSpec) -> TierClient:
    """The tier's client, built once on first use."""
    with _clients_lock:
        client = _clients.get(spec.name)
        if client is None:
            if spec.factory is None:  # pragma: no cover - a spec is never built without one
                raise ExternalServiceError(spec.name, "Tier has no client factory.")
            client = spec.factory()
            _clients[spec.name] = client
            log.info("llm.client_built", tier=spec.name, model=spec.model)
        return client


async def close_clients() -> None:
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        # Shutdown must not raise: a provider whose socket is already gone is exactly
        # the state this chain exists to tolerate.
        with suppress(Exception):
            await client.aclose()
