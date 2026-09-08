"""Replayable local evidence for derived bill status and substitution mutations.

This is intentionally separate from ``CorpusUpdateEvidence``.  Status is
computed from local corpus inputs; it must never be represented as a newly
fetched upstream assertion.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from billcommons_ingest import status as status_mod
from billcommons_shared.normalize import normalize_bill_number

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

PROCESSING_VERSION = "status_derivation_evidence/2"
# Increment when the local status/substitution algorithm changes in a way that
# makes an old input record replay under different semantics.
ALGORITHM_VERSION = "status-recompute/2"
REPLAY_INPUT_VERSION = "derived-status-replay/2"
SESSION_ACTIVITY_SNAPSHOT_VERSION = "session-activity/1"
SURVIVOR_STATUS_SNAPSHOT_VERSION = "survivor-status/1"
MAX_BLOB_BYTES = 8 * 1024 * 1024
# The maintenance command's established default is 2,000 bills. Keep the
# derivation entry point within that published unit of work regardless of a
# caller-provided command-line chunk value.
MAX_BILLS_PER_RECOMPUTE = 2_000
MAX_ACTIONS_PER_BILL = 1_000
MAX_RELATIONS_PER_BILL = 1_000
MAX_ACTION_ROWS_PER_RECOMPUTE = 100_000
MAX_RELATION_ROWS_PER_RECOMPUTE = 100_000
STREAM_FETCH_SIZE = 500


class DerivationInputLimitExceeded(ValueError):
    """A bounded evidence input cannot safely represent this recompute unit."""


class DerivedStatusReplayError(ValueError):
    """A retained record cannot be replayed as a self-contained derivation."""


def algorithm_source_sha256() -> str:
    """Fingerprint the source files that execute the persisted algorithm."""
    package_dir = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ("status.py", "cli.py", "derived_status_evidence.py"):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((package_dir / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DerivedStatusReplayError("invalid ISO date in derivation input") from exc


def _normalized_identifier(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return normalize_bill_number(value)
    except ValueError:
        return None


def _relation_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    created_at = row.get("created_at")
    relation_id = row.get("id")
    if not isinstance(created_at, str) or not isinstance(relation_id, str):
        raise DerivedStatusReplayError("relation replay input is missing created_at or id")
    return (created_at, relation_id)


def _semantic_relations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"related_bill_id": row.get("related_bill_id"), "related_identifier": row.get("related_identifier"), "relation_type": row.get("relation_type")}
        for row in sorted(rows, key=_relation_sort_key)
    ]


def replay_derivation(derivation_input_data: dict[str, Any], before_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Recompute one v2 mutation using retained JSON only, never a database."""
    if derivation_input_data.get("replay_input_version") != REPLAY_INPUT_VERSION:
        raise DerivedStatusReplayError("derivation input predates offline replay support")
    if derivation_input_data.get("algorithm_version") != ALGORITHM_VERSION:
        raise DerivedStatusReplayError("unsupported derived-status algorithm version")
    if derivation_input_data.get("algorithm_source_sha256") != algorithm_source_sha256():
        raise DerivedStatusReplayError("local derived-status source does not match retained input")
    session, external = derivation_input_data.get("session"), derivation_input_data.get("external_snapshots")
    if not isinstance(session, dict) or not isinstance(external, dict):
        raise DerivedStatusReplayError("derivation input is missing session or external snapshots")
    activity = external.get("session_activity")
    if not isinstance(activity, dict) or activity.get("version") != SESSION_ACTIVITY_SNAPSHOT_VERSION:
        raise DerivedStatusReplayError("derivation input is missing versioned session activity snapshot")
    if activity.get("has_recent_chamber_activity") is not session.get("has_recent_chamber_activity"):
        raise DerivedStatusReplayError("session activity snapshot disagrees with session input")
    if activity.get("as_of_date") != derivation_input_data.get("as_of_date"):
        raise DerivedStatusReplayError("session activity snapshot has the wrong effective date")
    if not isinstance(activity.get("window_days"), int) or not isinstance(activity.get("classification_patterns"), list):
        raise DerivedStatusReplayError("session activity snapshot is missing policy inputs")
    actions = derivation_input_data.get("actions")
    if not isinstance(actions, list):
        raise DerivedStatusReplayError("derivation input actions are missing")
    replayed_actions = [status_mod.ActionRow(action_date=_parse_date(row.get("action_date")), classification=row.get("classification"), description=row.get("description"), organization_id=row.get("organization_id"), order=row.get("order"), source_name=row.get("source_name")) for row in actions]
    base_status = status_mod.apply_session_outcome(status_mod.derive_status(replayed_actions), _parse_date(session.get("end_date")), today=_parse_date(derivation_input_data.get("as_of_date")), session_active=bool(session.get("active")), session_has_recent_activity=bool(activity.get("has_recent_chamber_activity")))
    before_relations = before_snapshot.get("substitution_relations")
    consulted = derivation_input_data.get("consulted_substitution_relations")
    if not isinstance(before_relations, list) or consulted != before_relations:
        raise DerivedStatusReplayError("consulted relations do not match the retained before snapshot")
    ordered_relations = sorted(consulted, key=_relation_sort_key)
    text_target = None
    for action in replayed_actions:
        target = status_mod.substitution_target(action.description)
        if target:
            text_target = target
    selected_identifier, selected_bill_id = text_target, None
    if text_target is None:
        for row in ordered_relations:
            if row.get("related_bill_id") is not None:
                selected_bill_id = row["related_bill_id"]
            elif row.get("related_identifier"):
                normalized = _normalized_identifier(row.get("related_identifier"))
                if normalized is not None:
                    selected_identifier = normalized
    else:
        for row in ordered_relations:
            if row.get("related_bill_id") is not None and _normalized_identifier(row.get("related_identifier")) == text_target:
                selected_bill_id = row["related_bill_id"]
    lookup = derivation_input_data.get("substitution_lookup")
    if not isinstance(lookup, dict) or lookup.get("version") != "substitution-lookup/1":
        raise DerivedStatusReplayError("derivation input is missing versioned substitution lookup")
    jurisdiction = lookup.get("jurisdiction")
    if not isinstance(jurisdiction, dict) or not isinstance(jurisdiction.get("abbreviation"), str):
        raise DerivedStatusReplayError("substitution lookup is missing jurisdiction policy input")
    if bool(jurisdiction.get("print_suffix")) != (jurisdiction["abbreviation"].upper() == "NY"):
        raise DerivedStatusReplayError("substitution lookup jurisdiction policy is inconsistent")
    if selected_bill_id is None and selected_identifier is not None:
        expected_candidates = status_mod.substitution_lookup_candidates(selected_identifier, print_suffix=bool(jurisdiction["print_suffix"]))
        ranks = lookup.get("ranks")
        if not isinstance(ranks, list) or [r.get("identifier") for r in ranks] != expected_candidates:
            raise DerivedStatusReplayError("substitution lookup candidates do not match the retained policy")
        for rank, expected_identifier in enumerate(expected_candidates):
            candidates = ranks[rank].get("candidates")
            if not isinstance(candidates, list):
                raise DerivedStatusReplayError("substitution lookup rank is missing ordered candidates")
            for candidate in candidates:
                if candidate.get("identifier") != expected_identifier:
                    raise DerivedStatusReplayError("substitution lookup candidate has the wrong identifier")
                if candidate.get("source") not in {"chunk", "database"}:
                    raise DerivedStatusReplayError("substitution lookup candidate has an invalid source")
                candidate_id = candidate.get("bill_id")
                if not isinstance(candidate_id, str):
                    raise DerivedStatusReplayError("substitution lookup candidate is missing bill id")
                if candidate_id != derivation_input_data.get("bill_id"):
                    selected_bill_id = candidate_id
                    break
            if selected_bill_id is not None:
                break
    decision = derivation_input_data.get("substitution_decision")
    if not isinstance(decision, dict):
        raise DerivedStatusReplayError("derivation input is missing substitution decision")
    expected_decision = {"base_status": base_status, "text_derived_identifier": text_target, "selected_identifier": selected_identifier, "selected_survivor_bill_id": selected_bill_id}
    for key, expected in expected_decision.items():
        if decision.get(key) != expected:
            raise DerivedStatusReplayError(f"saved substitution decision has the wrong {key}")
    final_status = base_status
    if selected_identifier is not None or selected_bill_id is not None:
        survivor = derivation_input_data.get("resolved_survivor")
        if not isinstance(survivor, dict) or survivor.get("bill_id") != selected_bill_id:
            raise DerivedStatusReplayError("resolved survivor does not match the replayed selection")
        survivor_snapshot = external.get("survivor_status")
        if not isinstance(survivor_snapshot, dict) or survivor_snapshot.get("version") != SURVIVOR_STATUS_SNAPSHOT_VERSION:
            raise DerivedStatusReplayError("derivation input is missing versioned survivor status snapshot")
        if survivor_snapshot.get("bill_id") != selected_bill_id or survivor_snapshot.get("status") != survivor.get("status"):
            raise DerivedStatusReplayError("survivor status snapshot disagrees with resolved survivor")
        final_status = survivor.get("status") if survivor.get("status") in status_mod.TERMINAL_STATUSES else status_mod.SUBSTITUTED
    if decision.get("final_status") != final_status:
        raise DerivedStatusReplayError("saved substitution decision has the wrong final status")
    after_relations = list(ordered_relations)
    if text_target is not None:
        matching = [r for r in after_relations if _normalized_identifier(r.get("related_identifier")) == text_target]
        current = next((r for r in reversed(matching) if r.get("related_bill_id") is not None), matching[0] if matching else None)
        after_relations = [r for r in after_relations if r is current]
        if current is None:
            after_relations.append({"id": "new", "created_at": "9999-12-31T23:59:59+00:00", "related_bill_id": selected_bill_id, "related_identifier": text_target, "relation_type": "substituted-by"})
        elif current.get("related_bill_id") is None and selected_bill_id is not None:
            current = dict(current); current["related_bill_id"] = selected_bill_id; after_relations = [current]
    return {"bill": {"id": before_snapshot.get("bill", {}).get("id"), "status": final_status}, "substitution_relations": _semantic_relations(after_relations)}


