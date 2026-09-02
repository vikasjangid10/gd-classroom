"""Spotting personal contact details in what somebody says.

A group discussion is a room of strangers, and a live transcript is copied, summarised
and shown to everyone in it. Contact details said out loud in that setting are almost
never meant as part of the discussion — so they are caught here, the speaker is removed
from the round, and the words are never written down.

**Why this is rules and not a model.** A moderator that catches a phone number four
times out of five is worse than no protection at all, because it teaches people the room
is safe when it is not. These patterns are deterministic, they run in microseconds, they
cost nothing per turn, and every one of them can be tested. A language model asked "does
this contain personal information?" is slower, costs a request in the middle of a turn,
and is wrong in both directions — it will invent a phone number in "ninety seconds" and
wave through one it has decided is fictional.

**Two ways a number becomes personal.** Either it is long enough to be one on its own —
eight digits or more, which no measurement in a technical discussion ever is — or the
speaker has said whose it is: *"my file number is 63771346"*. The second rule is what
catches short identifiers, and it is the framing rather than the length that earns it.

**What this deliberately does not try to do.** Postal addresses, workplaces and full
names have no reliable shape, and a detector that guesses at them would eject people for
saying "I work at a bank in Pune". Contact details and self-declared identifiers are the
ones that are both dangerous and unambiguous, so they are the ones enforced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

#: What a redacted span is replaced with. Deliberately not the original length: the
#: number of digits in a phone number is itself worth not publishing.
REDACTION = "[removed]"


class PersonalInfoKind(str, Enum):
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    #: A number somebody presents as *theirs* — file number, account, roll number,
    #: Aadhaar, PAN. Short on its own, but personal because of how it is introduced.
    IDENTIFIER = "IDENTIFIER"


@dataclass(frozen=True, slots=True)
class PersonalInfoMatch:
    kind: PersonalInfoKind
    start: int
    end: int

    def describe(self) -> str:
        """A label for logs and events. Never carries the value itself."""
        return self.kind.value.lower()


# --------------------------------------------------------------------- patterns
#: An address as written: someone typing a turn, or Whisper transcribing one letter
#: at a time.
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")

#: An address as *said*: "priya at gmail dot com". Whisper writes it out in words, and
#: it is the form that actually turns up in a spoken discussion.
_SPOKEN_EMAIL = re.compile(
    r"\b[\w.+-]+\s+at\s+[\w-]+\s+dot\s+(?:com|in|org|net|co|io|edu|gov)\b",
    re.IGNORECASE,
)

#: How many bare digits in a row are personal by themselves. Eight, not ten: a mobile
#: number is ten, but a landline is eight and the identifiers people read out — file
#: numbers, account numbers — sit between the two. Eight is still far above anything
#: that turns up as a quantity in technical talk, where figures are almost always five
#: digits or fewer ("15000 requests", "2400 ms", "chunk size 512", "in 2024") and
#: larger ones are said in words.
MIN_BARE_DIGITS = 8

#: A run of digits long enough to be personal, tolerating the spaces, dashes, brackets
#: and dots people put in them. A leading country code counts, so "+91 98765 43210" and
#: "+44 20 7946 0958" are both caught.
_PHONE = re.compile(
    r"""
    (?<![\w.])                 # not mid-word, and not the tail of a decimal
    (?:\+\d{1,3}[\s.\-()]*)?   # optional country code
    (?:\d[\s.\-()]*){7,}\d     # eight or more digits, however they are spaced
    (?!\d)                     # a following full stop is sentence punctuation
    """,
    re.VERBOSE,
)

#: Nouns that make a number somebody's own rather than a measurement.
_ID_NOUN = (
    r"(?:phone|mobile|contact|whats\s?app|number|no\.?|"
    r"aadhaar|aadhar|pan|passport|licence|license|"
    r"account|card|roll|registration|enrolment|enrollment|employee|student|file|"
    r"customer|policy|ticket|reference|id|i\.?d\.?)"
)

#: "My file number is 63771346." Four digits is enough here, because the sentence has
#: already said whose number it is — that framing, not the length, is what makes it
#: personal. Without this, an eight-digit threshold would let a six-digit roll number
#: through simply for being short.
_DISCLOSED_ID = re.compile(
    rf"""
    \b(?:my|his|her|their)\s+          # presented as belonging to a person
    (?:\w+\s+){{0,2}}{_ID_NOUN}\b      # ...file number, ...account number, ...id
    [^.\n]{{0,20}}?                    # "is", ":", "would be"
    \d(?:[\s.\-/]*\d){{3,}}            # four or more digits
    """,
    re.VERBOSE | re.IGNORECASE,
)

#: Indian PAN: five letters, four digits, one letter. A shape distinctive enough that
#: nothing else in a discussion looks like it.
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")

#: Digits joined only by dots are a number — pi, a version, a price — not a phone
#: number. Anything genuinely dialled has a space, a dash, brackets or a country code.
_DECIMAL_LOOKING = re.compile(r"^\d+(?:\.\d+)+$")

#: Spoken digit-by-digit: "nine eight seven six five four three two one zero".
_DIGIT_WORD = r"(?:zero|one|two|three|four|five|six|seven|eight|nine|oh|double|triple)"
_SPOKEN_DIGITS = re.compile(
    rf"\b(?:{_DIGIT_WORD}[\s,-]+){{8,}}{_DIGIT_WORD}\b",
    re.IGNORECASE,
)

_PATTERNS: tuple[tuple[PersonalInfoKind, re.Pattern[str]], ...] = (
    (PersonalInfoKind.EMAIL, _EMAIL),
    (PersonalInfoKind.EMAIL, _SPOKEN_EMAIL),
    (PersonalInfoKind.IDENTIFIER, _DISCLOSED_ID),
    (PersonalInfoKind.IDENTIFIER, _PAN),
    (PersonalInfoKind.PHONE, _PHONE),
    (PersonalInfoKind.PHONE, _SPOKEN_DIGITS),
)


def _digit_count(text: str) -> int:
    return sum(1 for character in text if character.isdigit())


def find_personal_information(text: str) -> list[PersonalInfoMatch]:
    """Every contact detail in ``text``, earliest first, without overlaps."""
    found: list[PersonalInfoMatch] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            if pattern is _PHONE:
                token = match.group().strip()
                # The pattern permits separators, so re-count the digits themselves:
                # "1, 2, 3 and 4" must not qualify on its punctuation alone.
                if _digit_count(token) < MIN_BARE_DIGITS or _DECIMAL_LOOKING.match(token):
                    continue
            found.append(PersonalInfoMatch(kind, match.start(), match.end()))

    found.sort(key=lambda m: (m.start, -m.end))
    merged: list[PersonalInfoMatch] = []
    for span in found:
        if merged and span.start < merged[-1].end:
            continue  # already inside a span we are keeping
        merged.append(span)
    return merged


def contains_personal_information(text: str) -> bool:
    return bool(find_personal_information(text))


def redact(text: str) -> str:
    """``text`` with every contact detail replaced.

    Used wherever a phrase has to be shown or logged at all — the live caption, an
    error report — so that the thing being protected is never the thing being written
    down in the course of protecting it.
    """
    matches = find_personal_information(text)
    if not matches:
        return text

    out: list[str] = []
    cursor = 0
    for span in matches:
        out.append(text[cursor : span.start])
        out.append(REDACTION)
        cursor = span.end
    out.append(text[cursor:])
    return "".join(out)


def kinds_in(text: str) -> list[str]:
    """The kinds present, for an event payload. Never the values."""
    seen: list[str] = []
    for span in find_personal_information(text):
        label = span.describe()
        if label not in seen:
            seen.append(label)
    return seen
