"""SQLAlchemy 2.0 declarative models for the Bill Commons canonical data model.

See docs/architecture/ARCHITECTURE.md ("Canonical data model") for the locked
spec. This module is the single source of truth for the schema; Alembic
migrations in packages/schema/alembic/versions derive DDL from it (plus raw
DDL for generated tsvector columns, which SQLAlchemy's Computed() emits but
which we manage explicitly in migration 0001 for clarity and index control).

Ingestion is idempotent: upsert on (jurisdiction, session, upstream_id)
natural keys; unchanged checksum implies no write. Provenance columns live
on every imported entity via ProvenanceMixin.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from billcommons_schema.base import Base, ProvenanceMixin, TimestampMixin, UUIDPkMixin


# ---------------------------------------------------------------------------
# Jurisdictions / legislative bodies / sessions
# ---------------------------------------------------------------------------


class Jurisdiction(UUIDPkMixin, TimestampMixin, ProvenanceMixin, Base):
    """A state, DC, or (later) other governing jurisdiction."""

    __tablename__ = "jurisdictions"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    abbreviation: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    classification: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. "state"
    openstates_id: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)

    legislative_bodies: Mapped[list["LegislativeBody"]] = relationship(
        back_populates="jurisdiction"
    )
    sessions: Mapped[list["Session"]] = relationship(back_populates="jurisdiction")
    coverage_rows: Mapped[list["JurisdictionCoverage"]] = relationship(
        back_populates="jurisdiction"
    )


class LegislativeBody(UUIDPkMixin, TimestampMixin, ProvenanceMixin, Base):
    """A chamber/body within a jurisdiction (e.g. House, Senate, unicameral)."""

    __tablename__ = "legislative_bodies"

    jurisdiction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jurisdictions.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(Text, nullable=False)  # "lower"/"upper"/"legislature"

    jurisdiction: Mapped["Jurisdiction"] = relationship(back_populates="legislative_bodies")


class Session(UUIDPkMixin, TimestampMixin, ProvenanceMixin, Base):
    """A legislative session/biennium within a jurisdiction."""

    __tablename__ = "sessions"

    jurisdiction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jurisdictions.id"), nullable=False
    )
    identifier: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    jurisdiction: Mapped["Jurisdiction"] = relationship(back_populates="sessions")
    bills: Mapped[list["Bill"]] = relationship(back_populates="session")

    __table_args__ = (
        UniqueConstraint("jurisdiction_id", "identifier", name="uq_sessions_jurisdiction_identifier"),
    )


# ---------------------------------------------------------------------------
# Bills
# ---------------------------------------------------------------------------


class Bill(UUIDPkMixin, TimestampMixin, ProvenanceMixin, Base):
    """A bill/resolution. search_tsv is a generated column created via raw DDL
    in migration 0001 (SQLAlchemy Computed() reflects it for ORM awareness but
    the migration owns the authoritative DDL/index).
    """

    __tablename__ = "bills"

    jurisdiction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jurisdictions.id"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    chamber: Mapped[str | None] = mapped_column(Text, nullable=True)
    identifier: Mapped[str] = mapped_column(Text, nullable=False)
    identifier_norm: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    short_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    bill_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    introduced_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    latest_action_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_action_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    openstates_id: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)

    # Generated column: owned/created by raw DDL in migration 0001.
    search_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(identifier, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(description, '')), 'B')",
            persisted=True,
        ),
        nullable=True,
    )

    session: Mapped["Session"] = relationship(back_populates="bills")
    identifiers: Mapped[list["BillIdentifier"]] = relationship(back_populates="bill")
    versions: Mapped[list["BillVersion"]] = relationship(back_populates="bill")
    actions: Mapped[list["BillAction"]] = relationship(back_populates="bill")
    subjects: Mapped[list["BillSubject"]] = relationship(back_populates="bill")
    sponsorships: Mapped[list["Sponsorship"]] = relationship(back_populates="bill")
    vote_events: Mapped[list["VoteEvent"]] = relationship(back_populates="bill")

    __table_args__ = (
        UniqueConstraint("session_id", "identifier_norm", name="uq_bills_session_identifier_norm"),
        Index("ix_bills_identifier_norm", "identifier_norm"),
        # Serves the /changes keyset scan; see migration 0004.
        Index("ix_bills_updated_at_id", "updated_at", "id"),
    )


class BillIdentifier(UUIDPkMixin, TimestampMixin, ProvenanceMixin, Base):
    """Alternate/historical identifiers for a bill (renumbering, cross-refs)."""

    __tablename__ = "bill_identifiers"

    bill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bills.id"), nullable=False
    )
    identifier: Mapped[str] = mapped_column(Text, nullable=False)
    identifier_norm: Mapped[str] = mapped_column(Text, nullable=False)
    scheme: Mapped[str | None] = mapped_column(Text, nullable=True)

    bill: Mapped["Bill"] = relationship(back_populates="identifiers")

    __table_args__ = (Index("ix_bill_identifiers_identifier_norm", "identifier_norm"),)


class BillVersion(UUIDPkMixin, TimestampMixin, ProvenanceMixin, Base):
    """A textual version of a bill (introduced, engrossed, enrolled, etc.)."""

    __tablename__ = "bill_versions"

    bill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bills.id"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[date | None] = mapped_column(Date, nullable=True)

    bill: Mapped["Bill"] = relationship(back_populates="versions")
    documents: Mapped[list["BillDocument"]] = relationship(back_populates="version")


class BillDocument(UUIDPkMixin, TimestampMixin, ProvenanceMixin, Base):
    """A downloadable document/link attached to a bill version, plus extracted
    full text and its own generated tsvector for full-text search.
    """

    __tablename__ = "bill_documents"

    bill_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bill_versions.id"), nullable=False
    )
    media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Generated column: owned/created by raw DDL in migration 0001, bounded by
    # migration 0018 (see 0018's docstring for the byte-vs-truncation math).
    text_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', CASE WHEN octet_length(coalesce(extracted_text, '')) <= 250000 THEN coalesce(extracted_text, '') WHEN octet_length(coalesce(extracted_text, '')) = char_length(coalesce(extracted_text, '')) THEN left(coalesce(extracted_text, ''), 250000) ELSE left(coalesce(extracted_text, ''), 62500) END)",
            persisted=True,
        ),
        nullable=True,
    )

    version: Mapped["BillVersion"] = relationship(back_populates="documents")


class BillAction(UUIDPkMixin, TimestampMixin, ProvenanceMixin, Base):
    """A single legislative action/event in a bill's history."""

    __tablename__ = "bill_actions"

    bill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bills.id"), nullable=False
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    action_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    classification: Mapped[str | None] = mapped_column(Text, nullable=True)
    order: Mapped[int | None] = mapped_column(Integer, nullable=True)

    bill: Mapped["Bill"] = relationship(back_populates="actions")


