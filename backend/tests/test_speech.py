"""Moderator audio for listeners who are not on WebRTC, and the pacing it drives."""

from __future__ import annotations

import array
import asyncio
import struct
import uuid
import wave
from collections.abc import AsyncIterator
from io import BytesIO

import av
import pytest

from app.modules.voice.mixer import (
    BYTES_PER_FRAME,
    SAMPLE_RATE,
    SAMPLES_PER_FRAME,
    SILENCE,
    SessionMixer,
)
from app.modules.voice.plane import PeerSlot, VoicePlane
from app.modules.voice.speech import MAX_CLIPS, SpeechCache

#: One second of 48 kHz PCM16 mono.
ONE_SECOND = bytes(SAMPLE_RATE * 2)


class ToyTts:
    """Emits a fixed amount of silence per sentence, at the mixer's own rate."""

    name = "toy"
    sample_rate = SAMPLE_RATE

    def __init__(self, seconds: float = 1.0) -> None:
        self._bytes = int(SAMPLE_RATE * 2 * seconds)

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        yield bytes(self._bytes)


async def _sentences(*items: str) -> AsyncIterator[str]:
    for item in items:
        yield item


def build_plane(seconds: float = 1.0) -> tuple[VoicePlane, list]:
    clips: list = []
    plane = VoicePlane(
        session_id=uuid.uuid4(),
        stt_provider=None,  # type: ignore[arg-type]
        tts_provider=ToyTts(seconds),
        on_transcript=lambda *_: None,
        on_connection=lambda *_: None,
        on_clip=clips.append,
    )
    return plane, clips


# ===================================================================== the cache
def test_a_clip_reports_its_own_duration() -> None:
    cache = SpeechCache()
    clip = cache.add("Hello.", ONE_SECOND)
    assert clip.duration_ms == 1000


def test_a_clip_is_a_playable_wav() -> None:
    cache = SpeechCache()
    clip = cache.add("Hello.", ONE_SECOND)

    with wave.open(BytesIO(clip.to_wav())) as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == SAMPLE_RATE
        assert handle.getnframes() == SAMPLE_RATE


def test_the_cache_is_bounded() -> None:
    """A long discussion must not accumulate its own audio in memory."""
    cache = SpeechCache(max_clips=3)
    clips = [cache.add(f"s{i}", b"\x00\x00") for i in range(5)]

    assert len(cache) == 3
    assert cache.get(clips[0].id) is None  # evicted
    assert cache.get(clips[-1].id) is not None


def test_the_default_bound_is_modest() -> None:
    assert MAX_CLIPS <= 32


# ===================================================================== the mixer
def _tone(seconds: float, value: int = 4000) -> bytes:
    """Audible PCM, so "was it delivered" is not confused with "was it silence"."""
    samples = array.array("h", [value] * int(SAMPLE_RATE * seconds))
    return samples.tobytes()


def _drain(mixer: SessionMixer, user_id, frames: int) -> bytes:
    return b"".join(mixer.read_frame(user_id) for _ in range(frames))


def test_a_whole_sentence_reaches_a_webrtc_listener() -> None:
    """The defect: listeners on WebRTC heard only the tail of every sentence.

    A five-second sentence is synthesised in about one second and arrives in the buffer
    all at once. The peer can only consume 20 ms per frame, so it looks four seconds
    "behind" immediately — and the lag rule skipped it to the live edge, throwing the
    sentence away and playing its last fragment.
    """
    mixer = SessionMixer()
    listener = uuid.uuid4()
    mixer.attach(listener)

    mixer.push_moderator(_tone(3.0))  # three seconds, delivered instantly

    # 150 frames is exactly three seconds. Every one of them must carry audio.
    heard = _drain(mixer, listener, 150)
    audible = sum(1 for i in range(0, len(heard), BYTES_PER_FRAME)
                  if heard[i : i + BYTES_PER_FRAME] != SILENCE)
    assert audible == 150, f"only {audible} of 150 frames carried the moderator's voice"


def test_a_lagging_listener_still_skips_ahead_on_live_microphone_audio() -> None:
    """The lag rule is right for a genuinely live source, and must stay."""
    mixer = SessionMixer()
    listener = uuid.uuid4()
    mixer.attach(listener)
    mixer.floor_holder = uuid.uuid4()  # somebody else is speaking

    mixer.push_human(_tone(3.0))
    mixer.read_frame(listener)

    # One frame in, the cursor has jumped to the live edge rather than working through
    # three seconds of backlog.
    assert mixer.cursors[listener].human >= len(mixer.human) - BYTES_PER_FRAME


def test_a_speaker_never_hears_their_own_microphone() -> None:
    mixer = SessionMixer()
    speaker = uuid.uuid4()
    mixer.attach(speaker)
    mixer.floor_holder = speaker
    mixer.push_human(_tone(0.1))

    assert mixer.read_frame(speaker) == SILENCE


