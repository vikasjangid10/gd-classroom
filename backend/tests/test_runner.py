"""A whole discussion, driven through the command queue with fakes.

No database, no WebRTC, no provider. If the moderator's behaviour is wrong, it is wrong
here first.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from app.domain.enums import EndReason, ModeratorState, SessionStatus, TurnKind
from app.domain.events import EventType, session_topic
from app.modules.moderation.commands import (
    EndSessionRequested,
    ParticipantConnected,
    ParticipantDisconnected,
    SpeechStarted,
    UtteranceFinal,
)
from app.modules.moderation.runner import (
    SessionRunner,
    _chunk_sentences,
    _strip_speaker_label,
)


def build_runner(live_session, plane, providers, bus, persistence, settings) -> SessionRunner:
    return SessionRunner(
        session=live_session,
        plane=plane,  # type: ignore[arg-type]
        providers=providers,
        bus=bus,
        persistence=persistence,  # type: ignore[arg-type]
        settings=settings,
    )


async def drain(bus, session_id) -> list:
    return bus.replay_since([session_topic(session_id)], last_seq=0)


async def connect_everyone(runner: SessionRunner) -> None:
    for user_id in list(runner.session.participants):
        runner.submit(ParticipantConnected(user_id))
    await settle()


async def settle(times: int = 40) -> None:
    """Let the runner drain its queue and every side task it spawned."""
    for _ in range(times):
        await asyncio.sleep(0.01)


# ===================================================================== tests
async def test_discussion_opens_with_an_introduction_then_the_rules(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    runner.start()
    await connect_everyone(runner)

    kinds = [turn.kind for turn in live_session.buffered_turns]
    assert kinds[:2] == [TurnKind.INTRO, TurnKind.RULES]
    assert persistence.active == [live_session.session_id]
    assert live_session.status is SessionStatus.ACTIVE

    await runner.stop()


async def test_the_floor_is_granted_to_a_named_participant(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    runner.start()
    await connect_everyone(runner)

    assert plane.granted, "the moderator never handed anyone the floor"
    assert live_session.floor_holder == plane.granted[0]
    assert live_session.moderator_state is ModeratorState.LISTENING

    granted = [e for e in await drain(bus, live_session.session_id)
               if e.type == EventType.FLOOR_GRANTED]
    assert granted and granted[0].payload["max_seconds"] == settings.turn_max_seconds

    await runner.stop()


async def test_a_full_answer_moves_the_floor_to_someone_else(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    runner.start()
    await connect_everyone(runner)

    first = live_session.floor_holder
    assert first is not None
    runner.submit(
        UtteranceFinal(
            first,
            "Retrieval quality dominates everything else in a RAG system, and in my "
            "experience the chunking strategy decides recall long before the model does. "
            "We measured it across three corpora and the pattern held every time.",
        )
    )
    await settle()

    assert live_session.ledger.tallies[first].turns_taken == 1
    assert len(plane.granted) >= 2
    assert plane.granted[1] != first, "the same person was given the floor twice in a row"

    await runner.stop()


async def test_a_thin_answer_earns_a_follow_up_before_the_floor_moves(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    runner.start()
    await connect_everyone(runner)

    first = live_session.floor_holder
    runner.submit(UtteranceFinal(first, "It depends."))
    await settle()

    assert live_session.ledger.tallies[first].follow_ups_received == 1
    assert plane.granted[1] == first, "a follow-up must go back to the same person"
    assert any(t.kind is TurnKind.FOLLOW_UP for t in live_session.buffered_turns)

    await runner.stop()


async def test_losing_the_speaker_mid_turn_continues_the_discussion(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    runner.start()
    await connect_everyone(runner)

    speaker = live_session.floor_holder
    runner.submit(ParticipantDisconnected(speaker))
    await settle()

    assert live_session.participants[speaker].is_present is False
    assert live_session.floor_holder != speaker
    assert live_session.status is SessionStatus.ACTIVE

    await runner.stop()


async def test_dropping_below_two_participants_ends_the_discussion(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    task = runner.start()
    await connect_everyone(runner)

    for user_id in list(live_session.participants)[:3]:
        runner.submit(ParticipantDisconnected(user_id))
    await asyncio.wait_for(task, timeout=10)

    assert persistence.statuses[-1][2] is EndReason.NOT_ENOUGH_PARTICIPANTS


async def test_ending_flushes_the_transcript_once_then_summarises(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    task = runner.start()
    await connect_everyone(runner)

    speaker = live_session.floor_holder
    runner.submit(UtteranceFinal(speaker, "A reasonable, complete answer about retrieval."))
    await settle()

    runner.submit(EndSessionRequested(EndReason.HOST_ENDED))
    await asyncio.wait_for(task, timeout=15)

    assert len(persistence.flushed) == 1, "the transcript must be written exactly once"
    assert persistence.flushed[0] > 0
    assert persistence.summaries and persistence.summaries[0] is not None
    assert persistence.classrooms_completed == [False]


async def test_everything_in_memory_is_wiped_when_the_session_ends(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    task = runner.start()
    await connect_everyone(runner)
    runner.submit(EndSessionRequested(EndReason.HOST_ENDED))
    await asyncio.wait_for(task, timeout=15)

    assert live_session.buffered_turns == []
    assert live_session.rolling_summary == ""
    assert live_session.participants == {}
    assert list(live_session.recent_turns) == []
    assert plane.closed
    # The replay ring holds transcript fragments, so it must go too.
    assert bus.replay_since([session_topic(live_session.session_id)], last_seq=0) == []


async def test_a_join_timeout_with_nobody_present_aborts(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    task = runner.start()
    await asyncio.wait_for(task, timeout=10)  # join window is 2 s in the test settings

    assert live_session.status is SessionStatus.ABORTED
    assert persistence.statuses[-1][1] is SessionStatus.ABORTED
    assert persistence.classrooms_completed == [True]


# ===================================================================== chunker
async def test_sentence_chunker_flushes_on_punctuation() -> None:
    async def tokens():
        for piece in ["Welcome ", "everyone. ", "Let's ", "begin ", "now. ", "Priya"]:
            yield piece

    assert [s async for s in _chunk_sentences(tokens())] == [
        "Welcome everyone.",
        "Let's begin now.",
        "Priya",
    ]


@pytest.mark.parametrize("text", ["", "   "])
async def test_sentence_chunker_yields_nothing_for_empty_output(text: str) -> None:
    async def tokens():
        yield text

    assert [s async for s in _chunk_sentences(tokens())] == []


# ===================================================================== privacy
PHONE_NUMBER = "9876543210"
CONTACT = f"Sure, just call me on {PHONE_NUMBER} and I'll walk you through it."


async def test_reading_out_a_phone_number_ends_your_round(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    runner.start()
    await connect_everyone(runner)

    offender = live_session.floor_holder
    runner.submit(UtteranceFinal(offender, CONTACT))
    await settle(60)

    assert live_session.participants[offender].is_removed
    assert live_session.floor_holder != offender

    removals = [
        e for e in await drain(bus, live_session.session_id)
        if e.type == EventType.PARTICIPANT_REMOVED
    ]
    assert len(removals) == 1
    assert removals[0].payload["reason"] == "SHARED_PERSONAL_INFORMATION"
    assert removals[0].payload["kinds"] == ["phone"]

    await runner.stop()


async def test_the_number_itself_is_never_written_down(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    """The point of the rule. Storing it while enforcing the rule would be the failure.

    Everywhere it could survive: the buffered transcript that gets flushed to Postgres,
    the moderator's own context, and every event published to every screen in the room.
    """
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    runner.start()
    await connect_everyone(runner)

    offender = live_session.floor_holder
    runner.submit(UtteranceFinal(offender, CONTACT))
    await settle(60)

    persisted = " ".join(turn.text for turn in live_session.buffered_turns)
    remembered = " ".join(turn.text for turn in live_session.recent_turns)
    published = " ".join(
        str(event.payload) for event in await drain(bus, live_session.session_id)
    )

    for place, haystack in (
        ("the transcript", persisted),
        ("the moderator's memory", remembered),
        ("the event stream", published),
    ):
        assert PHONE_NUMBER not in haystack, f"the number survived in {place}"

    await runner.stop()


async def test_the_room_is_told_why_in_fixed_words(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    runner.start()
    await connect_everyone(runner)

    offender = live_session.floor_holder
    name = live_session.name_of(offender)
    runner.submit(UtteranceFinal(offender, CONTACT))
    await settle(60)

    spoken = " ".join(turn.text for turn in live_session.buffered_turns if turn.is_moderator)
    assert name in spoken
    assert "personal contact details" in spoken
    assert PHONE_NUMBER not in spoken

    await runner.stop()


async def test_a_removed_participant_cannot_reconnect_into_the_round(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    runner.start()
    await connect_everyone(runner)

    offender = live_session.floor_holder
    runner.submit(UtteranceFinal(offender, CONTACT))
    await settle(60)

    runner.submit(ParticipantConnected(offender))
    await settle()

    assert live_session.participants[offender].is_removed
    assert offender not in [t.user_id for t in live_session.ledger.eligible]

    await runner.stop()


async def test_ordinary_technical_talk_keeps_your_seat(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    """The number that matters most: the false-positive one."""
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    runner.start()
    await connect_everyone(runner)

    speaker = live_session.floor_holder
    runner.submit(
        UtteranceFinal(
            speaker,
            "We serve about 15000 requests a day and latency fell from 2400 ms to 180 ms "
            "once we streamed the first sentence, measured over three corpora.",
        )
    )
    await settle(60)

    assert not live_session.participants[speaker].is_removed
    assert live_session.ledger.tallies[speaker].turns_taken == 1

    await runner.stop()


async def test_the_policy_can_be_switched_off(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    lenient = settings.model_copy(update={"remove_on_personal_information": False})
    runner = build_runner(live_session, plane, providers, bus, persistence, lenient)
    runner.start()
    await connect_everyone(runner)

    speaker = live_session.floor_holder
    runner.submit(UtteranceFinal(speaker, CONTACT))
    await settle(60)

    assert not live_session.participants[speaker].is_removed

    await runner.stop()


# ===================================================================== patience
async def test_a_slow_starter_gets_the_floor_back_after_being_checked_on(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    """Somebody gathering their thoughts has not declined to take part.

    The moderator used to give twelve seconds to *begin*, then hand the floor to
    somebody else — while having just promised ninety seconds out loud. It felt like
    being rushed through a queue, because it was.
    """
    patient = settings.model_copy(
        update={"silence_before_speaking_seconds": 0.2, "silence_after_nudge_seconds": 5.0}
    )
    runner = build_runner(live_session, plane, providers, bus, persistence, patient)
    runner.start()
    await connect_everyone(runner)

    first = live_session.floor_holder
    await settle(60)  # long enough for the first window to lapse

    kinds = [t.kind for t in live_session.buffered_turns]
    assert TurnKind.NUDGE in kinds, "the host never checked whether they were there"
    # The floor came back to the same person rather than moving on.
    assert plane.granted[-1] == first
    assert live_session.floor_holder == first
    assert live_session.consecutive_silent_turns == 0, "their turn was spent too early"

    await runner.stop()


async def test_a_turn_is_only_spent_after_the_second_window(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    impatient = settings.model_copy(
        update={"silence_before_speaking_seconds": 0.2, "silence_after_nudge_seconds": 0.2}
    )
    runner = build_runner(live_session, plane, providers, bus, persistence, impatient)
    runner.start()
    await connect_everyone(runner)

    first = live_session.floor_holder
    await settle(120)  # both windows lapse

    assert live_session.consecutive_silent_turns >= 1
    assert live_session.floor_holder != first, "the floor never moved on"

    await runner.stop()


async def test_speaking_up_cancels_the_check(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    runner.start()
    await connect_everyone(runner)

    first = live_session.floor_holder
    runner.submit(SpeechStarted(first))
    await settle(80)

    kinds = [t.kind for t in live_session.buffered_turns]
    assert TurnKind.NUDGE not in kinds, "the host interrupted somebody who had started"

    await runner.stop()


# ===================================================================== silence
async def test_a_room_that_never_speaks_is_closed_not_improvised(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    """Four people present, nobody says a word.

    Live, the moderator kept nudging and re-questioning forever, and with no
    contributions to draw on it began reporting its own questions back as though
    participants had made them. Giving up is the honest outcome.
    """
    live_session.budget = replace(live_session.budget, max_silent_turns=2)
    quick = settings.model_copy(
        update={"silence_before_speaking_seconds": 0.15, "silence_after_nudge_seconds": 0.15}
    )
    runner = build_runner(live_session, plane, providers, bus, persistence, quick)
    runner.start()
    await connect_everyone(runner)

    # Nobody submits anything at all; the silence timers drive the whole thing.
    await settle(300)

    assert live_session.consecutive_silent_turns >= 2
    assert persistence.statuses, "the session never reached a terminal status"
    assert persistence.statuses[-1][2] is EndReason.NOBODY_SPOKE

    await runner.stop()


async def test_an_unheard_utterance_does_not_take_the_turn_away(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    """A false start, a cough, a word too quiet to survive: not an answer, not a decision.

    Groq finalises an utterance after a pause, and if nothing usable came out it reports
    an empty final. Treating that as a completed answer is what made the moderator seem
    deaf — you began, paused for breath, and the floor had already moved on.
    """
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    runner.start()
    await connect_everyone(runner)

    holder = live_session.floor_holder
    runner.submit(UtteranceFinal(holder, ""))  # unforced: the recogniser heard nothing
    await settle()

    assert live_session.floor_holder == holder, "their turn was taken away"
    assert live_session.consecutive_silent_turns == 0
    assert live_session.ledger.tallies[holder].turns_taken == 0

    # And what they say next still lands on their own turn.
    runner.submit(UtteranceFinal(holder, "Chunk size and overlap decide recall, in my view."))
    await settle()
    assert live_session.ledger.tallies[holder].turns_taken == 1

    await runner.stop()


async def test_the_turn_cap_still_closes_a_turn_that_produced_nothing(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    """The moderator asking for the answer is different from the recogniser giving up."""
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    runner.start()
    await connect_everyone(runner)

    holder = live_session.floor_holder
    runner.submit(UtteranceFinal(holder, "", forced=True))
    await settle()

    assert live_session.floor_holder != holder
    assert live_session.consecutive_silent_turns == 1

    await runner.stop()


async def test_one_real_answer_resets_the_patience(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    runner = build_runner(live_session, plane, providers, bus, persistence, settings)
    runner.start()
    await connect_everyone(runner)

    runner.submit(UtteranceFinal(live_session.floor_holder, "", forced=True))
    await settle()
    assert live_session.consecutive_silent_turns == 1

    runner.submit(
        UtteranceFinal(
            live_session.floor_holder,
            "Retrieval quality dominates everything else, and we measured that.",
        )
    )
    await settle()
    assert live_session.consecutive_silent_turns == 0

    await runner.stop()


# ===================================================================== the pause
async def test_the_host_takes_a_beat_before_replying(
    live_session, plane, providers, bus, persistence, settings
) -> None:
    """A host that answers with zero delay reads as a machine, not a listener.

    The beat also does real work: a late final transcript, or a participant who pauses
    mid-thought and carries on, both land inside it.
    """
    thinking = settings.model_copy(update={"moderator_think_seconds": 0.25})
    runner = build_runner(live_session, plane, providers, bus, persistence, thinking)
    runner.start()
    await connect_everyone(runner)

    first = live_session.floor_holder
    started = asyncio.get_running_loop().time()
    runner.submit(UtteranceFinal(first, "It depends."))

    # Long enough for the reply to have happened if there were no pause at all.
    await settle(10)
    events = [e.type for e in await drain(bus, live_session.session_id)]
    assert EventType.MODERATOR_THINKING in events
    assert live_session.ledger.tallies[first].follow_ups_received == 0

    await settle(60)
    assert live_session.ledger.tallies[first].follow_ups_received == 1
    assert asyncio.get_running_loop().time() - started >= 0.25

    await runner.stop()


# ===================================================================== speaker labels
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Moderator: Arjun, could you expand?", "Arjun, could you expand?"),
        ("moderator - Thanks everyone.", "Thanks everyone."),
        ("Moderator — Let's begin.", "Let's begin."),
        ("  Moderator:Priya, your turn.", "Priya, your turn."),
        # Not a label: the word is part of the sentence.
        ("Moderators often struggle here.", "Moderators often struggle here."),
        ("As your moderator: I will keep time.", "As your moderator: I will keep time."),
    ],
)
def test_a_speaker_label_never_reaches_the_synthesiser(raw: str, expected: str) -> None:
    assert _strip_speaker_label(raw) == expected


async def test_the_chunker_strips_a_label_a_real_model_added() -> None:
    """gpt-4o-mini does this: it is shown "Name: text" transcripts and imitates them."""

    async def tokens():
        for piece in ["Moderator: ", "Arjun made ", "a good point. ", "Dev, your view?"]:
            yield piece

    assert [s async for s in _chunk_sentences(tokens())] == [
        "Arjun made a good point.",
        "Dev, your view?",
    ]


async def test_only_the_opening_label_is_stripped() -> None:
    """A later sentence quoting the word must survive untouched."""

    async def tokens():
        yield "Moderator: I will keep time. "
        yield "Nobody said moderator: that is wrong."

    assert [s async for s in _chunk_sentences(tokens())] == [
        "I will keep time.",
        "Nobody said moderator: that is wrong.",
    ]
