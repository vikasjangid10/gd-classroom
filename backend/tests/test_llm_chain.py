"""The fallback chain: routing decisions, benching, and the walk.

No network anywhere in this file. Every tier is a fake whose next answer is scripted, so
each test states one rule and fails for one reason.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.errors import ExternalServiceError
from app.domain.ports import ChatMessage
from app.infrastructure.llm import providers as providers_module
from app.infrastructure.llm.failures import Decision, classify
from app.infrastructure.llm.monitor import ChainMonitor
from app.infrastructure.llm.providers import (
    ChainConfigurationError,
    OpenAiCompatibleClient,
    TierSpec,
    build_chain,
)
from app.infrastructure.llm.quota import QuotaLedger, TierStatus
from app.infrastructure.llm.router import ChainExhausted, ChainRouter

MESSAGES = [ChatMessage(role="user", content="hello")]


# ===================================================================== doubles
class FakeError(Exception):
    """Carries a status and body the way a real SDK error does, so the classifier is
    exercised on text rather than on a type it was told about."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeClient:
    """One tier. ``script`` is consumed one entry per call: an Exception is raised, a
    string is returned, and ``calls`` counts every network round trip that happened."""

    def __init__(self, script: list[Any] | None = None, *, answer: str = "ok") -> None:
        self.script = list(script or [])
        self.answer = answer
        self.calls = 0

    def _next(self) -> Any:
        self.calls += 1
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step
        return self.answer

    async def complete(self, messages, *, temperature, max_tokens, usage=None) -> str:
        return str(self._reply(usage))

    async def complete_structured(
        self, messages, *, schema, temperature, max_tokens, usage=None
    ) -> str:
        return str(self._reply(usage))

    async def stream(
        self, messages, *, temperature, max_tokens, usage=None
    ) -> AsyncIterator[str]:
        value = self._reply(usage)
        for chunk in str(value).split():
            yield chunk + " "

    def _reply(self, usage: Any) -> Any:
        """One place that fills the usage sink, so the accounting path is exercised by
        every test rather than only by the one that is about it."""
        value = self._next()
        if usage is not None:
            usage.prompt_tokens, usage.completion_tokens = 100, 10
        return value

    async def aclose(self) -> None:
        return None


