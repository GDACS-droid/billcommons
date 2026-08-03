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

    # Generated column: owned/created by raw DDL in migration 0001.
    text_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(extracted_text, ''))", persisted=True),
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
