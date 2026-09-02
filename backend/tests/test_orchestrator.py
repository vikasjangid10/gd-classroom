"""``AIOrchestratorService.leave`` — the explicit "I am leaving" action.

Kept separate from ``test_runner.py``: everything here is about the seam between a
person's own "I'm leaving" click and the runner ever hearing about it, not about what
the runner then does with a disconnect — that half is already covered there.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.modules.moderation.commands import ParticipantDisconnected
from app.modules.moderation.orchestrator import AIOrchestratorService, LiveSession

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakePlane:
    """A peer connection that was never created — the text-mode and never-joined case.

    ``remove_peer`` on a real ``VoicePlane`` is a silent no-op here (nothing in
    ``self.peers`` to pop), which is exactly the condition that used to swallow a
    genuine departure.
    """

    async def remove_peer(self, user_id) -> None:
        return None


class FakeRunner:
    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, command) -> None:
        self.submitted.append(command)


def orchestrator() -> AIOrchestratorService:
    # `leave` touches only `self._live`; the constructor args below are never used by
    # it, so fakes stand in without pulling in a database or a real event bus.
    return AIOrchestratorService(providers=None, bus=None, gateway=None, settings=None)  # type: ignore[arg-type]


async def test_leaving_is_reported_even_when_no_peer_connection_ever_existed() -> None:
    """The bug: accept the invitation, join by text or not at all, then leave.

    ``plane.remove_peer`` only reports a disconnect when it finds a WebRTC peer that
    was connected — for text-mode, or for someone who left before connecting anything,
    there is nothing to find. Left at that, the runner is never told, the ledger never
    marks them absent, and the moderator keeps trying to hand them the floor.
    """
    orch = orchestrator()
    session_id = uuid4()
    user_id = uuid4()
    runner = FakeRunner()
    orch._live[session_id] = LiveSession(
        runner=runner, plane=FakePlane(), task=asyncio.get_event_loop().create_future()  # type: ignore[arg-type]
    )

    await orch.leave(session_id, user_id)

    assert any(
        isinstance(c, ParticipantDisconnected) and c.user_id == user_id
        for c in runner.submitted
    ), "leaving must be reported to the runner even with no WebRTC peer to remove"


async def test_leaving_an_unknown_session_is_a_quiet_no_op() -> None:
    """Leaving a session that was never provisioned — e.g. before the host started it
    — has nothing to update yet, and must not raise."""
    orch = orchestrator()
    await orch.leave(uuid4(), uuid4())  # must not raise
