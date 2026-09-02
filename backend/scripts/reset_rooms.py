"""Close every classroom and discussion the host still has open.

A session in a running state holds all of its participants out of *every* other
classroom, so one discussion nobody ended blocks the next one you try to start —
the symptom is "already in another live discussion" when somebody accepts.

The janitor sweeps these on its own, but not for fifteen minutes (a room nobody
joined) or ninety (one that was running). This is the button for when you are
sitting in front of it and want to go again now.

    docker compose exec api python -m scripts.reset_rooms
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from app.core.config import settings

BASE = f"http://localhost:8000{settings.api_prefix}"
HOST_EMAIL = "super@gdclassroom.io"
HOST_PASSWORD = "Password123!"
#: Statuses that still hold seats.
OPEN = {"DRAFT", "INVITING", "READY", "LIVE"}


async def main() -> int:
    async with httpx.AsyncClient(timeout=30) as http:
        try:
            login = await http.post(
                f"{BASE}/auth/login", json={"email": HOST_EMAIL, "password": HOST_PASSWORD}
            )
            login.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"  could not sign in as the host: {exc}")
            return 1

        head = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        rooms = (await http.get(f"{BASE}/classrooms?limit=50", headers=head)).json()["data"]

        cleared = 0
        for room in rooms:
            if room["status"] not in OPEN:
                continue
            detail = (
                await http.get(f"{BASE}/classrooms/{room['id']}", headers=head)
            ).json()["data"]
            if detail.get("session_id"):
                await http.post(
                    f"{BASE}/sessions/{detail['session_id']}/end", headers=head, json={}
                )
            await http.post(f"{BASE}/classrooms/{room['id']}/cancel", headers=head, json={})
            cleared += 1
            print(f"  closed  {room['title'][:60]}")

        if not cleared:
            print("  nothing was open — everyone is free to be invited")
        else:
            # Ending is asynchronous: the moderator says goodbye and writes a summary
            # before the seats are actually released.
            print(f"\n  closed {cleared}; giving them a moment to finish…")
            await asyncio.sleep(6)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
