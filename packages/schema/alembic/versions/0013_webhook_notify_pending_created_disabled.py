"""webhook_subscriptions.notify_pending: allow 'created_disabled'

Verify round-3 fix #13: a subscription that verifies and then gets
auto-disabled again within the same window (before
workers/alerts/send_alerts.py's next nightly run ever drains the pending
'created' notice) must not silently lose the fact that it ever verified --
`notify_pending` is a single-slot column, so the two facts are combined
into ONE new sentinel value, 'created_disabled', which
workers/webhooks/dispatch_webhooks.py's `_notify_disabled` writes and
send_alerts.py's `render_webhook_notice` renders as a combined "verified
earlier today and has since been auto-disabled" message.

Migration 0012 was already applied to prod before this fix landed (see
`ck_webhook_notify_pending`'s original definition, `notify_pending is null
or notify_pending in ('created','disabled')`) -- this widens that CHECK
constraint rather than editing 0012 in place, so an already-applied
database gets the new value via a normal `alembic upgrade`, not by hoping
nobody re-runs 0012 from scratch.

Revision ID: 0013
Revises: 0012
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_webhook_notify_pending", "webhook_subscriptions", type_="check")
    op.create_check_constraint(
        "ck_webhook_notify_pending",
        "webhook_subscriptions",
        "notify_pending is null or notify_pending in ('created','disabled','created_disabled')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_webhook_notify_pending", "webhook_subscriptions", type_="check")
    op.create_check_constraint(
        "ck_webhook_notify_pending",
        "webhook_subscriptions",
        "notify_pending is null or notify_pending in ('created','disabled')",
    )