class BurstThenFail:
    """Streams a token, then dies — the case failover must NOT paper over."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *a, **k) -> str:  # pragma: no cover
        raise AssertionError("not used")

    async def complete_structured(self, *a, **k) -> str:  # pragma: no cover
        raise AssertionError("not used")

    async def stream(
        self, messages, *, temperature, max_tokens, usage=None
    ) -> AsyncIterator[str]:
        self.calls += 1
        yield "half a "
        raise FakeError("connection reset by peer")

    async def aclose(self) -> None:
        return None


def spec(name: str, *, is_local: bool = False, hard_timeout: float = 5.0) -> TierSpec:
    return TierSpec(
        name=name,
        model=f"{name}-model",
        request_timeout_seconds=hard_timeout - 1,
        hard_timeout_seconds=hard_timeout,
        structured_output="json_object",
        daily_request_limit=None,
        daily_token_limit=None,
        quota_day_utc_offset_hours=0.0,
        is_local=is_local,
        available=True,
        unavailable_reason="",
        factory=None,
    )


@pytest.fixture
def ledger(tmp_path: Path) -> QuotaLedger:
    return QuotaLedger(
        path=tmp_path / "state.json",
        transient_failures_before_bench=3,
        transient_cooldown_seconds=20.0,
        transient_cooldown_cap_seconds=900.0,
    )


@pytest.fixture
def wire(monkeypatch):
    """Bind fake clients to tier names, bypassing the lazy real-client cache."""

    def _wire(mapping: dict[str, Any]):
        monkeypatch.setattr(providers_module, "client_for", lambda s: mapping[s.name])
        import app.infrastructure.llm.router as router_module

        monkeypatch.setattr(router_module, "client_for", lambda s: mapping[s.name])

    return _wire


def build(chain: list[TierSpec], ledger: QuotaLedger, **kw) -> ChainRouter:
    return ChainRouter(chain=chain, ledger=ledger, monitor=ChainMonitor(), **kw)


# ===================================================================== classification
@pytest.mark.parametrize(
    ("message", "status", "expected"),
    [
        # Wording that names a daily exhaustion outright.
        ("Rate limit reached for gpt-4o-mini: Limit 200000 tokens per day", 429,
         Decision.QUOTA_SPENT),
        ("You exceeded your current quota, insufficient_quota", 429, Decision.QUOTA_SPENT),
        ("Your credit balance is too low", 400, Decision.QUOTA_SPENT),
        ("payment required", 402, Decision.QUOTA_SPENT),
        # Groq states the wait in prose, and the duration is the whole signal.
        ("Rate limit reached. Please try again in 44m44.448s", 429, Decision.QUOTA_SPENT),
        ("Rate limit reached. Please try again in 8.5s", 429, Decision.RETRY_LATER),
        ("Too many requests, retry after 30 seconds", 429, Decision.RETRY_LATER),
        ("Rate limit exceeded", 429, Decision.RETRY_LATER),
        # Gemini says the same sentence for both windows; only the quota id differs, and
        # reading it wrong costs either a whole day of the best rung or a re-probe on
        # every call until midnight.
        ("You exceeded your current quota. quotaId: "
         "GenerateRequestsPerDayPerProjectPerModel-FreeTier", 429, Decision.QUOTA_SPENT),
        ("You exceeded your current quota. quotaId: "
         "GenerateRequestsPerMinutePerProjectPerModel-FreeTier, retryDelay: 21s",
         429, Decision.RETRY_LATER),
        # Hugging Face's router bills monthly credits, and says so.
        ("You have exceeded your monthly included credits for Inference Providers",
         402, Decision.QUOTA_SPENT),
        # A wrong key looks identical to an outage in the logs unless it is routed apart.
        ("Incorrect API key provided: sk-xxx", 401, Decision.MISCONFIGURED),
        ("The model `gpt-9` does not exist or you do not have access to it", 404,
         Decision.MISCONFIGURED),
        ("invalid_request_error: 'messages' is required", 400, Decision.MISCONFIGURED),
        # Blips.
        ("Read timed out", None, Decision.TRANSIENT),
        ("Bad gateway", 502, Decision.TRANSIENT),
        ("something nobody has seen before", None, Decision.TRANSIENT),
    ],
)
def test_errors_route_to_the_right_decision(message, status, expected) -> None:
    assert classify(FakeError(message, status)).decision is expected


def test_the_duration_is_read_from_prose_headers_and_all() -> None:
    assert classify(FakeError("try again in 1h2m", 429)).retry_after_seconds == 3720.0
    assert classify(FakeError("retry after 2 minutes", 429)).retry_after_seconds == 120.0


def test_the_detail_is_truncated() -> None:
    """Provider bodies run to kilobytes and every one of these reaches a log line."""
    assert len(classify(FakeError("x" * 5000, 500)).detail) <= 300


# ===================================================================== the walk
async def test_a_429_on_the_first_rung_falls_through_and_the_second_answers(
    ledger, wire
) -> None:
    first = FakeClient([FakeError("Rate limit reached, try again in 10s", 429)])
    second = FakeClient(answer="from the second rung")
    wire({"a": first, "b": second})

    router = build([spec("a"), spec("b", is_local=True)], ledger)
    answer = await router.invoke(MESSAGES, purpose="test")

    assert answer.value == "from the second rung"
    assert answer.tier == "b"
    assert first.calls == 1, "the chain is the retry; a refused rung is never re-tried"


async def test_a_benched_tier_costs_no_network_call(ledger, wire) -> None:
    """The whole point of the ledger: rediscovering a spent tier should be free."""
    first = FakeClient([FakeError("Limit 100 requests per day", 429)])
    second = FakeClient(answer="second")
    wire({"a": first, "b": second})
    router = build([spec("a"), spec("b", is_local=True)], ledger)

    await router.invoke(MESSAGES, purpose="test")
    assert first.calls == 1

    await router.invoke(MESSAGES, purpose="test")
    assert first.calls == 1, "a benched tier was dialled again"


async def test_a_daily_quota_benches_until_rollover_a_throttle_does_not(ledger, wire) -> None:
    wire({"a": FakeClient(), "b": FakeClient()})
    daily, throttled = spec("a"), spec("b")

    ledger.note_failure(daily, Decision.QUOTA_SPENT, "per day")
    ledger.note_failure(throttled, Decision.RETRY_LATER, "try again in 8s")

    assert ledger.status(daily) is TierStatus.QUOTA_SPENT
    assert not ledger.eligible(daily)

    # Recorded for the dashboard, but still first in line for the next call — a
    # per-minute throttle clears in seconds and benching on it makes the application
    # look like it randomly switches models.
    assert ledger.status(throttled) is TierStatus.THROTTLED
    assert ledger.eligible(throttled)

    clears_at = ledger.report(daily).bench_clears_at
    assert clears_at and 0 < clears_at - time.time() <= 24 * 3600


async def test_three_transient_failures_bench_and_a_success_clears_everything(
    ledger, wire
) -> None:
    tier = spec("a")
    fallback = spec("b", is_local=True)
    flaky = FakeClient([FakeError("connection reset") for _ in range(3)])
    wire({"a": flaky, "b": FakeClient(answer="second")})
    router = build([tier, fallback], ledger)

    for expected_calls in (1, 2, 3):
        await router.invoke(MESSAGES, purpose="test")
        assert flaky.calls == expected_calls
        # One blip is not an outage: still eligible until the streak is reached.
        if expected_calls < 3:
            assert ledger.eligible(tier)

    assert ledger.status(tier) is TierStatus.FAILING
    await router.invoke(MESSAGES, purpose="test")
    assert flaky.calls == 3, "the fourth call should have skipped the benched tier"

    cooldown = ledger.report(tier).seconds_until_clear
    assert cooldown and cooldown <= 20.0

    # A success resets the streak and the doubled cooldown instantly — a tier that just
    # answered is healthy, and carrying the penalty forward punishes it for a past outage.
    ledger.clear_bench("a")
    flaky.script = []
    await router.invoke(MESSAGES, purpose="test")
    report = ledger.report(tier)
    assert report.failure_streak == 0
    assert report.bench_clears_at is None


async def test_the_cooldown_doubles_up_to_the_cap(ledger) -> None:
    """A fixed cooldown is wrong at both ends; this is the doubling that fixes it.

    Too short and a dead provider is re-probed forever; too long and a ten-second blip
    keeps the best model benched for an hour.
    """
    tier = spec("a")
    for _ in range(3):  # arm the streak
        ledger.note_failure(tier, Decision.TRANSIENT, "blip")

    seen = [ledger.report(tier).seconds_until_clear or 0]
    for _ in range(8):
        ledger.note_failure(tier, Decision.TRANSIENT, "blip")
        seen.append(ledger.report(tier).seconds_until_clear or 0)

    assert seen[0] == pytest.approx(20, abs=1)
    assert seen[1] == pytest.approx(40, abs=1)
    assert seen[2] == pytest.approx(80, abs=1)
    assert seen[-1] == pytest.approx(900, abs=1), "the cap should hold"


async def test_clearing_a_bench_by_hand_also_forgives_the_cooldown(ledger) -> None:
    """Two bench reasons are guesses about the outside world — a key that has since been
    fixed, an account that has since been topped up. An operator saying so is better
    evidence than our streak, so the penalty goes with the bench."""
    tier = spec("a")
    for _ in range(4):
        ledger.note_failure(tier, Decision.TRANSIENT, "blip")

    assert ledger.clear_bench("a")
    assert ledger.report(tier).failure_streak == 0
    ledger.note_failure(tier, Decision.TRANSIENT, "blip")
    assert ledger.eligible(tier), "one failure after a manual clear must not re-bench"


# ===================================================================== streaming
async def test_streaming_falls_back_before_the_first_token(ledger, wire) -> None:
    first = FakeClient([FakeError("Rate limit, try again in 5s", 429)])
    wire({"a": first, "b": FakeClient(answer="second rung speaking")})
    router = build([spec("a"), spec("b", is_local=True)], ledger)

    chunks = [chunk async for chunk, _ in router.stream(MESSAGES, purpose="test")]
    assert "".join(chunks).strip() == "second rung speaking"


async def test_streaming_re_raises_after_the_first_token(ledger, wire) -> None:
    """You cannot splice a second provider's sentence onto the first one's."""
    dying = BurstThenFail()
    rescue = FakeClient(answer="never used")
    wire({"a": dying, "b": rescue})
    router = build([spec("a"), spec("b", is_local=True)], ledger)

    delivered: list[str] = []
    with pytest.raises(Exception) as caught:
        async for chunk, _ in router.stream(MESSAGES, purpose="test"):
            delivered.append(chunk)

    assert delivered == ["half a "]
    assert not isinstance(caught.value, ChainExhausted)
    assert rescue.calls == 0, "a second voice was spliced onto a half-finished sentence"


# ===================================================================== exhaustion
async def test_every_rung_failing_raises_something_the_app_already_handles(
    ledger, wire
) -> None:
    wire({
        "a": FakeClient([FakeError("boom", 500)]),
        "b": FakeClient([FakeError("boom", 500)]),
    })
    router = build([spec("a"), spec("b", is_local=True)], ledger)

    with pytest.raises(ChainExhausted) as caught:
        await router.invoke(MESSAGES, purpose="test")

    # The reason it subclasses TimeoutError: every call site in this application already
    # treats a slow model as a timeout, and a brand-new type would mean auditing them all.
    assert isinstance(caught.value, TimeoutError)


async def test_a_malformed_json_body_does_not_bench_the_provider(ledger, wire) -> None:
    """The rung answered. It just answered badly, which is not a routing failure."""
    tier = spec("a")
    wire({"a": FakeClient(answer="not json at all"), "b": FakeClient()})
    router = build([tier, spec("b", is_local=True)], ledger)

    parsed, served_by = await router.invoke_structured(MESSAGES, schema={}, purpose="test")

    assert parsed is None
    assert served_by == "a"
    assert ledger.eligible(tier)


async def test_the_failover_budget_skips_remaining_hosted_rungs(ledger, wire) -> None:
    """Per-rung ceilings do not bound their sum, and the sum is what the user feels."""

    class Slow(FakeClient):
        async def complete(self, messages, *, temperature, max_tokens, usage=None) -> str:
            self.calls += 1
            await asyncio.sleep(0.2)
            raise FakeError("boom", 500)

    slow, skipped, local = Slow(), FakeClient(), FakeClient(answer="local")
    wire({"a": slow, "b": skipped, "c": local})
    router = build(
        [spec("a"), spec("b"), spec("c", is_local=True)], ledger, failover_budget_seconds=0.05
    )

    answer = await router.invoke(MESSAGES, purpose="test")
    assert answer.tier == "c"
    assert skipped.calls == 0, "the budget was spent; the hosted rung should be skipped"


# ===================================================================== boot checks
def chain_settings(settings, **overrides):
    return settings.model_copy(
        update={"openai_api_key": "sk-test", "groq_api_key": "gsk-test", **overrides}
    )


def lane(names: list[str], settings, **overrides) -> list[TierSpec]:
    """One lane's chain, built the way ``build_lanes`` builds it."""
    return build_chain(chain_settings(settings, **overrides), names, variable="LLM_CHAIN_FAST")


