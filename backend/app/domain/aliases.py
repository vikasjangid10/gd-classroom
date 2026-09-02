"""Who somebody is *inside* a discussion.

A group discussion is judged on what people say. The name on an account carries region,
religion, community and family, and every one of those is something a listener — human
or model — can be swayed by without meaning to be. So the account name does not enter
the room: the session gives each seat a name of its own, and that is the only name the
other participants, the transcript, the summary and the moderator ever see.

The moderator never learns the real name at all. That is structural rather than a matter
of prompt hygiene: a name that is never put into the context cannot be repeated out of
it.

**Why real names rather than "Speaker 1".** Numbering people reads as a queue, not a
discussion — the moderator saying "Speaker 3, what do you think?" sounds like a ticketing
system, and participants stop sounding like participants. A name does the same anonymity
work while leaving the room human.

**Gender is self-declared and never guessed.** Inferring it from somebody's real name is
wrong often enough to matter, and it is wrong most often for the least common names —
the worst possible distribution for a mistake. A seat with no declared gender simply
draws from the whole pool.

**Stable without being stored.** The name is derived from the session id and the seat, so
it survives a reconnect, a reload and a process restart with no extra column and no extra
state. **Shuffled without being random.** Seat order is the order people accepted their
invitations, so handing names out in seat order would leak it; the shuffle is seeded from
the session id, which makes it unpredictable from outside and identical every time it is
computed.
"""

from __future__ import annotations

from random import Random
from uuid import UUID

from app.domain.enums import Gender

#: Deliberately not the names of anybody this project seeds, so a demo never shows a
#: call-sign that happens to match a real participant in the same room.
MALE_NAMES: tuple[str, ...] = (
    "Aarav", "Vihaan", "Ishaan", "Kabir", "Aditya", "Nikhil",
    "Siddharth", "Rohit", "Varun", "Aniket", "Harsh", "Yash",
    "Manav", "Tarun", "Zaid", "Joel",
)

FEMALE_NAMES: tuple[str, ...] = (
    "Ananya", "Ishita", "Kavya", "Nandini", "Riya", "Shreya",
    "Tanvi", "Aditi", "Diya", "Simran", "Pooja", "Sneha",
    "Fatima", "Anjali", "Leher", "Naina",
)

#: A seat that declared nothing draws from everybody. Not a third list of "neutral"
#: names — inventing one would be its own kind of labelling.
ANY_NAMES: tuple[str, ...] = tuple(sorted(MALE_NAMES + FEMALE_NAMES))

_POOLS = {
    Gender.MALE: MALE_NAMES,
    Gender.FEMALE: FEMALE_NAMES,
    Gender.UNSPECIFIED: ANY_NAMES,
}


def _as_gender(value: Gender | str | None) -> Gender:
    """Tolerant on the way in: a missing or unrecognised value is simply unspecified."""
    if isinstance(value, Gender):
        return value
    if isinstance(value, str) and value in Gender.__members__:
        return Gender[value]
    return Gender.UNSPECIFIED


def aliases_for(
    session_id: UUID,
    roster: dict[UUID, tuple[int, Gender | str | None]],
) -> dict[UUID, str]:
    """``{user_id: name}`` for one discussion.

    Seats are walked in seat order with a per-session shuffle of each pool, and a name
    already handed out is skipped — so two people in the same room can never end up
    sharing one, whatever they declared.
    """
    if not roster:
        return {}

    shuffles: dict[str, list[str]] = {}
    for kind, names_for_kind in _POOLS.items():
        shuffled = list(names_for_kind)
        Random(session_id.int + len(shuffled)).shuffle(shuffled)
        shuffles[kind.value] = shuffled

    taken: set[str] = set()
    names: dict[UUID, str] = {}
    # Seat order is deterministic, which is what makes the whole mapping reproducible
    # without storing it. The shuffle above is what stops it revealing that order.
    for user_id, (seat_no, gender) in sorted(roster.items(), key=lambda kv: kv[1][0]):
        pool = shuffles[_as_gender(gender).value]
        chosen = next((n for n in pool if n not in taken), None)
        if chosen is None:  # more people than names, which would need a huge classroom
            chosen = f"Participant {seat_no}"
        taken.add(chosen)
        names[user_id] = chosen
    return names
