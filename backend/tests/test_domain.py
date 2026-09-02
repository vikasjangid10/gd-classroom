"""Pure-domain tests: state machines, the fairness ledger and the turn policy."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.errors import IllegalTransitionError
from app.domain.enums import ClassroomStatus, SessionStatus
from app.domain.ledger import SpeakingTimeLedger, select_next_speaker
from app.domain.state_machines import CE, CLASSROOM_FSM, SE, SESSION_FSM
from app.domain.turn_policy import DiscussionBudget, decide_follow_up, should_close


# ===================================================================== FSMs
def test_classroom_happy_path() -> None:
    status = ClassroomStatus.DRAFT
    for event in (CE.INVITATIONS_SENT, CE.QUORUM_REACHED, CE.STARTED, CE.SESSION_ENDED):
        status = CLASSROOM_FSM.next(status, event)
    assert status is ClassroomStatus.COMPLETED
    assert CLASSROOM_FSM.is_terminal(status)


def test_classroom_cannot_start_before_quorum() -> None:
    with pytest.raises(IllegalTransitionError):
        CLASSROOM_FSM.next(ClassroomStatus.INVITING, CE.STARTED)


def test_aborted_session_returns_classroom_to_ready_for_one_retry() -> None:
    assert CLASSROOM_FSM.next(ClassroomStatus.LIVE, CE.SESSION_ABORTED) is ClassroomStatus.READY


def test_completed_classroom_is_final() -> None:
    assert CLASSROOM_FSM.allowed(ClassroomStatus.COMPLETED) == []


def test_session_summary_failure_still_ends_cleanly() -> None:
    assert SESSION_FSM.next(SessionStatus.SUMMARIZING, SE.FATAL) is SessionStatus.ENDED


def test_session_cannot_skip_connecting() -> None:
    with pytest.raises(IllegalTransitionError):
        SESSION_FSM.next(SessionStatus.PENDING, SE.ALL_CONNECTED)


# ===================================================================== ledger
def _ledger(*names: str) -> tuple[SpeakingTimeLedger, dict[str, object]]:
    ledger = SpeakingTimeLedger()
    ids = {}
    for name in names:
        user_id = uuid4()
        ids[name] = user_id
        ledger.register(user_id, name)
    return ledger, ids


def test_floor_goes_to_whoever_has_spoken_least() -> None:
    ledger, ids = _ledger("Priya", "Arjun", "Meera", "Dev")
    ledger.add_speech(ids["Priya"], 40_000)
    ledger.add_speech(ids["Arjun"], 10_000)
    ledger.add_speech(ids["Meera"], 25_000)

    assert select_next_speaker(ledger, last_speaker=None) == ids["Dev"]


def test_the_same_person_never_speaks_twice_in_a_row() -> None:
    ledger, ids = _ledger("Priya", "Arjun")
    ledger.add_speech(ids["Arjun"], 60_000)
    # Priya has spoken least, but she just finished — the floor must move.
    assert select_next_speaker(ledger, last_speaker=ids["Priya"]) == ids["Arjun"]


def test_a_lone_survivor_may_speak_again() -> None:
    ledger, ids = _ledger("Priya", "Arjun")
    ledger.set_connected(ids["Arjun"], False)
    assert select_next_speaker(ledger, last_speaker=ids["Priya"]) == ids["Priya"]


def test_disconnected_participants_are_not_offered_the_floor() -> None:
    ledger, ids = _ledger("Priya", "Arjun")
    ledger.add_speech(ids["Arjun"], 90_000)
    ledger.set_connected(ids["Priya"], False)
    assert select_next_speaker(ledger, last_speaker=None) == ids["Arjun"]


def test_a_participant_who_never_connects_is_never_offered_the_floor(live_session) -> None:
    """The bug: accept the invitation, close the tab, and keep getting asked anyway.

    `add_participant` runs at session bootstrap, from the roster of everyone who
    *accepted* -- before any of them has actually joined the room. Left at the ledger's
    own default (eligible from the moment it is registered), someone who never connects
    sits in round-robin forever: nothing else ever marks them disconnected, because no
    peer connection ever existed for anything to notice dropping.
    """
    assert select_next_speaker(live_session.ledger, last_speaker=None) is None


def test_connecting_is_what_earns_a_participant_the_floor(live_session) -> None:
    """The other half of the same fix: a real join must still work exactly as before."""
    user_id = next(iter(live_session.participants))
    live_session.mark_connected(user_id)

    assert select_next_speaker(live_session.ledger, last_speaker=None) == user_id


def test_fairness_spread_reports_the_real_gap() -> None:
    ledger, ids = _ledger("Priya", "Arjun")
    ledger.add_speech(ids["Priya"], 30_000)
    ledger.add_speech(ids["Arjun"], 12_000)
    assert ledger.fairness_spread_ms() == 18_000


def test_snapshot_is_absolute_not_incremental() -> None:
    ledger, ids = _ledger("Priya")
    ledger.add_speech(ids["Priya"], 41_500)
    ledger.add_speech(ids["Priya"], 8_500)
    [row] = ledger.snapshot()
    assert row["seconds"] == 50 and row["turns"] == 2


# ===================================================================== policy
BUDGET = DiscussionBudget(target_seconds=600, max_seconds=1200, min_turns_per_participant=2)


def test_a_thin_answer_earns_a_follow_up() -> None:
    ledger, ids = _ledger("Priya")
    decision = decide_follow_up(
        answer="It depends.",
        ledger=ledger,
        speaker_id=ids["Priya"],
        budget=BUDGET,
        elapsed_seconds=60,
    )
    assert decision.should_follow_up and decision.reason == "answer_too_thin"


def test_an_unsupported_claim_earns_a_follow_up() -> None:
    ledger, ids = _ledger("Priya")
    answer = " ".join(["hybrid search is always better than dense retrieval"] * 5)
    decision = decide_follow_up(
        answer=answer, ledger=ledger, speaker_id=ids["Priya"], budget=BUDGET, elapsed_seconds=60
    )
    assert decision.should_follow_up and decision.reason == "unsupported_claim"


def test_the_follow_up_quota_is_respected() -> None:
    ledger, ids = _ledger("Priya")
    ledger.add_follow_up(ids["Priya"])
    ledger.add_follow_up(ids["Priya"])
    decision = decide_follow_up(
        answer="Yes.", ledger=ledger, speaker_id=ids["Priya"], budget=BUDGET, elapsed_seconds=60
    )
    assert not decision.should_follow_up


def test_no_follow_ups_once_the_time_budget_is_spent() -> None:
    ledger, ids = _ledger("Priya")
    decision = decide_follow_up(
        answer="Sure.", ledger=ledger, speaker_id=ids["Priya"], budget=BUDGET, elapsed_seconds=999
    )
    assert decision.reason == "time_budget_spent"


def test_discussion_closes_on_the_hard_time_limit() -> None:
    ledger, _ = _ledger("Priya", "Arjun")
    close, reason = should_close(ledger=ledger, budget=BUDGET, elapsed_seconds=1_300)
    assert close and reason == "TIME_LIMIT"


def test_discussion_does_not_close_while_someone_is_still_short() -> None:
    ledger, ids = _ledger("Priya", "Arjun")
    for _ in range(2):
        ledger.add_speech(ids["Priya"], 30_000)
    ledger.add_speech(ids["Arjun"], 30_000)  # only one turn
    close, _ = should_close(ledger=ledger, budget=BUDGET, elapsed_seconds=700)
    assert not close


def test_discussion_closes_when_everyone_has_had_their_turns() -> None:
    ledger, ids = _ledger("Priya", "Arjun")
    for user_id in ids.values():
        ledger.add_speech(user_id, 30_000)
        ledger.add_speech(user_id, 30_000)
    close, reason = should_close(ledger=ledger, budget=BUDGET, elapsed_seconds=700)
    assert close and reason == "COMPLETED"
