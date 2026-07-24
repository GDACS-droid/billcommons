"""initial schema

Creates the pg_trgm, unaccent, and pgcrypto extensions, then all 23 tables of
the Bill Commons canonical data model, plus generated tsvector search columns
(bills.search_tsv, bill_documents.text_tsv, search_documents.search_tsv) with
their GIN indexes, and trigram (gin_trgm_ops) indexes on bills.identifier_norm
and bills.title.

Revision ID: 0001
Revises:
Create Date: 2026-07-23
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def _provenance_columns() -> list[sa.Column]:
    return [
        sa.Column("source_name", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("upstream_id", sa.Text(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("upstream_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_ref", sa.Text(), nullable=True),
        sa.Column("checksum", sa.Text(), nullable=True),
        sa.Column("parser_version", sa.Text(), nullable=True),
        sa.Column("license_note", sa.Text(), nullable=True),
    ]


def upgrade() -> None:
    # --- extensions --------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # --- jurisdictions / legislative_bodies / sessions ----------------------
    op.create_table(
        "jurisdictions",
        _uuid_pk(),
        *_timestamps(),
        *_provenance_columns(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("abbreviation", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=False),
        sa.Column("openstates_id", sa.Text(), nullable=True),
        sa.UniqueConstraint("abbreviation", name="uq_jurisdictions_abbreviation"),
        sa.UniqueConstraint("openstates_id", name="uq_jurisdictions_openstates_id"),
    )

    op.create_table(
        "legislative_bodies",
        _uuid_pk(),
        *_timestamps(),
        *_provenance_columns(),
        sa.Column("jurisdiction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdictions.id"]),
    )

    op.create_table(
        "sessions",
        _uuid_pk(),
        *_timestamps(),
        *_provenance_columns(),
        sa.Column("jurisdiction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("classification", sa.Text(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdictions.id"]),
        sa.UniqueConstraint(
            "jurisdiction_id", "identifier", name="uq_sessions_jurisdiction_identifier"
        ),
    )

    # --- bills + supporting tables ------------------------------------------
    op.create_table(
        "bills",
        _uuid_pk(),
        *_timestamps(),
        *_provenance_columns(),
        sa.Column("jurisdiction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chamber", sa.Text(), nullable=True),
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column("identifier_norm", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("short_title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("bill_type", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("status_date", sa.Date(), nullable=True),
        sa.Column("introduced_date", sa.Date(), nullable=True),
        sa.Column("latest_action_text", sa.Text(), nullable=True),
        sa.Column("latest_action_date", sa.Date(), nullable=True),
        sa.Column("openstates_id", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdictions.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.UniqueConstraint(
            "session_id", "identifier_norm", name="uq_bills_session_identifier_norm"
        ),
        sa.UniqueConstraint("openstates_id", name="uq_bills_openstates_id"),
    )
    op.create_index("ix_bills_identifier_norm", "bills", ["identifier_norm"])

    # Generated tsvector column for bill search (identifier + title weighted A,
    # description weighted B). Raw DDL because SQLAlchemy's op.create_table
    # cannot express GENERATED ALWAYS AS ... STORED for a table under
    # construction in a fully portable way across ORM Computed() reflection.
    op.execute(
        """
        ALTER TABLE bills
        ADD COLUMN search_tsv tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(identifier, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(description, '')), 'B')
        ) STORED
        """
    )
    op.execute("CREATE INDEX ix_bills_search_tsv ON bills USING GIN (search_tsv)")

    # Trigram indexes for fuzzy match on identifier_norm and title.
    op.execute(
        "CREATE INDEX ix_bills_identifier_norm_trgm ON bills "
        "USING GIN (identifier_norm gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_bills_title_trgm ON bills USING GIN (title gin_trgm_ops)"
    )

    op.create_table(
        "bill_identifiers",
        _uuid_pk(),
        *_timestamps(),
        *_provenance_columns(),
        sa.Column("bill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column("identifier_norm", sa.Text(), nullable=False),
        sa.Column("scheme", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"]),
    )
    op.create_index(
        "ix_bill_identifiers_identifier_norm", "bill_identifiers", ["identifier_norm"]
    )

    op.create_table(
        "bill_versions",
        _uuid_pk(),
        *_timestamps(),
        *_provenance_columns(),
        sa.Column("bill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("date", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"]),
    )

    op.create_table(
        "bill_documents",
        _uuid_pk(),
        *_timestamps(),
        *_provenance_columns(),
        sa.Column("bill_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["bill_version_id"], ["bill_versions.id"]),
    )
    op.execute(
        """
        ALTER TABLE bill_documents
        ADD COLUMN text_tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english', coalesce(extracted_text, ''))
        ) STORED
        """
    )
    op.execute(
        "CREATE INDEX ix_bill_documents_text_tsv ON bill_documents USING GIN (text_tsv)"
    )

    op.create_table(
        "bill_actions",
        _uuid_pk(),
        *_timestamps(),
        *_provenance_columns(),
        sa.Column("bill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("action_date", sa.Date(), nullable=True),
        sa.Column("classification", sa.Text(), nullable=True),
        sa.Column("order", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"]),
        # organizations FK added after organizations table exists (below).
    )

    op.create_table(
        "bill_subjects",
        _uuid_pk(),
        *_timestamps(),
        sa.Column("bill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"]),
        sa.UniqueConstraint("bill_id", "subject", name="uq_bill_subjects_bill_subject"),
    )

    # --- people / organizations / committees / sponsorships -----------------
    op.create_table(
        "people",
        _uuid_pk(),
        *_timestamps(),
        *_provenance_columns(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("party", sa.Text(), nullable=True),
        sa.Column("jurisdiction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("openstates_id", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdictions.id"]),
        sa.UniqueConstraint("openstates_id", name="uq_people_openstates_id"),
    )

    op.create_table(
        "organizations",
        _uuid_pk(),
        *_timestamps(),
        *_provenance_columns(),
        sa.Column("jurisdiction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=True),
        sa.Column("openstates_id", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdictions.id"]),
        sa.UniqueConstraint("openstates_id", name="uq_organizations_openstates_id"),
    )

    # Now that organizations exists, add the deferred FK on bill_actions.
    op.create_foreign_key(
        "fk_bill_actions_organization_id",
        "bill_actions",
        "organizations",
        ["organization_id"],
        ["id"],
    )

    op.create_table(
        "committees",
        _uuid_pk(),
        *_timestamps(),
        *_provenance_columns(),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=True),
        sa.Column("openstates_id", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.UniqueConstraint("openstates_id", name="uq_committees_openstates_id"),
    )

    op.create_table(
        "sponsorships",
        _uuid_pk(),
        *_timestamps(),
        *_provenance_columns(),
        sa.Column("bill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("classification", sa.Text(), nullable=True),
        sa.Column("primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"]),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
    )

    # --- votes ---------------------------------------------------------------
    op.create_table(
        "vote_events",
        _uuid_pk(),
        *_timestamps(),
        *_provenance_columns(),
        sa.Column("bill_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("motion_text", sa.Text(), nullable=True),
        sa.Column("motion_classification", sa.Text(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("yes_count", sa.Integer(), nullable=True),
        sa.Column("no_count", sa.Integer(), nullable=True),
        sa.Column("other_count", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
    )

    op.create_table(
        "vote_records",
        _uuid_pk(),
        *_timestamps(),
        sa.Column("vote_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("voter_name", sa.Text(), nullable=True),
        sa.Column("option", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["vote_event_id"], ["vote_events.id"]),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"]),
    )

    # --- events / relations ----------------------------------------------------
    op.create_table(
        "legislative_events",
        _uuid_pk(),
        *_timestamps(),
        *_provenance_columns(),
        sa.Column("jurisdiction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("bill_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("committee_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("start_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdictions.id"]),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"]),
        sa.ForeignKeyConstraint(["committee_id"], ["committees.id"]),
    )

    op.create_table(
        "related_bills",
        _uuid_pk(),
        *_timestamps(),
        sa.Column("bill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("related_bill_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("related_identifier", sa.Text(), nullable=True),
        sa.Column("relation_type", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"]),
        sa.ForeignKeyConstraint(["related_bill_id"], ["bills.id"]),
    )

    # --- ingestion / validation / coverage / search / queue ---------------------
    op.create_table(
        "source_records",
        _uuid_pk(),
        *_timestamps(),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("upstream_id", sa.Text(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_ref", sa.Text(), nullable=True),
        sa.Column("checksum", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_source_records_entity", "source_records", ["entity_type", "entity_id"]
    )

    op.create_table(
        "ingestion_runs",
        _uuid_pk(),
        *_timestamps(),
        sa.Column("jurisdiction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("bills_created", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("bills_updated", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdictions.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
    )

    op.create_table(
        "validation_runs",
        _uuid_pk(),
        *_timestamps(),
        sa.Column("jurisdiction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pass_rate", sa.Numeric(), nullable=True),
        sa.Column("checks_run", sa.Integer(), nullable=True),
        sa.Column("checks_failed", sa.Integer(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdictions.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
    )

    op.create_table(
        "jurisdiction_coverage",
        _uuid_pk(),
        *_timestamps(),
        sa.Column("jurisdiction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'NOT_STARTED'")),
        sa.Column("bill_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("full_text_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("validation_pass_rate", sa.Numeric(), nullable=True),
        sa.Column("known_gaps", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdictions.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.CheckConstraint(
            "status in ("
            "'NOT_STARTED','SOURCE_IDENTIFIED','BOOTSTRAPPED','METADATA_SEARCHABLE',"
            "'FULL_TEXT_SEARCHABLE','VALIDATING','GREEN','DEGRADED','BLOCKED')",
            name="ck_jurisdiction_coverage_status",
        ),
        sa.UniqueConstraint(
            "jurisdiction_id", "session_id", name="uq_jurisdiction_coverage_jurisdiction_session"
        ),
    )

    op.create_table(
        "search_documents",
        _uuid_pk(),
        *_timestamps(),
        sa.Column("bill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("jurisdiction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("identifier_norm", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("subjects", postgresql.JSONB(), nullable=True),
        sa.Column("sponsors", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("latest_action_date", sa.Date(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"]),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdictions.id"]),
        sa.UniqueConstraint("bill_id", name="uq_search_documents_bill_id"),
    )
    op.create_index(
        "ix_search_documents_identifier_norm", "search_documents", ["identifier_norm"]
    )
    op.execute(
        """
        ALTER TABLE search_documents
        ADD COLUMN search_tsv tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(identifier_norm, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(summary, '')), 'B')
        ) STORED
        """
    )
    op.execute(
        "CREATE INDEX ix_search_documents_search_tsv ON search_documents USING GIN (search_tsv)"
    )

    op.create_table(
        "ingest_jobs",
        _uuid_pk(),
        *_timestamps(),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status in ('queued','running','done','failed','dead')",
            name="ck_ingest_jobs_status",
        ),
    )
    op.create_index("ix_ingest_jobs_status_run_after", "ingest_jobs", ["status", "run_after"])


def downgrade() -> None:
    op.drop_table("ingest_jobs")
    op.drop_table("search_documents")
    op.drop_table("jurisdiction_coverage")
    op.drop_table("validation_runs")
    op.drop_table("ingestion_runs")
    op.drop_table("source_records")
    op.drop_table("related_bills")
    op.drop_table("legislative_events")
    op.drop_table("vote_records")
    op.drop_table("vote_events")
    op.drop_table("sponsorships")
    op.drop_table("committees")
    op.drop_constraint("fk_bill_actions_organization_id", "bill_actions", type_="foreignkey")
    op.drop_table("organizations")
    op.drop_table("people")
    op.drop_table("bill_subjects")
    op.drop_table("bill_actions")
    op.drop_table("bill_documents")
    op.drop_table("bill_versions")
    op.drop_table("bill_identifiers")
    op.drop_table("bills")
    op.drop_table("sessions")
    op.drop_table("legislative_bodies")
    op.drop_table("jurisdictions")