def assert_replay_matches_after(derivation_input_data: dict[str, Any], before_snapshot: dict[str, Any], after_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return pure replay output or raise if it differs from retained after-state."""
    replayed = replay_derivation(derivation_input_data, before_snapshot)
    expected = {"bill": {"id": after_snapshot.get("bill", {}).get("id"), "status": after_snapshot.get("bill", {}).get("status")}, "substitution_relations": _semantic_relations(after_snapshot.get("substitution_relations", []))}
    if replayed != expected:
        raise DerivedStatusReplayError("offline replay does not match retained after snapshot")
    return replayed


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
    if len(bill_ids) > MAX_BILLS_PER_RECOMPUTE:
        raise DerivationInputLimitExceeded("derived status evidence bill batch exceeds configured cap")
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
        .execution_options(stream_results=True)
    ).scalars().yield_per(STREAM_FETCH_SIZE)
    relations: dict[object, list[dict[str, Any]]] = {bill_id: [] for bill_id in bill_ids}
    total_rows = 0
    for relation in rows:
        total_rows += 1
        if total_rows > MAX_RELATION_ROWS_PER_RECOMPUTE:
            raise DerivationInputLimitExceeded(
                "derived status evidence relation batch exceeds configured record cap"
            )
        records = relations.setdefault(relation.bill_id, [])
        if len(records) >= MAX_RELATIONS_PER_BILL:
            raise DerivationInputLimitExceeded(
                "derived status evidence relation input exceeds configured record cap"
            )
        records.append(
            {
                "id": str(relation.id),
                "created_at": relation.created_at.astimezone(timezone.utc).isoformat() if relation.created_at.tzinfo else relation.created_at.replace(tzinfo=timezone.utc).isoformat(),
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
    substitution_decision: dict[str, Any] | None,
    substitution_lookup: dict[str, Any],
    session_activity_snapshot: dict[str, Any],
    survivor_status_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Build the bounded, canonical local inputs used for one bill."""
    if len(actions) > MAX_ACTIONS_PER_BILL:
        raise DerivationInputLimitExceeded(
            "derived status evidence action input exceeds configured record cap"
        )
    if len(consulted_relations) > MAX_RELATIONS_PER_BILL:
        raise DerivationInputLimitExceeded(
            "derived status evidence relation input exceeds configured record cap"
        )
    return {
        "replay_input_version": REPLAY_INPUT_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "algorithm_source_sha256": algorithm_source_sha256(),
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
        "substitution_decision": substitution_decision,
        "substitution_lookup": substitution_lookup,
        "external_snapshots": {
            "session_activity": session_activity_snapshot,
            "survivor_status": survivor_status_snapshot,
        },
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
        if causal_id is not None:
            causal_record = db.get(CorpusUpdateEvidence, causal_id)
            if causal_record is None:
                raise ValueError("derived status evidence causal corpus record is absent")
            if causal_record.bill_id != bill_id:
                raise ValueError("derived status evidence causal corpus record belongs to another bill")
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
