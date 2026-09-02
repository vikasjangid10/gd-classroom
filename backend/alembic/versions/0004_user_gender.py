"""Self-declared gender, for choosing a discussion call-sign that fits.

Revision ID: 0004
Revises: 0003

Participants are anonymous inside a discussion — they are known by a call-sign rather
than by the name on their account. For that call-sign to read as a person rather than a
row number it has to fit the person using it, and the only honest source for that is the
person themselves. It is never inferred from their name.

``UNSPECIFIED`` is the default and stays perfectly usable: those seats draw from the
whole pool.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "gender",
            sa.String(length=16),
            nullable=False,
            server_default="UNSPECIFIED",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "gender")
