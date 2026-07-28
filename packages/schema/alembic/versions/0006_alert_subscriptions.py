"""alert_subscriptions: email alerts over the change feed

The site's conversion hook: "email me when bills on this topic move". Each
row is one (email, kind, target) subscription; the nightly sender walks
bill_events from the subscription's last_seq to the current watermark and
mails a digest when anything matched.

`last_seq` is a bill_events.seq value, NOT a timestamp -- the same reasoning
as the feed itself (see 0005): timestamps tie in bulk and cannot page.

`unsubscribe_token` is a random URL-safe secret; the unsubscribe link in
every email is a GET carrying it, so one click works from any mail client
with no login. Single opt-in on purpose: the only thing an address can be
subscribed to is public legislative data, and the first digest carries the
same one-click unsubscribe.

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alert_subscriptions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.Text(), nullable=False),
        # 'topic' today; the column exists so bill- and search-scoped alerts
        # can land without a migration.
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("unsubscribe_token", sa.Text(), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        # Where the sender left off in bill_events. 0 means "never sent";
        # the first run fast-forwards to the current watermark rather than
        # replaying history at a new subscriber.
        sa.Column("last_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("email", "kind", "target", name="uq_alert_email_kind_target"),
    )
    op.create_index(
        "ix_alert_subscriptions_active", "alert_subscriptions", ["active"]
    )


def downgrade() -> None:
    op.drop_index("ix_alert_subscriptions_active", table_name="alert_subscriptions")
    op.drop_table("alert_subscriptions")
