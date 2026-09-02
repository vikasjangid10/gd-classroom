"""The session runner — one asyncio task per live discussion.

Everything that can change a session arrives here as a command on a single queue, and
this task is the only writer of session state. There is therefore no lock anywhere in
the discussion hot path, and every moderator decision is reproducible from the command
sequence alone.

Speaking never blocks the loop: a moderator utterance runs as a side task that posts
``ModeratorFinishedSpeaking`` back onto the queue when the audio has actually played out.
That is what allows a participant to interrupt mid-sentence.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID

from app.core.config import Settings
from app.core.logging import bind_contextvars, get_logger
from app.domain.enums import (
    EndReason,
    ModeratorState,
    SessionStatus,
    SummaryStatus,
    TurnKind,
)
from app.domain.events import DomainEvent, EventType, session_topic
from app.domain.ledger import select_next_speaker
from app.domain.ports import Assessment, ChatMessage
from app.domain.privacy import contains_personal_information, kinds_in, redact
from app.domain.prompts import PromptBuilder, TurnRecord, ground_rules, removal_notice
from app.domain.session import DiscussionSession
from app.domain.turn_policy import decide_follow_up, should_close, turn_deadline_seconds
from app.infrastructure.ai.factory import AiProviders
from app.modules.moderation.commands import (
    Command,
    EndSessionRequested,
    FloorReleased,
    JoinWindowExpired,
    ModeratorFinishedSpeaking,
    ModeratorSpeechFailed,
    ParticipantConnected,
    ParticipantDisconnected,
    SilenceTimeout,
    SpeechStarted,
    TranscriptPartial,
    TurnHardCap,
    UtteranceFinal,
)
from app.modules.moderation.protocols import SessionPersistence
from app.modules.notification.event_bus import EventBus
from app.modules.voice.plane import VoicePlane

log = get_logger(__name__)

#: States in which the moderator is producing audio.
_SPEAKING_STATES = frozenset(
    {
        ModeratorState.INTRODUCING,
        ModeratorState.EXPLAINING_RULES,
        ModeratorState.QUESTIONING,
        ModeratorState.FOLLOWING_UP,
        ModeratorState.CLOSING,
    }
)

_SENTENCE_END = re.compile(r"[.!?…](?=\s|$)")
_SCRIPTED_FALLBACK = {
    TurnKind.INTRO: "Welcome everyone. Let's begin today's discussion.",
    TurnKind.RULES: (
        "One person speaks at a time and I will say whose turn it is. "
        "Everyone will get an equal share."
    ),
    TurnKind.QUESTION: "Over to you — what is your take on this?",
    TurnKind.FOLLOW_UP: "Could you say a little more about that?",
    TurnKind.NUDGE: "Would you like a moment, or shall I move on?",
    TurnKind.CLOSING: "That is where we will stop. Thank you all for taking part.",
}

QUEUE_SIZE = 64
CAP_GRACE_SECONDS = 2.5
FOLD_AFTER_TURNS = 6
FOLD_BATCH = 3


class SessionRunner:
    def __init__(
        self,
        *,
        session: DiscussionSession,
        plane: VoicePlane,
        providers: AiProviders,
        bus: EventBus,
        persistence: SessionPersistence,
        settings: Settings,
    ) -> None:
        self.session = session
        self.plane = plane
        self.providers = providers
        self.bus = bus
        self.persistence = persistence
        self.settings = settings

        self.prompts = PromptBuilder(session.topic)
        self.topic = session_topic(session.session_id)

        self._queue: asyncio.Queue[Command] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._timers: dict[str, asyncio.Task] = {}
        self._speech: asyncio.Task | None = None
        self._pending_target: UUID | None = None
        #: Who has already been asked "are you still there?" for the current turn, so
        #: the check happens once rather than every silence.
        self._nudged: UUID | None = None
        self._folded_upto = 0
        self._finished = asyncio.Event()
        self._end_reason: EndReason | None = None
        self._task: asyncio.Task | None = None

    # ================================================================ public API
    def submit(self, command: Command) -> None:
        """Producer entry point. Never blocks, never raises into the caller."""
        try:
            self._queue.put_nowait(command)
        except asyncio.QueueFull:
            log.error("runner.queue_full", session=str(self.session.session_id))

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(
            self.run(), name=f"session-runner-{self.session.session_id}"
        )
        return self._task

    async def wait(self) -> None:
        await self._finished.wait()

    async def stop(self, reason: EndReason = EndReason.HOST_ENDED) -> None:
        self.submit(EndSessionRequested(reason))
        with suppress(TimeoutError):
            await asyncio.wait_for(self._finished.wait(), timeout=20)

    # ================================================================ main loop
    async def run(self) -> None:
        bind_contextvars(session_id=str(self.session.session_id))
        log.info("session.runner_start", topic=self.session.topic.title)
        try:
            self.session.status = SessionStatus.CONNECTING
            self._publish(EventType.SESSION_STATE, to=SessionStatus.CONNECTING.value)
            self._arm("join", self.settings.session_join_window_seconds, JoinWindowExpired())

            while not self._finished.is_set():
                command = await self._queue.get()
                try:
                    await self._handle(command)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("runner.command_failed", command=type(command).__name__)
                    self._publish(
                        EventType.ERROR,
                        code="moderator_error",
                        message=str(exc)[:200],
                        recoverable=True,
                    )
        except asyncio.CancelledError:
            self._end_reason = self._end_reason or EndReason.FATAL_ERROR
            raise
        finally:
            await self._teardown()

    # ================================================================ dispatch
    async def _handle(self, command: Command) -> None:
        match command:
            case ParticipantConnected(user_id=user_id):
                await self._on_connected(user_id)
            case ParticipantDisconnected(user_id=user_id):
                await self._on_disconnected(user_id)
            case JoinWindowExpired():
                await self._on_join_expired()
            case SpeechStarted(user_id=user_id):
                await self._on_speech_started(user_id)
            case TranscriptPartial(user_id=user_id, text=text):
                self._on_partial(user_id, text)
            case UtteranceFinal(user_id=user_id, text=text, forced=forced):
                await self._on_answer(user_id, text, forced=forced)
            case FloorReleased(user_id=user_id):
                await self._on_release(user_id)
            case SilenceTimeout(turn_index=index):
                await self._on_silence(index)
            case TurnHardCap(turn_index=index):
                await self._on_hard_cap(index)
            case ModeratorFinishedSpeaking() as done:
                await self._on_spoke(done)
            case ModeratorSpeechFailed(kind=kind):
                await self._on_spoke(
                    ModeratorFinishedSpeaking(kind, _SCRIPTED_FALLBACK.get(kind, ""), 0)
                )
            case EndSessionRequested(reason=reason):
                await self._begin_closing(reason)
            case _:
                log.warning("runner.unknown_command", command=type(command).__name__)

    # ================================================================ connection
    async def _on_connected(self, user_id: UUID) -> None:
        self.session.mark_connected(user_id)
        self._publish(
            EventType.PARTICIPANT_CONNECTED,
            participant_id=str(user_id),
            display_name=self.session.name_of(user_id),
            connected=self.session.connected_count,
            expected=self.session.expected_count,
        )
        if (
            self.session.status is SessionStatus.CONNECTING
            and self.session.connected_count >= self.session.expected_count
        ):
            await self._begin_discussion()

    async def _on_disconnected(self, user_id: UUID) -> None:
        self.session.mark_disconnected(user_id)
        self._publish(
            EventType.PARTICIPANT_DISCONNECTED,
            participant_id=str(user_id),
            display_name=self.session.name_of(user_id),
            connected=self.session.connected_count,
        )
        self._publish(EventType.SPEAKING_TIME_UPDATED, participants=self.session.ledger.snapshot())

        if self.session.status is not SessionStatus.ACTIVE:
            return
        if self.session.connected_count < 2:
            await self._begin_closing(EndReason.NOT_ENOUGH_PARTICIPANTS)
            return
        if self.session.floor_holder == user_id:
            # The speaker vanished mid-turn. Bank what they said and move on.
            await self._close_turn(user_id, text="", interrupted=True)
            await self._next_turn()

    async def _on_join_expired(self) -> None:
        if self.session.status is not SessionStatus.CONNECTING:
            return
        if self.session.connected_count >= 2:
            log.warning("session.partial_start", connected=self.session.connected_count)
            await self._begin_discussion()
        else:
            self._end_reason = EndReason.JOIN_TIMEOUT
            self.session.status = SessionStatus.ABORTED
            self._finished.set()

    # ================================================================ discussion
    async def _begin_discussion(self) -> None:
        self._cancel_timer("join")
        self.session.status = SessionStatus.ACTIVE
        self.session.started_at = datetime.now(UTC)
        await self.persistence.mark_active(self.session.session_id)
        self._publish(EventType.SESSION_STATE, to=SessionStatus.ACTIVE.value)

        names = [p.display_name for p in self.session.participants.values() if p.is_present]
        self.session.moderator_state = ModeratorState.INTRODUCING
        self._speak(TurnKind.INTRO, self.prompts.introduction(names))

    async def _on_spoke(self, done: ModeratorFinishedSpeaking) -> None:
        """A moderator utterance finished playing. Advance."""
        if done.text:
            turn = self.session.record_turn(
                kind=done.kind, text=done.text, speaker_user_id=None, duration_ms=done.duration_ms
            )
            self._publish(
                EventType.TRANSCRIPT_FINAL,
                turn_index=turn.turn_index,
                speaker="moderator",
                participant_id=None,
                display_name="Moderator",
                text=done.text,
                kind=done.kind.value,
            )

        match done.kind:
            case TurnKind.INTRO:
                self.session.moderator_state = ModeratorState.EXPLAINING_RULES
                self._say(TurnKind.RULES, ground_rules(self.settings.turn_max_seconds))
            case TurnKind.RULES:
                await self._next_turn()
            case TurnKind.QUESTION | TurnKind.FOLLOW_UP:
                if self._pending_target is not None:
                    await self._grant_floor(self._pending_target)
            case TurnKind.NUDGE:
                # The nudge was addressed to somebody. Give them the floor back rather
                # than asking "are you there?" and immediately calling on someone else.
                if self._pending_target is not None:
                    await self._grant_floor(
                        self._pending_target,
                        listen_seconds=self.settings.silence_after_nudge_seconds,
                    )
                else:
                    await self._next_turn()
            case TurnKind.CLOSING:
                await self._summarize()

    async def _next_turn(self) -> None:
        if self.session.status is not SessionStatus.ACTIVE:
            return

        close, reason = should_close(
            ledger=self.session.ledger,
            budget=self.session.budget,
            elapsed_seconds=self.session.elapsed_seconds,
            consecutive_silent_turns=self.session.consecutive_silent_turns,
        )
        if close:
            await self._begin_closing(EndReason(reason))
            return

        target = select_next_speaker(self.session.ledger, last_speaker=self.session.last_speaker)
        if target is None:
            await self._begin_closing(EndReason.NOT_ENOUGH_PARTICIPANTS)
            return

        self._pending_target = target
        self.session.moderator_state = ModeratorState.QUESTIONING
        self._speak(
            TurnKind.QUESTION,
            self.prompts.question(
                target_name=self.session.name_of(target),
                rolling_summary=self.session.rolling_summary,
                recent=list(self.session.recent_turns),
                ledger=self.session.ledger,
                turn_index=self.session.turn_index,
            ),
        )

    async def _grant_floor(self, user_id: UUID, *, listen_seconds: float | None = None) -> None:
        """Hand over the floor and start listening.

        Two timers, and confusing them is what made the moderator feel like it was
        rushing everyone: ``silence`` is how long somebody has to *begin*, and ``cap``
        is how long a turn may run once it is under way. The cap is the ninety seconds
        the host promises out loud; the silence window used to be a hardcoded twelve,
        which meant the promise was never the thing being enforced.
        """
        self.session.floor_holder = user_id
        self.session.floor_granted_at = datetime.now(UTC)
        self.session.moderator_state = ModeratorState.LISTENING
        await self.plane.grant_floor(user_id)

        seconds = turn_deadline_seconds(self.session.budget, self.session.ledger)
        to_begin = (
            listen_seconds
            if listen_seconds is not None
            else self.settings.silence_before_speaking_seconds
        )
        self._publish(
            EventType.FLOOR_GRANTED,
            participant_id=str(user_id),
            display_name=self.session.name_of(user_id),
            max_seconds=seconds,
            seconds_to_begin=int(to_begin),
            turn_index=self.session.turn_index,
        )
        self._arm("silence", to_begin, SilenceTimeout(self.session.turn_index))
        self._arm("cap", float(seconds), TurnHardCap(self.session.turn_index))

    # ================================================================ listening
    async def _on_speech_started(self, user_id: UUID) -> None:
        if self.session.moderator_state is ModeratorState.LISTENING:
            if user_id == self.session.floor_holder:
                self._cancel_timer("silence")
            return

        # Barge-in: only the person the moderator just addressed may cut in.
        if self.session.moderator_state in _SPEAKING_STATES and user_id == self._pending_target:
            self._interrupt()
            await self._grant_floor(user_id)

    def _on_partial(self, user_id: UUID, text: str) -> None:
        if user_id != self.session.floor_holder or not text:
            return
        self._publish(
            EventType.TRANSCRIPT_PARTIAL,
            participant_id=str(user_id),
            display_name=self.session.name_of(user_id),
            # The live caption goes to every screen in the room, and it is published
            # several seconds before the final transcript that triggers a removal. If it
            # went out untouched, the number would already be on four screens by the time
            # anybody was removed for saying it.
            text=redact(text),
        )

    async def _on_silence(self, turn_index: int) -> None:
        if (
            turn_index != self.session.turn_index
            or self.session.moderator_state is not ModeratorState.LISTENING
        ):
            return
        target = self.session.floor_holder
        if target is None:
            return
        await self._release_floor()

        if self._nudged is not target:
            # First silence. Check whether they are still there and hand the floor
            # straight back — a person who was gathering their thoughts, or looking for
            # the unmute button, has not declined to take part. Moving on here was why
            # the moderator felt like it was racing through the room: one twelve-second
            # window and your turn was gone.
            self._nudged = target
            self._pending_target = target
            self.session.moderator_state = ModeratorState.FOLLOWING_UP
            self._speak(
                TurnKind.NUDGE,
                self.prompts.nudge_silence(target_name=self.session.name_of(target)),
            )
            return

        # Asked directly, and still nothing. Now the turn is spent.
        self._nudged = None
        self._pending_target = None
        self.session.consecutive_silent_turns += 1
        # Recording the empty turn is what moves the rotation on. Without it the
        # quietest person permanently has the least speaking time, so the round-robin
        # keeps handing the floor back to exactly the person who is not using it.
        self.session.ledger.add_speech(target, 0)
        self.session.last_speaker = target
        await self._next_turn()

    async def _on_hard_cap(self, turn_index: int) -> None:
        if (
            turn_index != self.session.turn_index
            or self.session.moderator_state is not ModeratorState.LISTENING
        ):
            return
        await self.plane.flush_stt()
        # Give the provider a moment to emit the final transcript before giving up on it.
        self._arm(
            "cap_grace",
            CAP_GRACE_SECONDS,
            UtteranceFinal(self.session.floor_holder, "", forced=True),  # type: ignore[arg-type]
        )

    async def _on_release(self, user_id: UUID) -> None:
        if user_id != self.session.floor_holder:
            return
        await self.plane.flush_stt()
        self._arm("cap_grace", CAP_GRACE_SECONDS, UtteranceFinal(user_id, "", forced=True))

    async def _on_answer(self, user_id: UUID, text: str, *, forced: bool = False) -> None:
        if user_id != self.session.floor_holder:
            return  # a late transcript for a turn that is already closed

        if not text.strip() and not forced:
            # The recogniser finished an utterance and got nothing out of it — a false
            # start, a cough, a word too quiet to survive. That is not an answer, and it
            # is certainly not a decision to stop talking. Keep the floor where it is and
            # give them the rest of their turn.
            #
            # Treating this as a completed answer is what made the moderator feel deaf:
            # somebody would begin, pause for breath, and find the floor had already
            # moved on without a word of theirs being heard.
            log.info("turn.nothing_heard", user=str(user_id), turn=self.session.turn_index)
            self._arm(
                "silence",
                self.settings.silence_after_nudge_seconds,
                SilenceTimeout(self.session.turn_index),
            )
            return

        if self.settings.remove_on_personal_information and contains_personal_information(text):
            await self._remove_for_privacy(user_id, text)
            return

        await self._close_turn(user_id, text=text, interrupted=False)
        await self._consider()

        clean = text.strip()
        if clean:
            assessment = await self._assess(user_id, clean)
            decision = decide_follow_up(
                answer=clean,
                ledger=self.session.ledger,
                speaker_id=user_id,
                budget=self.session.budget,
                elapsed_seconds=self.session.elapsed_seconds,
                assessment=assessment,
            )
            if decision.should_follow_up:
                self.session.ledger.add_follow_up(user_id)
                self._pending_target = user_id
                self.session.moderator_state = ModeratorState.FOLLOWING_UP
                self._speak(
                    TurnKind.FOLLOW_UP,
                    self.prompts.follow_up(
                        target_name=self.session.name_of(user_id),
                        answer=clean,
                        reason=decision.reason,
                        rolling_summary=self.session.rolling_summary,
                        recent=list(self.session.recent_turns),
                        ledger=self.session.ledger,
                    ),
                )
                return

        await self._maybe_fold_summary()
        await self._next_turn()

    async def _remove_for_privacy(self, user_id: UUID, said: str) -> None:
        """Take somebody out of the round, immediately, for reading out contact details.

        Three things happen, and the order is the point:

        1. **The words are dropped.** Not redacted into the transcript, not summarised,
           not logged — dropped. Persisting a phone number in the course of protecting it
           would be the whole failure. Only the *kind* of detail travels anywhere.
        2. **The floor is taken back and the seat is closed**, so a reconnect cannot put
           them back in the round.
        3. **The room is told, in fixed words.** The announcement is a constant rather
           than a generated line, because a model asked to explain the removal will
           happily quote the thing that caused it.
        """
        kinds = kinds_in(said)
        log.warning(
            "turn.removed_for_privacy",
            user=str(user_id),
            kinds=kinds,
            turn=self.session.turn_index,
        )

        self._cancel_timer("silence")
        self._cancel_timer("cap")
        self._cancel_timer("cap_grace")
        await self._release_floor()

        name = self.session.name_of(user_id)
        self.session.mark_removed(user_id)
        self._nudged = None
        self._pending_target = None
        self.session.last_speaker = user_id
        with suppress(Exception):
            await self.plane.remove_peer(user_id)

        self._publish(
            EventType.PARTICIPANT_REMOVED,
            participant_id=str(user_id),
            display_name=name,
            reason="SHARED_PERSONAL_INFORMATION",
            kinds=kinds,
            connected=self.session.connected_count,
        )
        self._publish(EventType.SPEAKING_TIME_UPDATED, participants=self.session.ledger.snapshot())

        if self.session.status is not SessionStatus.ACTIVE:
            return

        # Announce it either way, then let the normal path decide what happens next:
        # with nobody left to hold a discussion, ``_next_turn`` closes the session on
        # its own. Explaining before closing matters — the people still in the room
        # should not simply find themselves alone.
        self.session.moderator_state = ModeratorState.FOLLOWING_UP
        self._say(TurnKind.NUDGE, removal_notice(name))

    async def _consider(self) -> None:
        """A beat between someone finishing and the moderator answering.

        A human host does not begin talking the instant the last syllable lands: they
        take a moment, and everyone can hear them taking it. Replying with zero delay is
        the single thing that makes an AI moderator feel like a machine reading a script
        rather than somebody who was listening.

        It also buys something concrete — a late final transcript, or a participant who
        pauses mid-thought and carries on, both arrive inside this window.
        """
        pause = self.settings.moderator_think_seconds
        if pause <= 0:
            return
        self.session.moderator_state = ModeratorState.EVALUATING
        self._publish(EventType.MODERATOR_THINKING, seconds=pause)
        await asyncio.sleep(pause)

    async def _close_turn(self, user_id: UUID, *, text: str, interrupted: bool) -> None:
        self._cancel_timer("silence")
        self._cancel_timer("cap")
        self._cancel_timer("cap_grace")

        duration_ms = self.session.floor_held_ms()
        self.session.ledger.add_speech(user_id, duration_ms)
        self.session.last_speaker = user_id
        self.session.moderator_state = ModeratorState.EVALUATING

        clean = text.strip()
        # Tracked here rather than inferred later: a turn that yields no words is the
        # signal that the room is not actually talking, and it is the only thing standing
        # between "nobody spoke" and a moderator inventing a discussion.
        self.session.consecutive_silent_turns = 0 if clean else (
            self.session.consecutive_silent_turns + 1
        )
        if clean:
            self._nudged = None  # they spoke; the next silence deserves its own check
        if clean:
            turn = self.session.record_turn(
                kind=TurnKind.ANSWER,
                text=clean,
                speaker_user_id=user_id,
                duration_ms=duration_ms,
            )
            self._publish(
                EventType.TRANSCRIPT_FINAL,
                turn_index=turn.turn_index,
                speaker="participant",
                participant_id=str(user_id),
                display_name=self.session.name_of(user_id),
                text=clean,
                kind=TurnKind.ANSWER.value,
            )

        await self._release_floor()
        self._publish(
            EventType.SPEAKING_TIME_UPDATED, participants=self.session.ledger.snapshot()
        )
        if interrupted:
            log.info("turn.interrupted", user=str(user_id))

    async def _release_floor(self) -> None:
        previous = self.session.floor_holder
        self.session.floor_holder = None
        self.session.floor_granted_at = None
        await self.plane.release_floor()
        if previous is not None:
            self._publish(EventType.FLOOR_RELEASED, participant_id=str(previous))

    # ================================================================ closing
    async def _begin_closing(self, reason: EndReason) -> None:
        if self.session.moderator_state in (ModeratorState.CLOSING, ModeratorState.SUMMARIZING):
            return
        self._end_reason = reason
        self._cancel_all_timers()
        self._interrupt()
        await self._release_floor()

        self.session.moderator_state = ModeratorState.CLOSING
        self._speak(
            TurnKind.CLOSING,
            self.prompts.closing(
                rolling_summary=self.session.rolling_summary,
                recent=list(self.session.recent_turns),
                ledger=self.session.ledger,
            ),
        )

    async def _summarize(self) -> None:
        self.session.moderator_state = ModeratorState.SUMMARIZING
        self.session.status = SessionStatus.SUMMARIZING
        self._publish(EventType.SESSION_STATE, to=SessionStatus.SUMMARIZING.value)

        # The transcript is written before the summary is attempted: a failed summary
        # must never cost the discussion.
        await self.persistence.flush_transcript(self.session)

        # The report is reasoning about a whole discussion, not a line to be spoken, so it
        # goes to the deep lane — the same chain that judged the answers it is built on —
        # and through the *structured* path, because the models worth putting on that lane
        # will otherwise fence the JSON or think until the budget is gone. Nobody is
        # waiting on audio here, which is why this is the one call allowed 45 seconds.
        messages = self.prompts.final_summary(
            rolling_summary=self.session.rolling_summary,
            transcript=self.session.transcript(),
            ledger=self.session.ledger,
            assessments=[
                (self.session.name_of(user_id), assessment.note)
                for user_id, assessment in self.session.assessments
            ],
        )
        summary: dict | None = None
        model = self.providers.llm.name
        error: str | None = None
        try:
            if self.providers.deep is not None:
                summary, model = await asyncio.wait_for(
                    self.providers.deep.write_report(
                        messages, schema=PromptBuilder.SUMMARY_SCHEMA
                    ),
                    timeout=45,
                )
            else:
                # The scripted moderator, which has no lanes and answers in one shape.
                raw = await asyncio.wait_for(
                    self.providers.llm.complete(messages, temperature=0.2, max_tokens=900),
                    timeout=45,
                )
                summary = _parse_json(raw)
            if summary is None:
                error = "The model did not return valid JSON."
        except Exception as exc:
            error = str(exc)[:300]
            log.error("summary.failed", error=error)

        await self.persistence.save_summary(
            self.session.session_id,
            summary=summary,
            model=model,
            error=error,
        )
        self._publish(
            EventType.SESSION_SUMMARY_READY,
            status=(SummaryStatus.READY if summary else SummaryStatus.FAILED).value,
        )
        self.session.status = SessionStatus.ENDED
        self._finished.set()

    # ================================================================ speaking
    def _speak(self, kind: TurnKind, messages: list[ChatMessage]) -> None:
        self._interrupt()
        self._speech = asyncio.create_task(
            self._speak_task(kind, messages), name=f"speak-{kind.value}"
        )

    def _say(self, kind: TurnKind, text: str) -> None:
        """Speak fixed words. No model, no stream, no chance of improvisation.

        Used for the ground rules, which are the same every session and which a language
        model has no reason to author. Asking one to do it cost a request per discussion
        and reliably produced a hand-off the moderator was not ready to make — an
        invented name, or the first question asked a turn early, of whoever happened to
        be listed first. Content that never varies should not be generated.
        """
        self._interrupt()
        self._speech = asyncio.create_task(
            self._say_task(kind, text), name=f"say-{kind.value}"
        )

    async def _say_task(self, kind: TurnKind, text: str) -> None:
        started = asyncio.get_running_loop().time()
        try:
            self._publish(EventType.MODERATOR_SPEAKING, text=text, is_final=False, kind=kind.value)
            await self.plane.speak(_sentences_of(text))
            await self.plane.wait_until_silent()
            duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            self._publish(EventType.MODERATOR_SPEAKING, text=text, is_final=True, kind=kind.value)
            self.submit(ModeratorFinishedSpeaking(kind, text, duration_ms))
        except asyncio.CancelledError:
            self._publish(EventType.MODERATOR_INTERRUPTED, reason="barge_in", kind=kind.value)
            raise
        except Exception as exc:
            log.error("say.failed", kind=kind.value, error=str(exc))
            self.submit(ModeratorFinishedSpeaking(kind, text, 0))

    async def _speak_task(self, kind: TurnKind, messages: list[ChatMessage]) -> None:
        started = asyncio.get_running_loop().time()
        collected: list[str] = []
        try:
            stream = self.providers.llm.stream(messages, temperature=0.7, max_tokens=220)

            async def sentences() -> AsyncIterator[str]:
                async for sentence in _chunk_sentences(stream):
                    collected.append(sentence)
                    self._publish(
                        EventType.MODERATOR_SPEAKING, text=sentence, is_final=False, kind=kind.value
                    )
                    yield sentence

            await self.plane.speak(sentences())
            await self.plane.wait_until_silent()

            text = " ".join(collected).strip() or _SCRIPTED_FALLBACK.get(kind, "")
            duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            self._publish(EventType.MODERATOR_SPEAKING, text=text, is_final=True, kind=kind.value)
            self.submit(ModeratorFinishedSpeaking(kind, text, duration_ms))
        except asyncio.CancelledError:
            self._publish(EventType.MODERATOR_INTERRUPTED, reason="barge_in", kind=kind.value)
            raise
        except Exception as exc:
            log.error("speak.failed", kind=kind.value, error=str(exc))
            fallback = _SCRIPTED_FALLBACK.get(kind, "")
            if fallback:
                with suppress(Exception):
                    await self.plane.speak(_single(fallback))
                    await self.plane.wait_until_silent()
            self.submit(ModeratorFinishedSpeaking(kind, fallback, 0))

    def _interrupt(self) -> None:
        if self._speech and not self._speech.done():
            self._speech.cancel()
        self._speech = None
        self.plane.mixer.clear_moderator()

    # ================================================================ judgement
    async def _assess(self, user_id: UUID, answer: str) -> Assessment | None:
        """Ask the deep lane what it made of that answer, within a hard deadline.

        This runs in the one gap the room can feel — between a participant stopping and
        the moderator replying — so it is bounded twice over: the deep chain has its own
        per-rung ceilings, and this refuses to wait past
        ``LLM_ASSESSMENT_TIMEOUT_SECONDS`` regardless. On expiry the word-count heuristic
        decides, exactly as it did before this existed. **Nothing here may raise**: a
        judge having a bad day must cost a cruder follow-up, never a stalled discussion.
        """
        if self.providers.deep is None or not self.settings.llm_assessment_enabled:
            return None

        asked = next(
            (t.text for t in reversed(self.session.recent_turns) if t.is_moderator), ""
        )
        started = time.monotonic()
        try:
            assessment = await asyncio.wait_for(
                self.providers.deep.assess(
                    self.prompts.assess_answer(
                        speaker_name=self.session.name_of(user_id),
                        answer=answer,
                        question_asked=asked,
                        recent=list(self.session.recent_turns),
                        rolling_summary=self.session.rolling_summary,
                    )
                ),
                timeout=self.settings.llm_assessment_timeout_seconds,
            )
        except TimeoutError:
            log.info("assessment.timed_out", turn=self.session.turn_index)
            return None
        except Exception as exc:
            log.warning("assessment.failed", error=str(exc)[:200])
            return None

        if assessment is None:
            return None
        self.session.assessments.append((user_id, assessment))
        log.info(
            "assessment.done",
            turn=self.session.turn_index,
            tier=assessment.tier,
            substance=assessment.substance,
            follow_up=assessment.needs_follow_up,
            reason=assessment.follow_up_reason,
            took_ms=int((time.monotonic() - started) * 1000),
        )
        return assessment

    # ================================================================ context budget
    async def _maybe_fold_summary(self) -> None:
        pending = len(self.session.buffered_turns) - self._folded_upto
        if pending <= FOLD_AFTER_TURNS:
            return
        batch = self.session.buffered_turns[self._folded_upto : self._folded_upto + FOLD_BATCH]
        self._folded_upto += len(batch)
        records = [
            TurnRecord(t.turn_index, t.speaker_name, t.text, t.is_moderator) for t in batch
        ]
        try:
            folded = await asyncio.wait_for(
                self.providers.llm.complete(
                    self.prompts.fold_summary(
                        rolling_summary=self.session.rolling_summary, dropped=records
                    ),
                    temperature=0.1,
                    max_tokens=350,
                ),
                timeout=12,
            )
            self.session.rolling_summary = folded.strip()[:1800]
        except Exception as exc:
            log.warning("summary.fold_failed", error=str(exc))

    # ================================================================ timers
    def _arm(self, name: str, seconds: float, command: Command) -> None:
        self._cancel_timer(name)

        async def fire() -> None:
            try:
                await asyncio.sleep(seconds)
                self._timers.pop(name, None)
                self.submit(command)
            except asyncio.CancelledError:
                raise

        self._timers[name] = asyncio.create_task(fire(), name=f"timer-{name}")

    def _cancel_timer(self, name: str) -> None:
        task = self._timers.pop(name, None)
        if task and not task.done():
            task.cancel()

    def _cancel_all_timers(self) -> None:
        for name in list(self._timers):
            self._cancel_timer(name)

    # ================================================================ events
    def _publish(self, event_type: str, **payload: object) -> None:
        self.bus.publish(DomainEvent(topic=self.topic, type=event_type, payload=payload))

    # ================================================================ teardown
    async def _teardown(self) -> None:
        self._cancel_all_timers()
        self._interrupt()
        if self._speech:
            with suppress(asyncio.CancelledError, Exception):
                await self._speech

        aborted = self.session.status is SessionStatus.ABORTED
        reason = self._end_reason or (
            EndReason.FATAL_ERROR if aborted else EndReason.COMPLETED
        )

        try:
            if aborted:
                await self.persistence.mark_status(
                    self.session.session_id, SessionStatus.ABORTED, end_reason=reason
                )
            else:
                self.session.ended_at = datetime.now(UTC)
                await self.persistence.mark_status(
                    self.session.session_id, SessionStatus.ENDED, end_reason=reason
                )
            await self.persistence.complete_classroom(self.session.session_id, aborted=aborted)
        except Exception:
            log.exception("session.persist_failed")

        with suppress(Exception):
            await self.plane.close()

        self._publish(
            EventType.SESSION_ENDED,
            reason=reason.value,
            duration_s=self.session.elapsed_seconds,
            aborted=aborted,
        )
        # The replay ring holds transcript fragments; it is conversation data too.
        self.bus.drop_topic(self.topic)
        self.session.wipe()
        self._finished.set()
        log.info("session.runner_stop", reason=reason.value)


# ===================================================================== helpers
async def _chunk_sentences(stream: AsyncIterator[str]) -> AsyncIterator[str]:
    """Turn a token stream into sentences so synthesis can start before generation ends."""
    buffer = ""
    first = True
    async for delta in stream:
        buffer += delta
        while True:
            match = _SENTENCE_END.search(buffer)
            if not match:
                break
            head, buffer = buffer[: match.end()], buffer[match.end() :]
            if first:
                head, first = _strip_speaker_label(head), False
            if head.strip():
                yield head.strip()
        if len(buffer) > 140:  # a very long clause: flush at the last comma
            cut = buffer.rfind(", ")
            if cut > 40:
                head = buffer[: cut + 1]
                if first:
                    head, first = _strip_speaker_label(head), False
                yield head.strip()
                buffer = buffer[cut + 1 :]
    if buffer.strip():
        yield (_strip_speaker_label(buffer) if first else buffer).strip()


#: The moderator sees the transcript as "Name: text", and a real model will sometimes
#: imitate that format in its own reply. The persona forbids it, but a prompt is a
#: request, not a guarantee — and this text goes straight to a speech synthesiser, which
#: would cheerfully say the word "Moderator" out loud to four people.
#: Colon, hyphen, en dash and em dash: models reach for all four. The dashes are written
#: as escapes because they are indistinguishable from a hyphen in most editors.
_SPEAKER_LABEL = re.compile("^\\s*moderator\\s*[:\\-\u2013\u2014]\\s*", re.IGNORECASE)


def _strip_speaker_label(text: str) -> str:
    return _SPEAKER_LABEL.sub("", text, count=1)


async def _single(text: str) -> AsyncIterator[str]:
    yield text


async def _sentences_of(text: str) -> AsyncIterator[str]:
    """Split fixed text the same way a model stream is split, so TTS behaves identically."""
    rest = text
    while match := _SENTENCE_END.search(rest):
        head, rest = rest[: match.end()], rest[match.end() :]
        if head.strip():
            yield head.strip()
    if rest.strip():
        yield rest.strip()


def _parse_json(raw: str) -> dict | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None