# ===================================================================== speaking
async def test_every_sentence_becomes_a_clip_even_with_nobody_on_webrtc() -> None:
    """The case this exists for: everyone joined without a microphone.

    Before, synthesis was skipped when the mixer had no peers, so those listeners got a
    silent moderator and a caption racing past them.
    """
    plane, clips = build_plane(seconds=0.5)
    assert plane.mixer.cursors == {}

    await plane.speak(_sentences("First.", "Second."))

    assert [c.text for c in clips] == ["First.", "Second."]
    assert all(c.duration_ms == 500 for c in clips)


async def test_the_moderator_is_paced_by_the_length_of_what_it_said() -> None:
    """Without this the whole discussion scrolls past in under a second."""
    plane, _ = build_plane(seconds=0.4)
    await plane.speak(_sentences("One.", "Two."))

    started = asyncio.get_running_loop().time()
    await plane.wait_until_silent(timeout=5)
    waited = asyncio.get_running_loop().time() - started

    # Two sentences of 0.4 s each: the wait covers what has not been "played" yet.
    assert waited >= 0.5, f"returned after only {waited:.2f}s"


async def test_barge_in_drops_the_audio_that_was_not_reached() -> None:
    plane, _ = build_plane(seconds=1.0)

    async def slow() -> AsyncIterator[str]:
        yield "First."
        await asyncio.sleep(5)
        yield "Never spoken."

    task = asyncio.create_task(plane.speak(slow()))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The pacing clock is reset too, or the runner would keep waiting on audio that
    # was thrown away.
    await asyncio.wait_for(plane.wait_until_silent(timeout=2), timeout=2)


# ===================================================================== inbound tracks
class ControlledTrack:
    """A fake inbound WebRTC track: delivers ``limit`` frames of one tone, then blocks.

    Blocking rather than ending is deliberate — a real microphone track never stops on
    its own, so the only way this consumer task ever finishes is by being cancelled.
    """

    kind = "audio"

    def __init__(self, value: int, *, limit: int) -> None:
        self._value = value
        self._remaining = limit

    async def recv(self) -> av.AudioFrame:
        if self._remaining <= 0:
            await asyncio.sleep(3600)  # a live track that has simply gone quiet
        self._remaining -= 1
        frame = av.AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_FRAME)
        frame.sample_rate = SAMPLE_RATE
        tone = array.array("h", [self._value] * SAMPLES_PER_FRAME).tobytes()
        frame.planes[0].update(tone)
        return frame


def _tones(pcm: bytearray) -> list[int]:
    """One sample per frame is enough to tell which track produced it — the whole frame
    is a single repeated tone."""
    return [struct.unpack_from("<h", pcm, i)[0] for i in range(0, len(pcm), BYTES_PER_FRAME)]


async def test_a_second_track_event_replaces_the_consumer_rather_than_doubling_it() -> None:
    """The bug: a participant's voice repeating, heard by everyone else.

    A WebRTC renegotiation can fire the browser's 'track' event twice for what is, on
    the wire, the same microphone. If the first consumer task is left running, both it
    and the new one read the track and both push into the mixer's shared buffer — a
    speaker's voice playing twice, in a stutter, for as long as both survive.
    """
    plane, _ = build_plane()
    user_id = uuid.uuid4()
    plane.mixer.attach(user_id)
    plane.mixer.floor_holder = user_id
    slot = PeerSlot(user_id=user_id, pc=None)  # type: ignore[arg-type]

    first = ControlledTrack(value=1000, limit=1)
    plane._bind_consumer(slot, user_id, first)
    first_task = slot.consumer
    assert first_task is not None

    for _ in range(50):
        if len(plane.mixer.human) >= BYTES_PER_FRAME:
            break
        await asyncio.sleep(0.01)
    assert _tones(plane.mixer.human) == [1000], "the first track's frame never arrived"

    # The same connection's 'track' event fires again — the exact case that must not
    # start a second consumer alongside the first.
    second = ControlledTrack(value=2000, limit=3)
    plane._bind_consumer(slot, user_id, second)

    assert slot.consumer is not first_task, "a fresh consumer must replace the old one"
    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert first_task.cancelled(), "the superseded consumer must not keep running"

    for _ in range(50):
        if len(plane.mixer.human) >= BYTES_PER_FRAME * 4:
            break
        await asyncio.sleep(0.01)

    # Exactly the first track's one frame, then the second's three — never the first
    # track again after the handoff, and never both running at once.
    assert _tones(plane.mixer.human) == [1000, 2000, 2000, 2000]


async def test_closing_destroys_the_recording() -> None:
    """Clips are conversation state, and the brief says that dies with the session."""
    plane, clips = build_plane(seconds=0.2)
    await plane.speak(_sentences("Something."))
    assert len(plane.speech) == 1

    await plane.close()
    assert len(plane.speech) == 0
    assert plane.speech.get(clips[0].id) is None
