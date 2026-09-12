"""Durable API-sync evidence-snapshot overflow blockers.

Revision ID: 0031_snapshot_blockers
Revises: 0030

The pending saved-monitor branch also has an undeployed 0031 revision.  That
branch must be rebased or given a merge revision before both migrations are
deployed together; this migration intentionally does not modify that branch.
"""
from alembic import op

revision = "0031_snapshot_blockers"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE api_sync_snapshot_blockers ("
        "jurisdiction_id UUID NOT NULL, "
        "bill_id UUID, "
        "source_name TEXT NOT NULL, "
        "source_identity_sha256 TEXT NOT NULL, "
        "component TEXT NOT NULL, "
        "record_cap INTEGER NOT NULL, "
        "first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "active BOOLEAN DEFAULT true NOT NULL, "
        "resolved_at TIMESTAMP WITH TIME ZONE, "
        "processing_version TEXT NOT NULL, "
        "cycle_started_at TIMESTAMP WITH TIME ZONE, "
        "cycle_start_page INTEGER NOT NULL, "
        "updated_since_sha256 TEXT, "
        "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "id UUID DEFAULT gen_random_uuid() NOT NULL, "
        "PRIMARY KEY (id), "
        "CONSTRAINT uq_api_sync_snapshot_blocker_identity UNIQUE (jurisdiction_id, source_name, source_identity_sha256), "
        "CONSTRAINT ck_api_sync_snapshot_blocker_cap CHECK (record_cap >= 1), "
        "CONSTRAINT ck_api_sync_snapshot_blocker_page CHECK (cycle_start_page >= 1), "
        "CONSTRAINT ck_api_sync_snapshot_blocker_component CHECK (component IN ('actions', 'sponsorships', 'versions', 'documents')), "
        "CONSTRAINT ck_api_sync_snapshot_blocker_resolution CHECK ((active AND resolved_at IS NULL) OR (NOT active AND resolved_at IS NOT NULL)), "
        "CONSTRAINT ck_api_sync_snapshot_blocker_identity_hash CHECK (source_identity_sha256 ~ '^[0-9a-f]{64}$'), "
        "CONSTRAINT ck_api_sync_snapshot_blocker_window_hash CHECK (updated_since_sha256 IS NULL OR updated_since_sha256 ~ '^[0-9a-f]{64}$'), "
        "FOREIGN KEY(jurisdiction_id) REFERENCES jurisdictions (id), "
        "FOREIGN KEY(bill_id) REFERENCES bills (id) ON DELETE SET NULL"
        ")"
    )
    op.execute(
        "CREATE INDEX ix_api_sync_snapshot_blocker_active "
        "ON api_sync_snapshot_blockers (jurisdiction_id, source_name) WHERE active"
    )


def downgrade() -> None:
    op.drop_table("api_sync_snapshot_blockers")
