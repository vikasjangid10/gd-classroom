"""The rules that decide what the moderator does next.

Pure functions over a snapshot of the session. No I/O, no clock of their own — every
input is passed in, which makes the whole of the moderator's judgement unit-testable in
microseconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from app.domain.ledger import SpeakingTimeLedger
from app.domain.ports import Assessment

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class DiscussionBudget:
    target_seconds: int = 1500
    max_seconds: int = 2700
    min_turns_per_participant: int = 2
    turn_max_seconds: int = 90
    max_follow_ups_per_participant: int = 2
    #: Consecutive turns that produce no words at all before the discussion is closed.
    max_silent_turns: int = 3


@dataclass(frozen=True, slots=True)
class FollowUpDecision:
    should_follow_up: bool
    reason: str


#: Below this an answer is a fragment, not a contribution.
_THIN_ANSWER_WORDS = 25
#: Above this the participant has said plenty; move the floor on.
_FULL_ANSWER_WORDS = 110
#: Claims that invite a "why do you say that?". Crude on purpose — this list is the
#: *floor*, reached only when the deep lane has nothing to say.
_UNSUPPORTED_MARKERS = (
    "better",
    "worse",
    "best",
    "worst",
    "always",
    "never",
    "should",
    "shouldn't",
    "faster",
    "slower",
    "cheaper",
)


def word_count(text: str) -> int:
    return len(text.split())


def decide_follow_up(
    *,
    answer: str,
    ledger: SpeakingTimeLedger,
    speaker_id: object,
    budget: DiscussionBudget,
    elapsed_seconds: int,
    assessment: Assessment | None = None,
) -> FollowUpDecision:
    """One follow-up, and only when it would genuinely add something.

    ``assessment`` is the deep lane's read of the answer, or ``None`` when it was off,
    too slow, or unusable. It only ever gets to decide the *last* question — "was that
    answer worth returning to?" — because every check above it is a rule about the
    discussion rather than a judgement about the words: a participant is out of
    follow-ups, or the clock has run out, and no model's opinion changes either. Letting
    a verdict overrule those is how a good assessment turns into an unfair round.
    """
    # ``speaker_id`` is typed ``object`` so this module stays free of uuid plumbing;
    # the ledger is keyed by UUID, and a miss is handled two lines down either way.
    tally = ledger.tallies.get(cast("UUID", speaker_id))
    if tally is None:
        return FollowUpDecision(False, "unknown_speaker")

    if tally.follow_ups_received >= budget.max_follow_ups_per_participant:
        return FollowUpDecision(False, "follow_up_quota_reached")

    if elapsed_seconds > budget.target_seconds:
        return FollowUpDecision(False, "time_budget_spent")

    words = word_count(answer)
    if words == 0:
        return FollowUpDecision(False, "empty_answer")

    if assessment is not None:
        # The heuristic below counts words; this counts what was said. A long answer that
        # says nothing and a short one that lands a supported point are the two cases the
        # word count gets exactly backwards, and they are not rare.
        if not assessment.needs_follow_up:
            return FollowUpDecision(False, "assessed_sufficient")
        reason = assessment.follow_up_reason
        return FollowUpDecision(True, reason if reason != "none" else "unclear")

    if words < _THIN_ANSWER_WORDS:
        return FollowUpDecision(True, "answer_too_thin")

    if words > _FULL_ANSWER_WORDS:
        return FollowUpDecision(False, "answer_already_complete")

    lowered = answer.lower()
    if any(marker in lowered for marker in _UNSUPPORTED_MARKERS):
        return FollowUpDecision(True, "unsupported_claim")

    return FollowUpDecision(False, "answer_sufficient")


def should_close(
    *,
    ledger: SpeakingTimeLedger,
    budget: DiscussionBudget,
    elapsed_seconds: int,
    consecutive_silent_turns: int = 0,
) -> tuple[bool, str]:
    """The discussion ends when it is fair *and* long enough — or when time runs out."""
    if elapsed_seconds >= budget.max_seconds:
        return True, "TIME_LIMIT"

    if len(ledger.eligible) < 2:
        return True, "NOT_ENOUGH_PARTICIPANTS"

    # Everyone is connected and the floor keeps coming back empty: a broken microphone,
    # a room that walked away, speech recognition that is not working. Whatever the
    # cause, a moderator that keeps going is a moderator holding a discussion with
    # itself — and it will start attributing its own questions to people.
    if consecutive_silent_turns >= budget.max_silent_turns:
        return True, "NOBODY_SPOKE"

    covered = ledger.everyone_has_spoken(budget.min_turns_per_participant)
    if covered and elapsed_seconds >= budget.target_seconds:
        return True, "COMPLETED"

    return False, ""


def turn_deadline_seconds(budget: DiscussionBudget, ledger: SpeakingTimeLedger) -> int:
    """Shrink the per-turn cap late in the session so the tail does not overrun."""
    if ledger.min_turns() >= budget.min_turns_per_participant:
        return max(30, budget.turn_max_seconds // 2)
    return budget.turn_max_seconds
