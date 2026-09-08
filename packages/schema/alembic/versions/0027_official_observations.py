"""Corpus-owned official observations and replayable reconciliation evidence.

Revision ID: 0027
Revises: 0026

DDL is pinned here deliberately; future ORM changes cannot alter this migration.
"""
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TABLE official_raw_blobs (\n\tsha256 TEXT NOT NULL, \n\tdata BYTEA NOT NULL, \n\tcontent_type TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (sha256), \n\tCONSTRAINT ck_official_blob_hash CHECK (sha256 ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_official_blob_size CHECK (length(data) BETWEEN 0 AND 8388608)\n)")
    op.execute("CREATE TABLE official_source_targets (\n\tjurisdiction_id UUID NOT NULL, \n\tadapter_name TEXT NOT NULL, \n\tsource_url TEXT NOT NULL, \n\tscope JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tenabled BOOLEAN DEFAULT false NOT NULL, \n\tcadence_seconds INTEGER DEFAULT 3600 NOT NULL, \n\tnext_check_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tconsecutive_failures INTEGER DEFAULT 0 NOT NULL, \n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_official_target_adapter_url UNIQUE (adapter_name, source_url), \n\tCONSTRAINT ck_official_target_cadence CHECK (cadence_seconds BETWEEN 300 AND 604800), \n\tCONSTRAINT ck_official_target_failures CHECK (consecutive_failures >= 0), \n\tCONSTRAINT ck_official_target_scope_size CHECK (octet_length(scope::text) <= 65536), \n\tFOREIGN KEY(jurisdiction_id) REFERENCES jurisdictions (id)\n)")
    op.execute('CREATE INDEX ix_official_targets_due ON official_source_targets (next_check_at) WHERE enabled')
    op.execute("CREATE TABLE official_source_observations (\n\ttarget_id UUID NOT NULL, \n\tadapter_name TEXT NOT NULL, \n\tadapter_version TEXT NOT NULL, \n\tsource_url TEXT NOT NULL, \n\tscope JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tretrieved_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupstream_updated_at TIMESTAMP WITH TIME ZONE, \n\thttp_status INTEGER, \n\traw_sha256 TEXT, \n\tstatus TEXT NOT NULL, \n\terror_class TEXT, \n\trecord_count INTEGER, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_official_observation_status CHECK (status IN ('succeeded','failed','invalid')), \n\tCONSTRAINT ck_official_observation_success CHECK (status <> 'succeeded' OR (raw_sha256 IS NOT NULL AND record_count IS NOT NULL AND error_class IS NULL)), \n\tCONSTRAINT ck_official_observation_count CHECK (record_count IS NULL OR record_count >= 0), \n\tCONSTRAINT ck_official_observation_http CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599), \n\tCONSTRAINT ck_official_observation_scope_size CHECK (octet_length(scope::text) <= 65536), \n\tFOREIGN KEY(target_id) REFERENCES official_source_targets (id), \n\tFOREIGN KEY(raw_sha256) REFERENCES official_raw_blobs (sha256)\n)")
    op.execute('CREATE INDEX ix_official_observations_target_time ON official_source_observations (target_id, retrieved_at)')
    op.execute("CREATE TABLE official_reconciliation_runs (\n\tobservation_id UUID NOT NULL, \n\tbill_id UUID, \n\tofficial_bill_id TEXT NOT NULL, \n\tlocal_snapshot_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcomparator_version TEXT NOT NULL, \n\tstatus TEXT NOT NULL, \n\tsummary JSONB NOT NULL, \n\tlocal_snapshot_sha256 TEXT, \n\tdiff_sha256 TEXT, \n\terror_class TEXT, \n\tcompleted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_official_run_status CHECK (status IN ('completed','partial','failed')), \n\tCONSTRAINT ck_official_run_complete CHECK (status <> 'completed' OR (local_snapshot_sha256 IS NOT NULL AND diff_sha256 IS NOT NULL AND error_class IS NULL)), \n\tCONSTRAINT ck_official_run_summary_size CHECK (octet_length(summary::text) <= 65536), \n\tCONSTRAINT uq_official_run_replay UNIQUE (observation_id, official_bill_id, local_snapshot_sha256, comparator_version), \n\tFOREIGN KEY(observation_id) REFERENCES official_source_observations (id), \n\tFOREIGN KEY(bill_id) REFERENCES bills (id), \n\tFOREIGN KEY(local_snapshot_sha256) REFERENCES official_raw_blobs (sha256), \n\tFOREIGN KEY(diff_sha256) REFERENCES official_raw_blobs (sha256)\n)")
    op.execute('CREATE INDEX ix_official_runs_bill_time ON official_reconciliation_runs (bill_id, completed_at)')
    op.execute('CREATE INDEX ix_official_runs_observation ON official_reconciliation_runs (observation_id)')


def downgrade() -> None:
    op.drop_table('official_reconciliation_runs')
    op.drop_table('official_source_observations')
    op.drop_table('official_source_targets')
    op.drop_table('official_raw_blobs')