class BillEvent(Base):
    """One entry in the append-only change log that `/changes` serves.

    Deliberately NOT a UUIDPkMixin/TimestampMixin row: the primary key IS the
    feed cursor, so it has to be a gapless-ordered bigserial rather than a
    random UUID, and it carries a single `changed_at` rather than the
    created/updated pair (a log entry is never updated).

    See migration 0005 for why this table exists and why readers must serve
    from behind a watermark rather than from the head.
    """

    __tablename__ = "bill_events"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bills.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_bill_events_seq", "seq"),
        Index("ix_bill_events_changed_at", "changed_at"),
        Index("ix_bill_events_bill_id_seq", "bill_id", "seq"),
    )


class BillSubject(UUIDPkMixin, TimestampMixin, Base):
    """Subject/topic tag applied to a bill."""

    __tablename__ = "bill_subjects"

    bill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bills.id"), nullable=False
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)

    bill: Mapped["Bill"] = relationship(back_populates="subjects")

    __table_args__ = (UniqueConstraint("bill_id", "subject", name="uq_bill_subjects_bill_subject"),)


# ---------------------------------------------------------------------------
# People / organizations / committees / sponsorships
# ---------------------------------------------------------------------------


class Person(UUIDPkMixin, TimestampMixin, ProvenanceMixin, Base):
    """A legislator or other individual actor."""

    __tablename__ = "people"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    party: Mapped[str | None] = mapped_column(Text, nullable=True)
    jurisdiction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jurisdictions.id"), nullable=True
    )
    openstates_id: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)

    sponsorships: Mapped[list["Sponsorship"]] = relationship(back_populates="person")
    vote_records: Mapped[list["VoteRecord"]] = relationship(back_populates="person")


class Organization(UUIDPkMixin, TimestampMixin, ProvenanceMixin, Base):
    """A chamber, committee parent, party, or other organizational actor."""

    __tablename__ = "organizations"

    jurisdiction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jurisdictions.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str | None] = mapped_column(Text, nullable=True)
    openstates_id: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)

    committees: Mapped[list["Committee"]] = relationship(back_populates="organization")


