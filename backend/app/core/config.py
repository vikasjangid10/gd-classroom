"""Typed application settings, loaded once at import and injected everywhere else."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------- application
    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "AI GD Classroom"
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    # ------------------------------------------------------------- database
    postgres_user: str = "gd"
    postgres_password: str = "gd_password"
    postgres_db: str = "gd_classroom"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    database_url_override: str | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False

    # ------------------------------------------------------------- security
    jwt_secret: str = "insecure-dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 604_800
    ticket_ttl_seconds: int = 60
    # NoDecode: these arrive as comma-separated strings, not JSON, so the validator
    # below owns the parsing instead of pydantic-settings' default JSON decoder.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # ------------------------------------------------------------- public identity
    #: The origin a real person types into a real browser. Every link in an outgoing
    #: email is built from this, so on a phone it must be a URL that phone can reach —
    #: a LAN address or a tunnel hostname, never ``localhost``.
    public_app_url: str = "http://localhost:5173"

    # ------------------------------------------------------------- email
    #: Off by default: invitations are delivered *inside the app* over the lobby event
    #: stream, and everyone who takes part already has an account here. Turning this on
    #: additionally emails the invitation and the room link — see ``docs/architecture``.
    mail_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_timeout_seconds: int = 20
    mail_from: str = ""
    mail_from_name: str = "AI GD Classroom"
    #: Where the console transport drops .eml files when SMTP is not configured.
    mail_outbox_dir: str = "/tmp/gd-outbox"

    # ------------------------------------------------------------- discussion
    #: Seats a classroom offers — how many people the host may invite.
    participants_per_classroom: int = 4
    #: How many have to accept before a discussion can actually run. A group discussion
    #: needs someone to disagree with, so two; beyond that, whoever turns up turns up.
    #: A seat nobody took is not a reason to cancel on the people who did.
    min_participants_to_start: int = 2
    invitation_ttl_seconds: int = 1800
    session_join_window_seconds: int = 90
    turn_max_seconds: int = 90
    #: Quiet that marks the end of an utterance. A dictation app can use a second,
    #: because you are reading prepared words. Somebody thinking aloud in a discussion
    #: pauses far longer than that mid-sentence — and ending their turn on a breath is
    #: indistinguishable, from their side, from not being listened to at all.
    silence_end_ms: int = 2800
    #: How long somebody has to *begin* speaking after the floor is handed to them.
    #: Distinct from ``turn_max_seconds``, which caps a turn once it is under way.
    #: Generous on purpose: a real person hears the question, thinks, finds their words,
    #: and sometimes unmutes — and being cut off before starting is the rudest thing a
    #: moderator can do.
    silence_before_speaking_seconds: float = 25.0
    #: The second window, after the host has checked whether they are still there.
    silence_after_nudge_seconds: float = 15.0
    #: The pause a human host takes before responding to what was just said.
    moderator_think_seconds: float = 1.4
    discussion_max_seconds: int = 2700
    discussion_target_seconds: int = 1500
    min_turns_per_participant: int = 2
    #: Turns in a row yielding no words before the discussion is closed. Without a limit
    #: the moderator keeps questioning an empty room and starts inventing the answers.
    max_silent_turns: int = 3
    #: Remove anybody who reads out an email address or phone number, on the spot. The
    #: words are dropped rather than stored either way; this decides whether the speaker
    #: also leaves the round.
    remove_on_personal_information: bool = True
    transcript_retention_days: int = 30
    janitor_interval_seconds: int = 60

    # ------------------------------------------------------------- AI providers
    ai_provider: Literal["fake", "live"] = "fake"
    allow_text_input: bool = True

    #: Which adapter fills each port when ``AI_PROVIDER=live``. ``auto`` keeps the
    #: original behaviour — pick whichever vendor has a key configured — and naming one
    #: explicitly is what lets two vendors' keys coexist without ambiguity.
    stt_backend: Literal["auto", "deepgram", "groq", "fake"] = "auto"
    tts_backend: Literal["auto", "elevenlabs", "piper", "fake"] = "auto"
    llm_backend: Literal["auto", "openai", "fake"] = "auto"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    #: Not a gpt-5 model: those reject ``max_tokens`` and a temperature other than 1, and
    #: this whole package is built on one wire format rather than a branch per vendor.
    openai_strong_model: str = "gpt-4.1"
    openai_base_url: str = "https://api.openai.com/v1"
    #: 0 means "no locally-enforced budget" — the provider's own 429 is still the truth.
    openai_daily_request_limit: int = 0
    openai_daily_token_limit: int = 0

    #: Gemini through Google's OpenAI-compatible endpoint, so it is the same adapter as
    #: every other rung rather than a second SDK. Key: https://aistudio.google.com/apikey
    gemini_api_key: str = ""
    #: A *lite* model on purpose. The moderator writes one sentence, and the reasoning
    #: flash models spend the whole ``max_tokens`` budget thinking and return nothing.
    gemini_model: str = "gemini-3.5-flash-lite"
    #: The deep lane's rung. Here reasoning is the point, not the problem — it judges an
    #: answer rather than speaking one, so it gets a bigger budget and a longer ceiling.
    gemini_strong_model: str = "gemini-3.5-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    gemini_daily_request_limit: int = 0
    gemini_daily_token_limit: int = 0

    #: Hugging Face's router, which is OpenAI-compatible and fronts several inference
    #: providers. The free allowance is monthly credits, not a daily count, so it is
    #: left unbudgeted here and the router's own 402 is what benches the rung.
    huggingface_api_key: str = ""
    huggingface_model: str = "meta-llama/Llama-3.1-8B-Instruct"
    huggingface_strong_model: str = "deepseek-ai/DeepSeek-V4-Flash"
    huggingface_base_url: str = "https://router.huggingface.co/v1"
    huggingface_daily_request_limit: int = 0
    huggingface_daily_token_limit: int = 0

    # ------------------------------------------------------------- LLM chain
    #: Two chains, because the moderator does two different jobs. **Fast** writes what
    #: the room hears — a question, a hand-off, a nudge — where a plain instruct model is
    #: better than a clever one and every second is dead air. **Deep** judges what a
    #: participant actually said, where reasoning is the whole point and the answer is
    #: never spoken aloud. Each is walked top-down; each must end on a local tier.
    #:
    #: Rung names are per lane (``gemini`` vs ``gemini-strong``) and so are their ledger
    #: rows — deliberately, because a provider's quota is counted per model.
    llm_chain_fast: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["gemini", "groq", "huggingface", "openai", "scripted"]
    )
    llm_chain_deep: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "groq-strong", "gemini-strong", "huggingface-strong", "openai-strong", "scripted",
        ]
    )
    #: Once hunting for a working hosted provider has cost this long, skip the rest of
    #: the hosted rungs and take the local one. Per-rung ceilings bound each attempt but
    #: not their sum, and the sum is what the person waiting experiences.
    #:
    #: Sized for the chain, not for one call: five rungs cannot be walked inside the
    #: twelve seconds two could. It only ever bites on the *first* call after a provider
    #: goes down — a rung that failed is benched, and a benched rung costs no network at
    #: all on every call after that.
    llm_failover_budget_seconds: float = 25.0
    #: The line between "busy" (throttle, clears in seconds) and "spent" (done for the
    #: day) when a provider only tells us how long to wait. No per-minute throttle asks
    #: for longer than five minutes; no daily limit asks for less.
    llm_daily_quota_threshold_seconds: float = 300.0
    #: One blip is not an outage. Bench only once a tier has failed this many times in a
    #: row; any success resets the streak.
    llm_transient_failures_before_bench: int = 3
    #: First cooldown, then doubling to the cap. A fixed value is wrong at both ends.
    llm_transient_cooldown_seconds: float = 20.0
    llm_transient_cooldown_cap_seconds: float = 900.0
    #: A bad key needs a human, so re-probing it every turn only burns timeouts.
    llm_misconfigured_bench_seconds: float = 1800.0
    #: Counters that reset on every dev-server reload are worse than none.
    llm_state_path: str = "/app/.llm/state.json"
    llm_event_buffer_size: int = 200
    llm_prewarm_local: bool = True

    #: Whether the deep lane judges answers at all. Off falls back to the word-count
    #: heuristic in ``app/domain/turn_policy.py``, which is also the floor when it is on.
    llm_assessment_enabled: bool = True
    #: How long the room may wait for that judgement. It runs between a participant
    #: finishing and the moderator replying, so it is capped well under the pause a person
    #: would read as thinking — and on expiry the heuristic decides, silently and instantly.
    llm_assessment_timeout_seconds: float = 6.0

    deepgram_api_key: str = ""
    deepgram_model: str = "nova-2"

    #: Groq serves Whisper as a file endpoint, so the adapter does its own utterance
    #: detection. See ``app/infrastructure/ai/groq_stt.py``.
    groq_api_key: str = ""
    groq_stt_model: str = "whisper-large-v3-turbo"
    groq_llm_model: str = "openai/gpt-oss-20b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    #: Sent as ``reasoning_effort`` on every Groq call. Empty omits the field. Groq's
    #: catalogue is reasoning models, and at their default effort a 220-token budget is
    #: gone before the first word of the answer.
    groq_reasoning_effort: str = "low"
    groq_strong_llm_model: str = "openai/gpt-oss-120b"
    groq_strong_reasoning_effort: str = "low"
    groq_daily_request_limit: int = 0
    groq_daily_token_limit: int = 0

    #: A local model, if one is running. Empty means the tier is skipped with a reason
    #: rather than attempted and failed.
    ollama_base_url: str = ""
    ollama_model: str = "llama3.1:8b"
    #: Seconds of continuous speech between live-caption transcriptions. 0 disables
    #: interim passes entirely and bills only for finals.
    stt_interim_seconds: float = 4.0

    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"

    #: Piper runs locally: no key, no network, one ONNX file.
    piper_model_path: str = "/app/.piper/en_US-lessac-medium.onnx"
    piper_voice: str = "en_US-lessac-medium"

    # ------------------------------------------------------------- webrtc
    stun_urls: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["stun:stun.l.google.com:19302"]
    )
    turn_url: str = ""
    turn_username: str = ""
    turn_password: str = ""

    @field_validator(
        "cors_origins", "stun_urls", "llm_chain_fast", "llm_chain_deep", mode="before"
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mail_transport(self) -> Literal["smtp", "console", "off"]:
        """How outgoing mail leaves — or that it does not.

        ``off`` is the default and is not a silent no-op: with mail disabled nothing is
        ever queued, because the invitation was delivered in the app instead. The
        distinction that matters is between "sent nowhere" and "sent to a file"; both
        are visible at boot and on ``/readyz``.
        """
        if not self.mail_enabled:
            return "off"
        return "smtp" if self.smtp_host.strip() else "console"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mail_sender_address(self) -> str:
        return (self.mail_from or self.smtp_username or "no-reply@gd-classroom.local").strip()

    def invite_url(self, token: str) -> str:
        return f"{self.public_app_url.rstrip('/')}/invite/{token}"

    def room_url(self, session_id: str) -> str:
        return f"{self.public_app_url.rstrip('/')}/room/{session_id}"

    def ice_servers(self) -> list[dict[str, object]]:
        servers: list[dict[str, object]] = [{"urls": self.stun_urls}]
        if self.turn_url:
            servers.append(
                {
                    "urls": [self.turn_url],
                    "username": self.turn_username,
                    "credential": self.turn_password,
                }
            )
        return servers


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
