"""Pydantic response models for the public API. Read-only projections of the
SQLAlchemy models in billcommons_schema; field sets follow SPEC.md's
"Bill data" capture list. Missing upstream data is null, never fabricated.
"""
from __future__ import annotations

import uuid
from datetime import date as date_
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Jurisdiction(OrmModel):
    id: uuid.UUID
    name: str
    abbreviation: str
    classification: str
    openstates_id: str | None = None


class LegislativeBody(OrmModel):
    id: uuid.UUID
    jurisdiction_id: uuid.UUID
    name: str
    classification: str


class Session(OrmModel):
    id: uuid.UUID
    jurisdiction_id: uuid.UUID
    jurisdiction_abbreviation: str | None = None
    jurisdiction_name: str | None = None
    identifier: str
    name: str | None = None
    classification: str | None = None
    start_date: date_ | None = None
    end_date: date_ | None = None
    active: bool


class BillSummary(OrmModel):
    """Row shape used in list/search results (lighter than the detail view)."""

    id: uuid.UUID
    jurisdiction_id: uuid.UUID
    session_id: uuid.UUID
    chamber: str | None = None
    identifier: str
    identifier_norm: str
    title: str
    short_title: str | None = None
    bill_type: str | None = None
    status: str | None = None
    status_date: date_ | None = None
    introduced_date: date_ | None = None
    latest_action_text: str | None = None
    latest_action_date: date_ | None = None
    source_url: str | None = None


class BillDetail(BillSummary):
    description: str | None = None
    source_name: str | None = None
    retrieved_at: datetime | None = None
    upstream_updated_at: datetime | None = None


class BillIdentifierOut(OrmModel):
    id: uuid.UUID
    identifier: str
    identifier_norm: str
    scheme: str | None = None


class BillVersionOut(OrmModel):
    id: uuid.UUID
    bill_id: uuid.UUID
    note: str | None = None
    date: date_ | None = None


class BillDocumentOut(OrmModel):
    id: uuid.UUID
    bill_version_id: uuid.UUID
    media_type: str | None = None
    url: str | None = None
    has_extracted_text: bool = False


class RelatedBillOut(OrmModel):
    """A cross-reference from one bill to another.

    `related_bill_id` is resolved at read time where we can find the target in
    this corpus, and is null otherwise -- most commonly for `prior-session`
    links, whose target sits in a session we do not hold. Returning the
    identifier regardless is deliberate: knowing a bill HAS a prior-session
    predecessor called "A 4171" is the answer to a multi-year tracking question
    even when we cannot serve that bill ourselves.
    """

    id: uuid.UUID
    relation_type: str | None = None
    related_identifier: str | None = None
    related_bill_id: uuid.UUID | None = None


class BillActionOut(OrmModel):
    id: uuid.UUID
    bill_id: uuid.UUID
    description: str
    action_date: date_ | None = None
    classification: str | None = None
    order: int | None = None


class SponsorshipOut(OrmModel):
    id: uuid.UUID
    bill_id: uuid.UUID
    person_id: uuid.UUID | None = None
    name: str | None = None
    classification: str | None = None
    primary: bool


class VoteRecordOut(OrmModel):
    id: uuid.UUID
    person_id: uuid.UUID | None = None
    voter_name: str | None = None
    option: str


class VoteEventOut(OrmModel):
    id: uuid.UUID
    bill_id: uuid.UUID | None = None
    motion_text: str | None = None
    motion_classification: str | None = None
    start_date: date_ | None = None
    result: str | None = None
    yes_count: int | None = None
    no_count: int | None = None
    other_count: int | None = None
    votes: list[VoteRecordOut] = []


class PersonSummary(OrmModel):
    id: uuid.UUID
    name: str
    party: str | None = None
    jurisdiction_id: uuid.UUID | None = None


class PersonDetail(PersonSummary):
    openstates_id: str | None = None


class CommitteeOut(OrmModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    classification: str | None = None


class LegislativeEventOut(OrmModel):
    id: uuid.UUID
    jurisdiction_id: uuid.UUID | None = None
    jurisdiction_abbreviation: str | None = None
    bill_id: uuid.UUID | None = None
    committee_id: uuid.UUID | None = None
    name: str
    description: str | None = None
    start_date: datetime | None = None
    location: str | None = None


class SourceRecordOut(OrmModel):
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    source_name: str
    source_url: str | None = None
    upstream_id: str | None = None
    retrieved_at: datetime | None = None


class CoverageRowOut(BaseModel):
    """A row in the public coverage matrix (SPEC.md "Coverage"). Assembled by
    joining jurisdiction_coverage + jurisdictions + sessions -- not a direct
    ORM projection of any single table."""

    jurisdiction_code: str
    jurisdiction_name: str
    session_identifier: str | None = None
    session_status: str | None = None  # active | adjourned | special
    bill_count: int
    full_text_count: int
    # Share of ALL bills we hold text for. Reads low for a jurisdiction whose
    # source simply publishes few documents, so it is not the GREEN criterion.
    full_text_pct: float | None = None
    # Bills whose text is obtainable at all (excludes bills with no published
    # document, and documents that are robots-disallowed or have no text
    # layer). This is the denominator SPEC GREEN criterion #5 is judged on.
    full_text_available_count: int | None = None
    full_text_of_available_pct: float | None = None
    last_update: str | None = None
    source_name: str | None = None
    validation_sample: int | None = None
    validation_pass_rate: float | None = None
    status: str
    known_gaps: list[str] = []


class DiffLineOut(BaseModel):
    type: str  # add | remove | context | meta
    text: str


class BillCompareOut(BaseModel):
    bill_id: uuid.UUID
    from_version_id: uuid.UUID
    to_version_id: uuid.UUID
    diff_lines: list[DiffLineOut]


class BillCompareEnvelope(BaseModel):
    data: BillCompareOut
    meta: dict


class SearchResult(BillSummary):
    rank: float | None = None
    highlight: str | None = Field(
        default=None,
        description=(
            "Plain text (never HTML) with matched fragments wrapped in the "
            "sentinel tokens '⟦H⟧'...'⟦/H⟧' (see billcommons_api.search "
            "HIGHLIGHT_START_SENTINEL/HIGHLIGHT_STOP_SENTINEL). Source titles/"
            "descriptions/document text come from upstream jurisdictions and "
            "are not sanitized -- clients must split on the sentinels and "
            "render their own emphasis (e.g. <mark>); never render this "
            "field with dangerouslySetInnerHTML or any raw-HTML sink."
        ),
    )
    match_type: str


class HealthOut(BaseModel):
    status: str
    database: str


class ReadyOut(BaseModel):
    ready: bool
    database: str