class Committee(UUIDPkMixin, TimestampMixin, ProvenanceMixin, Base):
    """A legislative committee, linked to its parent organization/chamber."""

    __tablename__ = "committees"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str | None] = mapped_column(Text, nullable=True)
    openstates_id: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="committees")


class Sponsorship(UUIDPkMixin, TimestampMixin, ProvenanceMixin, Base):
    """A person's (or organization's) sponsorship of a bill."""

    __tablename__ = "sponsorships"

    bill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bills.id"), nullable=False
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id"), nullable=True
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True
    )
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification: Mapped[str | None] = mapped_column(Text, nullable=True)  # primary/cosponsor
    primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    bill: Mapped["Bill"] = relationship(back_populates="sponsorships")
    person: Mapped["Person | None"] = relationship(back_populates="sponsorships")


# ---------------------------------------------------------------------------
# Votes
# ---------------------------------------------------------------------------


class VoteEvent(UUIDPkMixin, TimestampMixin, ProvenanceMixin, Base):
    """A recorded vote on a bill (e.g. floor vote, committee vote)."""

    __tablename__ = "vote_events"

    bill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bills.id"), nullable=True
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True
    )
    motion_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    motion_classification: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)  # pass/fail
    yes_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    no_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    other_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    bill: Mapped["Bill | None"] = relationship(back_populates="vote_events")
    vote_records: Mapped[list["VoteRecord"]] = relationship(back_populates="vote_event")


class VoteRecord(UUIDPkMixin, TimestampMixin, Base):
    """An individual legislator's recorded vote within a vote event."""

    __tablename__ = "vote_records"

    vote_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vote_events.id"), nullable=False
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id"), nullable=True
    )
    voter_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    option: Mapped[str] = mapped_column(Text, nullable=False)  # yes/no/other/absent/excused

    vote_event: Mapped["VoteEvent"] = relationship(back_populates="vote_records")
    person: Mapped["Person | None"] = relationship(back_populates="vote_records")


# ---------------------------------------------------------------------------
# Events / relations
# ---------------------------------------------------------------------------


class LegislativeEvent(UUIDPkMixin, TimestampMixin, ProvenanceMixin, Base):
    """A hearing or other scheduled legislative event."""

    __tablename__ = "legislative_events"

    jurisdiction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jurisdictions.id"), nullable=True
    )
    bill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bills.id"), nullable=True
    )
    committee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("committees.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)


class RelatedBill(UUIDPkMixin, TimestampMixin, Base):
    """A relationship between two bills (companion, prior-session, etc.)."""

    __tablename__ = "related_bills"

    bill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bills.id"), nullable=False
    )
    related_bill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bills.id"), nullable=True
    )
    related_identifier: Mapped[str | None] = mapped_column(Text, nullable=True)
    relation_type: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Ingestion / validation / coverage / search materialization / job queue
# ---------------------------------------------------------------------------


class SourceRecord(UUIDPkMixin, TimestampMixin, Base):
    """Raw-source pointer for an ingested entity (links entity -> rawstore key)."""

    __tablename__ = "source_records"

    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_source_records_entity", "entity_type", "entity_id"),)


