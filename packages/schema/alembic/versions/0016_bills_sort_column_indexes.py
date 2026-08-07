"""bills: btree indexes on (latest_action_date, id) and (introduced_date, id)

Follow-up to the /search MATCH_CAP rewrite (514f85a + d4259f1): the capped
full-text candidate CTEs now order by the REQUESTED sort key, not always by
id, when a saturated query is sorted "latest_action" or "introduced" (see
billcommons_api.search._capped_probe_order) -- so the MATCH_CAP-sized sample
is the true global top-MATCH_CAP under that sort instead of an arbitrary
id-ordered slice re-sorted afterward. That ORDER BY needs a supporting index
to stay cheap; without one, `ORDER BY latest_action_date DESC NULLS LAST, id
LIMIT :match_cap_plus_one` forces a full sequential-scan-and-sort of every
row matching the WHERE clause before the LIMIT can apply -- the exact cost
class MATCH_CAP exists to avoid, just moved from ts_rank to a sort now.

DESC NULLS LAST explicitly (not the btree default of DESC NULLS FIRST) to
match `_order_by`'s/`_capped_probe_order`'s ORDER BY clauses exactly -- an
index built with the wrong null ordering can't be used to satisfy that
ORDER BY without an extra sort step, defeating the point.

Composite with `id` as the tiebreaker (same convention as
0004_bills_updated_at_index): a lone `latest_action_date`/`introduced_date`
index would still leave same-day rows (common; NULL in particular can be
the whole non-trivial tail) to be sorted separately.

This migration does NOT add a supporting index for the bill_documents branch
of the same capped query (docs_matches_probe/docs_matches in search.py):
that branch deliberately stays id-ordered rather than sort-key-ordered --
see the "Deliberately still ORDER BY bill_id" comment in full_text_search --
because sorting it would need the matched-document set ordered by a column
on a DIFFERENT, only-joined table (bills), and no single btree/GIN index can
serve (bill_documents full-text match) x (bills.latest_action_date/
introduced_date) together the way this migration's two indexes serve the
bills-only branch. Flagged as a known limitation, not fixed here.

CONCURRENTLY, with an autocommit block -- same rationale as
0010_sponsor_name_trgm: `bills` is a live, read-heavy table and a plain
CREATE INDEX takes an ACCESS EXCLUSIVE lock for the build's duration. This
migration therefore cannot run inside a transaction; a failure leaves an
INVALID index behind in pg_index rather than rolling back cleanly.

Self-healing against exactly that leftover, rather than requiring a manual
drop-and-rerun: `CREATE INDEX CONCURRENTLY IF NOT EXISTS` only checks
whether the NAME exists, not whether the existing entry is valid -- a retry
after a failed concurrent build would see the name, skip creation, and
Alembic would stamp 0016 applied with an unusable index in place. The
planner then silently falls back to a full sort for the affected query,
the exact regression this migration exists to prevent, with no error
anywhere to flag it. `_drop_if_invalid` checks pg_index for an
existing-but-INVALID index of each name and drops it (also CONCURRENTLY,
also outside a transaction) immediately before the corresponding CREATE, so
a retry always either finds a genuinely valid index already in place or
builds a fresh one -- never silently keeps a broken one.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-07
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LATEST_ACTION_INDEX = "ix_bills_latest_action_date_id"
INTRODUCED_INDEX = "ix_bills_introduced_date_id"


def _drop_if_invalid(index_name: str) -> None:
    """Drop `index_name` if it exists in pg_index but is marked INVALID (a
    leftover from a CREATE INDEX CONCURRENTLY that failed partway through) --
    see the module docstring's self-healing paragraph for why this can't be
    skipped in favor of just `IF NOT EXISTS` on the CREATE itself."""
    conn = op.get_bind()
    invalid = conn.exec_driver_sql(
        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = %s AND NOT i.indisvalid",
        (index_name,),
    ).scalar()
    if invalid:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        _drop_if_invalid(LATEST_ACTION_INDEX)
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {LATEST_ACTION_INDEX} "
            "ON bills (latest_action_date DESC NULLS LAST, id)"
        )
        _drop_if_invalid(INTRODUCED_INDEX)
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INTRODUCED_INDEX} "
            "ON bills (introduced_date DESC NULLS LAST, id)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {LATEST_ACTION_INDEX}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INTRODUCED_INDEX}")
