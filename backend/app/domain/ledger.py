"""Speaking-time ledger and the fairness policy built on top of it.

"Everyone gets equal speaking time" needs a definition to be enforceable. The one used
here: *the next question always goes to whoever has spoken least*, with a no-repeat rule
so the floor visibly moves around the group, and a hard cap so one long answer cannot
eat the session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID


@dataclass(slots=True)
class ParticipantTally:
    user_id: UUID
    display_name: str
    spoken_ms: int = 0
    turns_taken: int = 0
    follow_ups_received: int = 0
    connected: bool = True

    @property
    def spoken_seconds(self) -> int:
        return self.spoken_ms // 1000


@dataclass(slots=True)
class SpeakingTimeLedger:
    tallies: dict[UUID, ParticipantTally] = field(default_factory=dict)

    # ---------------------------------------------------------------- mutation
    def register(self, user_id: UUID, display_name: str) -> None:
        self.tallies.setdefault(user_id, ParticipantTally(user_id, display_name))

    def add_speech(self, user_id: UUID, duration_ms: int) -> None:
        tally = self.tallies.get(user_id)
        if tally is None:
            return
        tally.spoken_ms += max(0, duration_ms)
        tally.turns_taken += 1

    def add_follow_up(self, user_id: UUID) -> None:
        if tally := self.tallies.get(user_id):
            tally.follow_ups_received += 1

    def set_connected(self, user_id: UUID, connected: bool) -> None:
        if tally := self.tallies.get(user_id):
            tally.connected = connected

    # ---------------------------------------------------------------- queries
    @property
    def eligible(self) -> list[ParticipantTally]:
        return [t for t in self.tallies.values() if t.connected]

    @property
    def total_ms(self) -> int:
        return sum(t.spoken_ms for t in self.tallies.values())

    def min_turns(self) -> int:
        eligible = self.eligible
        return min((t.turns_taken for t in eligible), default=0)

    def fairness_spread_ms(self) -> int:
        """Gap between the most and least talkative connected participant."""
        eligible = self.eligible
        if len(eligible) < 2:
            return 0
        times = [t.spoken_ms for t in eligible]
        return max(times) - min(times)

    def everyone_has_spoken(self, minimum_turns: int) -> bool:
        eligible = self.eligible
        return bool(eligible) and all(t.turns_taken >= minimum_turns for t in eligible)

    def snapshot(self) -> list[dict[str, object]]:
        """Payload for the ``speaking_time.updated`` event — absolute values only."""
        return [
            {
                "participant_id": str(t.user_id),
                "display_name": t.display_name,
                "seconds": t.spoken_seconds,
                "turns": t.turns_taken,
                "connected": t.connected,
            }
            for t in sorted(self.tallies.values(), key=lambda t: t.display_name)
        ]


def select_next_speaker(
    ledger: SpeakingTimeLedger,
    *,
    last_speaker: UUID | None,
) -> UUID | None:
    """Least floor time wins; ties break on fewest turns, then on name for determinism.

    The previous speaker is excluded unless they are the only person still connected —
    back-to-back turns read as the moderator forgetting the room.
    """
    candidates = [t for t in ledger.eligible if t.user_id != last_speaker]
    if not candidates:
        candidates = ledger.eligible
    if not candidates:
        return None

    best = min(candidates, key=lambda t: (t.spoken_ms, t.turns_taken, t.display_name))
    return best.user_id