class IngestionRun(UUIDPkMixin, TimestampMixin, Base):
    """A single run of an ingestion job (bootstrap or incremental)."""

    __tablename__ = "ingestion_runs"

    jurisdiction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jurisdictions.id"), nullable=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # running/success/failed
    bills_created: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    bills_updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ValidationRun(UUIDPkMixin, TimestampMixin, Base):
    """A validation pass over ingested data for a jurisdiction/session."""

    __tablename__ = "validation_runs"

    jurisdiction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jurisdictions.id"), nullable=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pass_rate: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    checks_run: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checks_failed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class JurisdictionCoverage(UUIDPkMixin, TimestampMixin, Base):
    """Per-jurisdiction (optionally per-session) coverage state machine row.

    status follows the locked state machine:
    NOT_STARTED -> SOURCE_IDENTIFIED -> BOOTSTRAPPED -> METADATA_SEARCHABLE ->
    FULL_TEXT_SEARCHABLE -> VALIDATING -> GREEN | DEGRADED | BLOCKED
    """

    __tablename__ = "jurisdiction_coverage"

    jurisdiction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jurisdictions.id"), nullable=False
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'NOT_STARTED'"))
    bill_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    full_text_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Bills with >=1 document whose text we could still legitimately obtain --
    # the honest denominator for SPEC GREEN criterion #5 ("full text
    # searchable wherever technically available"). Excludes bills with no
    # document at all and bills whose every document is terminally
    # unfetchable (robots-disallowed, scanned PDF with no text layer).
    # NULL means "not yet recomputed", which is NOT the same as 0 ("nothing
    # obtainable") -- 0 lets a jurisdiction be GREEN on a vacuous criterion
    # #5, so the unknown case must stay distinguishable and block promotion.
    full_text_available_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    validation_pass_rate: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    known_gaps: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    jurisdiction: Mapped["Jurisdiction"] = relationship(back_populates="coverage_rows")

    __table_args__ = (
        CheckConstraint(
            "status in ("
            "'NOT_STARTED','SOURCE_IDENTIFIED','BOOTSTRAPPED','METADATA_SEARCHABLE',"
            "'FULL_TEXT_SEARCHABLE','VALIDATING','GREEN','DEGRADED','BLOCKED')",
            name="ck_jurisdiction_coverage_status",
        ),
        # Enforces uniqueness for rows with a non-null session_id. Postgres
        # treats NULL as distinct from itself in a UNIQUE constraint, so this
        # alone does NOT prevent duplicate (jurisdiction_id, NULL) rows --
        # migration 0002 adds a partial unique index
        # (uq_jurisdiction_coverage_jurisdiction_null_session, ON
        # jurisdiction_coverage(jurisdiction_id) WHERE session_id IS NULL) to
        # close that gap for the jurisdiction-level (no-session) coverage row.
        UniqueConstraint(
            "jurisdiction_id", "session_id", name="uq_jurisdiction_coverage_jurisdiction_session"
        ),
    )


class SearchDocument(UUIDPkMixin, TimestampMixin, Base):
    """Materialized denormalized search row (rebuilt/refreshed by a worker job)."""

    __tablename__ = "search_documents"

    bill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bills.id"), nullable=False, unique=True
    )
    jurisdiction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jurisdictions.id"), nullable=False
    )
    identifier_norm: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    subjects: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    sponsors: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_action_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Generated column: owned/created by raw DDL in migration 0001.
    search_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(identifier_norm, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(summary, '')), 'B')",
            persisted=True,
        ),
        nullable=True,
    )

    __table_args__ = (Index("ix_search_documents_identifier_norm", "identifier_norm"),)


class IngestJob(UUIDPkMixin, TimestampMixin, Base):
    """Postgres-backed job queue row (worker claims via SELECT ... FOR UPDATE
    SKIP LOCKED on (status, run_after)).
    """

    __tablename__ = "ingest_jobs"

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status in ('queued','running','done','failed','dead')",
            name="ck_ingest_jobs_status",
        ),
        Index("ix_ingest_jobs_status_run_after", "status", "run_after"),
    )


class AlertSubscription(Base):
    """One "email me when X moves" subscription (see migration 0006).

    Not a UUIDPkMixin/TimestampMixin row by convention alone -- it does use a
    UUID pk, but `last_seq` is the load-bearing column: the sender's private
    cursor into bill_events, advanced only after a digest is handed to the
    mail provider, so a crashed run re-sends rather than silently skips.
    """

    __tablename__ = "alert_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    #: Jurisdiction abbreviation to narrow the digest to, or NULL for national
    #: (see migration 0011). A city or county affairs office wants its own
    #: legislature, not all 51.
    jurisdiction: Mapped[str | None] = mapped_column(Text, nullable=True)
    unsubscribe_token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # NULLS NOT DISTINCT: `jurisdiction` is nullable, and under the default
        # NULLS DISTINCT the constraint would stop binding national
        # subscriptions entirely. See migration 0011.
        UniqueConstraint(
            "email",
            "kind",
            "target",
            "jurisdiction",
            name="uq_alert_email_kind_target_jurisdiction",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_alert_subscriptions_active", "active"),
    )


