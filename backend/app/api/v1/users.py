from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, Users
from app.core.responses import ok
from app.modules.identity.schemas import InterestsIn, UserOut

router = APIRouter(tags=["users"])


@router.get("/users/me")
async def me(user: CurrentUser, users: Users) -> dict:
    record, interests = await users.get_profile(user.id)
    return ok(
        {
            **UserOut.model_validate(record).model_dump(mode="json"),
            "interests": [
                {"topic_id": str(topic_id), "proficiency": level}
                for topic_id, level in interests
            ],
        }
    )


@router.put("/users/me/interests")
async def set_interests(payload: InterestsIn, user: CurrentUser, users: Users) -> dict:
    await users.set_interests(
        user.id, [(item.topic_id, item.proficiency) for item in payload.interests]
    )
    return ok({"updated": len(payload.interests)})


@router.get("/users/participants")
async def list_participants(user: CurrentUser, users: Users) -> dict:
    """Roster the host sees when reviewing who could be matched."""
    people = await users.list_participants()
    return ok(
        [
            {"id": str(p.id), "display_name": p.display_name, "email": p.email}
            for p in people
        ]
    )
