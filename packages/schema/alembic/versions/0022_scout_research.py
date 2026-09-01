"""Add the isolated, owner-scoped Scout research queue and provenance tables.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scout_research_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("customer_id", UUID(as_uuid=True), sa.ForeignKey("api_customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_query", sa.Text(), nullable=False),
        sa.Column("normalized_query", sa.Text(), nullable=False),
        sa.Column("jurisdiction", sa.Text(), nullable=False),
        sa.Column("cache_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("strategy", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("claim_owner", sa.Text(), nullable=True),
        sa.Column("claim_token", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("limits", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("usage", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("partial_success", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fresh_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status in ('queued','running','completed','partial','failed','canceled')", name="ck_scout_research_jobs_status"),
    )
    op.create_index("ix_scout_research_jobs_claim", "scout_research_jobs", ["status", "lease_expires_at", "created_at"])
    op.create_index("ix_scout_research_jobs_customer_created", "scout_research_jobs", ["customer_id", "created_at"])
    op.execute("CREATE UNIQUE INDEX uq_scout_research_jobs_active_cache ON scout_research_jobs (customer_id, cache_key) WHERE status IN ('queued', 'running')")

    op.create_table(
        "scout_job_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("scout_research_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_scout_job_events_job_created", "scout_job_events", ["job_id", "created_at"])

    op.create_table(
        "scout_sources",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("scout_research_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("official", sa.Boolean(), nullable=False),
        sa.Column("retrieval_mechanism", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("document_hash", sa.Text(), nullable=True),
        sa.Column("raw_ref", sa.Text(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("upstream_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prior_source_id", UUID(as_uuid=True), sa.ForeignKey("scout_sources.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_scout_sources_job", "scout_sources", ["job_id"])
    op.create_index("ix_scout_sources_url_hash", "scout_sources", ["canonical_url", "content_hash"])

    op.create_table(
        "scout_findings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("scout_research_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("scout_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("what_happened", sa.Text(), nullable=False),
        sa.Column("why_it_matters", sa.Text(), nullable=True),
        sa.Column("relevant_date", sa.Date(), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("excerpt_hash", sa.Text(), nullable=True),
        sa.Column("excerpt_start", sa.Integer(), nullable=True),
        sa.Column("excerpt_end", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Text(), nullable=False, server_default=sa.text("'low'")),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("bill_id", UUID(as_uuid=True), sa.ForeignKey("bills.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_scout_findings_job", "scout_findings", ["job_id"])

    op.create_table(
        "scout_browser_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("scout_research_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("scout_sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_session_id", sa.Text(), nullable=True),
        sa.Column("replay_url", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'starting'")),
        sa.Column("pages", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("actions", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("runtime_ms", sa.Integer(), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status in ('starting','running','released','cleanup_failed')", name="ck_scout_browser_sessions_status"),
    )
    op.create_index("ix_scout_browser_sessions_live", "scout_browser_sessions", ["status", "created_at"])
    op.create_index("ix_scout_browser_sessions_job", "scout_browser_sessions", ["job_id"])


def downgrade() -> None:
    op.drop_table("scout_browser_sessions")
    op.drop_table("scout_findings")
    op.drop_table("scout_sources")
    op.drop_table("scout_job_events")
    op.drop_table("scout_research_jobs")
