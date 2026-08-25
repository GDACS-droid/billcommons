"""api_keys/billing: customer identity, API keys, metering, and the
Stripe/snapshot tables Phase 2/3 need (created now so those phases are
purely additive -- no later migration has to retrofit a foreign key onto a
table a live customer's key already points at).

Why now, all at once: `api_keys.customer_id` and `snapshot_entitlements.
customer_id` both point at `api_customers`, and `api_key_usage`/
`api_key_usage_subnets` both point at `api_keys`. Phasing the Stripe/
snapshot tables into a LATER migration would mean either standing up
`api_customers`/`api_keys` twice (once bare, once with the columns Stripe
sync needs) or blocking Phase 1 on Stripe integration finishing first --
neither is worth it when the tables cost nothing unused. Phase 1 code
(`billcommons_api.api_keys`, `billcommons_api.quota`) only ever reads/
writes `api_customers`, `api_keys`, `api_key_usage`, `api_key_usage_
subnets`, and `account_login_tokens`; `stripe_events`, `snapshot_
artifacts`, `snapshot_entitlements`, and `snapshot_downloads` are unused
until Phase 2/3 land.

Design notes carried over from the locked spec (2026-08-21 monetization
spec, `SPEC-LOCKED.md` amendments R9/A1/A3/A4/A6/A7/B1/B3/B4/B5):

  * `api_keys.plan` is DENORMALIZED on purpose (R9/base spec): the auth hot
    path (`api_keys.resolve_key`) must be one indexed lookup, not a join
    across customer/subscription; the Stripe webhook (Phase 2) is its only
    writer once billing exists, and every subscription event recomputes it
    onto ALL of a customer's usable keys in one UPDATE (B4).
  * `api_customers` carries `extra_requests_per_day` / `extra_heavy_per_day`
    / `override_expires_at` (manual quota bumps -- a founder support
    override) and `suspended_at` / `suspension_reason` (operator kill
    switch) directly, rather than a separate table -- these are account-
    level facts, not billing events, and folding them onto the row the auth
    hot path already loads avoids a second query per keyed request (Codex
    R9, adopted).
  * `api_subscriptions` has a PARTIAL unique index on `customer_id` WHERE
    `status NOT IN ('canceled')` -- at most one non-canceled subscription
    per customer (B4's "one billing authority per customer" invariant).
    `past_due_since` (A3) anchors the 7-day dunning window; `last_event_
    created_at` (A4) makes every subscription-event handler idempotent
    against Stripe delivering events out of order (apply only if the
    incoming event's `created` is >= this column).
  * `api_keys` carries the reveal-once machinery (B1): `reveal_ciphertext`
    (Fernet-encrypted plaintext key, nulled the moment it is revealed or
    expires), `reveal_token_hash` / `reveal_expires_at` (24h). The key
    format (`bc_live_`/`bc_test_` + 32 base62) never appears in this
    schema -- it lives in `key_prefix` (first 16 chars, for O(1) display-
    safe lookup) and `key_hash` (sha256 hex of the full key, compared with
    `hmac.compare_digest`); the full key itself is never stored anywhere
    except transiently in `reveal_ciphertext` between mint and reveal.
  * `api_key_usage` is the billable read/write path (`billcommons_api.
    quota.QuotaMiddleware`): PK `(key_id, usage_date)` so the post-response
    accounting statement (B6) is a single `INSERT ... ON CONFLICT DO
    UPDATE` per keyed request. `api_key_usage_subnets` (A6) is the same
    shape one level finer -- `(key_id, usage_date, subnet)` -- so the admin
    usage endpoint can flag key sharing (`count(distinct subnet)`) without
    logging any IP address more granular than its containing subnet.
  * `stripe_events` is the idempotency ledger for Phase 2 (`INSERT ... ON
    CONFLICT (id) DO NOTHING RETURNING id` -- no row back means already
    processed). `outcome` records `skipped_foreign_app` for any event
    whose object lacks `metadata.app == "billcommons"` -- this Stripe
    account also runs the owner's other sub-businesses (R7's HIGH finding).
  * `account_login_tokens` backs both magic-link login (`purpose='login'`)
    and the post-checkout key-reveal flow (`purpose='reveal'`, B1) -- one
    table, one token shape (sha256 hash, 15-min TTL, single use), because
    both are "prove you own this email, once, briefly."
  * `snapshot_entitlements` gains `delivered_at` (B5, supersedes A7's
    originally-proposed `fulfilled_at` name -- same column, same rule:
    "revoke on `charge.refunded` iff `delivered_at IS NULL`", set by the
    manual runbook once the 7-day R2 link is sent) and `stripe_payment_
    intent_id` (A7, unique partial -- `charge.refunded` carries a charge,
    not a checkout session, so refund-time lookup goes charge -> payment
    intent -> this column).
  * Every Stripe-id column (`api_customers.stripe_customer_id`, `api_
    subscriptions.stripe_subscription_id`, `snapshot_entitlements.stripe_
    checkout_session_id`, `snapshot_entitlements.stripe_payment_intent_id`)
    is a PARTIAL unique index `WHERE ... IS NOT NULL` (R9) -- Postgres's
    plain `UNIQUE` on a nullable column already treats NULLs as distinct,
    so a bare unique constraint would work identically, but the partial
    form is explicit about intent and matches migration 0012's `NULLS NOT
    DISTINCT` precedent of being deliberate about null semantics on a
    unique column.
  * `webhook_subscriptions` gets one new nullable column, `customer_id` FK
    -> `api_customers` (Codex R9: ownership attaches to the account, not to
    an API key). It is deliberately left UNMAPPED on the `WebhookSubscription`
    ORM model -- see that model's own docstring for why: this repo's live
    DB is migrated by the operator, not by this branch, so a column mapped
    into every INSERT/UPDATE SQLAlchemy generates for that entity would
    break EVERY webhook subscription creation the moment this merged, same
    footgun `challenge_attempted_at` (migration 0015) already documents.

No backfill: every new table starts empty (no existing customers/keys), and
the new `webhook_subscriptions` column is nullable with no default.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-21
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- api_customers ----------------------------------------------------
    op.create_table(
        "api_customers",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("stripe_customer_id", sa.Text(), nullable=True),
        # Manual founder overrides (support credits) -- Codex R9, folded onto
        # the customer row so the auth hot path is one query, not a join.
        sa.Column("extra_requests_per_day", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extra_heavy_per_day", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("override_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspension_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Upsert key is lower(email) (A1) -- the unique index enforces it.
        sa.CheckConstraint("email = lower(email)", name="ck_api_customers_email_lowercase"),
    )
    op.create_index("uq_api_customers_email", "api_customers", ["email"], unique=True)
    op.execute(
        "CREATE UNIQUE INDEX uq_api_customers_stripe_customer_id ON api_customers "
        "(stripe_customer_id) WHERE stripe_customer_id IS NOT NULL"
    )

    # ---- api_keys -----------------------------------------------------------
    op.create_table(
        "api_keys",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("api_customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False, server_default="default"),
        sa.Column("key_prefix", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("plan", sa.Text(), nullable=False, server_default="developer"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("rotated_from", UUID(as_uuid=True), nullable=True),
        sa.Column("revoke_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        # Reveal-once machinery (B1). `reveal_ciphertext` holds the Fernet-
        # encrypted plaintext key from mint until the customer reveals it (or
        # the 24h window lapses); nulled either way. The plaintext itself is
        # NEVER stored unencrypted, logged, or emailed.
        sa.Column("reveal_ciphertext", sa.Text(), nullable=True),
        sa.Column("reveal_token_hash", sa.Text(), nullable=True),
        sa.Column("reveal_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("environment in ('live','test')", name="ck_api_keys_environment"),
        sa.CheckConstraint(
            "plan in ('developer','builder','scale','enterprise')", name="ck_api_keys_plan"
        ),
        sa.CheckConstraint(
            "status in ('active','rotating','revoked')", name="ck_api_keys_status"
        ),
    )
    op.create_index("uq_api_keys_key_prefix", "api_keys", ["key_prefix"], unique=True)
    op.create_index("uq_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)
    op.create_index("ix_api_keys_customer_id", "api_keys", ["customer_id"])
    op.create_index("ix_api_keys_status_plan", "api_keys", ["status", "plan"])

    # ---- api_subscriptions ---------------------------------------------------
    op.create_table(
        "api_subscriptions",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("api_customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stripe_subscription_id", sa.Text(), nullable=True),
        sa.Column("plan", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        # A3: dunning anchor -- set on first invoice.payment_failed, cleared
        # on invoice.paid. 402 fires once now() - past_due_since > 7 days.
        sa.Column("past_due_since", sa.DateTime(timezone=True), nullable=True),
        # A4: out-of-order Stripe event guard -- a handler applies an event
        # only if event.created >= this column.
        sa.Column("last_event_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "plan in ('builder','scale','enterprise')", name="ck_api_subscriptions_plan"
        ),
    )
    op.create_index("ix_api_subscriptions_customer_status", "api_subscriptions", ["customer_id", "status"])
    op.execute(
        "CREATE UNIQUE INDEX uq_api_subscriptions_stripe_subscription_id ON api_subscriptions "
        "(stripe_subscription_id) WHERE stripe_subscription_id IS NOT NULL"
    )
    # B4: at most one non-canceled subscription per customer.
    op.execute(
        "CREATE UNIQUE INDEX uq_api_subscriptions_one_active_per_customer ON api_subscriptions "
        "(customer_id) WHERE status NOT IN ('canceled')"
    )

    # ---- api_key_usage --------------------------------------------------------
    op.create_table(
        "api_key_usage",
        sa.Column(
            "key_id", UUID(as_uuid=True), sa.ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("heavy_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mcp_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("key_id", "usage_date", name="pk_api_key_usage"),
    )
    op.create_index("ix_api_key_usage_usage_date", "api_key_usage", ["usage_date"])

    # ---- api_key_usage_subnets (A6) --------------------------------------------
    op.create_table(
        "api_key_usage_subnets",
        sa.Column(
            "key_id", UUID(as_uuid=True), sa.ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("subnet", sa.Text(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("key_id", "usage_date", "subnet", name="pk_api_key_usage_subnets"),
    )
    op.create_index("ix_api_key_usage_subnets_usage_date", "api_key_usage_subnets", ["usage_date"])

    # ---- api_customer_usage (round-2 amendment C1) -------------------------------
    # Quota is enforced per CUSTOMER, not per key (a customer with 2 active
    # keys shares ONE daily budget across both) -- `api_key_usage` above
    # stays as a per-key REPORTING breakdown only (admin usage endpoint),
    # written in the same transaction as this table's row.
    op.create_table(
        "api_customer_usage",
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("api_customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("heavy_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("customer_id", "usage_date", name="pk_api_customer_usage"),
    )
    op.create_index("ix_api_customer_usage_usage_date", "api_customer_usage", ["usage_date"])

    # ---- stripe_events ----------------------------------------------------------
    op.create_table(
        "stripe_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        # Round-3 amendment D5: two more outcomes beyond the original pair --
        # `duplicate_subscription_canceled` (Phase 2's checkout handler
        # cancels a SECOND subscription a customer somehow created, per B4's
        # one-subscription-per-customer invariant) and `permanent_error` (a
        # handler that fails in a way retrying will never fix, so it's
        # recorded rather than left forever `processed_at IS NULL` and
        # retried by Stripe until the 3-day webhook retry window lapses).
        # `last_error` carries the error CLASS for that outcome only --
        # never a raw exception message (same "class only, never the
        # message" convention as `webhook_subscriptions.last_error` /
        # `ToolInvocation.error_code` elsewhere in this schema).
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "outcome is null or outcome in "
            "('processed','skipped_foreign_app','duplicate_subscription_canceled','permanent_error')",
            name="ck_stripe_events_outcome",
        ),
    )
    op.create_index("ix_stripe_events_processed_at", "stripe_events", ["processed_at"])

    # ---- account_login_tokens ----------------------------------------------------
    op.create_table(
        "account_login_tokens",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("stripe_session_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_ip", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "purpose in ('login','reveal')", name="ck_account_login_tokens_purpose"
        ),
    )
    op.create_index("ix_account_login_tokens_expires_at", "account_login_tokens", ["expires_at"])
    op.create_index("ix_account_login_tokens_email", "account_login_tokens", ["email"])

    # ---- snapshot_artifacts (Phase 3, unused until then) --------------------------
    op.create_table(
        "snapshot_artifacts",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("jurisdiction", sa.Text(), nullable=True),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("row_counts", JSONB(), nullable=False),
        sa.Column("format", sa.Text(), nullable=False, server_default="parquet"),
        sa.CheckConstraint("scope in ('full','jurisdiction')", name="ck_snapshot_artifacts_scope"),
    )
    op.create_index("uq_snapshot_artifacts_object_key", "snapshot_artifacts", ["object_key"], unique=True)
    op.create_index(
        "ix_snapshot_artifacts_scope_jurisdiction_built_at",
        "snapshot_artifacts",
        ["scope", "jurisdiction", sa.text("built_at DESC")],
    )

    # ---- snapshot_entitlements (Phase 2/3, unused until then) ----------------------
    op.create_table(
        "snapshot_entitlements",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("api_customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("jurisdiction", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stripe_checkout_session_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # B5 (supersedes A7's originally-proposed `fulfilled_at` name): set
        # by the manual runbook once the 7-day R2 link is emailed. Refund
        # revocation rule = revoke iff delivered_at IS NULL.
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        # A7: charge.refunded carries a charge, not a checkout session --
        # refund-time lookup goes charge -> payment_intent -> this column.
        sa.Column("stripe_payment_intent_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind in ('subscription','one_time')", name="ck_snapshot_entitlements_kind"
        ),
    )
    op.create_index("ix_snapshot_entitlements_customer_id", "snapshot_entitlements", ["customer_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_snapshot_entitlements_checkout_session ON snapshot_entitlements "
        "(stripe_checkout_session_id) WHERE stripe_checkout_session_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_snapshot_entitlements_payment_intent ON snapshot_entitlements "
        "(stripe_payment_intent_id) WHERE stripe_payment_intent_id IS NOT NULL"
    )

    # ---- snapshot_downloads (Phase 3, unused until then) --------------------------
    op.create_table(
        "snapshot_downloads",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("api_customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            UUID(as_uuid=True),
            sa.ForeignKey("snapshot_artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("bytes", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_snapshot_downloads_customer_requested_at", "snapshot_downloads", ["customer_id", "requested_at"]
    )

    # ---- webhook_subscriptions.customer_id (Codex R9) ------------------------------
    op.add_column(
        "webhook_subscriptions",
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("api_customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_webhook_subscriptions_customer_id", "webhook_subscriptions", ["customer_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_subscriptions_customer_id", table_name="webhook_subscriptions")
    op.drop_column("webhook_subscriptions", "customer_id")

    op.drop_index("ix_snapshot_downloads_customer_requested_at", table_name="snapshot_downloads")
    op.drop_table("snapshot_downloads")

    op.execute("DROP INDEX IF EXISTS uq_snapshot_entitlements_payment_intent")
    op.execute("DROP INDEX IF EXISTS uq_snapshot_entitlements_checkout_session")
    op.drop_index("ix_snapshot_entitlements_customer_id", table_name="snapshot_entitlements")
    op.drop_table("snapshot_entitlements")

    op.drop_index(
        "ix_snapshot_artifacts_scope_jurisdiction_built_at", table_name="snapshot_artifacts"
    )
    op.drop_index("uq_snapshot_artifacts_object_key", table_name="snapshot_artifacts")
    op.drop_table("snapshot_artifacts")

    op.drop_index("ix_account_login_tokens_email", table_name="account_login_tokens")
    op.drop_index("ix_account_login_tokens_expires_at", table_name="account_login_tokens")
    op.drop_table("account_login_tokens")

    op.drop_index("ix_stripe_events_processed_at", table_name="stripe_events")
    op.drop_table("stripe_events")

    op.drop_index("ix_api_key_usage_subnets_usage_date", table_name="api_key_usage_subnets")
    op.drop_table("api_key_usage_subnets")

    op.drop_index("ix_api_key_usage_usage_date", table_name="api_key_usage")
    op.drop_table("api_key_usage")

    op.drop_index("ix_api_customer_usage_usage_date", table_name="api_customer_usage")
    op.drop_table("api_customer_usage")

    op.execute("DROP INDEX IF EXISTS uq_api_subscriptions_one_active_per_customer")
    op.execute("DROP INDEX IF EXISTS uq_api_subscriptions_stripe_subscription_id")
    op.drop_index("ix_api_subscriptions_customer_status", table_name="api_subscriptions")
    op.drop_table("api_subscriptions")

    op.drop_index("ix_api_keys_status_plan", table_name="api_keys")
    op.drop_index("ix_api_keys_customer_id", table_name="api_keys")
    op.drop_index("uq_api_keys_key_hash", table_name="api_keys")
    op.drop_index("uq_api_keys_key_prefix", table_name="api_keys")
    op.drop_table("api_keys")

    op.execute("DROP INDEX IF EXISTS uq_api_customers_stripe_customer_id")
    op.drop_index("uq_api_customers_email", table_name="api_customers")
    op.drop_table("api_customers")
