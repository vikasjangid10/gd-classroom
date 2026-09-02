"""WebRTC termination and floor routing for one session.

Every browser opens exactly one ``RTCPeerConnection`` to this process. The server needs
the raw audio (the moderator's decisions are made from it) and needs to publish audio of
its own (the moderator has a voice), so a peer-to-peer mesh is not an option.

What would normally be an SFU is trivial here: the turn-taking rule guarantees at most
one human speaker, so routing is "relay the floor-holder, drop everyone else", and
transcription only ever runs for one participant at a time.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from uuid import UUID

from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.domain.ports import STT_SAMPLE_RATE, SttProvider, SttStream, Transcript, TtsProvider
from app.modules.voice.mixer import SAMPLE_RATE, SessionMixer, resample
from app.modules.voice.speech import SpeechCache, SpeechClip
from app.modules.voice.tracks import AIORTC_AVAILABLE, PeerOutboundTrack

log = get_logger(__name__)

if AIORTC_AVAILABLE:
    import av
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

TranscriptHandler = Callable[[UUID, Transcript], None]
ConnectionHandler = Callable[[UUID, bool], None]
ClipHandler = Callable[[SpeechClip], None]


@dataclass
class PeerSlot:
    user_id: UUID
    pc: RTCPeerConnection
    consumer: asyncio.Task | None = None
    connected: bool = False


@dataclass
class VoicePlane:
    """Owns every peer connection belonging to a single session."""

    session_id: UUID
    stt_provider: SttProvider
    tts_provider: TtsProvider
    on_transcript: TranscriptHandler
    on_connection: ConnectionHandler
    #: Called when a moderator sentence has been synthesised, so the runner can tell
    #: browsers where to fetch it. Optional: the fakes in the tests do not use it.
    on_clip: ClipHandler | None = None

    mixer: SessionMixer = field(default_factory=SessionMixer)
    speech: SpeechCache = field(default_factory=SpeechCache)
    peers: dict[UUID, PeerSlot] = field(default_factory=dict)
    _stt: SttStream | None = None
    _stt_owner: UUID | None = None
    _speech_task: asyncio.Task | None = None
    #: One lock per user, so two overlapping join attempts for the *same* person cannot
    #: interleave their setup. Without it, request B's ``remove_peer`` can land between
    #: request A finishing setup and A's caller actually seeing the answer — tearing down
    #: a connection whose SDP answer had already been sent to the browser. See
    #: ``add_peer``.
    _join_locks: dict[UUID, asyncio.Lock] = field(default_factory=dict)
    #: Loop time at which the audio synthesised so far will have finished playing.
    _audio_until: float = 0.0
    _closed: bool = False

    # ================================================================ peers
    async def add_peer(self, user_id: UUID, sdp: str, sdp_type: str) -> dict[str, str]:
        if not AIORTC_AVAILABLE:  # pragma: no cover
            raise ExternalServiceError("webrtc", "aiortc is not installed in this build.")
        if self._closed:
            raise ExternalServiceError("webrtc", "This session is closed.")

        # Serialised per user: two joins for the same person racing each other must not
        # interleave their setup, or the second's `remove_peer` can tear down the first's
        # connection *after* its SDP answer has already gone back to a browser that is
        # now negotiating against a peer connection which no longer exists on our side.
        lock = self._join_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            await self.remove_peer(user_id)  # a rejoin replaces the old connection

            ice = [
                RTCIceServer(urls=server["urls"], username=server.get("username"),  # type: ignore[arg-type]
                             credential=server.get("credential"))  # type: ignore[arg-type]
                for server in settings.ice_servers()
            ]
            pc = RTCPeerConnection(RTCConfiguration(iceServers=ice))
            slot = PeerSlot(user_id=user_id, pc=pc)
            self.peers[user_id] = slot
            self.mixer.attach(user_id)

            @pc.on("connectionstatechange")
            async def _on_state() -> None:
                state = pc.connectionState
                log.info(
                    "rtc.state", session=str(self.session_id), user=str(user_id), state=state
                )
                if state == "connected" and not slot.connected:
                    slot.connected = True
                    self.on_connection(user_id, True)
                elif state in ("failed", "closed", "disconnected") and slot.connected:
                    slot.connected = False
                    self.on_connection(user_id, False)

            @pc.on("track")
            def _on_track(track: object) -> None:
                if getattr(track, "kind", None) != "audio":
                    return
                self._bind_consumer(slot, user_id, track)

            pc.addTrack(PeerOutboundTrack(self.mixer, user_id))

            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            return {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
            }

    async def remove_peer(self, user_id: UUID) -> None:
        slot = self.peers.pop(user_id, None)
        if slot is None:
            return
        if slot.consumer:
            slot.consumer.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await slot.consumer
        self.mixer.detach(user_id)
        if self._stt_owner == user_id:
            await self._close_stt()
        with suppress(Exception):
            await slot.pc.close()
        if slot.connected:
            self.on_connection(user_id, False)

    @property
    def connected_users(self) -> set[UUID]:
        return {uid for uid, slot in self.peers.items() if slot.connected}

    # ================================================================ inbound
    def _bind_consumer(self, slot: PeerSlot, user_id: UUID, track: object) -> None:
        """Start reading one participant's inbound track, replacing any consumer
        already reading one for that slot.

        A WebRTC renegotiation on an existing connection can fire the browser's 'track'
        event a second time for what is, on the wire, still the same microphone. Left
        alone, the old task and the new one both go on reading and both push every frame
        into the mixer's shared ``human`` buffer — which every other listener hears as
        that speaker's voice playing twice, stuttering, for as long as both survive.
        Cancelling the stale one first is what keeps exactly one consumer per slot.
        """
        if slot.consumer is not None and not slot.consumer.done():
            slot.consumer.cancel()
        slot.consumer = asyncio.create_task(
            self._consume(user_id, track), name=f"rtc-in-{user_id}"
        )

    async def _consume(self, user_id: UUID, track: object) -> None:
        """Pull frames from one participant; forward only while they hold the floor."""
        to_mix = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        to_stt = av.audio.resampler.AudioResampler(
            format="s16", layout="mono", rate=STT_SAMPLE_RATE
        )
        try:
            while True:
                frame = await track.recv()  # type: ignore[attr-defined]
                if self.mixer.floor_holder != user_id:
                    continue

                for resampled in _as_list(to_mix.resample(frame)):
                    self.mixer.push_human(_frame_bytes(resampled), SAMPLE_RATE)

                if self._stt is not None and self._stt_owner == user_id:
                    for resampled in _as_list(to_stt.resample(frame)):
                        await self._stt.send_audio(_frame_bytes(resampled))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("rtc.track_ended", user=str(user_id), reason=str(exc))

    # ================================================================ floor
    async def grant_floor(self, user_id: UUID) -> None:
        self.mixer.floor_holder = user_id
        await self._open_stt(user_id)

    async def release_floor(self) -> None:
        self.mixer.floor_holder = None
        await self._close_stt()

    async def _open_stt(self, user_id: UUID) -> None:
        await self._close_stt()
        try:
            self._stt = await self.stt_provider.open(
                on_transcript=lambda t: self.on_transcript(user_id, t)
            )
            self._stt_owner = user_id
        except Exception as exc:
            log.error("stt.open_failed", user=str(user_id), error=str(exc))
            self._stt = None
            self._stt_owner = None

    async def _close_stt(self) -> None:
        stream, self._stt, self._stt_owner = self._stt, None, None
        if stream is not None:
            with suppress(Exception):
                await stream.close()

    async def flush_stt(self) -> None:
        """Ask the provider for a final transcript now (turn cap reached, host ended…)."""
        if self._stt is not None:
            with suppress(Exception):
                await self._stt.finalize()

    # ================================================================ moderator voice
    async def speak(
        self, sentences: AsyncIterator[str], on_started: Callable[[], None] | None = None
    ) -> None:
        """Synthesise a stream of sentences, sentence by sentence.

        Every sentence goes to two places: the mixer, for anyone on WebRTC, and a clip
        in the speech cache, for anyone who joined without a microphone. Both hear the
        same audio because it is the same PCM — the clip is not a second rendering.

        Cancelling the returned task is the barge-in path: synthesis stops and any audio
        that has not yet been played is discarded.
        """
        started = False
        try:
            async for sentence in sentences:
                text = sentence.strip()
                if not text:
                    continue

                spoken = bytearray()
                async for chunk in self.tts_provider.stream(text):
                    if not started:
                        started = True
                        if on_started:
                            on_started()
                    self.mixer.push_moderator(chunk, self.tts_provider.sample_rate)
                    spoken += resample(chunk, self.tts_provider.sample_rate)

                if spoken:
                    self._publish_clip(text, bytes(spoken))
                elif not started and on_started:
                    # Synthesis produced nothing at all. The caption still has to fire,
                    # or the runner waits for an utterance that will never be announced.
                    started = True
                    on_started()
        except asyncio.CancelledError:
            self.mixer.clear_moderator()
            self._audio_until = 0.0
            raise
        except Exception as exc:
            log.error("tts.failed", session=str(self.session_id), error=str(exc))

    def _publish_clip(self, text: str, pcm: bytes) -> None:
        clip = self.speech.add(text, pcm)
        # Pace the moderator to the length of what it just said. Without this the runner
        # only waits on the mixer, which is empty when nobody is on WebRTC — so in a
        # text-mode room the moderator "spoke" four sentences in half a second and the
        # discussion scrolled past faster than anyone could read it.
        now = asyncio.get_running_loop().time()
        self._audio_until = max(now, self._audio_until) + clip.duration_ms / 1000
        if self.on_clip is not None:
            self.on_clip(clip)

    async def wait_until_silent(self, *, timeout: float = 120.0) -> None:
        """Block until the moderator's audio has actually finished playing.

        Two clocks, because there are two kinds of listener: the mixer backlog for
        WebRTC peers, and the synthesised duration for browsers playing clips. Whichever
        finishes last is when the moderator has stopped talking.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            backlog_done = self.mixer.moderator_backlog_ms() <= 40
            audio_done = loop.time() >= self._audio_until
            if backlog_done and audio_done:
                return
            if loop.time() > deadline:
                log.warning("tts.drain_timeout", session=str(self.session_id))
                return
            await asyncio.sleep(0.05)

    def interrupt(self) -> None:
        if self._speech_task and not self._speech_task.done():
            self._speech_task.cancel()
        self.mixer.clear_moderator()

    def start_speech(self, coro: Awaitable[None]) -> asyncio.Task:
        self._speech_task = asyncio.ensure_future(coro)
        return self._speech_task

    # ================================================================ teardown
    async def close(self) -> None:
        self._closed = True
        self.interrupt()
        await self._close_stt()
        for user_id in list(self.peers):
            await self.remove_peer(user_id)
        self.mixer.moderator.clear()
        self.mixer.human.clear()
        self.mixer.cursors.clear()
        # The clips are recordings of the conversation, and the brief says conversation
        # state dies with the session.
        self.speech.clear()


def _as_list(result: object) -> list:
    if result is None:
        return []
    return result if isinstance(result, list) else [result]


def _frame_bytes(frame: av.AudioFrame) -> bytes:
    return frame.to_ndarray().tobytes()
