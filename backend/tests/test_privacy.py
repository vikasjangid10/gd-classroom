"""Catching contact details, and — just as important — not catching anything else.

A detector that ejects somebody for saying "ninety seconds" is worse than none at all:
it punishes people for taking part. Most of this file is about what must NOT match.
"""

from __future__ import annotations

import pytest

from app.domain.privacy import (
    REDACTION,
    PersonalInfoKind,
    contains_personal_information,
    find_personal_information,
    kinds_in,
    redact,
)

# ===================================================================== caught
CONTACT_DETAILS = [
    # written
    "You can reach me at priya.sharma@gmail.com if you want the paper.",
    "mail me: arjun_k@company.co.in",
    # spoken, as Whisper writes it
    "My email is priya at gmail dot com, happy to share the benchmark.",
    "reach me at arjun dot k at outlook dot com",
    # phone, in the shapes people actually say and type
    "Call me on 9876543210.",
    "my number is 98765 43210",
    "ring +91 98765 43210 any time",
    "+919876543210",
    "98765-43210",
    "+44 20 7946 0958",
    "(0120) 456 7890",
    # spoken digit by digit
    "it's nine eight seven six five four three two one zero",
    # eight and nine digit runs: landlines, and the identifiers people read out
    "My file number is 63771346.",
    "My file number is 637717346.",
]

#: Numbers that are short, but personal because of how they were introduced.
DISCLOSED_IDENTIFIERS = [
    "My file number is 63771346.",
    "my roll number is 180245",
    "My account number is 4021 9987",
    "my employee id is 55219",
    "her registration number is 220145",
    "My PAN is ABCDE1234F",
    "my aadhaar is 2345 6789 0123",
    "My customer reference is 88-1029",
]


@pytest.mark.parametrize("said", DISCLOSED_IDENTIFIERS)
def test_a_number_someone_calls_their_own_is_personal(said: str) -> None:
    """"My file number is 63771346" is eight digits — under any phone-length rule.

    It is personal because of the words in front of it, not the length behind it.
    """
    assert contains_personal_information(said), f"missed: {said!r}"


@pytest.mark.parametrize("said", CONTACT_DETAILS)
def test_contact_details_are_caught(said: str) -> None:
    assert contains_personal_information(said), f"missed: {said!r}"


def test_the_kind_is_reported_but_never_the_value() -> None:
    said = "I'm on 9876543210 or priya@gmail.com"
    assert sorted(kinds_in(said)) == ["email", "phone"]
    # The label carries no part of the detail itself.
    assert "9876" not in "".join(kinds_in(said))
    assert "priya" not in "".join(kinds_in(said))


def test_matches_are_reported_in_order_without_overlaps() -> None:
    found = find_personal_information("mail priya@gmail.com or call 9876543210 today")
    assert [m.kind for m in found] == [PersonalInfoKind.EMAIL, PersonalInfoKind.PHONE]
    assert found[0].end <= found[1].start


# ===================================================================== not caught
ORDINARY_DISCUSSION = [
    # The exact phrases this system says out loud every single session.
    "You have up to 90 seconds, and I will make sure everybody gets a comparable share.",
    "One person speaks at a time, and I will say whose turn it is.",
    # Numbers that belong in a technical discussion.
    "We measured it across three corpora and the pattern held every time.",
    "GPT-4o mini handled it, but 4.1 was better on recall.",
    "Chunk size 512 with 64 overlap beat 1024 with 128.",
    "Latency went from 2400 ms to 180 ms after we streamed the first sentence.",
    "In 2024 the retrieval stack looked completely different.",
    "Version 1.2.3 of the spec dropped that field.",
    "We serve about 15000 requests a day at p99 of 250 milliseconds.",
    "It costs 0.15 per million tokens.",
    # Words that overlap the spoken-digit pattern without being a number.
    "One thing at a time, please.",
    "Two or three of us have seen this before.",
    # An "at" that is not an address.
    "I work at a bank in Pune and we hit this constantly.",
    "Look at the trade-off between latency and recall.",
    # "my ... number" about something that is not a person's.
    "My chunk number is 512 and the overlap is 64.",
    "My benchmark ran 40000 queries against the index.",
    "In my experience the number that matters is recall at 20.",
]


@pytest.mark.parametrize("said", ORDINARY_DISCUSSION)
def test_ordinary_talk_is_left_alone(said: str) -> None:
    assert not contains_personal_information(said), f"false positive on: {said!r}"


def test_a_long_number_is_still_a_long_number() -> None:
    """15000 is fine; a sixteen-digit run is not, whatever it is."""
    assert not contains_personal_information("we serve 15000 requests")
    assert contains_personal_information("4111 1111 1111 1111")


def test_digits_joined_only_by_dots_are_a_number_not_a_phone() -> None:
    """Pi and a long version string must not eject anybody."""
    assert not contains_personal_information("pi is 3.14159265358979 to fourteen places")
    assert not contains_personal_information("we pinned 1.22.3.4567.890123")
    # But the same digits dialled are still a phone number.
    assert contains_personal_information("call 98765 43210")


# ===================================================================== redaction
def test_redaction_removes_the_detail_and_keeps_the_sentence() -> None:
    said = "Sure, mail me at priya@gmail.com and I'll send it over."
    cleaned = redact(said)

    assert "priya@gmail.com" not in cleaned
    assert REDACTION in cleaned
    assert cleaned.startswith("Sure, mail me at ")
    assert cleaned.endswith(" and I'll send it over.")


def test_redaction_hides_how_long_the_number_was() -> None:
    """The digit count is itself worth not publishing."""
    assert redact("call 9876543210") == "call [removed]"


def test_redaction_handles_several_details_in_one_breath() -> None:
    cleaned = redact("priya@gmail.com or 9876543210, whichever is easier")
    assert cleaned.count(REDACTION) == 2
    assert "priya" not in cleaned
    assert "9876" not in cleaned


def test_redaction_leaves_clean_text_untouched() -> None:
    said = "Chunk size decides recall long before the model does."
    assert redact(said) is said or redact(said) == said
