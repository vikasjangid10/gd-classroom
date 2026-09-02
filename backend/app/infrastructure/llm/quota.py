"""The ledger: what each tier has spent today, and whether it is allowed to serve.

Two separate things live here, and conflating them is a bug waiting to happen.

**Budget** is a counter of requests and tokens for the current quota day. It is a cheap
pre-flight guard so that a tier already known to be spent costs no round trip to
rediscover — it is emphatically *not* the authority. Our count and the provider's differ
the moment anything else uses the same key, so the provider's 429 is truth and the
counter is only an optimisation.

**Bench** is a "not eligible until this moment" mark with a reason. It is what the router
actually reads.

Everything is wall clock, never monotonic. A quota day is a wall-clock concept, and a
monotonic value is meaningless the moment it is written to a file and read back after a
restart.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from app.core.logging import get_logger
from app.infrastructure.llm.failures import Decision
from app.infrastructure.llm.providers import TierSpec

log = get_logger(__name__)

#: Bumped when the on-disk shape changes. A file from an older version is discarded
#: rather than migrated: losing a day of counters is harmless, and the alternative is
#: migration code for a cache.
_STATE_VERSION = 4


class TierStatus(str, Enum):
    """Why a tier is or is not going to serve the next call.

    The router and the status page both read this, which is the point: two code paths
    computing eligibility separately will eventually disagree, and the disagreement
    always surfaces as "the dashboard says it is fine but it is not being used".
    """

    #: Served the most recent successful call.
    ACTIVE = "ACTIVE"
    #: Eligible, just not the one that served last.
    READY = "READY"
    #: Hit a short rate limit recently. Still eligible — this is not a bench.
    THROTTLED = "THROTTLED"
    #: Out of quota until the day rolls over.
    QUOTA_SPENT = "QUOTA_SPENT"
    #: Failing repeatedly; benched on a doubling cooldown.
    FAILING = "FAILING"
    #: Bad key, model or request. Needs a human.
    MISCONFIGURED = "MISCONFIGURED"
    #: No key, or the SDK is not installed. Never attempted.
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class TierState:
    """One tier's row in the ledger. Plain data so it serialises without ceremony."""

    quota_day: str = ""
    requests: int = 0
    #: The total, which is what a token budget is expressed against. The two halves below
    #: are kept as well because they are *priced* separately, usually four to one — a
    #: single total cannot be turned back into money.
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    bench_until: float = 0.0
    bench_reason: str = ""
    bench_detail: str = ""

    #: Consecutive TRANSIENT failures. Any success resets it, which is what stops a
    #: provider that fails every third call from ever being benched.
    failure_streak: int = 0
    #: The cooldown the *next* bench will use. Doubles per bench, resets on success.
    next_cooldown_seconds: float = 0.0

    #: Recorded for the dashboard only — a throttle never takes a tier out of rotation.
    last_throttle_at: float = 0.0
    last_throttle_detail: str = ""

    last_success_at: float = 0.0


@dataclass(frozen=True, slots=True)
class TierReport:
    """What the status endpoint renders for one rung."""

    name: str
    status: TierStatus
    model: str
    is_local: bool
    unavailable_reason: str
    quota_day: str
    requests: int
    request_limit: int | None
    tokens: int
    prompt_tokens: int
    completion_tokens: int
    token_limit: int | None
    bench_reason: str
    bench_detail: str
    bench_clears_at: float | None
    seconds_until_clear: float | None
    failure_streak: int
    last_throttle_detail: str


def _day_key(spec: TierSpec, now: float) -> str:
    """The provider's current quota day, in *its* timezone.

    Quota days are per tier and providers roll over in different ones. A single global
    "today" un-benches some tiers hours early, which is how a spent tier gets put back at
    the top of the chain to fail again.
    """
    local = datetime.fromtimestamp(now, UTC) + timedelta(hours=spec.quota_day_utc_offset_hours)
    return local.date().isoformat()


def _next_rollover(spec: TierSpec, now: float) -> float:
    """Epoch seconds of this tier's next quota-day boundary."""
    offset = timedelta(hours=spec.quota_day_utc_offset_hours)
    local = datetime.fromtimestamp(now, UTC) + offset
    midnight = datetime.combine(local.date() + timedelta(days=1), local.min.time())
    return (midnight.replace(tzinfo=UTC) - offset).timestamp()


