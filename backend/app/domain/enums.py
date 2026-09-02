"""Every enumerated value in the system, in one place.

``str, Enum`` rather than ``StrEnum`` so the codebase runs on Python 3.11 and the values
serialise to plain strings in JSON and in the database.
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    SUPER_USER = "SUPER_USER"
    PARTICIPANT = "PARTICIPANT"


class ClassroomStatus(str, Enum):
    DRAFT = "DRAFT"
    INVITING = "INVITING"
    READY = "READY"
    LIVE = "LIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class InvitationStatus(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class SessionStatus(str, Enum):
    PENDING = "PENDING"
    CONNECTING = "CONNECTING"
    ACTIVE = "ACTIVE"
    SUMMARIZING = "SUMMARIZING"
    ENDED = "ENDED"
    ABORTED = "ABORTED"


class EndReason(str, Enum):
    COMPLETED = "COMPLETED"
    TIME_LIMIT = "TIME_LIMIT"
    HOST_ENDED = "HOST_ENDED"
    NOT_ENOUGH_PARTICIPANTS = "NOT_ENOUGH_PARTICIPANTS"
    #: Everyone was present and nobody spoke, turn after turn. Ending is the honest
    #: outcome; the alternative is a moderator improvising a discussion by itself.
    NOBODY_SPOKE = "NOBODY_SPOKE"
    JOIN_TIMEOUT = "JOIN_TIMEOUT"
    FATAL_ERROR = "FATAL_ERROR"


class ModeratorState(str, Enum):
    IDLE = "IDLE"
    INTRODUCING = "INTRODUCING"
    EXPLAINING_RULES = "EXPLAINING_RULES"
    SELECTING_SPEAKER = "SELECTING_SPEAKER"
    QUESTIONING = "QUESTIONING"
    LISTENING = "LISTENING"
    EVALUATING = "EVALUATING"
    FOLLOWING_UP = "FOLLOWING_UP"
    CLOSING = "CLOSING"
    SUMMARIZING = "SUMMARIZING"
    DONE = "DONE"


class SpeakerType(str, Enum):
    MODERATOR = "MODERATOR"
    PARTICIPANT = "PARTICIPANT"


class TurnKind(str, Enum):
    INTRO = "INTRO"
    RULES = "RULES"
    QUESTION = "QUESTION"
    FOLLOW_UP = "FOLLOW_UP"
    ANSWER = "ANSWER"
    NUDGE = "NUDGE"
    CLOSING = "CLOSING"


class SummaryStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"


class Gender(str, Enum):
    """Self-declared, and used for one thing only: picking a call-sign that fits.

    Never inferred from somebody's name. Name-to-gender guessing is wrong often enough
    that it would hand people a call-sign contradicting who they are, in a room built
    to keep them anonymous — and it is wrong most often for exactly the names that are
    least common, which is the worst possible failure distribution.
    """

    MALE = "MALE"
    FEMALE = "FEMALE"
    UNSPECIFIED = "UNSPECIFIED"


class ParticipantConnection(str, Enum):
    INVITED = "INVITED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    LEFT = "LEFT"
    #: Taken out of the round by the moderator, and not allowed back into it.
    REMOVED = "REMOVED"