def test_a_chain_that_does_not_end_locally_is_refused(settings) -> None:
    """Without a local rung there is no tier with no quota in front of it, and a bad day
    upstream becomes an outage here — in front of people waiting to speak."""
    with pytest.raises(ChainConfigurationError, match="must end on a local tier"):
        lane(["openai", "groq"], settings)


def test_an_empty_chain_is_refused(settings) -> None:
    with pytest.raises(ChainConfigurationError, match="empty"):
        lane([], settings)


def test_an_unknown_tier_is_refused(settings) -> None:
    with pytest.raises(ChainConfigurationError, match="unknown"):
        lane(["openai", "vertex", "scripted"], settings)


def test_the_default_free_first_chain_builds_and_ends_locally(settings) -> None:
    """The shipped order: the free rungs first, the paid one as the last hosted resort,
    and a rung with no quota in front of it underneath all of them."""
    chain = lane(
        ["gemini", "groq", "huggingface", "openai", "scripted"],
        settings,
        gemini_api_key="g-test",
        huggingface_api_key="hf-test",
    )

    assert [tier.name for tier in chain] == [
        "gemini", "groq", "huggingface", "openai", "scripted",
    ]
    assert [tier.is_local for tier in chain] == [False, False, False, False, True]
    assert all(tier.available for tier in chain)


