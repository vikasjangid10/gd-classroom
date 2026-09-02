"""Moderator audio for people who are not on the media plane.

WebRTC is the right transport for a conversation: it is low latency and it carries the
participant's microphone back. But joining it requires a microphone, and plenty of
people will open a discussion on a laptop without one — or with one they have not
granted permission to. Before this existed those people got a silent moderator and a
caption racing past, which is not a spoken group discussion at all.

So every moderator sentence is also kept here as a short clip. The browser fetches it
over ordinary HTTP and plays it, in order. The audio is identical to what the WebRTC
listeners hear, because it is the same synthesised PCM.

Clips are conversation state: capped in number, and dropped with the session.
"""

from __future__ import annotations

import io
import wave
from collections import OrderedDict
from dataclasses import dataclass

from app.core.ids import uuid7_str
from app.modules.voice.mixer import SAMPLE_RATE

#: Enough for the moderator to run several sentences ahead of a slow client, and small
#: enough that a long discussion cannot grow memory: ~20 s of 48 kHz mono per session.
MAX_CLIPS = 24


@dataclass(slots=True, frozen=True)
class SpeechClip:
    id: str
    text: str
    pcm: bytes

    @property
    def duration_ms(self) -> int:
        return len(self.pcm) * 1000 // (SAMPLE_RATE * 2)

    def to_wav(self) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(self.pcm)
        return buffer.getvalue()


class SpeechCache:
    """The last few moderator clips for one session, oldest evicted first."""

    def __init__(self, max_clips: int = MAX_CLIPS) -> None:
        self._clips: OrderedDict[str, SpeechClip] = OrderedDict()
        self._max = max_clips

    def add(self, text: str, pcm: bytes) -> SpeechClip:
        clip = SpeechClip(id=uuid7_str(), text=text, pcm=pcm)
        self._clips[clip.id] = clip
        while len(self._clips) > self._max:
            self._clips.popitem(last=False)
        return clip

    def get(self, clip_id: str) -> SpeechClip | None:
        return self._clips.get(clip_id)

    def clear(self) -> None:
        self._clips.clear()

    def __len__(self) -> int:
        return len(self._clips)
