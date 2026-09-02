"""Discussion names: human, unique, stable, and never derived from a real name."""

from __future__ import annotations

import uuid

from app.domain.aliases import ANY_NAMES, FEMALE_NAMES, MALE_NAMES, aliases_for
from app.domain.enums import Gender

SESSION = uuid.UUID("01a04264-cb9b-75f0-8234-d5566f338714")
OTHER = uuid.UUID("01a05650-1c30-76e2-9734-a6e4d429a24c")


def roster(*genders: Gender) -> dict[uuid.UUID, tuple[int, Gender]]:
    return {uuid.uuid4(): (seat, g) for seat, g in enumerate(genders, start=1)}


# ===================================================================== fit
def test_a_woman_is_given_a_womans_name() -> None:
    people = roster(Gender.FEMALE, Gender.FEMALE)
    for name in aliases_for(SESSION, people).values():
        assert name in FEMALE_NAMES


def test_a_man_is_given_a_mans_name() -> None:
    people = roster(Gender.MALE, Gender.MALE)
    for name in aliases_for(SESSION, people).values():
        assert name in MALE_NAMES


def test_declaring_nothing_still_gets_you_a_name() -> None:
    """Leaving it unset must be a first-class choice, not a broken state."""
    people = roster(Gender.UNSPECIFIED, Gender.UNSPECIFIED)
    names = aliases_for(SESSION, people)

    assert len(names) == 2
    for name in names.values():
        assert name in ANY_NAMES


def test_a_mixed_room_gets_names_that_fit_each_person() -> None:
    people = roster(Gender.MALE, Gender.FEMALE, Gender.MALE, Gender.UNSPECIFIED)
    names = aliases_for(SESSION, people)

    by_seat = {seat: names[uid] for uid, (seat, _) in people.items()}
    assert by_seat[1] in MALE_NAMES
    assert by_seat[2] in FEMALE_NAMES
    assert by_seat[3] in MALE_NAMES
    assert by_seat[4] in ANY_NAMES


def test_nobody_is_called_speaker_anything() -> None:
    """The point of the change: a discussion, not a ticket queue."""
    names = aliases_for(SESSION, roster(Gender.MALE, Gender.FEMALE))
    assert not any(name.lower().startswith("speaker") for name in names.values())


# ===================================================================== mechanics
def test_two_people_never_share_a_name() -> None:
    people = roster(*([Gender.MALE] * 4))
    names = aliases_for(SESSION, people)
    assert len(set(names.values())) == 4


def test_the_same_seat_always_gets_the_same_name() -> None:
    """It survives a reconnect, a reload and a restart, with nothing stored."""
    people = roster(Gender.MALE, Gender.FEMALE, Gender.MALE)
    assert aliases_for(SESSION, people) == aliases_for(SESSION, people)


def test_the_same_person_is_not_the_same_name_every_time() -> None:
    people = roster(Gender.FEMALE, Gender.FEMALE)
    first = aliases_for(SESSION, people)
    second = aliases_for(OTHER, people)
    assert set(first.values()) != set(second.values())


def test_the_order_people_accepted_is_not_published() -> None:
    """Seat number is acceptance order, so unshuffled pools would reveal it."""
    people = roster(*([Gender.MALE] * 4))
    by_seat = [name for _, name in sorted(
        ((seat, aliases_for(SESSION, people)[uid]) for uid, (seat, _) in people.items())
    )]
    assert by_seat != list(MALE_NAMES[:4])


def test_an_empty_roster_is_not_an_error() -> None:
    assert aliases_for(SESSION, {}) == {}


def test_no_account_name_can_reach_it() -> None:
    """The signature is the guarantee: there is nowhere to pass a real name in."""
    import inspect

    assert set(inspect.signature(aliases_for).parameters) == {"session_id", "roster"}


def test_the_pools_do_not_overlap() -> None:
    assert not set(MALE_NAMES) & set(FEMALE_NAMES)
