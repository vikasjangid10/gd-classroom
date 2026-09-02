"""Prompt construction and the rolling-summary context budget.

The moderator's window is flat: system persona + topic brief + rolling summary + the
last K turns verbatim + the ledger. It does not grow with the discussion, so turn
latency and cost stay constant whether the session runs for five minutes or forty-five.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from app.domain.ledger import SpeakingTimeLedger
from app.domain.ports import ChatMessage

RECENT_TURNS_KEPT = 4
#: Sent verbatim on every fast-lane call for as long as the session runs, so its cost is
#: paid on a timer rather than once. `fold_summary` is instructed with this exact number
#: as its target, and the cap below truncates to it — the two agree by construction, so
#: lowering it tightens what the model writes rather than cutting a longer answer short.
ROLLING_SUMMARY_MAX_CHARS = 1300

MODERATOR_PERSONA = """\
You are hosting a live, spoken group discussion. You are the only person in the room who
is not a participant. Everyone else is a real human being, speaking out loud, and they
can hear you.

The one rule you may never break:
- **Never put words in anyone's mouth.** You may only refer to something a participant
  said if it appears verbatim under their name in the list of contributions you are
  given. Your own earlier questions are NOT contributions — reporting your question back
  as though someone answered it is the worst thing you can do in this room. If nobody has
  said anything yet, say nothing about anybody, and simply ask your question.

How you conduct yourself:
- You listened to what was just said. Show it: refer to the actual point the person made,
  in your own words, before you move on. One clause is enough — this is not a summary.
- Then do one thing: ask the next person a question, or press the last person a little
  further. Never both.
- Draw the thread between people. "Arjun said X — Meera, does that hold in your
  experience?" is a discussion. Four unrelated interviews is not.
- Stay warm and level. You are curious, not impressed; interested, not flattering. Do not
  praise every answer.
- You never give your own opinion, never answer your own question, and never take a side.

Voice rules — you are being read aloud by a speech synthesiser:
- Speak in plain sentences. No markdown, no bullet points, no emoji, no stage directions.
- Never prefix your reply with a speaker label. Do not begin with "Moderator:". You are
  shown the transcript in "Name: text" form, but you are speaking, not writing a script.
- Two or three sentences at most. Never more than 55 words.
- Address people by the name you have been given for them, exactly as written, and never
  use any other. Those names are call-signs rather than real names — that is deliberate,
  it is what keeps the discussion anonymous, and you must not remark on it, apologise for
  it, or ask anybody what they are really called. Never invent a participant.