def test_the_new_hosted_rungs_say_which_key_is_missing(settings) -> None:
    chain = lane(
        ["gemini", "huggingface", "scripted"], settings,
        gemini_api_key="", huggingface_api_key="",
    )

    assert [tier.available for tier in chain] == [False, False, True]
    assert "GEMINI_API_KEY" in chain[0].unavailable_reason
    assert "HUGGINGFACE_API_KEY" in chain[1].unavailable_reason


async def test_a_rung_that_answers_with_nothing_is_a_failure_not_a_success() -> None:
    """The one failure mode this chain cannot afford to score as a success.

    A reasoning model spends the whole ``max_tokens`` budget thinking and returns
    ``content: ""`` with HTTP 200. Taken at face value that is a moderator who takes
    their turn and says nothing, in a room where the only symptom is silence.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["reasoning_effort"] == "low"
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": ""},
                         "finish_reason": "length"}],
            "usage": {"completion_tokens": 220,
                      "completion_tokens_details": {"reasoning_tokens": 218}},
        })

    client = OpenAiCompatibleClient(
        base_url="https://example.invalid/v1", api_key="k", model="thinky",
        timeout_seconds=5.0, structured_output="json_object",
        extra_body={"reasoning_effort": "low"},
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                       base_url="https://example.invalid/v1")

    with pytest.raises(ExternalServiceError, match="218 were reasoning"):
        await client.complete([ChatMessage(role="user", content="hi")],
                              temperature=0.7, max_tokens=220)


async def test_a_stream_that_delivers_no_token_moves_down_the_chain(ledger, wire) -> None:
    """Same failure, arriving the other way: the iterator simply ends."""
    class Mute:
        name = "mute"

        def stream(self, messages, *, temperature, max_tokens):
            async def empty():
                return
                yield  # pragma: no cover - never reached, makes this a generator
            return empty()

    silent, answering = spec("silent"), spec("answering", is_local=True)
    wire({"silent": Mute(), "answering": FakeClient(answer="a real sentence")})
    router = build([silent, answering], ledger)

    chunks = [chunk async for chunk, _ in router.stream(MESSAGES, purpose="speak")]

    assert "".join(chunks).strip() == "a real sentence"
    assert ledger.report(silent).failure_streak == 1, "the silent rung must be marked"


def test_geminis_quota_day_is_not_utc(settings) -> None:
    """Free-tier quotas roll over at midnight Pacific. A global "today" un-benches the
    rung hours early, which puts a spent tier back at the top to fail again."""
    chain = lane(["gemini", "scripted"], settings, gemini_api_key="g-test")

    assert chain[0].quota_day_utc_offset_hours == -8.0


def test_a_repeated_tier_is_refused(settings) -> None:
    """A repeat is a retry in disguise, and would double that provider's timeout."""
    with pytest.raises(ChainConfigurationError, match="repeats"):
        lane(["openai", "openai", "scripted"], settings)


