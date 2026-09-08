"""Bounded, evidence-gated TLS full-text repair control records.

Revision ID: 0030
Revises: 0029

A repair row is not a general retry queue: it records one approved historical
missing-intermediate recovery path and an append-only outcome per outbound
attempt. The source error is represented only by a SHA-256 fingerprint; raw
exception text is not copied into a new operational table.
"""
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE tls_fulltext_repairs ("
        "document_id UUID NOT NULL, "
        "reason TEXT NOT NULL, "
        "remediation_version TEXT NOT NULL, "
        "source_dead_job_id UUID NOT NULL, "
        "source_error_sha256 TEXT NOT NULL, "
        "status TEXT DEFAULT 'planned' NOT NULL, "
        "attempts INTEGER DEFAULT 0 NOT NULL, "
        "max_attempts INTEGER DEFAULT 2 NOT NULL, "
        "next_attempt_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "expires_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "completed_at TIMESTAMP WITH TIME ZONE, "
        "last_outcome TEXT, "
        "reservation_token UUID, "
        "reserved_at TIMESTAMP WITH TIME ZONE, "
        "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, "
        "id UUID DEFAULT gen_random_uuid() NOT NULL, "
        "PRIMARY KEY (id), "
        "CONSTRAINT uq_tls_repair_document_reason_version UNIQUE (document_id, reason, remediation_version), "
        "CONSTRAINT ck_tls_repair_reason CHECK (reason IN ('missing_tls_intermediate','tx_ftp_witness_url')), "
        "CONSTRAINT ck_tls_repair_status CHECK (status IN ('planned','reserved','succeeded','exhausted','expired','skipped')), "
        "CONSTRAINT ck_tls_repair_attempts CHECK (attempts BETWEEN 0 AND max_attempts), "
        "CONSTRAINT ck_tls_repair_max_attempts CHECK (max_attempts BETWEEN 1 AND 2), "
        "CONSTRAINT ck_tls_repair_error_hash CHECK (source_error_sha256 ~ '^[0-9a-f]{64}$'), "
        "FOREIGN KEY(document_id) REFERENCES bill_documents (id) ON DELETE CASCADE, "
        "FOREIGN KEY(source_dead_job_id) REFERENCES ingest_jobs (id)"
        ")"
    )
    op.execute("CREATE INDEX ix_tls_repair_due ON tls_fulltext_repairs (next_attempt_at) WHERE status = 'planned'")
    op.execute(
        "CREATE TABLE tls_fulltext_repair_attempts ("
        "repair_id UUID NOT NULL, "
        "attempt_number INTEGER NOT NULL, "
        "outcome TEXT NOT NULL, "
        "document_status_before TEXT, "
        "document_status_after TEXT, "
        "started_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "finished_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "id UUID DEFAULT gen_random_uuid() NOT NULL, "
        "PRIMARY KEY (id), "
        "CONSTRAINT uq_tls_repair_attempt_event UNIQUE (repair_id, attempt_number, outcome), "
        "CONSTRAINT ck_tls_repair_attempt_number CHECK (attempt_number >= 1), "
        "CONSTRAINT ck_tls_repair_attempt_outcome CHECK (outcome IN ('admitted','succeeded','document_fetch_error','unfetchable','terminal_no_text','ineligible','unexpected')), "
        "FOREIGN KEY(repair_id) REFERENCES tls_fulltext_repairs (id) ON DELETE CASCADE"
        ")"
    )
    op.execute("CREATE INDEX ix_tls_repair_attempt_repair_time ON tls_fulltext_repair_attempts (repair_id, started_at)")


def downgrade() -> None:
    op.drop_table("tls_fulltext_repair_attempts")
    op.drop_table("tls_fulltext_repairs")
