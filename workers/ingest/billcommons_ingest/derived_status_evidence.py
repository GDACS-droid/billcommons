"""Replayable local evidence for derived bill status and substitution mutations.

This is intentionally separate from ``CorpusUpdateEvidence``.  Status is
computed from local corpus inputs; it must never be represented as a newly
fetched upstream assertion.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as OrmSession

from billcommons_schema.models import (
    Bill,
    CorpusUpdateEvidence,
    DerivedStatusEvidence,
    OfficialRawBlob,
    RelatedBill,
)

PROCESSING_VERSION = "status_derivation_evidence/1"
MAX_BLOB_BYTES = 8 * 1024 * 1024
MAX_ACTIONS_PER_BILL = 1_000
MAX_RELATIONS_PER_BILL = 1_000


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _store_blob(db: OrmSession, data: bytes) -> str:
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_BLOB_BYTES:
        raise ValueError("derived status evidence must be non-empty bytes within the storage cap")
    sha256 = hashlib.sha256(data).hexdigest()
    db.execute(
        insert(OfficialRawBlob)
        .values(sha256=sha256, data=data, content_type="application/json")
        .on_conflict_do_nothing(index_elements=(OfficialRawBlob.sha256,))
    )
    stored = db.get(OfficialRawBlob, sha256)
    if stored is None or hashlib.sha256(stored.data).hexdigest() != sha256:
        raise RuntimeError("derived status evidence blob integrity check failed")
    return sha256


def _date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def snapshot_state(db: OrmSession, bill_ids: list[object]) -> dict[object, dict[str, Any]]:
    """Capture only the semantic fields this derivation can mutate."""
    if not bill_ids:
        return {}
    # Query scalar columns rather than ORM ``Bill`` instances. Status is
    # updated with raw SQL in the caller, which deliberately does not refresh
    # any already-loaded identity-map instance.
    bills = {
        row.id: row.status
        for row in db.execute(
            select(Bill.id, Bill.status).where(Bill.id.in_(bill_ids))
        ).all()
    }
    rows = db.execute(
        select(RelatedBill)
        .where(
            RelatedBill.bill_id.in_(bill_ids),
            RelatedBill.relation_type == "substituted-by",
        )
        .order_by(RelatedBill.bill_id, RelatedBill.created_at, RelatedBill.id)
    ).scalars().all()
    relations: dict[object, list[dict[str, Any]]] = {bill_id: [] for bill_id in bill_ids}
    for relation in rows:
        records = relations.setdefault(relation.bill_id, [])
        if len(records) >= MAX_RELATIONS_PER_BILL:
            raise ValueError("derived status evidence relation input exceeds configured record cap")
        records.append(
            {
                "id": str(relation.id),
                "related_bill_id": str(relation.related_bill_id) if relation.related_bill_id else None,
                "related_identifier": relation.related_identifier,
                "relation_type": relation.relation_type,
            }
        )
    return {
        bill_id: {
            "bill": {"id": str(bill_id), "status": bills[bill_id]},
            "substitution_relations": relations.get(bill_id, []),
        }
        for bill_id in bill_ids
        if bill_id in bills
    }


def derivation_input(
    *,
    bill_id: object,
    session_id: object | None,
    session_end_date: date | None,
    session_active: bool,
    session_has_recent_activity: bool,
    as_of_date: date,
    actions: list[dict[str, Any]],
    consulted_relations: list[dict[str, Any]],
    resolved_survivor: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the bounded, canonical local inputs used for one bill."""
    if len(actions) > MAX_ACTIONS_PER_BILL:
        raise ValueError("derived status evidence action input exceeds configured record cap")
    if len(consulted_relations) > MAX_RELATIONS_PER_BILL:
        raise ValueError("derived status evidence relation input exceeds configured record cap")
    return {
        "as_of_date": as_of_date.isoformat(),
        "bill_id": str(bill_id),
        "session": {
            "id": str(session_id) if session_id else None,
            "end_date": _date(session_end_date),
            "active": bool(session_active),
            "has_recent_chamber_activity": bool(session_has_recent_activity),
        },
        "actions": actions,
        "consulted_substitution_relations": consulted_relations,
        "resolved_survivor": resolved_survivor,
    }


def record_changes(
    db: OrmSession,
    *,
    before_by_bill: dict[object, dict[str, Any]],
    after_by_bill: dict[object, dict[str, Any]],
    inputs_by_bill: dict[object, dict[str, Any]],
    causal_evidence_by_bill: dict[object, object] | None,
    derived_at: datetime,
) -> int:
    """Record changed status/relation states in the caller's transaction."""
    if derived_at.tzinfo is None:
        derived_at = derived_at.replace(tzinfo=timezone.utc)
    else:
        derived_at = derived_at.astimezone(timezone.utc)
    recorded = 0
    for bill_id, before in before_by_bill.items():
        after = after_by_bill.get(bill_id)
        if after is None or _canonical_json_bytes(before) == _canonical_json_bytes(after):
            continue
        causal_id = (causal_evidence_by_bill or {}).get(bill_id)
        if causal_id is not None and db.get(CorpusUpdateEvidence, causal_id) is None:
            raise ValueError("derived status evidence causal corpus record is absent")
        evidence = DerivedStatusEvidence(
            bill_id=bill_id,
            causal_corpus_update_evidence_id=causal_id,
            derivation_input_sha256=_store_blob(db, _canonical_json_bytes(inputs_by_bill[bill_id])),
            before_snapshot_sha256=_store_blob(db, _canonical_json_bytes(before)),
            after_snapshot_sha256=_store_blob(db, _canonical_json_bytes(after)),
            processing_version=PROCESSING_VERSION,
            changed_components=(
                ["status", "substitution_relations"]
                if before["bill"]["status"] != after["bill"]["status"]
                and before["substitution_relations"] != after["substitution_relations"]
                else ["status"]
                if before["bill"]["status"] != after["bill"]["status"]
                else ["substitution_relations"]
            ),
            derived_at=derived_at,
        )
        db.add(evidence)
        recorded += 1
    db.flush()
    return recorded
