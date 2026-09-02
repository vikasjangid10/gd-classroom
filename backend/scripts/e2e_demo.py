"""End-to-end walkthrough of the whole product, over HTTP, with no browser.

The host signs in, picks a topic, chooses four registered participants, and invites them.
This script then does what those four would do inside the app: sign in, find the
invitation waiting in their lobby, accept, join the room in text mode, listen on the SSE
stream, take turns when the moderator hands over the floor — and read the summary.

Nothing is emailed. The invitation reaches each participant over their own lobby event
stream, published in the same transaction that created the classroom, which is why it is
already waiting by the time they look.

    docker compose exec api python -m scripts.e2e_demo
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import suppress
from typing import Any

import httpx

BASE = "http://localhost:8000/api/v1"
PASSWORD = "Password123!"
HOST_EMAIL = "super@gdclassroom.io"

ANSWERS = [
    "I'd start from retrieval quality, because in production that is where almost every "
    "regression comes from. Chunk size and overlap decide recall long before the model "
    "gets involved, and we measured that across three different corpora.",
    "Evaluation is the part teams skip. If you cannot measure groundedness you are just "
    "trading one set of vibes for another, so we built a small labelled set first.",
    "It depends.",
    "Latency is a product feature here. Streaming the first sentence to synthesis rather "
    "than waiting for the whole completion removed about a second of dead air per turn.",
    "I'd push back on that slightly. Hybrid search helped us far more than a bigger "
    "embedding model did, and it was a tenth of the cost to run.",
]


def show(label: str, detail: str = "") -> None:
    print(f"  {label:<26} {detail}", flush=True)


class Client:
    def __init__(self, http: httpx.AsyncClient, email: str) -> None:
        self.http = http
        self.email = email
        self.token = ""
        self.user: dict[str, Any] = {}

    async def login(self) -> Client:
        response = await self.http.post(
            f"{BASE}/auth/login", json={"email": self.email, "password": PASSWORD}
        )
        response.raise_for_status()
        data = response.json()["data"]
        self.token = data["access_token"]
        self.user = data["user"]
        return self

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def get(self, path: str) -> Any:
        response = await self.http.get(f"{BASE}{path}", headers=self.headers)
        response.raise_for_status()
        return response.json()["data"]

    async def post(self, path: str, body: Any = None) -> Any:
        response = await self.http.post(f"{BASE}{path}", headers=self.headers, json=body)
        response.raise_for_status()
        return response.json()["data"] if response.content else None


async def await_summary(client: Client, session_id: str, timeout: float = 90) -> Any | None:
    """Wait for the closing summary, which is generated after the session ends."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            summary = await client.get(f"/sessions/{session_id}/summary")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            await asyncio.sleep(1.0)
            continue
        if summary["status"] != "PENDING":
            return summary
        await asyncio.sleep(1.0)
    return None


async def listen(client: Client, session_id: str, seen: list[dict], stop: asyncio.Event) -> None:
    """Consume the room's SSE stream exactly as the browser does."""
    ticket = (await client.post(f"/sessions/{session_id}/tickets"))["sse_ticket"]
    url = f"{BASE}/sessions/{session_id}/events?ticket={ticket}"

    async with httpx.AsyncClient(timeout=None) as http, http.stream("GET", url) as response:
        event_name = ""
        async for line in response.aiter_lines():
            if stop.is_set():
                return
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    frame = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict) or "type" not in frame:
                    continue
                seen.append(frame)
                render(frame)
                if frame["type"] == "session.ended":
                    stop.set()
                    return
                event_name = event_name  # keep the parser honest


def render(frame: dict) -> None:
    kind, payload = frame["type"], frame.get("payload", {})
    if kind == "moderator.speaking" and payload.get("is_final"):
        print(f"\n  \033[33mMODERATOR\033[0m  {payload['text']}", flush=True)
    elif kind == "floor.granted":
        print(
            f"  \033[36m→ floor to {payload['display_name']}"
            f" ({payload['max_seconds']}s)\033[0m",
            flush=True,
        )
    elif kind == "transcript.final" and payload.get("speaker") == "participant":
        print(f"  {payload['display_name']}: {payload['text'][:110]}", flush=True)
    elif kind == "speaking_time.updated":
        bars = "  ".join(
            f"{p['display_name']} {p['seconds']}s/{p['turns']}t" for p in payload["participants"]
        )
        print(f"  \033[90m{bars}\033[0m", flush=True)
    elif kind in ("session.state", "session.ended", "session.summary_ready"):
        print(f"  \033[90m[{kind}] {payload}\033[0m", flush=True)


