"""Include PENDING sessions in the stale-session index.

Revision ID: 0002
Revises: 0001

A session that is provisioned but never joined still holds its four participants through
``uq_session_participants_user_live``. The janitor has to be able to find those, so the
index that backs its sweep must cover ``PENDING`` too.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "status IN ('CONNECTING','ACTIVE','SUMMARIZING')"
_NEW = "status IN ('PENDING','CONNECTING','ACTIVE','SUMMARIZING')"


def upgrade() -> None:
    op.drop_index("ix_sessions_stale", table_name="sessions")
    op.create_index(
        "ix_sessions_stale",
        "sessions",
        ["status", "started_at"],
        postgresql_where=_NEW,
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_stale", table_name="sessions")
    op.create_index(
        "ix_sessions_stale",
        "sessions",
        ["status", "started_at"],
        postgresql_where=_OLD,
    )
