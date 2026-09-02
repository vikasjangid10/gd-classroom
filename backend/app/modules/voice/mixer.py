"""The session audio mixer.

Because the moderator enforces one speaker at a time, "mixing" is almost free: at any
instant there are at most two live sources — the moderator's synthesised voice and the
floor-holder's microphone — and they are never both active. Each is kept in one shared
buffer with a per-peer read cursor, so every browser gets its own paced stream without
the server ever copying audio N times.

All audio in this module is PCM16 little-endian, mono, 48 kHz.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

try:  # ``audioop`` is a C module removed in Python 3.13
    # ``unused-ignore`` because the import resolves on 3.12 and does not on 3.13; the
    # ignore has to be correct under both without the file caring which one is running.
    import audioop  # type: ignore[import-not-found,unused-ignore]
except ImportError:  # pragma: no cover - exercised only on 3.13+
    from app.modules.voice import _audioop_shim as audioop  # type: ignore[no-redef]

SAMPLE_RATE = 48_000
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000
BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2
SILENCE = bytes(BYTES_PER_FRAME)

#: If a peer falls this far behind on a *live* source, drop what it missed rather than
#: adding latency. Applies to microphone audio only — see ``SessionMixer._take``.
MAX_LAG_BYTES = SAMPLE_RATE * 2  # 1 second


def resample(pcm: bytes, from_rate: int, to_rate: int = SAMPLE_RATE) -> bytes:
    if from_rate == to_rate:
        return pcm
    converted, _ = audioop.ratecv(pcm, 2, 1, from_rate, to_rate, None)
    return converted


@dataclass(slots=True)
class _Cursor:
    moderator: int = 0
    human: int = 0


@dataclass(slots=True)
class SessionMixer:
    """One per live session. Owned and mutated only by the session's asyncio tasks."""

    moderator: bytearray = field(default_factory=bytearray)
    human: bytearray = field(default_factory=bytearray)
    cursors: dict[UUID, _Cursor] = field(default_factory=dict)
    floor_holder: UUID | None = None

    # ---------------------------------------------------------------- peers
    def attach(self, user_id: UUID) -> None:
        """A joining peer starts at the live edge — it must not replay old audio."""
        self.cursors[user_id] = _Cursor(
            moderator=len(self.moderator), human=len(self.human)
        )

    def detach(self, user_id: UUID) -> None:
        self.cursors.pop(user_id, None)
        self._trim()

    # ---------------------------------------------------------------- writing
    def push_moderator(self, pcm: bytes, rate: int = SAMPLE_RATE) -> None:
        self.moderator += resample(pcm, rate)

    def push_human(self, pcm: bytes, rate: int = SAMPLE_RATE) -> None:
        self.human += resample(pcm, rate)

    def clear_moderator(self) -> None:
        """Barge-in: discard everything not yet played."""
        played = min((c.moderator for c in self.cursors.values()), default=0)
        del self.moderator[:played]
        for cursor in self.cursors.values():
            cursor.moderator = 0

    def moderator_backlog_ms(self) -> int:
        if not self.cursors:
            return 0
        behind = len(self.moderator) - min(c.moderator for c in self.cursors.values())
        return max(0, behind) * 1000 // (SAMPLE_RATE * 2)

    # ---------------------------------------------------------------- reading
    def read_frame(self, user_id: UUID) -> bytes:
        """One 20 ms frame for one peer: moderator audio plus whoever holds the floor."""
        cursor = self.cursors.get(user_id)
        if cursor is None:
            return SILENCE

        moderator = self._take(self.moderator, cursor, "moderator", stay_live=False)
        human = self._take(self.human, cursor, "human", stay_live=True)

        # A speaker must never be sent their own microphone back.
        if self.floor_holder == user_id:
            human = SILENCE

        if human == SILENCE:
            frame = moderator
        elif moderator == SILENCE:
            frame = human
        else:
            frame = audioop.add(moderator, human, 2)

        self._trim()
        return frame

    def _take(
        self, buffer: bytearray, cursor: _Cursor, which: str, *, stay_live: bool
    ) -> bytes:
        """One frame from one buffer, advancing that peer's cursor.

        ``stay_live`` is the whole difference between the two sources, and getting it
        wrong made the moderator almost inaudible.

        A microphone is a *live* source: it produces one second of audio per second, so a
        peer that has fallen a second behind is genuinely lagging, and skipping to the
        live edge is right — you want the conversation as it is happening, not a growing
        delay.

        Synthesised speech is not live. A five-second sentence is generated in about one
        second and lands in the buffer all at once, while the peer can only ever consume
        20 ms per frame in real time. Measured as "lag" that is instantly four seconds
        behind, so the old rule skipped to the end of every sentence — and listeners on
        WebRTC heard nothing but the last fraction of each one. The moderator's buffer is
        a queue to be played in full, never a stream to catch up with.
        """
        position = getattr(cursor, which)
        available = len(buffer) - position
        if available <= 0:
            return SILENCE
        if stay_live and available > MAX_LAG_BYTES:
            position = len(buffer) - BYTES_PER_FRAME
        end = position + BYTES_PER_FRAME
        chunk = bytes(buffer[position:end])
        setattr(cursor, which, min(end, len(buffer)))
        if len(chunk) < BYTES_PER_FRAME:
            chunk += bytes(BYTES_PER_FRAME - len(chunk))
        return chunk

    def _trim(self) -> None:
        """Reclaim what every peer has already heard."""
        if not self.cursors:
            self.moderator.clear()
            self.human.clear()
            return

        mod_min = min(c.moderator for c in self.cursors.values())
        if mod_min > BYTES_PER_FRAME * 50:
            del self.moderator[:mod_min]
            for cursor in self.cursors.values():
                cursor.moderator -= mod_min

        human_min = min(c.human for c in self.cursors.values())
        if human_min > BYTES_PER_FRAME * 50:
            del self.human[:human_min]
            for cursor in self.cursors.values():
                cursor.human -= human_min