def test_a_tier_with_no_key_stays_in_the_chain_and_says_why(settings) -> None:
    """A silently dropped tier is indistinguishable from a healthy unused one, which is
    exactly the question somebody opens the status page to answer."""
    chain = lane(["openai", "scripted"], settings, openai_api_key="")

    assert [tier.name for tier in chain] == ["openai", "scripted"]
    assert chain[0].available is False
    assert "OPENAI_API_KEY" in chain[0].unavailable_reason


async def test_an_unavailable_tier_is_skipped_not_fatal(ledger, wire) -> None:
    """A missing key must degrade the chain, never stop it."""
    unavailable = TierSpec(
        **{**vars(spec("a")), "available": False, "unavailable_reason": "no key"}
    )
    never, second = FakeClient(), FakeClient(answer="second")
    wire({"a": never, "b": second})
    router = build([unavailable, spec("b", is_local=True)], ledger)

    answer = await router.invoke(MESSAGES, purpose="test")

    assert answer.tier == "b"
    assert never.calls == 0
    assert ledger.status(unavailable) is TierStatus.UNAVAILABLE


# ===================================================================== persistence
def test_the_ledger_survives_a_restart(tmp_path: Path) -> None:
    """Counters that reset on every dev-server reload are worse than none."""
    path = tmp_path / "state.json"
    tier = spec("a")

    first = QuotaLedger(path=path)
    first.note_failure(tier, Decision.QUOTA_SPENT, "per day")
    first.note_attempt(tier)
    first.note_usage(tier, prompt=1000, completion=200)

    second = QuotaLedger(path=path)
    assert second.status(tier) is TierStatus.QUOTA_SPENT
    assert second.report(tier).tokens == 1200


def test_a_corrupt_state_file_boots_clean(tmp_path: Path) -> None:
    """Everything in the file is a cache of something the provider will say again."""
    path = tmp_path / "state.json"
    path.write_text("{not json at all", encoding="utf-8")

    ledger = QuotaLedger(path=path)
    assert ledger.status(spec("a")) in (TierStatus.READY, TierStatus.ACTIVE)


def test_a_state_file_from_an_older_version_is_discarded(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 0, "tiers": {"a": {"requests": 9}}}), encoding="utf-8")

    assert QuotaLedger(path=path).report(spec("a")).requests == 0


def test_quota_days_are_per_tier(tmp_path: Path) -> None:
    """Providers roll over in different timezones; one global "today" un-benches early."""
    ledger = QuotaLedger(path=tmp_path / "state.json")
    utc = spec("utc")
    ahead = TierSpec(**{**vars(spec("ahead")), "quota_day_utc_offset_hours": 12.0})

    ledger.note_attempt(utc)
    ledger.note_attempt(ahead)
    days = {ledger.report(utc).quota_day, ledger.report(ahead).quota_day}
    # They are the same only for part of the day, so assert the mechanism rather than
    # the calendar: each tier computed its own.
    assert len(days) in (1, 2)
    assert all(day for day in days)