class WebhookSubscription(Base):
    """A push-delivery subscription over the change feed (see migration 0012).

    Created by the API with `verified=false`; the API never performs outbound
    HTTP (see billcommons_api.routers.webhooks). A separate Railway worker
    (workers/webhooks/dispatch_webhooks.py) challenges it, then drains
    bill_events into signed POSTs.

    `signing_secret` and `manage_token_hash` are two different secrets on
    purpose -- see the migration docstring. `last_seq` has no ORM-level
    default for the same reason: every row must be created with the
    safety-lag watermark computed at INSERT time, never 0 and never the raw
    head of bill_events.
    """

    __tablename__ = "webhook_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    creator_ip: Mapped[str] = mapped_column(Text, nullable=False)
    signing_secret: Mapped[str] = mapped_column(Text, nullable=False)
    manage_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    event_kinds: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    challenge_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    challenge_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    failing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    disabled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notify_pending: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # r11 fix #5 / migration 0015 (packages/schema/alembic/versions/0015_
    # webhook_challenge_attempted_at.py) adds `challenge_attempted_at` to
    # the TABLE, but it is deliberately NOT mapped as an ORM column here.
    # This repo's live DB is still on migration 0012 as of this column's
    # introduction (0013/0014 are also committed-but-not-yet-applied -- see
    # `_notify_pending_supports_created_disabled`'s and `_creation_events_
    # table_exists`'s own docstrings for the identical situation). A mapped
    # column -- even `deferred=True` -- is still included in every INSERT/
    # UPDATE SQLAlchemy generates for this entity (deferred only excludes a
    # column from the initial SELECT; it does NOT exclude it from writes),
    # so mapping it here would break EVERY webhook subscription creation
    # site-wide the moment this line merged, well before migration 0015 is
    # ever applied. workers/webhooks/dispatch_webhooks.py instead reads/
    # writes this column via raw `text()` SQL, gated behind `_challenge_
    # attempted_at_column_exists`'s probe -- see that function's own
    # docstring.
    #
    # Migration 0019 (monetization) adds a SECOND such column,
    # `customer_id` (nullable FK -> api_customers, Codex R9: ownership
    # attaches to the account, not to an API key) -- same reasoning, same
    # fix: deliberately NOT mapped here. The live DB is migrated by the
    # operator on their own schedule, and mapping it would put `customer_id`
    # into every INSERT this ORM entity generates before that migration
    # lands, breaking webhook subscription creation site-wide in the
    # meantime. Phase 2 (which is the first code to ever populate this
    # column) reads/writes it via raw `text()` SQL, the same way
    # `challenge_attempted_at` is handled above.

    __table_args__ = (
        CheckConstraint("kind in ('topic','jurisdiction','bills')", name="ck_webhook_kind"),
        CheckConstraint(
            # 'created_disabled' added by migration 0013 (verify round-3
            # fix #13) -- see that migration's docstring.
            "notify_pending is null or notify_pending in ('created','disabled','created_disabled')",
            name="ck_webhook_notify_pending",
        ),
        UniqueConstraint(
            "url",
            "kind",
            "target",
            "event_kinds",
            name="uq_webhook_url_kind_target_event_kinds",
            postgresql_nulls_not_distinct=True,
        ),
        Index(
            "ix_webhook_subscriptions_due",
            "next_attempt_at",
            "last_attempt_at",
            postgresql_where=text("active AND verified"),
        ),
        Index("ix_webhook_subscriptions_host", "host", postgresql_where=text("active")),
        Index("ix_webhook_subscriptions_creator_ip_created_at", "creator_ip", "created_at"),
    )


