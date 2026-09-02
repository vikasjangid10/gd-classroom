"""Provider selection.

The only file in the codebase that knows which vendor is in play. Everything else
depends on the Protocols in ``app.domain.ports``, which is what makes ``AI_PROVIDER=fake``
a one-line switch rather than a test harness.

Each port is chosen independently, so a deployment can run a real language model with a
local voice and no speech recognition at all. Falling back to the fake when a backend
cannot be satisfied is deliberate — a missing key should degrade the discussion, not
crash the process at boot — but every fallback is logged at WARNING, because a room
where the moderator is secretly scripted is not a room anyone should discover by ear.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.ports import DeepLane, LlmProvider, SttProvider, TtsProvider
from app.infrastructure.ai.fake import FakeLlmProvider, FakeSttProvider, FakeTtsProvider
from app.infrastructure.llm import LlmLanes

log = get_logger(__name__)


@dataclass(slots=True)
class AiProviders:
    stt: SttProvider
    llm: LlmProvider
    tts: TtsProvider
    #: The deep lane, for the two things that are judgement rather than speech: assessing
    #: an answer and writing the closing report. ``None`` when the scripted moderator is
    #: running, and every call site treats that as "do it the deterministic way".
    deep: DeepLane | None = None

    def describe(self) -> dict[str, str]:
        return {
            "stt": self.stt.name,
            "llm": self.llm.name,
            "tts": self.tts.name,
            "deep": self.deep.name if self.deep else "(none)",
        }

    async def warm(self) -> None:
        """Let any provider that has a cold start pay for it before a discussion does."""
        for provider in (self.tts, self.llm):
            warm = getattr(provider, "warm", None)
            if warm is not None:
                await warm()

    async def aclose(self) -> None:
        for provider in (self.llm, self.tts, self.stt):
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()


def _fallback(port: str, reason: str) -> None:
    log.warning("ai.provider_fallback", port=port, reason=reason)


def _build_stt(settings: Settings) -> SttProvider:
    choice = settings.stt_backend
    if choice == "fake":
        return FakeSttProvider()

    if choice in ("auto", "groq") and settings.groq_api_key:
        from app.infrastructure.ai.groq_stt import GroqSttProvider

        return GroqSttProvider()
    if choice == "groq":
        _fallback("stt", "STT_BACKEND=groq but GROQ_API_KEY is missing")
        return FakeSttProvider()

    if choice in ("auto", "deepgram") and settings.deepgram_api_key:
        from app.infrastructure.ai.deepgram_stt import DeepgramSttProvider

        return DeepgramSttProvider()
    if choice == "deepgram":
        _fallback("stt", "STT_BACKEND=deepgram but DEEPGRAM_API_KEY is missing")
        return FakeSttProvider()

    _fallback("stt", "no GROQ_API_KEY or DEEPGRAM_API_KEY is set")
    return FakeSttProvider()


def _build_llm(settings: Settings) -> LlmLanes | LlmProvider:
    """The moderator's language models — two fallback chains, not one provider.

    ``LLM_BACKEND=fake`` still short-circuits to the scripted moderator directly, which
    keeps the offline mode a single object rather than two one-rung chains with a ledger
    and a state file behind them.
    """
    if settings.llm_backend == "fake":
        return FakeLlmProvider()

    from app.infrastructure.llm import build_lanes

    # A malformed chain raises here, at boot, which is the point: the alternative is
    # discovering it in front of four people who are waiting to be spoken to.
    return build_lanes(settings)


def _build_tts(settings: Settings) -> TtsProvider:
    choice = settings.tts_backend
    if choice == "fake":
        return FakeTtsProvider()

    if choice == "piper":
        from app.infrastructure.ai.piper_tts import PiperTtsProvider

        # The model file is fetched at startup, so its absence right now is not a
        # reason to pin the whole process to the fake voice. The provider raises a
        # clear error at synthesis time if it never arrives.
        return PiperTtsProvider()

    if choice == "elevenlabs":
        if settings.elevenlabs_api_key:
            from app.infrastructure.ai.elevenlabs_tts import ElevenLabsTtsProvider

            return ElevenLabsTtsProvider()
        _fallback("tts", "TTS_BACKEND=elevenlabs but ELEVENLABS_API_KEY is missing")
        return FakeTtsProvider()

    # auto: a hosted voice if it is paid for, the local one if it is downloaded.
    if settings.elevenlabs_api_key:
        from app.infrastructure.ai.elevenlabs_tts import ElevenLabsTtsProvider

        return ElevenLabsTtsProvider()

    from pathlib import Path

    if Path(settings.piper_model_path).is_file():
        from app.infrastructure.ai.piper_tts import PiperTtsProvider

        return PiperTtsProvider()

    _fallback("tts", "no ELEVENLABS_API_KEY, and no Piper voice at PIPER_MODEL_PATH")
    return FakeTtsProvider()


def build_providers(settings: Settings) -> AiProviders:
    if settings.ai_provider == "fake":
        log.info("ai.providers", mode="fake")
        return AiProviders(FakeSttProvider(), FakeLlmProvider(), FakeTtsProvider())

    llm = _build_llm(settings)
    # Only the two-lane gateway can judge anything. Behind the scripted moderator there is
    # nothing to ask, and every call site falls back to the deterministic path — the same
    # thing that happens when the deep lane times out.
    lanes = llm if isinstance(llm, LlmLanes) else None
    providers = AiProviders(
        stt=_build_stt(settings),
        llm=llm,
        tts=_build_tts(settings),
        deep=lanes,
    )
    log.info(
        "ai.providers",
        mode="live",
        assessment="on" if lanes and settings.llm_assessment_enabled else "off",
        **providers.describe(),
    )
    return providers
