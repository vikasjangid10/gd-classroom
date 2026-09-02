"""Explicit, table-driven state machines.

A transition table is worth far more than scattered ``if status ==`` checks: it is
readable, it is exhaustively testable, and an illegal transition raises instead of
silently corrupting an aggregate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Generic, TypeVar

from app.core.errors import IllegalTransitionError
from app.domain.enums import ClassroomStatus, SessionStatus

S = TypeVar("S")
E = TypeVar("E")


class StateMachine(Generic[S, E]):
    """A frozen transition table plus the two questions callers actually ask."""

    def __init__(self, name: str, table: Mapping[S, Mapping[E, S]], terminal: frozenset[S]) -> None:
        self.name = name
        self._table = table
        self.terminal = terminal

    def can(self, current: S, event: E) -> bool:
        return event in self._table.get(current, {})

    def next(self, current: S, event: E) -> S:
        try:
            return self._table[current][event]
        except KeyError as exc:
            raise IllegalTransitionError(self.name, current, event) from exc

    def allowed(self, current: S) -> list[E]:
        return list(self._table.get(current, {}))

    def is_terminal(self, current: S) -> bool:
        return current in self.terminal


# --------------------------------------------------------------------- classroom
class ClassroomEvent(str):
    pass


class CE:
    INVITATIONS_SENT = "invitations_sent"
    QUORUM_REACHED = "quorum_reached"
    QUORUM_LOST = "quorum_lost"
    STARTED = "started"
    SESSION_ENDED = "session_ended"
    SESSION_ABORTED = "session_aborted"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


CLASSROOM_FSM: StateMachine[ClassroomStatus, str] = StateMachine(
    name="Classroom",
    table={
        ClassroomStatus.DRAFT: {
            CE.INVITATIONS_SENT: ClassroomStatus.INVITING,
            CE.CANCELLED: ClassroomStatus.CANCELLED,
        },
        ClassroomStatus.INVITING: {
            CE.QUORUM_REACHED: ClassroomStatus.READY,
            CE.CANCELLED: ClassroomStatus.CANCELLED,
            CE.EXPIRED: ClassroomStatus.EXPIRED,
        },
        ClassroomStatus.READY: {
            # a participant may withdraw between acceptance and start
            CE.QUORUM_LOST: ClassroomStatus.INVITING,
            CE.STARTED: ClassroomStatus.LIVE,
            CE.CANCELLED: ClassroomStatus.CANCELLED,
            CE.EXPIRED: ClassroomStatus.EXPIRED,
        },
        ClassroomStatus.LIVE: {
            CE.SESSION_ENDED: ClassroomStatus.COMPLETED,
            # nobody turned up: fall back to READY so the host may retry once
            CE.SESSION_ABORTED: ClassroomStatus.READY,
        },
    },
    terminal=frozenset(
        {ClassroomStatus.COMPLETED, ClassroomStatus.CANCELLED, ClassroomStatus.EXPIRED}
    ),
)


# --------------------------------------------------------------------- session
class SE:
    PARTICIPANT_JOINING = "participant_joining"
    ALL_CONNECTED = "all_connected"
    JOIN_TIMEOUT = "join_timeout"
    DISCUSSION_FINISHED = "discussion_finished"
    SUMMARY_DONE = "summary_done"
    FATAL = "fatal"
    HOST_ENDED = "host_ended"


SESSION_FSM: StateMachine[SessionStatus, str] = StateMachine(
    name="Session",
    table={
        SessionStatus.PENDING: {
            SE.PARTICIPANT_JOINING: SessionStatus.CONNECTING,
            SE.JOIN_TIMEOUT: SessionStatus.ABORTED,
            SE.FATAL: SessionStatus.ABORTED,
        },
        SessionStatus.CONNECTING: {
            SE.ALL_CONNECTED: SessionStatus.ACTIVE,
            SE.JOIN_TIMEOUT: SessionStatus.ABORTED,
            SE.HOST_ENDED: SessionStatus.ABORTED,
            SE.FATAL: SessionStatus.ABORTED,
        },
        SessionStatus.ACTIVE: {
            SE.DISCUSSION_FINISHED: SessionStatus.SUMMARIZING,
            SE.HOST_ENDED: SessionStatus.SUMMARIZING,
            SE.FATAL: SessionStatus.ABORTED,
        },
        SessionStatus.SUMMARIZING: {
            SE.SUMMARY_DONE: SessionStatus.ENDED,
            # a failed summary must still end the session cleanly
            SE.FATAL: SessionStatus.ENDED,
        },
    },
    terminal=frozenset({SessionStatus.ENDED, SessionStatus.ABORTED}),
)
