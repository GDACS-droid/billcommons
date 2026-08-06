"""webhook_subscriptions + webhook_deliveries: push delivery over the change feed

See workers/webhooks/dispatch_webhooks.py for the reader/writer, and
packages/shared/billcommons_shared/safe_http.py for the SSRF-guarded transport
it POSTs through. Two design decisions worth recording here because they are
schema, not code, and a later migration would have to redo them:

  * `signing_secret` and `manage_token_hash` are deliberately TWO different
    secrets. `signing_secret` has to be readable in plaintext forever (it's
    the HMAC key every delivery is signed with); `manage_token_hash` never
    needs to be -- only compared against, so it is stored as a sha256 hash and
    the plaintext manage token is shown to the caller exactly once, at
    creation. A DB disclosure that leaks `signing_secret` (letting an attacker
    forge deliveries a receiver would accept) does not also hand over the
    ability to read/delete/reactivate the subscription, and vice versa.
  * `last_seq` has NO DEFAULT and NO server_default. It is set in the same
    INSERT that creates the row, to the *safety-lag watermark*
    (max(seq) among bill_events with changed_at older than
    COMMIT_SAFETY_LAG_SECONDS), never to 0 and never to the raw head of the
    log. A subscription created with last_seq at the head could pick up a
    seq that becomes visible only after a slower concurrent writer commits at
    a lower seq -- see migration 0005's own warning, which is the same
    invariant `/changes` and the alerts sender already depend on.

`UNIQUE (url, kind, target, event_kinds)` uses NULLS NOT DISTINCT because
`event_kinds` is nullable (NULL = "all kinds") -- migration 0011 already hit
this: under the Postgres default (NULLS DISTINCT), every NULL-event_kinds row
is treated as distinct from every other, and the uniqueness guard silently
does not bind the most common case (subscribe to everything).

Revision ID: 0012
Revises: 0011
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("url", sa.Text(), nullable=False),
        # Registrable domain extracted at creation time, lowercase/IDN-
        # normalized -- used for the per-domain quota, kept as its own column
        # so the quota query is an index lookup rather than a per-row URL
        # parse at request time.
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("creator_ip", sa.Text(), nullable=False),
        sa.Column("signing_secret", sa.Text(), nullable=False),
        sa.Column("manage_token_hash", sa.Text(), nullable=False),
        # topic | jurisdiction | bills
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        # CSV of the seven change kinds; NULL = all.
        sa.Column("event_kinds", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("challenge_token", sa.Text(), nullable=True),
        sa.Column("challenge_attempts", sa.Integer(), nullable=False, server_default="0"),
        # NO server_default, deliberately -- see module docstring. Every
        # INSERT must supply the safety-lag watermark explicitly.
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.Integer(), nullable=True),
        # Error CLASS only -- see ToolInvocation (migration 0008) for the same
        # convention and why: a free-text message can quote a receiver's own
        # response body, which we do not control and should not persist.
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        # 'created' | 'disabled' -- drained by workers/alerts/send_alerts.py's
        # piggyback, which has the Resend creds this dispatcher does not.
        sa.Column("notify_pending", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("kind in ('topic','jurisdiction','bills')", name="ck_webhook_kind"),
        sa.CheckConstraint(
            "notify_pending is null or notify_pending in ('created','disabled')",
            name="ck_webhook_notify_pending",
        ),
    )
    op.execute(
        "ALTER TABLE webhook_subscriptions "
        "ADD CONSTRAINT uq_webhook_url_kind_target_event_kinds "
        "UNIQUE NULLS NOT DISTINCT (url, kind, target, event_kinds)"
    )
    # The dispatcher's eligibility scan: active AND verified subs due for a
    # tick. Partial so a disabled/unverified subscription (the majority once
    # abuse accumulates) never enters the index at all.
    op.execute(
        "CREATE INDEX ix_webhook_subscriptions_due "
        "ON webhook_subscriptions (next_attempt_at, last_attempt_at) "
        "WHERE active AND verified"
    )
    # The per-domain quota check.
    op.execute(
        "CREATE INDEX ix_webhook_subscriptions_host "
        "ON webhook_subscriptions (host) WHERE active"
    )
    op.create_index(
        "ix_webhook_subscriptions_creator_ip_created_at",
        "webhook_subscriptions",
        ["creator_ip", "created_at"],
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "subscription_id",
            UUID(as_uuid=True),
            sa.ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Per-ATTEMPT id, echoed in the X-BillCommons-Delivery header -- never
        # a dedupe key (a retry of the same events gets a new one). See
        # payload contract in workers/webhooks/dispatch_webhooks.py.
        sa.Column("delivery_id", UUID(as_uuid=True), nullable=False),
        sa.Column("first_seq", sa.BigInteger(), nullable=True),
        sa.Column("last_seq", sa.BigInteger(), nullable=True),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "attempted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_subscription_attempted",
        "webhook_deliveries",
        ["subscription_id", "attempted_at"],
    )
    # Retention sweep: the dispatcher prunes rows older than 30 days each tick.
    op.create_index("ix_webhook_deliveries_attempted_at", "webhook_deliveries", ["attempted_at"])


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_attempted_at", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_subscription_attempted", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")

    op.drop_index("ix_webhook_subscriptions_creator_ip_created_at", table_name="webhook_subscriptions")
    op.execute("DROP INDEX IF EXISTS ix_webhook_subscriptions_host")
    op.execute("DROP INDEX IF EXISTS ix_webhook_subscriptions_due")
    op.drop_table("webhook_subscriptions")
