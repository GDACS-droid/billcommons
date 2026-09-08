"""Forward evidence for mutations made from OpenStates v3 responses.

The source is an aggregator.  ``official_raw_blobs`` is reused only as the
corpus-wide content-addressed byte ledger; no record here claims that an
OpenStates response is a primary official source.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy import select

from billcommons_ingest.openstates_api import RetainedOpenStatesResponse
from billcommons_schema.models import (
    Bill,
    BillAction,
    BillDocument,
    BillVersion,
    CorpusUpdateEvidence,
    OfficialRawBlob,
    Sponsorship,
)


SOURCE_NAME = "openstates_v3_api"
PROCESSING_VERSION = "openstates_api_sync_evidence/1"
MAX_BLOB_BYTES = 8 * 1024 * 1024
# An evidence snapshot must be bounded before materializing ORM rows.  The
# official-action reconciliation path uses the same 1,000-record ceiling.
MAX_CHILD_RECORDS_PER_COMPONENT = 1_000


class SnapshotRecordLimitExceeded(RuntimeError):
    """A child collection cannot be captured safely within the evidence cap."""

    def __init__(self, component: str, cap: int):
        self.component = component
        self.cap = cap
        # Do not include bill identity, URLs, or child content in an error
        # which may reach worker logs or a persisted ingestion-run failure.
        super().__init__(f"corpus evidence snapshot {component} exceeds configured record cap {cap}")


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="microseconds")


def _date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _store_blob(db: OrmSession, data: bytes, *, content_type: str) -> str:
    """Content-address a bounded blob and verify it before it is referenced."""
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_BLOB_BYTES:
        raise ValueError("corpus update evidence must be non-empty bytes within the storage cap")
    sha256 = hashlib.sha256(data).hexdigest()
    db.execute(
        insert(OfficialRawBlob)
        .values(sha256=sha256, data=data, content_type=content_type)
        .on_conflict_do_nothing(index_elements=(OfficialRawBlob.sha256,))
    )
    stored = db.get(OfficialRawBlob, sha256)
    if stored is None or hashlib.sha256(stored.data).hexdigest() != sha256:
        raise RuntimeError("corpus evidence blob integrity check failed")
    return sha256


def _bounded_rows(db: OrmSession, statement, *, component: str) -> list:
    """Materialize at most one record above the cap to detect overflow."""
    rows = db.execute(statement.limit(MAX_CHILD_RECORDS_PER_COMPONENT + 1)).scalars().all()
    if len(rows) > MAX_CHILD_RECORDS_PER_COMPONENT:
        raise SnapshotRecordLimitExceeded(component, MAX_CHILD_RECORDS_PER_COMPONENT)
    return rows


def snapshot_bill(db: OrmSession, bill: Bill) -> dict[str, Any]:
    """Serialize every api-sync-managed bill field and child deterministically."""
    actions = _bounded_rows(
        db,
        select(BillAction).where(BillAction.bill_id == bill.id).order_by(BillAction.id),
        component="actions",
    )
    sponsorships = _bounded_rows(
        db,
        select(Sponsorship).where(Sponsorship.bill_id == bill.id).order_by(Sponsorship.id),
        component="sponsorships",
    )
    versions = _bounded_rows(
        db,
        select(BillVersion).where(BillVersion.bill_id == bill.id).order_by(BillVersion.id),
        component="versions",
    )
    documents = _bounded_rows(
        db,
        select(BillDocument)
        .join(BillVersion, BillVersion.id == BillDocument.bill_version_id)
        .where(BillVersion.bill_id == bill.id)
        .order_by(BillDocument.id),
        component="documents",
    )

    return {
        "bill": {
            "id": str(bill.id),
            "identifier": bill.identifier,
            "identifier_norm": bill.identifier_norm,
            "title": bill.title,
            "description": bill.description,
            "chamber": bill.chamber,
            "bill_type": bill.bill_type,
            "openstates_id": bill.openstates_id,
            "upstream_id": bill.upstream_id,
            "source_name": bill.source_name,
            "source_url": bill.source_url,
            "parser_version": bill.parser_version,
            "retrieved_at": _timestamp(bill.retrieved_at),
            "checksum": bill.checksum,
            "latest_action_date": _date(bill.latest_action_date),
            "latest_action_text": bill.latest_action_text,
        },
        "actions": sorted(
            [
                {
                    "id": str(action.id),
                    "description": action.description,
                    "action_date": _date(action.action_date),
                    "classification": action.classification,
                    "order": action.order,
                    "source_name": action.source_name,
                    "upstream_id": action.upstream_id,
                    "retrieved_at": _timestamp(action.retrieved_at),
                }
                for action in actions
            ],
            key=lambda value: (value["action_date"] or "", value["order"] if value["order"] is not None else -1, value["description"] or "", value["id"]),
        ),
        "sponsorships": sorted(
            [
                {
                    "id": str(sponsorship.id),
                    "name": sponsorship.name,
                    "classification": sponsorship.classification,
                    "primary": sponsorship.primary,
                    "source_name": sponsorship.source_name,
                    "upstream_id": sponsorship.upstream_id,
                    "retrieved_at": _timestamp(sponsorship.retrieved_at),
                }
                for sponsorship in sponsorships
            ],
            key=lambda value: (value["name"] or "", value["classification"] or "", value["id"]),
        ),
        "versions": sorted(
            [
                {
                    "id": str(version.id),
                    "note": version.note,
                    "date": _date(version.date),
                    "source_name": version.source_name,
                    "upstream_id": version.upstream_id,
                    "retrieved_at": _timestamp(version.retrieved_at),
                    "license_note": version.license_note,
                }
                for version in versions
            ],
            key=lambda value: (value["date"] or "", value["note"] or "", value["id"]),
        ),
        "documents": sorted(
            [
                {
                    "id": str(document.id),
                    "bill_version_id": str(document.bill_version_id),
                    "media_type": document.media_type,
                    "url": document.url,
                    "source_name": document.source_name,
                    "upstream_id": document.upstream_id,
                    "retrieved_at": _timestamp(document.retrieved_at),
                }
                for document in documents
            ],
            key=lambda value: (value["bill_version_id"], value["url"] or "", value["id"]),
        ),
    }


def _changed_components(before: dict[str, Any] | None, after: dict[str, Any]) -> list[str]:
    if before is None:
        return [component for component in after if component == "bill" or after[component]]
    return [component for component in after if before.get(component) != after[component]]


def record_update_evidence(
    db: OrmSession,
    *,
    bill: Bill,
    original_bill_upstream_id: str | None,
    response: RetainedOpenStatesResponse,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    retrieved_at: datetime,
) -> CorpusUpdateEvidence | None:
    """Store raw response plus full local before/after snapshots on mutation.

    A caller must pass an actual retained response from ``OpenStatesClient``;
    accepting a bare parsed dictionary would make fixture-generated canonical
    JSON look like upstream evidence.  This function does no transaction
    handling: a failure propagates to the api-sync worker, whose job
    transaction rolls back all local writes and these blobs together.
    """
    if not isinstance(response, RetainedOpenStatesResponse) or not response.is_retained_response:
        raise ValueError("api-sync provenance requires an explicit retained OpenStates response")
    before_bytes = _canonical_json_bytes(before) if before is not None else None
    after_bytes = _canonical_json_bytes(after)
    if before_bytes == after_bytes:
        return None
    components = _changed_components(before, after)
    if not components:
        raise RuntimeError("corpus update evidence cannot record an empty mutation")
    response_sha256 = _store_blob(db, response.raw_bytes, content_type="application/json")
    before_sha256 = (
        _store_blob(db, before_bytes, content_type="application/json")
        if before_bytes is not None
        else None
    )
    after_sha256 = _store_blob(db, after_bytes, content_type="application/json")
    evidence = CorpusUpdateEvidence(
        bill_id=bill.id,
        original_bill_upstream_id=original_bill_upstream_id,
        source_name=SOURCE_NAME,
        source_url=response.source_url,
        request_scope=response.request_scope,
        response_sha256=response_sha256,
        before_snapshot_sha256=before_sha256,
        after_snapshot_sha256=after_sha256,
        processing_version=PROCESSING_VERSION,
        mutation_kind="created" if before is None else "updated",
        changed_components=components,
        retrieved_at=retrieved_at,
    )
    db.add(evidence)
    db.flush()
    return evidence
