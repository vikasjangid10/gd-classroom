"""Two chains, not one: how they are built, kept apart, and read back.

The fast lane writes what the room hears; the deep lane judges what was said. These tests
are about the seams between them — the ones that look identical when they are wrong.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.domain.ledger import SpeakingTimeLedger
from app.domain.ports import Assessment
from app.domain.turn_policy import DiscussionBudget, decide_follow_up
from app.infrastructure.llm.facade import _as_assessment, build_lanes
from app.infrastructure.llm.monitor import ChainMonitor
from app.infrastructure.llm.providers import ChainConfigurationError

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def lanes_settings(settings, tmp_path: Path, **overrides):
    return settings.model_copy(
        update={
            "llm_state_path": str(tmp_path / "state.json"),
            "openai_api_key": "sk-test",
            "groq_api_key": "gsk-test",
            "gemini_api_key": "g-test",
            "huggingface_api_key": "hf-test",
            **overrides,
        }
    )


# ===================================================================== building
def test_both_lanes_are_built_and_neither_shares_a_rung_name(settings, tmp_path) -> None:
    """Distinct names are what keeps the ledger honest.

    The lanes run *different models* on the same key, and providers count a free-tier
    quota per model. A shared row would bench a healthy rung the moment its sibling ran
    out — the one failure that looks exactly like the feature working.
    """
    built = build_lanes(lanes_settings(settings, tmp_path))

    fast = [tier.name for tier in built.fast._chain]
    deep = [tier.name for tier in built.deep._chain]
    assert fast == ["gemini", "groq", "huggingface", "openai", "scripted"]
    assert deep == [
        "groq-strong", "gemini-strong", "huggingface-strong", "openai-strong", "scripted",
    ]
    # `scripted` is the deliberate exception: one local floor, nothing to make stronger.
    assert set(fast) & set(deep) == {"scripted"}


def test_the_lanes_lead_with_different_providers(settings, tmp_path) -> None:
    """So one bad day upstream cannot take out both jobs at once."""
    built = build_lanes(lanes_settings(settings, tmp_path))

    assert built.fast._chain[0].name.split("-")[0] != built.deep._chain[0].name.split("-")[0]


def test_a_strong_rung_runs_a_different_model_from_its_sibling(settings, tmp_path) -> None:
    built = build_lanes(lanes_settings(settings, tmp_path))
    models = {tier.name: tier.model for tier in built.fast._chain + built.deep._chain}

    assert models["gemini"] != models["gemini-strong"]
    assert models["groq"] != models["groq-strong"]
    # The fast lane's rung must not be a reasoning model: it has 220 tokens to say one
    # sentence, and a model that thinks first spends all of them before speaking.
    assert "lite" in models["gemini"]


def test_a_deep_chain_that_does_not_end_locally_is_refused(settings, tmp_path) -> None:
    with pytest.raises(ChainConfigurationError, match="LLM_CHAIN_DEEP must end"):
        build_lanes(
            lanes_settings(settings, tmp_path, llm_chain_deep=["groq-strong", "openai-strong"])
        )


def test_the_old_single_chain_variable_fails_the_boot(settings, tmp_path, monkeypatch) -> None:
    """A stale LLM_CHAIN must not be silently ignored.

    Ignored, it reads exactly like a chain that is being honoured — which is how somebody
    spends an afternoon wondering why their reordering changed nothing.
    """
    monkeypatch.setenv("LLM_CHAIN", "openai,scripted")

    with pytest.raises(ChainConfigurationError, match="has been split in two"):
        build_lanes(lanes_settings(settings, tmp_path))


# ===================================================================== the shared feed
def test_events_carry_their_lane_and_share_one_buffer() -> None:
    """One buffer, because the question people ask is "what happened, in what order".

    Two buffers throw away the interleaving, which is the only part that explains why the
    room went quiet at the same moment the report failed.
    """
    monitor = ChainMonitor(buffer_size=10)
    fast, deep = monitor.for_lane("fast"), monitor.for_lane("deep")

    fast.skipped("gemini", "utterance:question", "QUOTA_SPENT")
    deep.skipped("groq-strong", "assess:answer", "QUOTA_SPENT")

    both = monitor.recent(10)
    assert [(e.lane, e.tier) for e in both] == [
        ("deep", "groq-strong"), ("fast", "gemini"),
    ]
    assert [e.tier for e in monitor.recent(10, lane="fast")] == ["gemini"]


def test_a_lane_reads_only_its_own_events_by_default(settings, tmp_path) -> None:
    built = build_lanes(lanes_settings(settings, tmp_path))
    built.deep._monitor.skipped("openai-strong", "assess:answer", "MISCONFIGURED")

    assert built.fast.recent_events(10) == []
    assert [e.tier for e in built.deep.recent_events(10)] == ["openai-strong"]
    assert [e.tier for e in built.recent_events(10)] == ["openai-strong"]


# ===================================================================== the verdict
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"substance": 3, "needs_follow_up": False, "follow_up_reason": "none"}, "none"),
        # A reason the follow-up prompts have no line for becomes the one they do.
        (
            {"substance": 1, "needs_follow_up": True, "follow_up_reason": "waffling"},
            "unclear",
        ),
        ({"substance": "2", "needs_follow_up": True, "follow_up_reason": "unclear"}, "unclear"),
    ],
)
def test_a_usable_verdict_is_coerced_into_the_domain_type(payload, expected) -> None:
    assessment = _as_assessment(payload, "groq-strong")

    assert assessment is not None
    assert assessment.follow_up_reason == expected
    assert assessment.tier == "groq-strong"


@pytest.mark.parametrize(
    "payload",
    [
        {"needs_follow_up": True, "follow_up_reason": "unclear"},   # no substance
        {"substance": 9, "needs_follow_up": True, "follow_up_reason": "unclear"},
        {"substance": "high", "needs_follow_up": True, "follow_up_reason": "unclear"},
        {},
    ],
)
def test_an_unusable_verdict_is_rejected_rather_than_guessed_at(payload) -> None:
    """Guessing what a bad verdict meant is how it becomes an invisible one."""
    assert _as_assessment(payload, "groq-strong") is None


async def test_each_lane_tracks_its_own_active_rung(settings, tmp_path) -> None:
    """One value would have the chains overwrite each other.

    The symptom is quiet and wrong: the page shows a single ACTIVE row across both
    routers, so the lane that just answered looks like the lane that has never run.
    """
    from app.infrastructure.llm.quota import TierStatus

    built = build_lanes(lanes_settings(settings, tmp_path))
    ledger = built.fast._ledger
    fast_top, deep_top = built.fast._chain[0], built.deep._chain[0]

    ledger.note_success(fast_top)
    ledger.note_success(deep_top)

    assert ledger.status(fast_top) is TierStatus.ACTIVE
    assert ledger.status(deep_top) is TierStatus.ACTIVE
    assert ledger.status(built.fast._chain[1]) is TierStatus.READY


async def test_a_deep_lane_that_fails_returns_none_rather_than_raising(
    settings, tmp_path
) -> None:
    """The room must never stall on a judge. ``None`` puts the heuristic back in charge."""
    built = build_lanes(lanes_settings(settings, tmp_path))

    async def explode(*_a, **_kw):
        raise RuntimeError("every rung refused")

    built.deep.write_report = explode  # type: ignore[method-assign]

    assert await built.assess([]) is None


async def test_a_slow_judge_does_not_hold_the_floor(settings, tmp_path) -> None:
    """The caller's deadline, not the judge's, is what the room experiences."""
    built = build_lanes(lanes_settings(settings, tmp_path))

    async def forever(*_a, **_kw):
        await asyncio.sleep(30)
        raise AssertionError("should have been cancelled")

    built.deep.write_report = forever  # type: ignore[method-assign]

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(built.assess([]), timeout=0.2)


# ===================================================================== the policy seam
def _ledger_with(speaker) -> SpeakingTimeLedger:
    ledger = SpeakingTimeLedger()
    ledger.register(speaker, "Arjun")
    return ledger


def _verdict(**kw) -> Assessment:
    base = {
        "substance": 1,
        "engaged_with_prior": True,
        "needs_follow_up": True,
        "follow_up_reason": "unsupported_claim",
        "note": "",
        "tier": "groq-strong",
    }
    return Assessment(**{**base, **kw})


def test_a_verdict_overrules_the_word_count_in_both_directions() -> None:
    """The two cases the word count gets exactly backwards, and neither is rare.

    A long answer that says nothing, and a short one that lands a supported point.
    """
    from uuid import uuid4

    speaker = uuid4()
    ledger = _ledger_with(speaker)
    budget = DiscussionBudget()
    common = {
        "ledger": ledger, "speaker_id": speaker, "budget": budget, "elapsed_seconds": 10,
    }
    long_but_empty = " ".join(["padding"] * 150)
    short_but_complete = "Because detectors misfire on second-language writers."

    # Left alone, the heuristic calls the first complete and the second too thin.
    assert not decide_follow_up(answer=long_but_empty, **common).should_follow_up
    assert decide_follow_up(answer=short_but_complete, **common).should_follow_up

    assert decide_follow_up(
        answer=long_but_empty, assessment=_verdict(needs_follow_up=True), **common
    ).should_follow_up
    assert not decide_follow_up(
        answer=short_but_complete,
        assessment=_verdict(needs_follow_up=False, follow_up_reason="none"),
        **common,
    ).should_follow_up


def test_a_verdict_cannot_overrule_the_rules_of_the_round() -> None:
    """Fairness is not a judgement call.

    Out of follow-ups and out of time are rules about the discussion, not opinions about
    the words — and a model that gets to overrule them turns a good assessment into an
    unfair round.
    """
    from uuid import uuid4

    speaker = uuid4()
    ledger = _ledger_with(speaker)
    budget = DiscussionBudget()
    for _ in range(budget.max_follow_ups_per_participant):
        ledger.add_follow_up(speaker)

    spent = decide_follow_up(
        answer="a real answer, at length, with a point in it",
        ledger=ledger,
        speaker_id=speaker,
        budget=budget,
        elapsed_seconds=10,
        assessment=_verdict(needs_follow_up=True),
    )
    assert not spent.should_follow_up
    assert spent.reason == "follow_up_quota_reached"

    late = decide_follow_up(
        answer="a real answer",
        ledger=_ledger_with(speaker),
        speaker_id=speaker,
        budget=budget,
        elapsed_seconds=budget.target_seconds + 1,
        assessment=_verdict(needs_follow_up=True),
    )
    assert late.reason == "time_budget_spent"


def test_an_empty_answer_is_never_worth_a_follow_up_whatever_the_judge_says() -> None:
    from uuid import uuid4

    speaker = uuid4()
    decision = decide_follow_up(
        answer="   ",
        ledger=_ledger_with(speaker),
        speaker_id=speaker,
        budget=DiscussionBudget(),
        elapsed_seconds=10,
        assessment=_verdict(needs_follow_up=True),
    )

    assert not decision.should_follow_up
    assert decision.reason == "empty_answer"