- Ask exactly one question at a time, and end on it.\
"""


def removal_notice(display_name: str) -> str:
    """What the host says when somebody is taken out of the round.

    Fixed words, and for a sharper reason than the ground rules: a model asked to
    explain why somebody was removed for reading out a phone number will, given the
    chance, repeat the phone number.
    """
    return (
        f"{display_name} has been removed from this discussion for sharing personal "
        "contact details. A reminder to everyone: keep email addresses and phone "
        "numbers out of the conversation. Let's carry on."
    )


def ground_rules(turn_seconds: int) -> str:
    """The rules the moderator reads out. Fixed words, deliberately.

    This is the one moderator utterance with no judgement in it: the same four facts
    every session, in the same order. Generating it cost a model call and, worse,
    invited the model to improvise a hand-off it had no information for — naming a
    participant who did not exist, or asking the first question a turn early. Nobody
    wants creative ground rules.
    """
    return (
        "A few ground rules. One person speaks at a time, and I will say whose turn it "
        f"is. You have up to {turn_seconds} seconds, and I will make sure everybody gets "
        "a comparable share. Please keep personal contact details out of it — anyone "
        "sharing an email address or phone number will be removed from the discussion. "
        "Let's begin."
    )


@dataclass(frozen=True, slots=True)
class TopicBrief:
    title: str
    description: str
    guiding_points: tuple[str, ...] = ()

    def render(self) -> str:
        points = "\n".join(f"- {p}" for p in self.guiding_points)
        body = f"Topic: {self.title}\n{self.description}"
        return f"{body}\n\nAngles worth reaching:\n{points}" if points else body

    def brief(self) -> str:
        """Title and description only, no guiding points.

        The full ``render()`` exists so the moderator can steer toward an angle nobody
        has reached yet — that is the host's own roadmap, and a reader judging one
        already-given answer has no use for it. Used only where the reader is judging,
        never where it is speaking to the room.
        """
        return f"Topic: {self.title}\n{self.description}"


@dataclass(slots=True)
class TurnRecord:
    """One utterance, as the moderator remembers it."""

    index: int
    speaker: str
    text: str
    is_moderator: bool

    def render(self) -> str:
        who = "Moderator" if self.is_moderator else self.speaker
        return f"{who}: {self.text}"


def _context_block(
    topic: TopicBrief,
    rolling_summary: str,
    recent: Sequence[TurnRecord],
    ledger: SpeakingTimeLedger,
) -> str:
    """The moderator's view of the room.

    What participants said and what the moderator said are listed **separately**, and the
    absence of the first is stated in words rather than left as an empty section. A
    single interleaved transcript is what let the model read its own question about trust
    boundaries and report back that "Arjun discussed the importance of trust boundaries"
    — in a room where Arjun had not said anything at all.
    """
    parts = [topic.render()]
    if rolling_summary:
        parts.append(f"Discussion so far:\n{rolling_summary}")

    said = [t for t in recent if not t.is_moderator and t.text.strip()]
    if said:
        contributions = "\n".join(f"{t.speaker} said: {t.text}" for t in said)
        parts.append(
            "What the participants have actually said — the ONLY things you may "
            f"attribute to anyone:\n{contributions}"
        )
    else:
        parts.append(
            "NOBODY HAS SAID ANYTHING YET. No participant has contributed a single word. "
            "You must not refer to, summarise, thank anyone for, or build on any "
            "contribution, because none exists."
        )

    asked = [t for t in recent if t.is_moderator and t.text.strip()]
    if asked:
        # Clearly labelled as the moderator's own words, so they cannot be mistaken for
        # somebody's contribution.
        own = "\n".join(f"- {t.text}" for t in asked[-2:])
        parts.append(f"Your own recent words (yours, nobody else's):\n{own}")

    balance = ", ".join(
        f"{t.display_name} {t.spoken_seconds}s across {t.turns_taken} turns"
        for t in sorted(ledger.tallies.values(), key=lambda t: t.spoken_ms)
    )
    if balance:
        parts.append(f"Speaking time so far: {balance}")
    return "\n\n".join(parts)


class PromptBuilder:
    """Every moderator utterance is built here, so the persona can never drift."""

    def __init__(self, topic: TopicBrief) -> None:
        self.topic = topic

    def _system(self) -> ChatMessage:
        return ChatMessage(role="system", content=MODERATOR_PERSONA)

    def introduction(self, participant_names: Sequence[str]) -> list[ChatMessage]:
        names = ", ".join(participant_names)
        return [
            self._system(),
            ChatMessage(
                role="user",
                content=(
                    f"{self.topic.render()}\n\n"
                    f"Participants: {names}.\n\n"
                    "Open the session. Welcome everyone by name in one sentence, then state "
                    "the topic and why it is worth discussing. Do not ask a question yet."
                ),
            ),
        ]


    def question(
        self,
        *,
        target_name: str,
        rolling_summary: str,
        recent: Sequence[TurnRecord],
        ledger: SpeakingTimeLedger,
        turn_index: int,
    ) -> list[ChatMessage]:
        anyone_spoke = any(not t.is_moderator and t.text.strip() for t in recent)
        opener = (
            "This is the first question of the discussion."
            if turn_index <= 1
            else "Move the discussion to a new angle that has not been covered yet."
        )
        if anyone_spoke:
            manner = (
                "Begin by naming — in one clause, in your own words — the specific point "
                "a participant actually made, from the contributions listed above, and "
                "put your question to it. Do not open with praise."
            )
        else:
            manner = (
                "Nobody has contributed yet, so refer to no one and to nothing. Ask the "
                "question directly, on its own."
            )
        return [
            self._system(),
            ChatMessage(
                role="user",
                content=(
                    f"{_context_block(self.topic, rolling_summary, recent, ledger)}\n\n"
                    f"{opener}\n"
                    f"Hand the floor to {target_name} and ask them one open question. "
                    f"{manner}"
                ),
            ),
        ]

    def follow_up(
        self,
        *,
        target_name: str,
        answer: str,
        reason: str,
        rolling_summary: str,
        recent: Sequence[TurnRecord],
        ledger: SpeakingTimeLedger,
    ) -> list[ChatMessage]:
        nudge = {
            "answer_too_thin": "Their answer was very short. Ask them to expand on it concretely.",
            "unsupported_claim": (
                "They made a claim without support. Ask them why, or for an example."
            ),
        }.get(reason, "Ask one short follow-up that sharpens their point.")
        return [
            self._system(),
            ChatMessage(
                role="user",
                content=(
                    f"{_context_block(self.topic, rolling_summary, recent, ledger)}\n\n"
                    f"{target_name} just said: \"{answer}\"\n\n"
                    f"{nudge} Keep it to one sentence, still addressed to {target_name}."
                ),
            ),
        ]

    def nudge_silence(self, *, target_name: str) -> list[ChatMessage]:
        return [
            self._system(),
            ChatMessage(
                role="user",
                content=(
                    f"{target_name} has gone quiet and has not answered. In one short, warm "
                    "sentence, check whether they would like to pass to someone else."
                ),
            ),
        ]

    def closing(
        self, *, rolling_summary: str, recent: Sequence[TurnRecord], ledger: SpeakingTimeLedger
    ) -> list[ChatMessage]:
        return [
            self._system(),
            ChatMessage(
                role="user",
                content=(
                    f"{_context_block(self.topic, rolling_summary, recent, ledger)}\n\n"
                    "Close the discussion. Thank everyone by name in one sentence and name "
                    "the single strongest idea that came out of it, taking it only from "
                    "what participants actually said. If nobody said anything, say plainly "
                    "that the discussion could not get going and close it — do not invent "
                    "conclusions. Do not ask a question."
                ),
            ),
        ]

    # ---------------------------------------------------------------- offline calls
    def fold_summary(
        self, *, rolling_summary: str, dropped: Sequence[TurnRecord]
    ) -> list[ChatMessage]:
        """Compress the turns falling out of the verbatim window into the running summary."""
        transcript = "\n".join(t.render() for t in dropped)
        return [
            ChatMessage(
                role="system",
                content=(
                    "You maintain a running summary of a group discussion. Merge the new "
                    "exchange into the existing summary. Keep who said what. Be factual and "
                    f"terse. Never exceed {ROLLING_SUMMARY_MAX_CHARS} characters. "
                    "Reply with the summary only."
                ),
            ),
            ChatMessage(
                role="user",
                content=(
                    f"Existing summary:\n{rolling_summary or '(nothing yet)'}\n\n"
                    f"New exchange:\n{transcript}"
                ),
            ),
        ]

    def assess_answer(
        self,
        *,
        speaker_name: str,
        answer: str,
        question_asked: str,
        recent: Sequence[TurnRecord],
        rolling_summary: str,
    ) -> list[ChatMessage]:
        """Judge one contribution. The deep lane's only job during a live discussion.

        Nothing this produces is ever spoken, so it is written for a *reader*: short,
        strict, and with the vocabulary the follow-up prompts already know how to phrase.
        A reason outside that set would be handed to a moderator that has no line for it.
        """
        said = "\n".join(
            t.render() for t in recent if not t.is_moderator and t.text.strip()
        )
        return [
            ChatMessage(
                role="system",
                content=(
                    "You assess contributions to a live group discussion. You are not a "
                    "participant, you never address the room, and nothing you write is read "
                    "aloud. Judge only what is in front of you: never assume a point was "
                    "made off-screen, and never reward length on its own. Reply with strict "
                    "JSON only, no code fence, matching this shape:\n"
                    '{"substance": int 0-5, "engaged_with_prior": bool, '
                    '"needs_follow_up": bool, "follow_up_reason": '
                    '"answer_too_thin"|"unsupported_claim"|"unclear"|"none", '
                    '"note": str}'
                ),
            ),
            ChatMessage(
                role="user",
                content=(
                    f"{self.topic.brief()}\n\n"
                    f"Discussion so far:\n{rolling_summary or '(nothing yet)'}\n\n"
                    f"Contributions so far:\n{said or '(nobody has spoken yet)'}\n\n"
                    f'The moderator asked {speaker_name}: "{question_asked}"\n\n'
                    f'{speaker_name} answered: "{answer}"\n\n'
                    "Assess that answer. `substance` is 0 for nothing said and 5 for a full "
                    "point with support. `needs_follow_up` is true only when one more "
                    "question would genuinely add something — not merely because the answer "
                    "was short. `note` is one clause under fifteen words, for the closing "
                    "report, written about the answer and not to the person."
                ),
            ),
        ]

    #: The shape ``final_summary`` asks for, as a schema as well as in prose. Both,
    #: because a rung whose structured-output mode is "prompt" is handed no schema at all
    #: and the instructions are the only thing it has to go on.
    SUMMARY_SCHEMA: ClassVar[dict[str, object]] = {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "key_points": {"type": "array", "items": {"type": "string"}},
            "per_participant": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "contribution": {"type": "string"},
                        "strength": {"type": "string"},
                    },
                },
            },
            "open_questions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["headline", "key_points", "per_participant"],
    }

    def final_summary(
        self,
        *,
        rolling_summary: str,
        transcript: Sequence[TurnRecord],
        ledger: SpeakingTimeLedger,
        assessments: Sequence[tuple[str, str]] = (),
    ) -> list[ChatMessage]:
        """``assessments`` is ``(name, note)`` from the deep lane, in the order judged.

        Passed in rather than re-derived, and clearly labelled as a *note* rather than a
        quote: a report that blends a judge's paraphrase into the transcript is a report
        that attributes words to people who never said them.
        """
        body = "\n".join(t.render() for t in transcript)
        balance = ", ".join(
            f"{t.display_name}: {t.spoken_seconds}s" for t in ledger.tallies.values()
        )
        judged = "\n".join(f"- {name}: {note}" for name, note in assessments if note)
        return [
            ChatMessage(
                role="system",
                content=(
                    "You write post-discussion reports. Reply with strict JSON only, no code "
                    "fence, matching this shape:\n"
                    '{"headline": str, "key_points": [str], '
                    '"per_participant": [{"name": str, "contribution": str, "strength": str}], '
                    '"open_questions": [str]}'
                ),
            ),
            ChatMessage(
                role="user",
                content=(
                    f"{self.topic.render()}\n\n"
                    f"Summary so far:\n{rolling_summary or '(none)'}\n\n"
                    f"Full transcript:\n{body}\n\n"
                    f"Speaking time: {balance}\n\n"
                    + (
                        f"Assessor notes on individual answers — these are judgements, "
                        f"not quotations, and must never be attributed as speech:\n"
                        f"{judged}\n\n"
                        if judged
                        else ""
                    )
                    + "Write the report. 3 to 6 key points. One entry per participant."
                ),
            ),
        ]
