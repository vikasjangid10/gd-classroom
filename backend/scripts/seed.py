"""Idempotent seed: discussion topics, a host, and six participant accounts.

Invitations are delivered inside the application, so an invitee has to have an account
to receive one. These six exist so the whole flow can be driven locally with nothing
but browser tabs — six rather than four, so a decline has somebody to replace it with.

Safe to run on every boot — it inserts only what is missing.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.db.engine import dispose_engine, session_scope
from app.domain.enums import Gender, Role
from app.modules.classroom.models import Topic
from app.modules.identity.models import User, UserTopicInterest

log = get_logger(__name__)

DEMO_PASSWORD = "Password123!"

TOPICS = [
    {
        "slug": "langchain",
        "title": "LangChain",
        "description": (
            "Where orchestration frameworks help and where they get in the way when "
            "building production LLM applications."
        ),
        "guiding_points": [
            "Abstraction cost versus development speed",
            "Debuggability of chained calls",
            "When to drop the framework and call the API directly",
            "Testing non-deterministic pipelines",
        ],
        "difficulty": 2,
    },
    {
        "slug": "mcp",
        "title": "Model Context Protocol",
        "description": (
            "A standard interface between models and tools. What it unlocks, what it "
            "standardises away, and what it costs."
        ),
        "guiding_points": [
            "Tool discovery and capability negotiation",
            "Trust boundaries between a model and a server",
            "Versioning and backwards compatibility",
            "Comparison with bespoke function calling",
        ],
        "difficulty": 3,
    },
    {
        "slug": "rag",
        "title": "Retrieval Augmented Generation",
        "description": (
            "Retrieval as the real bottleneck in grounded generation: chunking, ranking, "
            "evaluation and the failure modes nobody demos."
        ),
        "guiding_points": [
            "Chunking strategy and its effect on recall",
            "Hybrid search versus pure vector search",
            "Measuring groundedness rather than vibes",
            "When fine-tuning beats retrieval",
        ],
        "difficulty": 2,
    },
    {
        "slug": "system-design",
        "title": "System Design for Real-Time AI",
        "description": (
            "Designing systems where latency is a product feature: streaming, "
            "backpressure, state ownership and graceful degradation."
        ),
        "guiding_points": [
            "Where to hold ephemeral state",
            "Backpressure across a streaming pipeline",
            "Degrading instead of failing when a provider is down",
            "Cost of a hop on the critical path",
        ],
        "difficulty": 4,
    },
]

#: Gender is here only so the demo shows the call-sign rule working — a discussion name
#: that fits the person. Real users declare it themselves at sign-up, or leave it unset.
USERS = [
    ("super@gdclassroom.io", "Nadia Rahman", Role.SUPER_USER, Gender.FEMALE, []),
    ("priya@gdclassroom.io", "Priya", Role.PARTICIPANT, Gender.FEMALE, ["rag", "langchain"]),
    ("arjun@gdclassroom.io", "Arjun", Role.PARTICIPANT, Gender.MALE, ["system-design", "mcp"]),
    ("meera@gdclassroom.io", "Meera", Role.PARTICIPANT, Gender.FEMALE, ["rag", "system-design"]),
    ("dev@gdclassroom.io", "Dev", Role.PARTICIPANT, Gender.MALE, ["langchain", "mcp"]),
    ("sana@gdclassroom.io", "Sana", Role.PARTICIPANT, Gender.FEMALE, ["mcp", "rag"]),
    (
        "rahul@gdclassroom.io",
        "Rahul",
        Role.PARTICIPANT,
        Gender.MALE,
        ["system-design", "langchain"],
    ),
]


async def seed() -> None:
    async with session_scope() as db:
        topics: dict[str, Topic] = {}
        for spec in TOPICS:
            existing = (
                await db.execute(select(Topic).where(Topic.slug == spec["slug"]))
            ).scalar_one_or_none()
            if existing is None:
                existing = Topic(**spec)
                db.add(existing)
            else:
                existing.title = spec["title"]
                existing.description = spec["description"]
                existing.guiding_points = spec["guiding_points"]
            topics[spec["slug"]] = existing
        await db.flush()

        created = 0
        for email, name, role, gender, interests in USERS:
            user = (
                await db.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if user is None:
                user = User(
                    email=email,
                    display_name=name,
                    role=role,
                    gender=gender,
                    password_hash=hash_password(DEMO_PASSWORD),
                )
                db.add(user)
                await db.flush()
                created += 1
            else:
                # Existing demo rows predate the column; keep them useful.
                user.gender = gender

            for slug in interests:
                topic = topics[slug]
                exists = (
                    await db.execute(
                        select(UserTopicInterest).where(
                            UserTopicInterest.user_id == user.id,
                            UserTopicInterest.topic_id == topic.id,
                        )
                    )
                ).scalar_one_or_none()
                if exists is None:
                    db.add(
                        UserTopicInterest(user_id=user.id, topic_id=topic.id, proficiency=4)
                    )

        log.info("seed.done", topics=len(TOPICS), users_created=created)


async def main() -> None:
    configure_logging()
    try:
        await seed()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
