"""Replayable local evidence for derived bill status and substitution relations.

Revision ID: 0029
Revises: 0028
"""
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE derived_status_evidence (
            bill_id UUID NOT NULL,
            causal_corpus_update_evidence_id UUID,
            derivation_input_sha256 TEXT NOT NULL,
            before_snapshot_sha256 TEXT NOT NULL,
            after_snapshot_sha256 TEXT NOT NULL,
            processing_version TEXT NOT NULL,
            changed_components JSONB DEFAULT '[]'::jsonb NOT NULL,
            derived_at TIMESTAMP WITH TIME ZONE NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            id UUID DEFAULT gen_random_uuid() NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT ck_derived_status_evidence_components CHECK (
                jsonb_typeof(changed_components) = 'array'
                AND jsonb_array_length(changed_components) > 0
            ),
            CONSTRAINT ck_derived_status_evidence_components_size CHECK (
                octet_length(changed_components::text) <= 65536
            ),
            FOREIGN KEY(bill_id) REFERENCES bills (id),
            FOREIGN KEY(causal_corpus_update_evidence_id) REFERENCES corpus_update_evidence (id),
            FOREIGN KEY(derivation_input_sha256) REFERENCES official_raw_blobs (sha256),
            FOREIGN KEY(before_snapshot_sha256) REFERENCES official_raw_blobs (sha256),
            FOREIGN KEY(after_snapshot_sha256) REFERENCES official_raw_blobs (sha256)
        )
    """)
    op.execute("CREATE INDEX ix_derived_status_evidence_bill_time ON derived_status_evidence (bill_id, derived_at)")
    op.execute("CREATE INDEX ix_derived_status_evidence_causal ON derived_status_evidence (causal_corpus_update_evidence_id)")


def downgrade() -> None:
    op.drop_table("derived_status_evidence")
