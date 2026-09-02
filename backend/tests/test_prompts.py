"""What the moderator is allowed to believe about the room.

Every test here exists because a live model got it wrong. The scripted provider never
did — it has no opinions to be wrong about.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.domain.ledger import SpeakingTimeLedger
from app.domain.prompts import PromptBuilder, TopicBrief, TurnRecord, ground_rules
from app.domain.turn_policy import DiscussionBudget, should_close

TOPIC = TopicBrief(
    title="Model Context Protocol",
    description="A standard interface between models and tools.",
    guiding_points=("Tool discovery", "Trust boundaries"),
)


def ledger_of(*names: str) -> SpeakingTimeLedger:
    ledger = SpeakingTimeLedger()
    for name in names:
        ledger.register(uuid4(), name)
    return ledger


def prompt_text(messages) -> str:
    return "\n".join(m.content for m in messages)


# ===================================================================== attribution
def test_an_empty_room_is_described_as_empty() -> None:
    """The defect: the moderator asked "how do you see tool discovery…", then next turn
    announced "Arjun highlighted the significance of tool discovery" — in a room where
    Arjun had said nothing. It was reading its own question back as his contribution.
    """
    builder = PromptBuilder(TOPIC)
    only_the_moderator = [
        TurnRecord(1, "Moderator", "Arjun, how do you see tool discovery?", True),
    ]

    text = prompt_text(
        builder.question(
            target_name="Priya",
            rolling_summary="",
            recent=only_the_moderator,
            ledger=ledger_of("Arjun", "Priya"),
            turn_index=2,
        )
    )

    assert "NOBODY HAS SAID ANYTHING YET" in text
    assert "refer to no one and to nothing" in text
    # The moderator's own question must be labelled as its own, never as a contribution.
    assert "Your own recent words" in text
    assert "actually made" not in text


def test_real_contributions_are_the_only_thing_quotable() -> None:
    builder = PromptBuilder(TOPIC)
    recent = [
        TurnRecord(1, "Moderator", "Arjun, where does the difficulty sit?", True),
        TurnRecord(2, "Arjun", "Capability negotiation is the hard part.", False),
    ]

    text = prompt_text(
        builder.question(
            target_name="Priya",
            rolling_summary="",
            recent=recent,
            ledger=ledger_of("Arjun", "Priya"),
            turn_index=3,
        )
    )

    assert "the ONLY things you may attribute to anyone" in text
    assert "Arjun said: Capability negotiation is the hard part." in text
    assert "NOBODY HAS SAID ANYTHING YET" not in text


def test_a_silent_participants_turn_does_not_become_a_contribution() -> None:
    """A turn that produced no words leaves no trace to quote."""
    builder = PromptBuilder(TOPIC)
    recent = [
        TurnRecord(1, "Moderator", "Arjun, your view?", True),
        TurnRecord(2, "Arjun", "   ", False),
    ]

    text = prompt_text(
        builder.question(
            target_name="Priya",
            rolling_summary="",
            recent=recent,
            ledger=ledger_of("Arjun", "Priya"),
            turn_index=3,
        )
    )
    assert "NOBODY HAS SAID ANYTHING YET" in text


def test_the_persona_forbids_putting_words_in_mouths() -> None:
    text = prompt_text(PromptBuilder(TOPIC).introduction(["Arjun", "Priya"]))
    assert "Never put words in anyone's mouth" in text
    assert "Your own earlier questions are NOT contributions" in text


def test_the_closing_may_not_invent_conclusions() -> None:
    text = prompt_text(
        PromptBuilder(TOPIC).closing(
            rolling_summary="", recent=[], ledger=ledger_of("Arjun", "Priya")
        )
    )
    assert "do not invent" in text
    assert "could not get going" in text


# ===================================================================== the rules turn
def test_the_ground_rules_are_fixed_words() -> None:
    assert ground_rules(90) == ground_rules(90)
    assert "90 seconds" in ground_rules(90)
    assert "45 seconds" in ground_rules(45)


def test_the_ground_rules_name_nobody() -> None:
    """A generated rules turn invented "Alex" and handed him the floor."""
    text = ground_rules(90).lower()
    assert "first speaker" not in text
    assert "?" not in text  # and it does not ask the first question either


# ===================================================================== giving up
@pytest.mark.parametrize("silent", [0, 1, 2])
def test_a_quiet_patch_is_not_a_reason_to_stop(silent: int) -> None:
    close, _ = should_close(
        ledger=ledger_of("Arjun", "Priya"),
        budget=DiscussionBudget(max_silent_turns=3),
        elapsed_seconds=60,
        consecutive_silent_turns=silent,
    )
    assert close is False


def test_a_room_that_never_speaks_is_closed_rather_than_improvised() -> None:
    close, reason = should_close(
        ledger=ledger_of("Arjun", "Priya"),
        budget=DiscussionBudget(max_silent_turns=3),
        elapsed_seconds=60,
        consecutive_silent_turns=3,
    )
    assert (close, reason) == (True, "NOBODY_SPOKE")