class WebhookDelivery(Base):
    """One delivery ATTEMPT for a webhook subscription (see migration 0012).

    Kept for 30 days (pruned by the dispatcher each tick) so
    GET /api/v1/webhooks/{id} can answer "why didn't we get event X" without
    the subscriber having to guess from HTTP status codes alone.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    delivery_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    first_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_webhook_deliveries_subscription_attempted", "subscription_id", "attempted_at"),
        Index("ix_webhook_deliveries_attempted_at", "attempted_at"),
    )


class WebhookCreationEvent(Base):
    """One webhook-creation ATTEMPT that passed input validation and every
    quota check (see migration 0014) -- verify round-5 fix #4 (kimi #4,
    opus MED #2).

    `webhook_subscriptions` is a set of SURVIVING rows: a hard `DELETE
    /api/v1/webhooks/{id}` and the dispatcher's own 24h unverified-GC (see
    workers/webhooks/dispatch_webhooks.py's `run_challenges`) both remove
    rows outright. Counting the per-IP daily creation quota against THAT
    table let one IP loop create -> delete forever, resetting its own
    quota on every cycle -- unbounded verification-challenge POSTs to an
    attacker-chosen endpoint (~7,200/day), and repeat "webhook created"
    emails to a caller-supplied, never-confirmed address. This table is
    append-only from the API's perspective (`create_webhook` only ever
    INSERTs); only workers/webhooks/dispatch_webhooks.py's
    `prune_creation_events` deletes from it, and only rows older than 48h
    (twice the 24h quota window, so a request straddling midnight always
    still sees its own full recent history).

    No foreign key to `webhook_subscriptions` on purpose -- this row must
    outlive the subscription it corresponds to (that survival is the whole
    point), and an attempt that was itself quota-rejected (429/403) never
    had a subscription row to begin with.
    """

    __tablename__ = "webhook_creation_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    creator_ip: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_webhook_creation_events_creator_ip_created_at", "creator_ip", "created_at"),
    )


class Feedback(Base):
    """One site-feedback message (see migration 0007). Write-only from the
    API's perspective: there is no read endpoint; rows are read by the owner
    directly."""

    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    page: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolInvocation(Base):
    """One MCP tool call, recorded in aggregate (see migration 0008).

    Exists because "is anyone using this?" was unanswerable. The MCP logs
    showed 139 successful POSTs and, over the same window, exactly ONE tool
    call -- the rest were connect, list tools, disconnect: directory health
    probers, not users. Nothing distinguished them without recording it.

    Aggregate-only by construction: no IP, no query text, no bill ids, no
    auth. Enough to tell a research session from a health check; not enough to
    profile a caller.
    """

    __tablename__ = "tool_invocations"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    # Error CLASS only. Never the message -- messages can quote user input.
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_family: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Monetization (2026-08-21 spec, migration 0019). No user system: identity is
# an email; the account system is Stripe (Phase 2). Phase 1 (this branch)
# only reads/writes ApiCustomer, ApiKey, ApiKeyUsage, ApiKeyUsageSubnet, and
# AccountLoginToken -- the rest are created now so Phase 2/3 are additive.
# ---------------------------------------------------------------------------


class ApiCustomer(Base):
    """One customer identity, keyed by (lowercased) email (see migration
    0019, amendment A1: upsert key is `lower(email)`, so a Developer-key
    holder who later buys Builder keeps one row).

    `extra_requests_per_day` / `extra_heavy_per_day` / `override_expires_at`
    are a manual founder support override (Codex R9) -- added to the plan
    limit while `override_expires_at > now()` (amendment A12e). `suspended_at`
    / `suspension_reason` are the operator kill switch: every keyed request
    is refused 403 `account_suspended` while `suspended_at` is set (A12e).
    Both folded onto this row rather than a separate table so the auth hot
    path (`billcommons_api.api_keys.resolve_key`) stays one query.
    """

    __tablename__ = "api_customers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_requests_per_day: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    extra_heavy_per_day: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    override_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspension_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("email = lower(email)", name="ck_api_customers_email_lowercase"),
        Index("uq_api_customers_email", "email", unique=True),
    )


class ApiKey(Base):
    """One API key (see migration 0019).

    `plan` is DENORMALIZED on purpose: the auth hot path must be one indexed
    lookup, not a three-table join; the Stripe webhook (Phase 2) is its only
    writer once billing exists. `key_prefix` (first 16 chars, unique,
    display-safe) is the O(1) lookup column; `key_hash` (sha256 hex of the
    full key) is compared with `hmac.compare_digest`, never `==`. The full
    plaintext key is never stored except transiently, Fernet-encrypted, in
    `reveal_ciphertext` between mint and reveal (B1) -- nulled (along with
    `reveal_token_hash`/`reveal_expires_at`) the moment it is revealed or the
    24h reveal window lapses.

    `status` in `active`/`rotating`/`revoked`. Usable = `active` OR
    (`rotating` AND `revoke_at > now()`); ceiling is 3 usable keys per
    customer (2 active + 1 rotating) -- B3, supersedes the earlier "rotating
    doesn't count" draft (A9).
    """

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_customers.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'default'"))
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'developer'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    rotated_from: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    revoke_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reveal_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    reveal_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    reveal_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # E6/Gate A (migration 0021): which `api_subscriptions` row this key
    # was minted FOR, if any (null for a Developer key minted at first
    # login/via `POST /account/keys`, or for a key predating this column).
    # `ON DELETE SET NULL` -- a key must never be destroyed by its
    # subscription row disappearing. Lets `billing._ensure_provisioned_
    # for_subscription` make "did we already mint a paid key for THIS
    # subscription" idempotent per subscription rather than per customer.
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_subscriptions.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint("environment in ('live','test')", name="ck_api_keys_environment"),
        CheckConstraint(
            "plan in ('developer','builder','scale','enterprise')", name="ck_api_keys_plan"
        ),
        CheckConstraint("status in ('active','rotating','revoked')", name="ck_api_keys_status"),
        Index("uq_api_keys_key_prefix", "key_prefix", unique=True),
        Index("uq_api_keys_key_hash", "key_hash", unique=True),
        Index("ix_api_keys_customer_id", "customer_id"),
        Index("ix_api_keys_status_plan", "status", "plan"),
        Index("ix_api_keys_subscription_id", "subscription_id"),
        Index(
            "uq_api_keys_one_live_per_subscription",
            "subscription_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'rotating') AND subscription_id IS NOT NULL"),
            sqlite_where=text("status IN ('active', 'rotating') AND subscription_id IS NOT NULL"),
        ),
    )


class ApiSubscription(Base):
    """One Stripe subscription's synced state (Phase 2; table exists now so
    Phase 1's `api_keys.plan` denormalization has somewhere to read from
    later without another migration).

    `past_due_since` (A3) anchors the 7-day dunning window: 402 fires once
    `status='past_due' AND now() - past_due_since > 7 days`, or `status in
    ('canceled','unpaid')`. `last_event_created_at` (A4) makes every
    subscription-event handler idempotent against Stripe delivering events
    out of order: a handler applies an event only if `event.created >=` this
    column. At most one non-canceled subscription per customer (B4) --
    enforced by a partial unique index on `customer_id`.
    """

    __tablename__ = "api_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_customers.id", ondelete="CASCADE"), nullable=False
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    past_due_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("plan in ('builder','scale','enterprise')", name="ck_api_subscriptions_plan"),
        Index("ix_api_subscriptions_customer_status", "customer_id", "status"),
        Index(
            "uq_api_subscriptions_one_active_per_customer",
            "customer_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'trialing', 'past_due', 'unpaid')"),
            sqlite_where=text("status IN ('active', 'trialing', 'past_due', 'unpaid')"),
        ),
    )


class ApiKeyUsage(Base):
    """One (key, day) counter row -- the billable read/write path
    (`billcommons_api.quota.QuotaMiddleware`). PK `(key_id, usage_date)` so
    the post-response accounting statement (B6) is a single `INSERT ... ON
    CONFLICT DO UPDATE` per keyed request: both `requests` and
    `heavy_requests` move atomically in one statement.
    """

    __tablename__ = "api_key_usage"

    key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True
    )
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    heavy_requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    mcp_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (Index("ix_api_key_usage_usage_date", "usage_date"),)


class ApiKeyUsageSubnet(Base):
    """Key-sharing telemetry (amendment A6): one (key, day, subnet) counter,
    upserted per keyed request alongside `ApiKeyUsage`. The admin usage
    endpoint reads `count(distinct subnet)` to flag a key used from more
    than ~20 distinct /24s in a day -- flag only, never auto-blocked.
    """

    __tablename__ = "api_key_usage_subnets"

    key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True
    )
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    subnet: Mapped[str] = mapped_column(Text, primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (Index("ix_api_key_usage_subnets_usage_date", "usage_date"),)


class ApiCustomerUsage(Base):
    """Round-2 amendment C1: quota is enforced per CUSTOMER, not per key --
    a customer with two active keys shares ONE daily budget across both,
    so `QuotaMiddleware`'s pre-check `SELECT` and post-response upsert (B6)
    target THIS table. `ApiKeyUsage` stays a per-key REPORTING breakdown
    only (what the admin usage endpoint groups by), written in the same
    transaction as this table's row. `X-Quota-*` response headers report
    this table's counter, never `ApiKeyUsage`'s.
    """

    __tablename__ = "api_customer_usage"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_customers.id", ondelete="CASCADE"), primary_key=True
    )
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    heavy_requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (Index("ix_api_customer_usage_usage_date", "usage_date"),)


class StripeEvent(Base):
    """Stripe webhook idempotency ledger (Phase 2; table exists now).

    First statement on delivery is `INSERT ... ON CONFLICT (id) DO NOTHING
    RETURNING id` -- no row back means already processed, return 200 at
    once. `outcome='skipped_foreign_app'` records an event whose object
    lacks `metadata.app == "billcommons"` -- this Stripe account also runs
    the owner's other sub-businesses (R7's HIGH finding); every object we
    create is tagged, and anything untagged is presumed foreign and ignored.
    """

    __tablename__ = "stripe_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Round-3 amendment D5: error CLASS only, never the raw message -- same
    # convention as `WebhookSubscription.last_error` / `ToolInvocation.error_code`.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "outcome is null or outcome in "
            "('processed','skipped_foreign_app','duplicate_subscription_canceled','permanent_error')",
            name="ck_stripe_events_outcome",
        ),
        Index("ix_stripe_events_processed_at", "processed_at"),
    )


class AccountLoginToken(Base):
    """One single-use, short-TTL token backing both magic-link login
    (`purpose='login'`) and the post-checkout key-reveal flow
    (`purpose='reveal'`, B1) -- both are "prove you own this email, once,
    briefly", so one table, one shape. `token_hash` is sha256 of the token
    sent by email; the plaintext token itself is never stored. 15-minute
    TTL, single use (`used_at` set on consumption).
    """

    __tablename__ = "account_login_tokens"

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    stripe_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_ip: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("purpose in ('login','reveal')", name="ck_account_login_tokens_purpose"),
        Index("ix_account_login_tokens_expires_at", "expires_at"),
        Index("ix_account_login_tokens_email", "email"),
    )


class SnapshotArtifact(Base):
    """One built snapshot file (Phase 3; table exists now, unused until the
    nightly builder ships). Parquet per table + manifest; `object_key` is
    the R2 key (`v1/{YYYY-MM-DD}/{scope}/...`)."""

    __tablename__ = "snapshot_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    jurisdiction: Mapped[str | None] = mapped_column(Text, nullable=True)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    row_counts: Mapped[dict] = mapped_column(JSONB, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'parquet'"))

    __table_args__ = (
        CheckConstraint("scope in ('full','jurisdiction')", name="ck_snapshot_artifacts_scope"),
        Index("uq_snapshot_artifacts_object_key", "object_key", unique=True),
        Index(
            "ix_snapshot_artifacts_scope_jurisdiction_built_at",
            "scope",
            "jurisdiction",
            text("built_at DESC"),
        ),
    )


class SnapshotEntitlement(Base):
    """One customer's right to a snapshot (Phase 2/3; table exists now,
    unused until checkout/download endpoints ship).

    `delivered_at` (B5, supersedes A7's originally-proposed `fulfilled_at`
    name -- same column, same rule) is set by the manual runbook once the
    7-day R2 link is emailed; `charge.refunded` for a one-time snapshot
    revokes the entitlement iff `delivered_at IS NULL`. `stripe_payment_
    intent_id` (A7) is how a `charge.refunded` event (which carries a
    charge, not a checkout session) finds this row: charge -> payment_intent
    -> this column.
    """

    __tablename__ = "snapshot_entitlements"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_customers.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    jurisdiction: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stripe_checkout_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("kind in ('subscription','one_time')", name="ck_snapshot_entitlements_kind"),
        Index("ix_snapshot_entitlements_customer_id", "customer_id"),
        # 2026-08-21 fix-pass item 22: this partial unique index already
        # exists in production (migration 0019's raw
        # `uq_snapshot_entitlements_payment_intent`) but, like every other
        # Stripe-id partial index in this file, was deliberately never
        # mapped here -- mapped now (same name, same predicate) SOLELY so
        # `billing._handle_snapshot_payment`'s `ON CONFLICT (
        # stripe_payment_intent_id) WHERE ...` insert has a matching index
        # to target under BOTH dialects (`billcommons_api.tests.
        # _monetization_sqlite`'s SQLite harness only ever creates indexes
        # declared here via `Base.metadata.create_all`, never migration
        # 0019's raw SQL). Alembic never autogenerates from this file
        # (every migration here is hand-written), so mapping this does not
        # emit or require a new migration.
        Index(
            "uq_snapshot_entitlements_payment_intent",
            "stripe_payment_intent_id",
            unique=True,
            postgresql_where=text("stripe_payment_intent_id IS NOT NULL"),
            sqlite_where=text("stripe_payment_intent_id IS NOT NULL"),
        ),
    )


class SnapshotDownload(Base):
    """One snapshot download (Phase 3; table exists now, unused until the
    download endpoint ships). Cap: 10 downloads/customer/day."""

    __tablename__ = "snapshot_downloads"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_customers.id", ondelete="CASCADE"), nullable=False
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snapshot_artifacts.id", ondelete="CASCADE"), nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        Index("ix_snapshot_downloads_customer_requested_at", "customer_id", "requested_at"),
    )
