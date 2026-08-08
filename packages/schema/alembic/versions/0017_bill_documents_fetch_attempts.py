"""bill_documents: add fetch_attempts counter for bounded retry

Bug: fetch_text jobs for a permanently-broken URL (host down for good, DNS
dead, etc.) retried forever -- observed as high as 220 attempts on a single
document -- because STATUS_FETCH_ERROR is deliberately excluded from
TERMINAL_STATUSES (a transient network error today doesn't mean the same
error tomorrow). This column gives the retry a hard ceiling
(fulltext.MAX_FETCH_ATTEMPTS) without reclassifying fetch_error as terminal.

A column, not a `COUNT(*)` over dead `ingest_jobs` rows: dead ingest_jobs
rows are slated for cleanup (84k of them); a counter derived from them
resets the poison loop the moment anyone purges. It also keeps the
perf-critical enqueue query (see fulltext.enqueue_fulltext_jobs) off
ingest_jobs.payload JSONB entirely.

No index: the predicate `fetch_attempts < MAX_FETCH_ATTEMPTS` is
low-selectivity (nearly every row is under the cap), so a btree index would
not be selective enough to be worth its maintenance cost.

NOT NULL + constant server_default is metadata-only on PG11+ (no table
rewrite), so this is safe against the live bill_documents table without an
autocommit/CONCURRENTLY dance.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "bill_documents",
        sa.Column("fetch_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("bill_documents", "fetch_attempts")
