"""jurisdiction_coverage: fix NULL-session-id duplicate coverage rows

Finding 3 (coverage duplicate rows): Postgres treats NULL as distinct from
itself in a UNIQUE constraint, so the existing
uq_jurisdiction_coverage_jurisdiction_session UNIQUE(jurisdiction_id,
session_id) allows unlimited duplicate (jurisdiction_id, NULL) rows --
corrupting the jurisdiction-level (no-session) coverage matrix.

This migration:
1. Dedupes any existing (jurisdiction_id, NULL session_id) duplicate rows,
   keeping the row with the latest updated_at per jurisdiction_id (ties
   broken by id, deterministic).
2. Adds a partial unique index enforcing at most one (jurisdiction_id, NULL)
   row going forward, while leaving the original composite UNIQUE constraint
   in place to keep enforcing uniqueness for non-null session_id rows.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-24
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Dedupe existing (jurisdiction_id, NULL) rows, keeping the newest
    #    (by updated_at, ties broken by id) per jurisdiction_id.
    op.execute(
        """
        DELETE FROM jurisdiction_coverage jc
        USING (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY jurisdiction_id
                       ORDER BY updated_at DESC, id DESC
                   ) AS rn
            FROM jurisdiction_coverage
            WHERE session_id IS NULL
        ) ranked
        WHERE jc.id = ranked.id
          AND ranked.rn > 1
        """
    )

    # 2. Enforce it going forward: a partial unique index on
    #    (jurisdiction_id) WHERE session_id IS NULL. The pre-existing
    #    composite UNIQUE(jurisdiction_id, session_id) constraint stays in
    #    place unchanged for non-null session_id rows.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_jurisdiction_coverage_jurisdiction_null_session
        ON jurisdiction_coverage (jurisdiction_id)
        WHERE session_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_jurisdiction_coverage_jurisdiction_null_session")
