"""The outbound audio track each browser receives.

One track per peer, paced by its own wall clock at 20 ms per frame. The frame content
comes from the session mixer, so a peer that joins late or stalls briefly recovers by
skipping ahead rather than by accumulating latency for the rest of the call.
"""

from __future__ import annotations

import asyncio
import time
from fractions import Fraction
from uuid import UUID

from app.core.logging import get_logger
from app.modules.voice.mixer import SAMPLE_RATE, SAMPLES_PER_FRAME, SessionMixer

log = get_logger(__name__)

try:
    import av
    from aiortc import MediaStreamTrack

    AIORTC_AVAILABLE = True
except ImportError:  # pragma: no cover - voice disabled build
    AIORTC_AVAILABLE = False
    MediaStreamTrack = object  # type: ignore[assignment,misc]


class PeerOutboundTrack(MediaStreamTrack):  # type: ignore[misc,unused-ignore]
    """Moderator voice + the floor-holder's voice, as heard by one participant."""

    kind = "audio"

    def __init__(self, mixer: SessionMixer, user_id: UUID) -> None:
        super().__init__()
        self._mixer = mixer
        self._user_id = user_id
        self._pts = 0
        self._start: float | None = None
        self._time_base = Fraction(1, SAMPLE_RATE)

    async def recv(self) -> av.AudioFrame:
        if self._start is None:
            self._start = time.monotonic()

        # Pace to real time: frame N is due at start + N * 20 ms.
        target = self._start + (self._pts / SAMPLE_RATE)
        drift = target - time.monotonic()
        if drift > 0:
            await asyncio.sleep(drift)
        elif drift < -0.25:
            # Badly behind (event loop stall). Resync instead of racing to catch up.
            self._start = time.monotonic() - (self._pts / SAMPLE_RATE) - 0.02

        payload = self._mixer.read_frame(self._user_id)

        frame = av.AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_FRAME)
        frame.planes[0].update(payload)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = self._time_base
        self._pts += SAMPLES_PER_FRAME
        return frame
