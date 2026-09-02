"""Single import surface for Alembic and ``create_all``.

Importing this module guarantees every table is attached to ``Base.metadata``.
"""

from __future__ import annotations

from app.db.base import Base
from app.modules.classroom.models import Classroom, ClassroomParticipant, Topic
from app.modules.identity.models import RefreshToken, User, UserTopicInterest
from app.modules.invitation.models import Invitation
from app.modules.session.models import (
    SessionParticipant,
    SessionRecord,
    SessionSummary,
    Turn,
)

__all__ = [
    "Base",
    "Classroom",
    "ClassroomParticipant",
    "Invitation",
    "RefreshToken",
    "SessionParticipant",
    "SessionRecord",
    "SessionSummary",
    "Topic",
    "Turn",
    "User",
    "UserTopicInterest",
]