async def main() -> int:
    async with httpx.AsyncClient(timeout=30) as http:
        print("\n\033[1m1. Host creates a classroom and chooses four people\033[0m")
        host = await Client(http, HOST_EMAIL).login()
        topics = await host.get("/topics")
        topic = next(t for t in topics if t["slug"] == "rag")

        roster = await host.get("/users/participants")
        chosen = [person["email"] for person in roster[:4]]
        if len(chosen) < 4:
            print(f"  ! only {len(chosen)} participants are registered; run scripts.seed")
            return 1

        classroom = await host.post(
            "/classrooms", {"topic_id": topic["id"], "invitee_emails": chosen}
        )
        show("classroom", f"{classroom['title']}  [{classroom['status']}]")
        show("invited", ", ".join(chosen))

        print("\n\033[1m2. The invitation reaches each of them, in the app\033[0m")
        participants: list[Client] = []
        session_id = None
        for address in chosen:
            person = await Client(http, address).login()
            participants.append(person)

            # The invitation is already there — it was published to this user's lobby
            # stream inside the transaction that created the classroom.
            waiting = [
                item
                for item in await person.get("/invitations")
                if item["classroom_id"] == classroom["id"]
            ]
            if not waiting:
                print(f"  ! no invitation waiting for {address}")
                return 1
            invitation = waiting[0]
            show(f"{person.user['display_name']}", f"sees “{invitation['topic_title']}”")

            result = await person.post(f"/invitations/{invitation['id']}/accept")
            show("  accepted", f"→ {result['classroom_status']}")
            session_id = result["session_id"] or session_id

        if not session_id:
            print("  ! quorum never reached")
            return 1
        show("session provisioned", session_id)

        print("\n\033[1m3. Everyone joins the room\033[0m")
        seen: list[dict] = []
        stop = asyncio.Event()
        stream = asyncio.create_task(listen(participants[0], session_id, seen, stop))
        await asyncio.sleep(0.6)

        for person in participants:
            await person.post(f"/sessions/{session_id}/join-text")
            show("joined", person.user["display_name"])

        print("\n\033[1m4. The moderator runs the discussion\033[0m")
        by_id = {p.user["id"]: p for p in participants}
        answered: set[int] = set()
        # The host is paced by the length of its own speech, so a real discussion takes
        # real time. This budget is the whole run, not one turn.
        deadline = asyncio.get_running_loop().time() + 420

        try:
            while not stop.is_set() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.4)
                grants = [f for f in seen if f["type"] == "floor.granted"]
                if not grants:
                    continue
                latest = grants[-1]
                if latest["seq"] in answered:
                    continue
                answered.add(latest["seq"])
                speaker = by_id.get(latest["payload"]["participant_id"])
                if speaker is None:
                    continue
                await asyncio.sleep(0.5)
                answer = ANSWERS[len(answered) % len(ANSWERS)]
                await speaker.post(f"/sessions/{session_id}/turn-text", {"text": answer})

                if len(answered) >= 8:
                    break
        finally:
            # Always, even on timeout or Ctrl-C. A session left ACTIVE holds all four
            # participants out of every future classroom until the janitor sweeps it —
            # which is how the previous run of this script wedged the next one.
            with suppress(Exception):
                await host.post(f"/sessions/{session_id}/end", {})
            stop.set()
            stream.cancel()

        print("\n\033[1m5. Results\033[0m")
        # Order matters: the runner flushes the transcript, *then* writes the summary.
        # Waiting for the summary is therefore also how you know the turns have landed.
        summary = await await_summary(participants[0], session_id)
        show("summary", summary["status"] if summary else "never arrived")
        if summary is None:
            print("\n\033[1mFAIL\033[0m — the summary never arrived\n")
            return 1

        turns = await participants[0].get(f"/sessions/{session_id}/transcript")
        show("turns persisted", str(len(turns)))

        if summary["status"] == "READY":
            print(f"\n  \033[1m{summary['headline']}\033[0m")
            for point in summary["key_points"]:
                print(f"   · {point}")

        info = await participants[0].get(f"/sessions/{session_id}")
        show("session status", f"{info['status']} / {info['end_reason']}")
        show("live state after end", str(info["live"]))
        for row in info["participants"]:
            show("  spoken", f"{row['spoken_ms']} ms over {row['turns_taken']} turns")

        ok = (
            info["status"] == "ENDED"
            and info["live"] is None
            and len(turns) > 4
            and summary["status"] == "READY"
        )
        print(f"\n\033[1m{'PASS' if ok else 'FAIL'}\033[0m — end-to-end flow\n")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
