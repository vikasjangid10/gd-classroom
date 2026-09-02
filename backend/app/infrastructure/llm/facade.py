"""The only module the rest of the application imports.

Nothing above this line knows a chain exists. The moderator asks for a question to be
written; it does not ask OpenAI, and it does not know that OpenAI was benched an hour ago
and Groq is answering instead. That ignorance is the feature: the day a provider is
swapped, this file changes and nothing else does.

Each call site's *intent* becomes a ``purpose`` string. It groups the cost log and it
turns "the chain fell through twice" into "the chain fell through twice **on the closing
summary**", which is the difference between a metric and a lead.

**There are two lanes, because the moderator does two different jobs.** The *fast* lane
writes what the room hears, where a plain instruct model beats a clever one and every
second is dead air. The *deep* lane judges what a participant actually said, where
reasoning is the point and nothing it produces is ever spoken. They are separate chains
with separate rungs and separate ledger rows — but one ledger file and one event buffer,
so "what happened, in what order" stays answerable across both.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.ports import Assessment, ChatMessage
from app.infrastructure.llm.monitor import ChainEvent, ChainMonitor
from app.infrastructure.llm.providers import (
    ChainConfigurationError,
    TierSpec,
    build_chain,
    client_for,
    close_clients,
)
from app.infrastructure.llm.quota import QuotaLedger, TierReport
from app.infrastructure.llm.router import Answer, ChainExhausted, ChainRouter

log = get_logger(__name__)

__all__ = [
    "Answer",
    "ChainConfigurationError",
    "ChainExhausted",
    "LlmGateway",
    "LlmLanes",
    "build_lanes",
]


class LlmGateway:
    """What the application calls. Also satisfies ``app.domain.ports.LlmProvider``.

    Satisfying the existing port matters more than it looks: the session runner already
    depends on that protocol, so the chain slots in underneath every existing call site
    without one of them changing, and ``AI_PROVIDER=fake`` keeps working exactly as
    before.
    """

    def __init__(
        self,
        *,
        lane: str,
        router: ChainRouter,
        chain: list[TierSpec],
        ledger: QuotaLedger,
        monitor: ChainMonitor,
    ) -> None:
        self.lane = lane
        self.name = f"llm-chain:{lane}"
        self._router = router
        self._chain = chain
        self._ledger = ledger
        self._monitor = monitor

    # ================================================================ the port
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int = 800,
        purpose: str = "complete",
    ) -> str:
        answer = await self._router.invoke(
            messages, purpose=purpose, temperature=temperature, max_tokens=max_tokens
        )
        return answer.value

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 300,
        purpose: str = "speak",
    ) -> AsyncIterator[str]:
        async for chunk, _tier in self._router.stream(
            messages, purpose=purpose, temperature=temperature, max_tokens=max_tokens
        ):
            yield chunk

    # ================================================================ intent
    async def write_utterance(
        self, messages: list[ChatMessage], *, kind: str, max_tokens: int = 220
    ) -> AsyncIterator[str]:
        """A moderator utterance, streamed. ``kind`` is the turn kind, for grouping."""
        async for chunk, _tier in self._router.stream(
            messages, purpose=f"utterance:{kind.lower()}", temperature=0.7, max_tokens=max_tokens
        ):
            yield chunk

    async def fold_summary(self, messages: list[ChatMessage]) -> Answer:
        return await self._router.invoke(
            messages, purpose="summary:fold", temperature=0.2, max_tokens=400
        )

    async def write_report(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        purpose: str = "summary:final",
        max_tokens: int = 900,
    ) -> tuple[dict[str, Any] | None, str]:
        """A structured answer. Returns ``(parsed, tier)``; ``parsed`` is ``None`` if the
        rung answered with unusable JSON — a bad answer, not a bad provider.

        ``max_tokens`` has to leave room for a reasoning model to think *and then answer*:
        on the deep lane the budget is spent on hidden tokens first, and a budget sized
        for the JSON alone comes back empty.
        """
        return await self._router.invoke_structured(
            messages, schema=schema, purpose=purpose, temperature=0.3, max_tokens=max_tokens
        )

    # ================================================================ operations
    def report(self) -> list[TierReport]:
        return [self._ledger.report(spec) for spec in self._chain]

    def recent_events(self, limit: int = 50, *, lane: str | None = None) -> list[ChainEvent]:
        """This lane's events. ``lane=""`` widens it to every lane's, newest first."""
        return self._monitor.recent(limit, lane=self.lane if lane is None else lane)

    def next_up(self) -> str:
        return self._router.next_up()

    def clear_bench(self, tier: str) -> bool:
        return self._ledger.clear_bench(tier)

    def reset_counters(self, tier: str) -> bool:
        return self._ledger.reset_counters(tier)

    async def healthy(self) -> bool:
        return self._router.next_up() != "(none)"

    async def warm(self) -> None:
        """Build the local rung's client before anybody needs it.

        Every error is swallowed, deliberately: this is an optimisation, and a local
        daemon that is not running is a normal state for a rung that exists to be there
        when the hosted ones are not. Without it, the first call that falls through pays
        a cold model load *on top of* an already-slow call — at the exact moment the
        chain is already having a bad time.
        """
        for spec in self._chain:
            if not spec.is_local or not spec.available:
                continue
            try:
                await asyncio.to_thread(client_for, spec)
                log.info("llm.local_prewarmed", tier=spec.name)
            except Exception as exc:
                log.info("llm.local_prewarm_skipped", tier=spec.name, error=str(exc)[:200])

    async def aclose(self) -> None:
        await close_clients()


class LlmLanes:
    """Both chains, and the one object the application holds.

    It *is* the fast lane as far as ``app.domain.ports.LlmProvider`` is concerned — every
    existing call site keeps working untouched — and it additionally offers ``deep`` for
    the two jobs that are judgement rather than speech: assessing an answer, and writing
    the closing report.
    """

    def __init__(self, *, fast: LlmGateway, deep: LlmGateway) -> None:
        self.fast = fast
        self.deep = deep
        self.name = f"{fast.name}+{deep.name}"

    # ---------------------------------------------------------- the LlmProvider port
    def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 300,
        purpose: str = "speak",
    ) -> AsyncIterator[str]:
        return self.fast.stream(
            messages, temperature=temperature, max_tokens=max_tokens, purpose=purpose
        )

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int = 800,
        purpose: str = "complete",
    ) -> str:
        return await self.fast.complete(
            messages, temperature=temperature, max_tokens=max_tokens, purpose=purpose
        )

    # ---------------------------------------------------------- the DeepLane port
    async def assess(self, messages: list[ChatMessage]) -> Assessment | None:
        """Judge one contribution, or return ``None``.

        ``None`` for every failure, deliberately: the caller's fallback is the word-count
        heuristic, which is always available and always correct enough. Raising here
        would let a slow judge stall a room, and that is a worse outcome than a cruder
        follow-up decision.
        """
        try:
            parsed, tier = await self.deep.write_report(
                messages, schema=ASSESSMENT_SCHEMA, purpose="assess:answer", max_tokens=700
            )
        except Exception as exc:
            log.info("llm.assessment_unavailable", error=str(exc)[:200])
            return None
        if parsed is None:
            log.info("llm.assessment_unparseable", tier=tier)
            return None
        return _as_assessment(parsed, tier)

    async def write_report(
        self, messages: list[ChatMessage], *, schema: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """The closing report, on the deep lane and through the *structured* path.

        Not ``complete()``: the models worth putting on this lane are reasoning models,
        and a plain completion asking for JSON in prose gets it fenced, prefaced, or
        thought about until the budget is spent. Measured — routing this at
        ``gemini-3.5-flash`` without a response format failed every time.
        """
        # Room for a reasoning model to think *and then* write the whole report.
        return await self.deep.write_report(messages, schema=schema, max_tokens=2000)

    # ---------------------------------------------------------- operations
    def recent_events(self, limit: int = 60) -> list[ChainEvent]:
        """Both lanes, newest first. Interleaved on purpose — see ``for_lane``."""
        return self.fast.recent_events(limit, lane="")

    def clear_bench(self, tier: str) -> bool:
        """By tier name, which already carries its lane. One ledger stands behind both."""
        return self.fast.clear_bench(tier)

    def reset_counters(self, tier: str) -> bool:
        return self.fast.reset_counters(tier)

    async def healthy(self) -> bool:
        return await self.fast.healthy()

    async def warm(self) -> None:
        await self.fast.warm()
        await self.deep.warm()

    async def aclose(self) -> None:
        # One client cache sits behind both lanes, so this closes everything once.
        await close_clients()


#: What ``assess`` asks for on the wire. The same shape is spelled out in prose inside
#: the prompt as well, because a rung whose structured-output mode is "prompt" is given
#: no schema at all and the instructions are all it has.
ASSESSMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "substance": {"type": "integer", "minimum": 0, "maximum": 5},
        "engaged_with_prior": {"type": "boolean"},
        "needs_follow_up": {"type": "boolean"},
        "follow_up_reason": {
            "type": "string",
            "enum": ["answer_too_thin", "unsupported_claim", "unclear", "none"],
        },
        "note": {"type": "string"},
    },
    "required": ["substance", "needs_follow_up", "follow_up_reason"],
}

#: The only reasons the follow-up prompts have a line for. A model that invents one gets
#: the generic sharpening question, rather than a moderator saying something it was never
#: given words for.
_KNOWN_REASONS = frozenset({"answer_too_thin", "unsupported_claim", "unclear", "none"})


def _as_assessment(parsed: dict[str, Any], tier: str) -> Assessment | None:
    """Coerce a model's JSON into the domain type, or reject it.

    Every field is treated as untrusted. A judge that answers ``substance: "high"`` has
    not answered the question, and guessing what it meant is how a bad verdict becomes an
    invisible one.
    """
    try:
        substance = int(parsed["substance"])
        needs = bool(parsed["needs_follow_up"])
        reason = str(parsed["follow_up_reason"]).strip().lower()
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= substance <= 5:
        return None
    return Assessment(
        substance=substance,
        engaged_with_prior=bool(parsed.get("engaged_with_prior", True)),
        needs_follow_up=needs,
        follow_up_reason=reason if reason in _KNOWN_REASONS else "unclear",
        note=str(parsed.get("note") or "").strip()[:160],
        tier=tier,
    )


def _build_lane(
    settings: Settings,
    *,
    lane: str,
    names: list[str],
    variable: str,
    ledger: QuotaLedger,
    monitor: ChainMonitor,
) -> LlmGateway:
    chain = build_chain(settings, names, variable=variable, lane=lane)
    view = monitor.for_lane(lane)
    router = ChainRouter(
        chain=chain,
        ledger=ledger,
        monitor=view,
        failover_budget_seconds=settings.llm_failover_budget_seconds,
        daily_threshold_seconds=settings.llm_daily_quota_threshold_seconds,
    )
    log.info(
        "llm.chain_built",
        lane=lane,
        chain=[spec.name for spec in chain],
        models={spec.name: spec.model for spec in chain},
        skipped=[spec.name for spec in chain if not spec.available],
    )
    return LlmGateway(lane=lane, router=router, chain=chain, ledger=ledger, monitor=view)


def build_lanes(settings: Settings) -> LlmLanes:
    """Validate both chains and wire them up. Raises at boot on a malformed one."""
    if os.getenv("LLM_CHAIN"):
        # Loud, because the alternative is a .env that still names a chain nobody reads.
        raise ChainConfigurationError(
            "LLM_CHAIN has been split in two: LLM_CHAIN_FAST for what the room hears, and "
            "LLM_CHAIN_DEEP for judging what was said. Rename it and pick a deep chain."
        )
    ledger = QuotaLedger(
        path=Path(settings.llm_state_path),
        transient_failures_before_bench=settings.llm_transient_failures_before_bench,
        transient_cooldown_seconds=settings.llm_transient_cooldown_seconds,
        transient_cooldown_cap_seconds=settings.llm_transient_cooldown_cap_seconds,
        misconfigured_bench_seconds=settings.llm_misconfigured_bench_seconds,
    )
    # One ledger and one event buffer across both lanes. The rows cannot collide — a
    # lane's rungs are named for it — and sharing is what makes "gemini-strong went down
    # four seconds after gemini did" one readable sequence instead of two.
    monitor = ChainMonitor(buffer_size=settings.llm_event_buffer_size)
    return LlmLanes(
        fast=_build_lane(
            settings,
            lane="fast",
            names=settings.llm_chain_fast,
            variable="LLM_CHAIN_FAST",
            ledger=ledger,
            monitor=monitor,
        ),
        deep=_build_lane(
            settings,
            lane="deep",
            names=settings.llm_chain_deep,
            variable="LLM_CHAIN_DEEP",
            ledger=ledger,
            monitor=monitor,
        ),
    )
