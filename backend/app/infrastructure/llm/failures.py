"""Turning any provider's error into one of four routing decisions.

This is the only place in the chain that reads an exception, and it exists because the
routing question — *may this tier serve the next call?* — has exactly four answers while
providers have hundreds of ways of saying them.

**Why classify on text rather than on exception type.** The same HTTP 429 arrives as
``httpx.HTTPStatusError`` from one adapter, ``openai.RateLimitError`` from another and a
bare ``Exception`` from a third, and a chain that matched on types would need a branch
per SDK — exactly the provider-specific leakage this package is built to avoid. Every
string a provider might have put the reason in is gathered into one blob and matched
once.

**The expensive decision is QUOTA_SPENT versus RETRY_LATER**, and it is expensive in both
directions. Call a daily quota a throttle and the router puts the dead tier back at the
top of the chain on the very next call, paying a full timeout to rediscover it, forever.
Call a per-minute throttle a daily quota and a tier that would have been healthy again in
forty seconds sits out until midnight. No provider marks the difference machine-readably,
so this uses explicit wording first and then the *duration* of the retry-after as the
signal — a limit that clears in under five minutes is a throttle; one that clears in
forty-four is a day.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

#: Provider error bodies routinely run to kilobytes of JSON, and every one of these ends
#: up in a log line and a status page. Three hundred characters is enough to see which
#: limit was hit and short enough to read.
_DETAIL_LIMIT = 300


class Decision(str, Enum):
    """What the router should do with the tier that just failed."""

    #: Done for the day. Bench until the tier's quota window rolls over.
    QUOTA_SPENT = "QUOTA_SPENT"
    #: Bad key, unknown model, malformed request. Bench long — a human has to act, and
    #: retrying every turn burns a full timeout while looking exactly like an outage.
    MISCONFIGURED = "MISCONFIGURED"
    #: A short throttle that clears in seconds. Fall through for this call only.
    RETRY_LATER = "RETRY_LATER"
    #: A blip. Advance this call, leave the tier eligible for the next one.
    TRANSIENT = "TRANSIENT"


@dataclass(frozen=True, slots=True)
class Failure:
    decision: Decision
    detail: str
    status_code: int | None = None
    retry_after_seconds: float | None = None


# --------------------------------------------------------------------- wording
#: Phrases that name a *daily* window outright, including the identifiers providers put
#: in a quota-violation body ("GenerateRequestsPerDayPerProjectPerModel-FreeTier").
#: Checked before any duration heuristic, because when a provider bothers to say which
#: window it is, that is the best evidence there is.
_DAILY_WORDING = (
    "per day",
    "per-day",
    "perday",
    "daily quota",
    "daily limit",
    "requests per day",
    "tokens per day",
)

#: Exhaustion of an account rather than of a window: no wait will fix it, so it benches
#: the same way a spent day does.
_CREDIT_WORDING = (
    "quota exceeded for",
    "insufficient_quota",
    "insufficient quota",
    "out of credits",
    "credit balance is too low",
    "billing hard limit",
    "exceeded your current quota",
    "free tier limit",
    "usage limit reached",
    "exceeded your monthly included credits",
)

#: The same sentence a provider uses for a spent day is often what it uses for a burst
#: limit that clears in seconds — Gemini's per-minute and per-day 429s differ only in the
#: quota id. Where that id is present it decides, and it decides *against* benching:
#: calling a per-minute throttle a daily quota costs a whole day of the best rung.
_PER_MINUTE_WORDING = (
    "per minute",
    "per-minute",
    "perminute",
    "per min",
    "requests per minute",
    "tokens per minute",
)

#: Wording that means the credential or the request is wrong, not that the tier is busy.
_MISCONFIGURED_WORDING = (
    "invalid api key",
    "incorrect api key",
    "invalid_api_key",
    # Google answers a bad key with 400 INVALID_ARGUMENT rather than a 401, so without
    # the wording it would only be caught by the catch-all at the bottom.
    "pass a valid api key",
    "api key not valid",
    "no api key",
    "api key not found",
    "unauthorized",
    "authentication",
    "permission denied",
    "does not exist or you do not have access",
    "model_not_found",
    "unknown model",
    "invalid model",
    "unsupported",
    "invalid_request_error",
    "must be one of",
)

_RATE_LIMIT_WORDING = ("rate limit", "rate_limit", "too many requests", "429", "overloaded")

#: Anything that is simply the network being the network.
_TRANSIENT_WORDING = (
    "timeout",
    "timed out",
    "connection",
    "connect",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "internal server error",
    "reset by peer",
    "eof occurred",
    "502",
    "503",
    "504",
)


# --------------------------------------------------------------------- duration
#: "44m44.448s", "1h2m", "30s" — Groq states the wait in prose rather than a header, and
#: it is the single most useful number in the whole error.
_COMPOUND = re.compile(
    r"(?:(?P<h>\d+(?:\.\d+)?)\s*h)?"
    r"(?:(?P<m>\d+(?:\.\d+)?)\s*m(?!s))?"
    r"(?:(?P<s>\d+(?:\.\d+)?)\s*s)?",
)
#: "try again in 30 seconds", "retry after 2 minutes", "wait 1 hour"
_SPELLED = re.compile(
    r"(?:try again in|retry after|retry in|wait|available in)\s+"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>second|sec|minute|min|hour|hr)s?",
    re.IGNORECASE,
)
_UNIT_SECONDS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600}


def _parse_duration(blob: str) -> float | None:
    """Seconds until the provider says it will accept traffic again, or ``None``."""
    if match := _SPELLED.search(blob):
        return float(match["value"]) * _UNIT_SECONDS[match["unit"].lower()]

    # Compound forms appear inside a sentence ("Please try again in 44m44.448s"), so
    # anchor on the phrase and read what follows rather than scanning the whole blob —
    # otherwise "gpt-4o" and "1h" of unrelated prose both look like durations.
    for anchor in ("try again in", "retry after", "retry in", "available in"):
        index = blob.find(anchor)
        if index == -1:
            continue
        tail = blob[index + len(anchor) : index + len(anchor) + 40].strip()
        if (match := _COMPOUND.match(tail)) and any(match.group(g) for g in ("h", "m", "s")):
            return (
                float(match["h"] or 0) * 3600
                + float(match["m"] or 0) * 60
                + float(match["s"] or 0)
            )
    return None


def _header_retry_after(exc: BaseException) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    for key in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        raw = headers.get(key) if hasattr(headers, "get") else None
        if not raw:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            # Some gateways send a duration string here rather than a number of seconds.
            if (parsed := _parse_duration(str(raw).lower())) is not None:
                return parsed
    return None


def _status_code(exc: BaseException, blob: str) -> int | None:
    for holder in (exc, getattr(exc, "response", None)):
        for attribute in ("status_code", "status"):
            value = getattr(holder, attribute, None)
            if isinstance(value, int) and 100 <= value < 600:
                return value
    if match := re.search(r"\b(4\d{2}|5\d{2})\b", blob):
        return int(match.group(1))
    return None


def _blob(exc: BaseException) -> str:
    """Every place a provider might have written the reason, lowercased into one string."""
    parts: list[str] = [str(exc), type(exc).__name__]
    for attribute in ("message", "body", "detail", "reason"):
        value = getattr(exc, attribute, None)
        if value:
            parts.append(str(value))
    response = getattr(exc, "response", None)
    if response is not None:
        for attribute in ("text", "content"):
            value = getattr(response, attribute, None)
            if value:
                parts.append(str(value)[:2000])
        if headers := getattr(response, "headers", None):
            with_get = getattr(headers, "get", None)
            if with_get and (retry := with_get("retry-after")):
                parts.append(f"retry-after: {retry}")
    if (cause := exc.__cause__) is not None and cause is not exc:
        parts.append(str(cause))
    return " | ".join(parts).lower()


def classify(exc: BaseException, *, daily_threshold_seconds: float = 300.0) -> Failure:
    """Decide what the chain should do about ``exc``.

    ``daily_threshold_seconds`` is the line between "busy" and "spent" when the provider
    only tells us how long to wait. Five minutes: no per-minute throttle asks for longer,
    and no daily limit asks for less.
    """
    blob = _blob(exc)
    status = _status_code(exc, blob)
    retry_after = _header_retry_after(exc) or _parse_duration(blob)
    detail = str(exc).strip()[:_DETAIL_LIMIT] or type(exc).__name__

    def failure(decision: Decision) -> Failure:
        return Failure(decision, detail, status, retry_after)

    # A credential problem answers instantly and answers the same way forever, so it is
    # the cheapest thing to get wrong and the most expensive to keep retrying.
    looks_misconfigured = status in (401, 403) or any(
        word in blob for word in _MISCONFIGURED_WORDING
    )
    says_daily = any(word in blob for word in _DAILY_WORDING)
    says_per_minute = any(word in blob for word in _PER_MINUTE_WORDING)
    # A named daily window is conclusive. A generic "you are out of quota" is not, when
    # the same body also names a per-minute one — that is a burst limit wearing the
    # wording of a spent day, and the duration below is the right signal for it.
    out_of_credit = says_daily or (
        any(word in blob for word in _CREDIT_WORDING) and not says_per_minute
    )
    # The credit check comes first because some providers report an empty account as a
    # 403, and that is a quota event rather than a broken key.
    if looks_misconfigured and not out_of_credit:
        return failure(Decision.MISCONFIGURED)

    if out_of_credit or status == 402:
        return failure(Decision.QUOTA_SPENT)

    if status == 429 or any(word in blob for word in _RATE_LIMIT_WORDING):
        # The duration is the only signal left, and it is a good one: a throttle that
        # clears in forty seconds and a quota that clears at midnight are the same
        # status code with the same wording and wildly different durations.
        if retry_after is not None and retry_after >= daily_threshold_seconds:
            return failure(Decision.QUOTA_SPENT)
        return failure(Decision.RETRY_LATER)

    if any(word in blob for word in _TRANSIENT_WORDING) or (status is not None and status >= 500):
        return failure(Decision.TRANSIENT)

    # A 4xx that never mentioned a limit is the request being wrong. Treating it as a
    # blip would retry a malformed body on every rung and then again next turn.
    if status is not None and 400 <= status < 500:
        return failure(Decision.MISCONFIGURED)

    return failure(Decision.TRANSIENT)