class QuotaLedger:
    """Process-wide, persisted, and safe to call from any task."""

    def __init__(
        self,
        *,
        path: Path,
        transient_failures_before_bench: int = 3,
        transient_cooldown_seconds: float = 20.0,
        transient_cooldown_cap_seconds: float = 900.0,
        misconfigured_bench_seconds: float = 1800.0,
        throttle_display_seconds: float = 120.0,
    ) -> None:
        self._path = path
        self._bench_after = max(1, transient_failures_before_bench)
        self._cooldown_base = transient_cooldown_seconds
        self._cooldown_cap = transient_cooldown_cap_seconds
        self._misconfigured_bench = misconfigured_bench_seconds
        self._throttle_display = throttle_display_seconds
        self._lock = threading.RLock()
        self._states: dict[str, TierState] = {}
        #: The rung that served last, **per lane**. One value would have the two chains
        #: overwrite each other, and the page would show one ACTIVE row across both — an
        #: answer to a question nobody asked.
        self._active: dict[str, str] = {}
        self._load()

    # ================================================================ persistence
    def _load(self) -> None:
        """Read the ledger, or start a clean day.

        A missing, truncated or hand-edited file must never stop the application from
        booting. Everything in here is a cache of something the provider will tell us
        again on the next 429.
        """
        try:
            raw = json.loads(self._path.read_text("utf-8"))
            if raw.get("version") != _STATE_VERSION:
                raise ValueError(f"state version {raw.get('version')}")
            self._states = {
                name: TierState(**row) for name, row in (raw.get("tiers") or {}).items()
            }
            self._active = dict(raw.get("active") or {})
        except FileNotFoundError:
            self._states = {}
            self._active = {}
        except Exception as exc:
            log.warning("llm.ledger_unreadable", path=str(self._path), error=str(exc)[:200])
            self._states = {}
            self._active = {}

    def _save(self) -> None:
        """Atomically, so a crash mid-write cannot leave a half-file behind.

        Best-effort on purpose: a read-only filesystem should cost us persistence, not
        the ability to answer the call we are in the middle of.
        """
        payload = {
            "version": _STATE_VERSION,
            "active": self._active,
            "tiers": {name: asdict(state) for name, state in self._states.items()},
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(payload, file)
            os.replace(temporary, self._path)
        except Exception as exc:  # pragma: no cover - filesystem-dependent
            log.warning("llm.ledger_unwritable", path=str(self._path), error=str(exc)[:200])

    # ================================================================ access
    def _state(self, spec: TierSpec, now: float) -> TierState:
        """This tier's row, rolled over to today if the day has turned.

        Rollover happens lazily on read rather than on a timer: a timer would have to
        know every tier's timezone and fire while the process happens to be running,
        and a process that was asleep across midnight would miss it entirely.
        """
        state = self._states.setdefault(spec.name, TierState())
        today = _day_key(spec, now)
        if state.quota_day != today:
            state.quota_day = today
            state.requests = 0
            state.tokens = 0
            state.prompt_tokens = 0
            state.completion_tokens = 0
            # A new day is exactly what a quota bench was waiting for. Failure benches
            # are about the provider being broken, which midnight does not fix.
            if state.bench_reason == Decision.QUOTA_SPENT.value:
                state.bench_until = 0.0
                state.bench_reason = ""
                state.bench_detail = ""
        return state

    def _budget_spent(self, spec: TierSpec, state: TierState) -> bool:
        if spec.daily_request_limit is not None and state.requests >= spec.daily_request_limit:
            return True
        return spec.daily_token_limit is not None and state.tokens >= spec.daily_token_limit

    def status(self, spec: TierSpec, *, now: float | None = None) -> TierStatus:
        """The single answer both the router and the dashboard act on."""
        if not spec.available:
            return TierStatus.UNAVAILABLE

        now = now if now is not None else time.time()
        with self._lock:
            state = self._state(spec, now)

            if state.bench_until > now:
                reason = state.bench_reason
                if reason == Decision.QUOTA_SPENT.value:
                    return TierStatus.QUOTA_SPENT
                if reason == Decision.MISCONFIGURED.value:
                    return TierStatus.MISCONFIGURED
                return TierStatus.FAILING

            if self._budget_spent(spec, state):
                return TierStatus.QUOTA_SPENT

            if now - state.last_throttle_at < self._throttle_display:
                # Deliberately still eligible. THROTTLED is a note for the reader, not a
                # gate — see ``note_failure``.
                return TierStatus.THROTTLED

            if self._active.get(spec.lane) == spec.name:
                return TierStatus.ACTIVE
            return TierStatus.READY

    def eligible(self, spec: TierSpec, *, now: float | None = None) -> bool:
        return self.status(spec, now=now) in (
            TierStatus.ACTIVE,
            TierStatus.READY,
            TierStatus.THROTTLED,
        )

    # ================================================================ recording
    def note_attempt(self, spec: TierSpec) -> None:
        """Counted before the call, because a request that fails still spent one."""
        with self._lock:
            state = self._state(spec, time.time())
            state.requests += 1
            self._save()

    def note_usage(self, spec: TierSpec, *, prompt: int, completion: int) -> None:
        """Counted after the call, because only the provider knows what it cost.

        Separate from ``note_attempt`` on purpose: the request count is what a per-day
        request limit is checked against and must be right even when the call fails, while
        the token count only exists once an answer has come back.
        """
        if prompt <= 0 and completion <= 0:
            return
        with self._lock:
            state = self._state(spec, time.time())
            state.prompt_tokens += max(0, prompt)
            state.completion_tokens += max(0, completion)
            state.tokens += max(0, prompt) + max(0, completion)
            self._save()

    def note_success(self, spec: TierSpec) -> None:
        """A success clears the failure streak *and* the cooldown it had earned.

        Instantly, not gradually: a tier that just answered is healthy, and carrying a
        doubled cooldown forward would punish it for an outage that is over.
        """
        with self._lock:
            state = self._state(spec, time.time())
            state.failure_streak = 0
            state.next_cooldown_seconds = 0.0
            state.last_success_at = time.time()
            if state.bench_reason in (Decision.TRANSIENT.value, ""):
                state.bench_until = 0.0
                state.bench_reason = ""
                state.bench_detail = ""
            self._active[spec.lane] = spec.name
            self._save()

    def note_failure(self, spec: TierSpec, decision: Decision, detail: str) -> float | None:
        """Apply a routing decision. Returns when the tier becomes eligible again."""
        now = time.time()
        with self._lock:
            state = self._state(spec, now)

            if decision is Decision.QUOTA_SPENT:
                # Benched until the tier's *real* rollover, not until the retry-after it
                # reported. Providers are unreliable about that number for daily limits —
                # some send the per-minute value, some send nothing — and being wrong
                # here means re-probing a spent tier on every call until midnight.
                state.bench_until = _next_rollover(spec, now)
                state.bench_reason = decision.value
                state.bench_detail = detail

            elif decision is Decision.MISCONFIGURED:
                state.bench_until = now + self._misconfigured_bench
                state.bench_reason = decision.value
                state.bench_detail = detail

            elif decision is Decision.RETRY_LATER:
                # Recorded, never benched. A per-minute token throttle is routine, and
                # benching on it makes the application look like it randomly switches
                # models for a limit that clears in seconds. Falling through for *this*
                # call is unavoidable; the top rung is still first in line for the next.
                state.last_throttle_at = now
                state.last_throttle_detail = detail
                self._save()
                return None

            else:  # TRANSIENT
                state.failure_streak += 1
                if state.failure_streak < self._bench_after:
                    # Deliberately not benched. One blip is not an outage, and taking the
                    # preferred provider out of rotation for a single dropped connection
                    # costs more than the retry it saves.
                    self._save()
                    return None
                # Doubling, because a fixed cooldown is wrong at both ends: short enough
                # to re-probe a dead provider forever, or long enough to keep a
                # ten-second blip benched for an hour.
                cooldown = min(
                    self._cooldown_cap,
                    state.next_cooldown_seconds * 2 if state.next_cooldown_seconds else
                    self._cooldown_base,
                )
                state.next_cooldown_seconds = cooldown
                state.bench_until = now + cooldown
                state.bench_reason = decision.value
                state.bench_detail = detail

            self._save()
            return state.bench_until

    # ================================================================ manual
    def clear_bench(self, tier: str) -> bool:
        """Put a tier back in rotation now.

        Two of the bench reasons are guesses about the outside world — a key that has
        since been fixed, an account that has since been topped up — and neither should
        have to wait for a rollover that is hours away.
        """
        with self._lock:
            state = self._states.get(tier)
            if state is None:
                return False
            state.bench_until = 0.0
            state.bench_reason = ""
            state.bench_detail = ""
            state.failure_streak = 0
            state.next_cooldown_seconds = 0.0
            self._save()
            return True

    def reset_counters(self, tier: str) -> bool:
        with self._lock:
            state = self._states.get(tier)
            if state is None:
                return False
            state.requests = 0
            state.tokens = 0
            state.prompt_tokens = 0
            state.completion_tokens = 0
            self._save()
            return True

    # ================================================================ reporting
    def report(self, spec: TierSpec, *, now: float | None = None) -> TierReport:
        now = now if now is not None else time.time()
        status = self.status(spec, now=now)
        with self._lock:
            state = self._state(spec, now)
            clears_at = state.bench_until if state.bench_until > now else None
            return TierReport(
                name=spec.name,
                status=status,
                model=spec.model,
                is_local=spec.is_local,
                unavailable_reason=spec.unavailable_reason,
                quota_day=state.quota_day,
                requests=state.requests,
                request_limit=spec.daily_request_limit,
                tokens=state.tokens,
                prompt_tokens=state.prompt_tokens,
                completion_tokens=state.completion_tokens,
                token_limit=spec.daily_token_limit,
                bench_reason=state.bench_reason,
                bench_detail=state.bench_detail,
                bench_clears_at=clears_at,
                seconds_until_clear=(clears_at - now) if clears_at else None,
                failure_streak=state.failure_streak,
                last_throttle_detail=(
                    state.last_throttle_detail
                    if now - state.last_throttle_at < self._throttle_display
                    else ""
                ),
            )
