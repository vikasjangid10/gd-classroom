"""The walk down the chain, and nothing else.

There is not a single provider name in this file, and that is the design: everything a
tier does differently is a field on its spec, so adding a provider is a spec and an
adapter, never an edit here.

**The chain is the retry.** No tier is ever attempted twice within one call. A retry
against a provider that just refused is the most expensive way to discover it is still
refusing, and it happens while somebody is waiting to be spoken to.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.domain.ports import ChatMessage
from app.infrastructure.llm.failures import Decision, classify
from app.infrastructure.llm.monitor import ChainMonitor
from app.infrastructure.llm.providers import TierSpec, Usage, client_for
from app.infrastructure.llm.quota import QuotaLedger, TierStatus

log = get_logger(__name__)

T = TypeVar("T")


class ChainExhausted(TimeoutError, ExternalServiceError):
    """Every rung refused.

    Subclasses ``TimeoutError`` deliberately. The call sites in this application already
    treat a slow or absent model as a timeout and degrade accordingly; a brand-new
    exception type would mean auditing every one of them, and a single miss turns a
    quota event into a crash in front of a live discussion.
    """

    def __init__(self, purpose: str, detail: str) -> None:
        ExternalServiceError.__init__(self, "llm-chain", detail)
        self.purpose = purpose


@dataclass(frozen=True, slots=True)
class Answer:
    """A result plus the rung that produced it.

    Callers need the provenance: to key a cache, and to tell somebody the discussion is
    running on the scripted moderator rather than letting them work it out by ear.
    """

    value: str
    tier: str


class ChainRouter:
    def __init__(
        self,
        *,
        chain: list[TierSpec],
        ledger: QuotaLedger,
        monitor: ChainMonitor,
        failover_budget_seconds: float = 12.0,
        daily_threshold_seconds: float = 300.0,
    ) -> None:
        self._chain = chain
        self._ledger = ledger
        self._monitor = monitor
        # Per-rung ceilings bound each attempt but not their sum, and the sum is what the
        # person waiting actually experiences. Once the hunt for a working hosted
        # provider has cost this long, stop hunting and take the local rung.
        self._failover_budget = failover_budget_seconds
        self._daily_threshold = daily_threshold_seconds

    # ================================================================ eligibility
    def _eligible(self, purpose: str, *, started: float) -> list[TierSpec]:
        """The rungs worth trying, in order, costing zero network calls to decide."""
        out: list[TierSpec] = []
        for spec in self._chain:
            status = self._ledger.status(spec)
            if status in (
                TierStatus.UNAVAILABLE,
                TierStatus.QUOTA_SPENT,
                TierStatus.MISCONFIGURED,
                TierStatus.FAILING,
            ):
                self._monitor.skipped(spec.name, purpose, status.value)
                continue
            out.append(spec)
        return out

    def _record_usage(self, spec: TierSpec, usage: Usage, purpose: str) -> None:
        """The cost log: one line per billed call, which is the one place noise is worth it.

        The monitor deliberately records only state transitions, so a chain that is
        working perfectly writes nothing at all — and "what did today cost, and on which
        job" is then unanswerable. ``purpose`` is what makes it a lead rather than a
        number: the same total spent on questions and on assessments means two different
        things.
        """
        if usage.total <= 0:
            return
        self._ledger.note_usage(
            spec, prompt=usage.prompt_tokens, completion=usage.completion_tokens
        )
        log.info(
            "llm.usage",
            lane=spec.lane,
            tier=spec.name,
            model=spec.model,
            purpose=purpose,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        )

    def _successor(self, specs: list[TierSpec], index: int) -> str:
        return specs[index + 1].name if index + 1 < len(specs) else "(none)"

    def next_up(self) -> str:
        """The rung that would serve right now.

        Distinct from "active", which is the rung that served *last*. The two differ for
        exactly as long as it takes someone to notice something is wrong and open the
        status page — which is to say, precisely when they are being looked at.
        """
        for spec in self._chain:
            if self._ledger.eligible(spec):
                return spec.name
        return "(none)"

    # ================================================================ the walk
    async def _walk(
        self,
        purpose: str,
        attempt: Callable[[TierSpec], Awaitable[T]],
    ) -> tuple[T, str]:
        started = time.monotonic()
        specs = self._eligible(purpose, started=started)
        if not specs:
            self._monitor.chain_exhausted(purpose, "no eligible tiers")
            raise ChainExhausted(purpose, "Every provider is benched or unavailable.")

        failures = 0
        last_detail = "no tier was reached"

        for index, spec in enumerate(specs):
            if (
                not spec.is_local
                and failures
                and time.monotonic() - started > self._failover_budget
            ):
                # Stacking another hosted timeout onto an already-slow call buys a small
                # chance of a better model at a large cost to the person waiting. Skip
                # to the rung that cannot be rate limited.
                self._monitor.skipped(spec.name, purpose, "failover_budget_spent")
                continue

            try:
                # The ceiling is the tier's own. One global value either aborts a local
                # model before it has loaded or lets a hosted stall run for minutes.
                result = await asyncio.wait_for(
                    attempt(spec), timeout=spec.hard_timeout_seconds
                )
            except Exception as exc:
                failures += 1
                last_detail = self._handle_failure(spec, exc, purpose, specs, index)
                continue

            self._ledger.note_success(spec)
            self._monitor.selected(spec.name, purpose, after_failures=failures)
            return result, spec.name

        self._monitor.chain_exhausted(purpose, last_detail)
        raise ChainExhausted(purpose, f"All {len(specs)} providers failed: {last_detail}")

    def _handle_failure(
        self,
        spec: TierSpec,
        exc: BaseException,
        purpose: str,
        specs: list[TierSpec],
        index: int,
    ) -> str:
        """Record the decision and tell the monitor. Returns a detail for the caller."""
        failure = classify(exc, daily_threshold_seconds=self._daily_threshold)
        clears_at = self._ledger.note_failure(spec, failure.decision, failure.detail)
        successor = self._successor(specs, index)

        if failure.decision is Decision.QUOTA_SPENT:
            self._monitor.exhausted(
                spec.name, purpose, failure.detail, successor=successor, clears_at=clears_at
            )
        elif failure.decision is Decision.MISCONFIGURED:
            self._monitor.misconfigured(
                spec.name, purpose, failure.detail, successor=successor, clears_at=clears_at
            )
        elif failure.decision is Decision.RETRY_LATER:
            self._monitor.throttled(spec.name, purpose, failure.detail, successor=successor)
        else:
            report = self._ledger.report(spec)
            self._monitor.failing(
                spec.name,
                purpose,
                failure.detail,
                streak=report.failure_streak,
                successor=successor,
                clears_at=clears_at,
            )
        return f"{spec.name}: {failure.detail}"

    # ================================================================ public
    async def invoke(
        self,
        messages: list[ChatMessage],
        *,
        purpose: str,
        temperature: float = 0.7,
        max_tokens: int = 300,
    ) -> Answer:
        async def attempt(spec: TierSpec) -> str:
            self._ledger.note_attempt(spec)
            usage = Usage()
            try:
                return await client_for(spec).complete(
                    messages, temperature=temperature, max_tokens=max_tokens, usage=usage
                )
            finally:
                # In a ``finally`` because a call that timed out on our side was still
                # served, and still billed, on theirs.
                self._record_usage(spec, usage, purpose)

        value, tier = await self._walk(purpose, attempt)
        return Answer(value, tier)

    async def invoke_structured(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        purpose: str,
        temperature: float = 0.3,
        max_tokens: int = 800,
    ) -> tuple[dict[str, Any] | None, str]:
        """A structured reply, or ``None`` if the rung answered with unusable JSON.

        A parse failure is **not** a routing failure. The provider answered — it just
        answered badly, which the next call may well not do. Benching a healthy provider
        over one malformed body would take the best model out of rotation for a fault
        that costs a single retry at the call site.
        """
        async def attempt(spec: TierSpec) -> str:
            self._ledger.note_attempt(spec)
            usage = Usage()
            try:
                return await client_for(spec).complete_structured(
                    messages,
                    schema=schema,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    usage=usage,
                )
            finally:
                self._record_usage(spec, usage, purpose)

        raw, tier = await self._walk(purpose, attempt)
        return _parse_json(raw), tier

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        purpose: str,
        temperature: float = 0.7,
        max_tokens: int = 300,
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield ``(chunk, tier)``.

        **Failover ends at the first token.** Up to then a failure is invisible and the
        chain can move down a rung. After it, the caller has already been handed the
        beginning of one provider's sentence, and there is no way to splice a second
        provider's continuation onto it — so a later failure is re-raised rather than
        papered over with a different voice mid-sentence.
        """
        started = time.monotonic()
        specs = self._eligible(purpose, started=started)
        if not specs:
            self._monitor.chain_exhausted(purpose, "no eligible tiers")
            raise ChainExhausted(purpose, "Every provider is benched or unavailable.")

        failures = 0
        last_detail = "no tier was reached"

        for index, spec in enumerate(specs):
            if (
                not spec.is_local
                and failures
                and time.monotonic() - started > self._failover_budget
            ):
                self._monitor.skipped(spec.name, purpose, "failover_budget_spent")
                continue

            delivered = False
            usage = Usage()
            try:
                self._ledger.note_attempt(spec)
                iterator = client_for(spec).stream(
                    messages, temperature=temperature, max_tokens=max_tokens, usage=usage
                )
                deadline = time.monotonic() + spec.hard_timeout_seconds
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"{spec.name} exceeded {spec.hard_timeout_seconds}s")
                    try:
                        chunk = await asyncio.wait_for(anext(iterator), timeout=remaining)
                    except StopAsyncIteration:
                        break
                    delivered = True
                    yield chunk, spec.name
                if not delivered:
                    # A stream that ends without a token is a rung that took its turn and
                    # said nothing — the one failure mode this chain cannot afford to
                    # score as a success, because the only symptom is a silent moderator.
                    raise ExternalServiceError(spec.name, "streamed no content")
            except Exception as exc:
                if delivered:
                    # Past the point of no return. Recorded so the tier's health is still
                    # tracked, then re-raised: the caller has half a sentence and needs to
                    # know it will not be finished.
                    self._record_usage(spec, usage, purpose)
                    self._handle_failure(spec, exc, purpose, specs, index)
                    raise
                failures += 1
                self._record_usage(spec, usage, purpose)
                last_detail = self._handle_failure(spec, exc, purpose, specs, index)
                continue

            self._record_usage(spec, usage, purpose)
            self._ledger.note_success(spec)
            self._monitor.selected(spec.name, purpose, after_failures=failures)
            return

        self._monitor.chain_exhausted(purpose, last_detail)
        raise ChainExhausted(purpose, f"All {len(specs)} providers failed: {last_detail}")


def _parse_json(raw: str) -> dict[str, Any] | None:
    """Models fence JSON in markdown even when told not to. Strip it before giving up."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
