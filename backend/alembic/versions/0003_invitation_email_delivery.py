"""Track who an invitation was emailed to, and whether it arrived.

Revision ID: 0003
Revises: 0002

Participants are now named by the host as email addresses rather than picked by the
matcher, so the address is part of the invitation itself. ``email_sent_at`` /
``email_error`` exist because a host who typed an address wrong needs to see that the
message bounced, not sit watching a seat that will never fill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invitations",
        sa.Column("invited_email", sa.String(length=254), nullable=False, server_default=""),
    )
    op.add_column(
        "invitations", sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("invitations", sa.Column("email_error", sa.Text(), nullable=True))

    # Backfill from the user rows so pre-existing invitations still render a recipient.
    op.execute(
        "UPDATE invitations i SET invited_email = u.email FROM users u WHERE u.id = i.user_id"
    )

    # Passwordless accounts: created by an invitation, they authenticate by magic link
    # only. A sentinel that bcrypt can never match is safer than a nullable column that
    # some future code path forgets to check.
    op.alter_column("users", "password_hash", existing_type=sa.String(length=128), nullable=False)


def downgrade() -> None:
    op.drop_column("invitations", "email_error")
    op.drop_column("invitations", "email_sent_at")
    op.drop_column("invitations", "invited_email")
