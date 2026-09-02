from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.api.deps import Bus, Cfg, Db, Orchestrator, SuperUser
from app.container import get_container
from app.core.errors import NotFoundError
from app.core.responses import ok
from app.infrastructure.llm import LlmGateway, LlmLanes

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def liveness() -> dict:
    """Liveness only. Deliberately touches nothing — a slow database is not a dead process."""
    return ok({"status": "ok"})


def _lane(gateway: LlmGateway, *, role: str) -> dict:
    tiers = [asdict(row) for row in gateway.report()]
    return {
        "lane": gateway.lane,
        "role": role,
        # ``active`` is the rung that served the last successful call; ``next_up`` is the
        # one that would serve the next. They differ for exactly as long as it takes
        # somebody to notice a degradation and open this page — which is to say, whenever
        # it is being read. Showing only one answers the wrong question half the time.
        "active": next((t["name"] for t in tiers if t["status"] == "ACTIVE"), "(none)"),
        "next_up": gateway.next_up(),
        "tiers": tiers,
        "requests_today": sum(t["requests"] for t in tiers),
        # Kept apart because they are priced apart, usually four to one. A single total
        # cannot be turned back into money.
        "prompt_tokens_today": sum(t["prompt_tokens"] for t in tiers),
        "completion_tokens_today": sum(t["completion_tokens"] for t in tiers),
    }


@router.get("/llm/chain")
async def llm_chain(user: SuperUser) -> dict:
    """Both fallback chains, read-only.

    The event feed is deliberately *not* split per lane: the two chains share providers
    and keys, so the question worth answering is what happened across both, in order.
    Each event carries its own ``lane``.
    """
    provider = get_container().providers.llm
    if not isinstance(provider, LlmLanes):
        # LLM_BACKEND=fake runs the scripted moderator directly, with no chain behind it.
        return ok({"enabled": False, "reason": "the scripted moderator is running"})

    lanes = provider
    return ok(
        {
            "enabled": True,
            "assessment": get_container().settings.llm_assessment_enabled,
            "lanes": [
                _lane(lanes.fast, role="what the room hears"),
                _lane(lanes.deep, role="judging what was said"),
            ],
            # Newest first, across both lanes.
            "recent_events": [
                asdict(event) for event in lanes.recent_events(60)
            ],
        }
    )


@router.post("/llm/tiers/{tier}/clear-bench")
async def clear_bench(tier: str, user: SuperUser) -> dict:
    """Put a benched rung back in rotation now.

    Two of the three bench reasons are guesses about the outside world — a key that has
    since been fixed, an account that has since been topped up — and neither should have
    to wait out a cooldown that was sized for the case where nobody was watching. A quota
    bench clears at the provider's own rollover and this will not help it: the next call
    simply spends a round trip rediscovering that the day is still spent.
    """
    provider = get_container().providers.llm
    if not isinstance(provider, LlmLanes) or not provider.clear_bench(tier):
        # Either no chain is running, or no rung goes by that name. Clearing a rung that
        # was not benched succeeds and does nothing, which is the right answer to a button
        # somebody pressed twice.
        raise NotFoundError(f"No rung called {tier!r}.")
    return ok({"tier": tier, "cleared": True})


@router.get("/readyz")
async def readiness(
    db: Db, bus: Bus, orchestrator: Orchestrator, cfg: Cfg, response: Response
) -> dict:
    checks: dict[str, bool] = {}
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False

    providers = get_container().providers
    for name, provider in (("stt", providers.stt), ("llm", providers.llm), ("tts", providers.tts)):
        healthy = getattr(provider, "healthy", None)
        checks[name] = await healthy() if healthy else True

    ready = checks["database"]
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ok(
        {
            "ready": ready,
            "checks": checks,
            "live_sessions": orchestrator.live_count,
            "bus": bus.stats,
            "ai_provider": cfg.ai_provider,
            # "off" means invitations are in-app only, which is the default. "console"
            # means they were written to disk and nothing left the machine. Both are
            # states people otherwise diagnose by reading the log.
            "mail_transport": get_container().mailman.transport,
            "public_app_url": cfg.public_app_url,
        }
    )
