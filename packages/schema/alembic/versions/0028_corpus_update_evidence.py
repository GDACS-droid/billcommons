"""Replayable source-response mutation evidence.

Revision ID: 0028
Revises: 0027

The raw blob table is corpus-owned content-addressed storage.  Each ledger
row names its source explicitly, so an OpenStates aggregator response never
appears to be an official source and future official document fetches have a
separate label.
"""
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TABLE corpus_update_evidence (\n\tbill_id UUID NOT NULL, \n\toriginal_bill_upstream_id TEXT, \n\tsource_name TEXT NOT NULL, \n\tsource_url TEXT NOT NULL, \n\trequest_scope JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tresponse_sha256 TEXT NOT NULL, \n\tbefore_snapshot_sha256 TEXT, \n\tafter_snapshot_sha256 TEXT NOT NULL, \n\tprocessing_version TEXT NOT NULL, \n\tmutation_kind TEXT NOT NULL, \n\tchanged_components JSONB DEFAULT '[]'::jsonb NOT NULL, \n\tretrieved_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_corpus_update_evidence_source CHECK (source_name IN ('openstates_v3_api','official_document_fetch')), \n\tCONSTRAINT ck_corpus_update_evidence_kind CHECK (mutation_kind IN ('created','updated')), \n\tCONSTRAINT ck_corpus_update_evidence_before CHECK ((mutation_kind = 'created') = (before_snapshot_sha256 IS NULL)), \n\tCONSTRAINT ck_corpus_update_evidence_scope_size CHECK (octet_length(request_scope::text) <= 65536), \n\tCONSTRAINT ck_corpus_update_evidence_components_size CHECK (octet_length(changed_components::text) <= 65536), \n\tFOREIGN KEY(bill_id) REFERENCES bills (id), \n\tFOREIGN KEY(response_sha256) REFERENCES official_raw_blobs (sha256), \n\tFOREIGN KEY(before_snapshot_sha256) REFERENCES official_raw_blobs (sha256), \n\tFOREIGN KEY(after_snapshot_sha256) REFERENCES official_raw_blobs (sha256)\n)")
    op.execute("CREATE INDEX ix_corpus_update_evidence_bill_time ON corpus_update_evidence (bill_id, retrieved_at)")
    op.execute("CREATE INDEX ix_corpus_update_evidence_response ON corpus_update_evidence (response_sha256)")


def downgrade() -> None:
    op.drop_table("corpus_update_evidence")
