"""Fail-closed one-off repair for California's 2025--26 session collision.

This module deliberately is *not* a general-purpose importer.  An early CA
load keyed measures only by identifier, so the 24 Special Session 1 measures
(``AB 1``, ``SB 1`` …) merged child collections into the public UUIDs of their
regular-session counterparts. Twenty-three still carried the special upstream
ID; AB 2 had later been re-keyed to its regular ID without separating its
children. The repair is constrained to the independently pinned daily
California ``pubinfo`` archive and frozen pre-repair production manifest:

* 24 existing regular UUIDs are retained and rebuilt from regular v3 payloads;
* 24 special measures and 13 other official-only regular measures are added;
* every affected child collection is replaced from a complete, staged v3
  payload inside one bounded transaction.

Network and ZIP work happen before the transaction begins.  ``--apply`` is
therefore the only operation that can write, and it refuses an unexpected
source hash, identity/count mismatch, concurrent repair lock, or an implicit
loss of extracted text.  This is intentionally kept separate from normal
incremental ingestion: it must be deleted after the audited repair has been
performed, rather than becoming a tempting broad mutation API.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import uuid
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

from sqlalchemy import String, cast, delete, func, select, text
from sqlalchemy.orm import Session as OrmSession

from billcommons_ingest import events, status
from billcommons_schema.models import (
    Bill,
    BillAction,
    BillDocument,
    BillEvent,
    BillIdentifier,
    BillSubject,
    BillVersion,
    IngestJob,
    Jurisdiction,
    Organization,
    Person,
    RelatedBill,
    SearchDocument,
    Session as SessionModel,
    Sponsorship,
    VoteEvent,
    VoteRecord,
)
from billcommons_shared.normalize import normalize_bill_number


SOURCE_NAME = "ca_session_collision_repair/2026-09-02"
OFFICIAL_ZIP_SHA256 = "fbdeaf33b805c9094d52a1b11bbbe3981ae31b78a2a82af791b2a94f1bd0e1c2"
MANIFEST_SHA256 = "ee278bed64658bb75b665191ca2b5c6b7133f6be93d1cffe5d384cd249918c6b"
OFFICIAL_SOURCE_URL = "https://downloads.leginfo.legislature.ca.gov/pubinfo_daily_Wed.zip"
OFFICIAL_ETAG = '"4449daed-65a7e451a6200"'
OFFICIAL_LAST_MODIFIED = "Wed, 02 Sep 2026 11:23:52 GMT"
REGULAR_SESSION_IDENTIFIER = "2025-2026 Regular Session"
SPECIAL_SESSION_IDENTIFIER = "2025-2026 Special Session 1"
REGULAR_OPENSTATES_SESSION = "20252026"
SPECIAL_OPENSTATES_SESSION = "20252026 Special Session 1"
CA_ABBREVIATION = "CA"
# Official ACR 1, not Open States' contradictory session estimate, records
# final adjournment on 2025-02-03.  The extraordinary session convened on
# 2024-12-02; keeping these facts here prevents the special bills from being
# presented as still live after the repair.
SPECIAL_SESSION_START = date(2024, 12, 2)
SPECIAL_SESSION_END = date(2025, 2, 3)
REGULAR_SESSION_START = date(2024, 12, 2)
REGULAR_SESSION_END = date(2026, 11, 30)

# New, dedicated transaction lock.  It is deliberately unrelated to scheduler
# locks; only this one-off repair must serialize with itself.
CA_SESSION_COLLISION_REPAIR_LOCK = 8_104_206_092
LOCK_TIMEOUT_MS = 5_000
STATEMENT_TIMEOUT_MS = 120_000
# ``subject`` is a bill scalar/list in v3; it is not a valid ``include``
# token.  Asking for ``subjects`` makes a real request fail before staging.
FULL_INCLUDE = [
    "sponsorships", "actions", "sources", "versions", "documents", "abstracts",
    "votes", "other_identifiers", "related_bills",
]

BILL_COLUMNS = (
    "bill_id", "session_year", "session_num", "measure_type", "measure_num",
    "measure_state", "chapter_year", "chapter_type", "chapter_session_num",
    "chapter_num", "latest_bill_version_id", "active_flg", "trans_uid",
    "trans_update", "current_location", "current_secondary_loc", "current_house",
    "current_status", "days_31st_in_print",
)


class CollisionRepairError(RuntimeError):
    """Safe operator-facing failure: no partial repair has been committed."""


class BillClient(Protocol):
    def search_bills(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_bill(self, openstates_id: str, *, include: list[str] | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class OfficialMeasure:
    ca_bill_id: str
    identifier: str


@dataclass(frozen=True)
class RepairScope:
    contaminated_regular: tuple[OfficialMeasure, ...]
    # Frozen public UUID mapping from the pre-sweep manifest.  It prevents a
    # coincidentally matching identifier from making this tool preserve the
    # wrong public record after an intervening manual repair.
    retained_regular_identities: tuple[tuple[str, uuid.UUID, str], ...]
    missing_regular: tuple[OfficialMeasure, ...]
    special: tuple[OfficialMeasure, ...]
    source_fingerprint: str

    @property
    def total(self) -> int:
        return len(self.contaminated_regular) + len(self.missing_regular) + len(self.special)

    @property
    def retained_uuid_by_identifier(self) -> dict[str, uuid.UUID]:
        return {identifier: bill_id for identifier, bill_id, _ in self.retained_regular_identities}

    @property
    def retained_old_openstates_id_by_identifier(self) -> dict[str, str]:
        return {identifier: old_id for identifier, _, old_id in self.retained_regular_identities}


@dataclass(frozen=True)
class StagedBill:
    measure: OfficialMeasure
    session_slug: str
    kind: str  # retained_regular, missing_regular, special
    payload: dict[str, Any]


@dataclass(frozen=True)
class RepairPlan:
    scope: RepairScope
    bills: tuple[StagedBill, ...]
    payload_fingerprint: str = ""


@dataclass(frozen=True)
class RepairResult:
    retained_regular: int
    created_regular: int
    created_special: int
    rebuilt_search_documents: int
    reset_fulltext_documents: int
    already_applied: bool = False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identifier(measure_type: str, measure_num: str) -> str:
    measure_type = measure_type.strip().upper()
    try:
        number = str(int(measure_num))
    except ValueError as exc:
        raise CollisionRepairError(f"official measure has nonnumeric number {measure_num!r}") from exc
    if not measure_type:
        raise CollisionRepairError("official measure has blank type")
    return f"{measure_type} {number}"


def _read_official_rows(zip_path: Path) -> list[dict[str, str]]:
    if not zip_path.is_file():
        raise CollisionRepairError(f"official zip is not a readable file: {zip_path}")
    if _sha256_file(zip_path) != OFFICIAL_ZIP_SHA256:
        raise CollisionRepairError("official ZIP SHA-256 does not match the pinned 2026-09-02 artifact")
    try:
        with zipfile.ZipFile(zip_path) as archive, archive.open("BILL_TBL.dat") as member:
            reader = csv.reader(io.TextIOWrapper(member, encoding="utf-8", errors="strict", newline=""), delimiter="\t", quotechar="`")
            rows: list[dict[str, str]] = []
            for line, values in enumerate(reader, start=1):
                if not values:
                    continue
                if len(values) != len(BILL_COLUMNS):
                    raise CollisionRepairError(f"BILL_TBL.dat:{line} has {len(values)} fields; expected {len(BILL_COLUMNS)}")
                rows.append(dict(zip(BILL_COLUMNS, values, strict=True)))
            return rows
    except KeyError as exc:
        raise CollisionRepairError("official ZIP is missing BILL_TBL.dat") from exc
    except zipfile.BadZipFile as exc:
        raise CollisionRepairError("official ZIP is not a valid archive") from exc


def _read_manifest(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        raise CollisionRepairError(f"production manifest is not a readable file: {path}")
    if _sha256_file(path) != MANIFEST_SHA256:
        raise CollisionRepairError("production manifest SHA-256 does not match the frozen pre-sweep artifact")
    with path.open("r", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        needed = {"production_bill_id", "ca_bill_id", "identifier_norm", "openstates_id"}
        if not reader.fieldnames or not needed.issubset(reader.fieldnames):
            raise CollisionRepairError("production manifest is missing required CA reconciliation columns")
        result: dict[str, dict[str, str]] = {}
        for line, row in enumerate(reader, start=2):
            ca_bill_id = (row.get("ca_bill_id") or "").strip()
            if not ca_bill_id or ca_bill_id in result:
                raise CollisionRepairError(f"production manifest has invalid/duplicate ca_bill_id at line {line}")
            result[ca_bill_id] = {key: (value or "").strip() for key, value in row.items() if key}
        return result


def derive_scope(*, zip_path: Path, manifest_path: Path) -> RepairScope:
    """Derive the immutable 24 + 13 + 24 identity scope from pinned inputs.

    No mutable operator list is accepted: a mismatch in today's production
    universe is a stop condition, not an invitation to repair a different
    number of bills.
    """
    rows = _read_official_rows(zip_path)
    manifest = _read_manifest(manifest_path)
    regular: dict[str, OfficialMeasure] = {}
    special: list[OfficialMeasure] = []
    for row in rows:
        if row["session_year"] != "20252026":
            continue
        item = OfficialMeasure(row["bill_id"], _identifier(row["measure_type"], row["measure_num"]))
        if row["session_num"] == "0":
            if item.ca_bill_id in regular:
                raise CollisionRepairError(f"duplicate regular official CA bill {item.ca_bill_id}")
            regular[item.ca_bill_id] = item
        elif row["session_num"] == "1":
            special.append(item)
    if len(special) != 24:
        raise CollisionRepairError(f"official special-session count is {len(special)}; expected exactly 24")
    if len({item.identifier for item in special}) != 24:
        raise CollisionRepairError("official special session has duplicate normalized identifiers")

    # Existing local rows map through the special official document identity,
    # while their public UUIDs/identifiers belong to the matching regular
    # measure. Their current Open States ID is pinned separately because AB 2
    # had already been re-keyed to regular while its children stayed merged.
    contaminated = [item for item in special if item.ca_bill_id in manifest]
    if len(contaminated) != 24:
        raise CollisionRepairError(
            f"frozen manifest contains {len(contaminated)} special IDs; expected all 24 contaminated rows"
        )
    if any(manifest[item.ca_bill_id]["identifier_norm"] != _normalise_identifier(item.identifier) for item in contaminated):
        raise CollisionRepairError("frozen manifest special identifiers do not match the official collision identities")
    try:
        retained_regular_identities = tuple(sorted(
            (
                (
                    _normalise_identifier(item.identifier),
                    uuid.UUID(manifest[item.ca_bill_id]["production_bill_id"]),
                    manifest[item.ca_bill_id]["openstates_id"],
                )
                for item in contaminated
            ),
            key=lambda item: item[0],
        ))
    except (KeyError, ValueError, AttributeError) as exc:
        raise CollisionRepairError("frozen manifest has an invalid retained public UUID") from exc
    if any(not old_id for _, _, old_id in retained_regular_identities):
        raise CollisionRepairError("frozen manifest has a blank retained Open States id")
    if len({value for _, value, _ in retained_regular_identities}) != 24:
        raise CollisionRepairError("frozen manifest retained public UUIDs are not one-to-one")
    if len({value for _, _, value in retained_regular_identities}) != 24:
        raise CollisionRepairError("frozen manifest retained Open States ids are not one-to-one")
    counterpart_ids = {
        f"202520260{item.identifier.replace(' ', '', 1)}" for item in special
    }
    if not counterpart_ids.issubset(regular):
        raise CollisionRepairError("one or more special identifiers lack an official regular-session counterpart")
    official_only = set(regular) - set(manifest)
    if len(official_only) != 37:
        raise CollisionRepairError(f"official-only regular measure count is {len(official_only)}; expected exactly 37")
    missing_regular_ids = official_only - counterpart_ids
    if len(missing_regular_ids) != 13:
        raise CollisionRepairError(f"unrelated missing regular count is {len(missing_regular_ids)}; expected exactly 13")
    if official_only & (set(manifest) - counterpart_ids):
        raise CollisionRepairError("internal scope calculation error")

    fingerprint = hashlib.sha256(json.dumps({
        "official_zip_sha256": OFFICIAL_ZIP_SHA256,
        "manifest_sha256": MANIFEST_SHA256,
        "etag": OFFICIAL_ETAG,
        "last_modified": OFFICIAL_LAST_MODIFIED,
        "special": sorted(item.ca_bill_id for item in special),
        "missing_regular": sorted(missing_regular_ids),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return RepairScope(
        contaminated_regular=tuple(sorted((regular[f"202520260{item.identifier.replace(' ', '', 1)}"] for item in special), key=lambda item: item.ca_bill_id)),
        retained_regular_identities=retained_regular_identities,
        missing_regular=tuple(sorted((regular[key] for key in missing_regular_ids), key=lambda item: item.ca_bill_id)),
        special=tuple(sorted(special, key=lambda item: item.ca_bill_id)),
        source_fingerprint=fingerprint,
    )


def _normalise_identifier(value: str) -> str:
    try:
        return normalize_bill_number(value)
    except ValueError as exc:
        raise CollisionRepairError(f"upstream payload has unnormalizable identifier {value!r}") from exc


def _normalise_alternate_identifier(value: str) -> str:
    """Match the bulk importer's deliberate opaque-ID fallback."""
    try:
        return normalize_bill_number(value)
    except ValueError:
        return value.upper().strip()


def _stage_one(client: BillClient, *, measure: OfficialMeasure, session_slug: str, kind: str) -> StagedBill:
    listing = client.search_bills(
        jurisdiction="ocd-jurisdiction/country:us/state:ca/government",
        session=session_slug,
        identifier=measure.identifier,
        include=FULL_INCLUDE,
        per_page=20,
    )
    results = listing.get("results") if isinstance(listing, dict) else None
    if not isinstance(results, list) or len(results) != 1:
        raise CollisionRepairError(
            f"Open States identity lookup for {session_slug!r} {measure.identifier!r} returned "
            f"{len(results) if isinstance(results, list) else 'non-list'} result(s), expected exactly one"
        )
    pagination = listing.get("pagination")
    if not isinstance(pagination, dict) or pagination.get("max_page") != 1:
        raise CollisionRepairError(
            f"Open States identity lookup for {session_slug!r} {measure.identifier!r} "
            "did not prove a single result page"
        )
    candidate = results[0]
    upstream_id = candidate.get("id") if isinstance(candidate, dict) else None
    if not isinstance(upstream_id, str) or not upstream_id:
        raise CollisionRepairError(f"Open States lookup for {measure.identifier!r} has no bill id")
    # The v3 search endpoint returns the complete included bill object. Using
    # that exact response avoids a second paid/rate-limited request per bill
    # and, more importantly, avoids mixing two snapshots if the source changes
    # between a list request and a detail request.
    payload = candidate
    if not isinstance(payload, dict):
        raise CollisionRepairError(f"Open States full payload for {measure.identifier!r} is not an object")
    if payload.get("id") != upstream_id or payload.get("session") != session_slug:
        raise CollisionRepairError(f"Open States full payload identity/session mismatch for {measure.identifier!r}")
    identifier = payload.get("identifier")
    if not isinstance(identifier, str) or _normalise_identifier(identifier) != _normalise_identifier(measure.identifier):
        raise CollisionRepairError(f"Open States full payload identifier mismatch for {measure.identifier!r}")
    # A complete payload must state every collection we replace.  Missing
    # fields are not silently treated as an empty collection.
    for field in (
        "actions", "sponsorships", "versions", "documents", "votes", "abstracts",
        "sources", "other_identifiers", "related_bills",
    ):
        if field not in payload or not isinstance(payload[field], list):
            raise CollisionRepairError(f"Open States full payload for {measure.identifier!r} lacks list {field!r}")
    if "subject" not in payload or not isinstance(payload["subject"], list):
        raise CollisionRepairError(f"Open States full payload for {measure.identifier!r} lacks list 'subject'")
    if _source_url(payload) is None:
        raise CollisionRepairError(f"Open States full payload for {measure.identifier!r} lacks a source URL")
    return StagedBill(measure=measure, session_slug=session_slug, kind=kind, payload=payload)


def stage_repair(client: BillClient, scope: RepairScope) -> RepairPlan:
    """Fetch all complete verified payloads before any DB write occurs."""
    staged: list[StagedBill] = []
    for item in scope.contaminated_regular:
        staged.append(_stage_one(client, measure=item, session_slug=REGULAR_OPENSTATES_SESSION, kind="retained_regular"))
    for item in scope.missing_regular:
        staged.append(_stage_one(client, measure=item, session_slug=REGULAR_OPENSTATES_SESSION, kind="missing_regular"))
    for item in scope.special:
        staged.append(_stage_one(client, measure=item, session_slug=SPECIAL_OPENSTATES_SESSION, kind="special"))
    if len(staged) != 61 or len({(item.session_slug, item.measure.identifier) for item in staged}) != 61:
        raise CollisionRepairError("staged payload count/identity invariant failed; expected exactly 61 unique measures")
    _validate_frozen_openstates_mapping(scope, staged)
    bills = tuple(staged)
    return RepairPlan(scope=scope, bills=bills, payload_fingerprint=_payload_fingerprint(bills))


def _validate_frozen_openstates_mapping(
    scope: RepairScope, staged: Iterable[StagedBill]
) -> None:
    by_kind = {
        (item.kind, _normalise_identifier(item.measure.identifier)): item.payload.get("id")
        for item in staged
        if item.kind in {"retained_regular", "special"}
    }
    for identifier, old_id in scope.retained_old_openstates_id_by_identifier.items():
        regular_id = by_kind.get(("retained_regular", identifier))
        special_id = by_kind.get(("special", identifier))
        # 23 rows still carry the special ID. AB 2 was later re-keyed back to
        # its regular ID while its child collections remained a regular/
        # special union. Both are valid frozen pre-state shapes; a third ID is
        # not, and must stop the repair.
        if old_id not in {regular_id, special_id}:
            raise CollisionRepairError(
                f"frozen Open States id for {identifier!r} matches neither exact staged session"
            )


def _payload_fingerprint(bills: tuple[StagedBill, ...]) -> str:
    return hashlib.sha256(json.dumps(
        [item.payload for item in bills],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()).hexdigest()


def save_repair_plan(plan: RepairPlan, path: Path) -> str:
    """Persist the exact staged payloads once, without credentials or URLs with capabilities."""
    document = {
        "version": 1,
        "scope_fingerprint": plan.scope.source_fingerprint,
        "payload_fingerprint": plan.payload_fingerprint or _payload_fingerprint(plan.bills),
        "bills": [
            {
                "ca_bill_id": item.measure.ca_bill_id,
                "identifier": item.measure.identifier,
                "session_slug": item.session_slug,
                "kind": item.kind,
                "payload": item.payload,
            }
            for item in plan.bills
        ],
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise CollisionRepairError(f"refusing to overwrite staged payload bundle: {path}") from exc
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(encoded).hexdigest()


def load_repair_plan(path: Path, scope: RepairScope) -> RepairPlan:
    """Load and revalidate one immutable staged bundle for restore + production."""
    if not path.is_file():
        raise CollisionRepairError(f"staged payload bundle is not a readable file: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CollisionRepairError("staged payload bundle is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("version") != 1:
        raise CollisionRepairError("staged payload bundle has an unsupported version")
    if document.get("scope_fingerprint") != scope.source_fingerprint:
        raise CollisionRepairError("staged payload bundle scope fingerprint does not match pinned inputs")
    expected = {
        (item.ca_bill_id, item.identifier, REGULAR_OPENSTATES_SESSION, "retained_regular"): item
        for item in scope.contaminated_regular
    }
    expected.update({
        (item.ca_bill_id, item.identifier, REGULAR_OPENSTATES_SESSION, "missing_regular"): item
        for item in scope.missing_regular
    })
    expected.update({
        (item.ca_bill_id, item.identifier, SPECIAL_OPENSTATES_SESSION, "special"): item
        for item in scope.special
    })
    rows = document.get("bills")
    if not isinstance(rows, list) or len(rows) != 61:
        raise CollisionRepairError("staged payload bundle does not contain exactly 61 bills")
    staged: list[StagedBill] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CollisionRepairError("staged payload bundle contains a non-object bill")
        key = (row.get("ca_bill_id"), row.get("identifier"), row.get("session_slug"), row.get("kind"))
        if key not in expected or key in seen:
            raise CollisionRepairError("staged payload bundle contains an unexpected/duplicate identity")
        seen.add(key)
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise CollisionRepairError("staged payload bundle contains a non-object payload")
        if payload.get("session") != key[2] or _normalise_identifier(payload.get("identifier", "")) != _normalise_identifier(key[1]):
            raise CollisionRepairError("staged payload bundle payload identity/session mismatch")
        for field in (
            "actions", "sponsorships", "versions", "documents", "votes", "abstracts",
            "sources", "other_identifiers", "related_bills", "subject",
        ):
            if not isinstance(payload.get(field), list):
                raise CollisionRepairError(f"staged payload bundle lacks list {field!r}")
        if _source_url(payload) is None:
            raise CollisionRepairError("staged payload bundle contains a bill without source URL")
        staged.append(StagedBill(expected[key], key[2], key[3], payload))
    if seen != set(expected):
        raise CollisionRepairError("staged payload bundle is missing one or more expected identities")
    bills = tuple(staged)
    plan = RepairPlan(scope=scope, bills=bills, payload_fingerprint=_payload_fingerprint(bills))
    if document.get("payload_fingerprint") is not None and document.get("payload_fingerprint") != plan.payload_fingerprint:
        raise CollisionRepairError("staged payload bundle payload fingerprint does not match its contents")
    _validate_frozen_openstates_mapping(scope, staged)
    return plan


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise CollisionRepairError(f"invalid date value {value!r}")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise CollisionRepairError(f"invalid ISO date {value!r}") from exc


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if not isinstance(value, str):
        raise CollisionRepairError(f"invalid datetime value {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollisionRepairError(f"invalid ISO datetime {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _classification(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return ",".join(item.strip() for item in value if item.strip()) or None
    raise CollisionRepairError(f"invalid classification {value!r}")


def _source_url(payload: dict[str, Any]) -> str | None:
    sources = payload.get("sources") or []
    for source in sources:
        if isinstance(source, dict) and isinstance(source.get("url"), str) and source["url"].strip():
            return source["url"].strip()
    return None


def _abstract(payload: dict[str, Any]) -> str | None:
    for item in payload["abstracts"]:
        if isinstance(item, dict) and isinstance(item.get("abstract"), str) and item["abstract"].strip():
            return item["abstract"].strip()
    return None


def _provenance(plan: RepairPlan) -> dict[str, str]:
    staged_fingerprint = plan.payload_fingerprint or _payload_fingerprint(plan.bills)
    return {
        "source_name": SOURCE_NAME,
        "raw_ref": (
            f"ca-pubinfo:{OFFICIAL_ZIP_SHA256}; manifest:{MANIFEST_SHA256}; "
            f"staged:{staged_fingerprint}"
        ),
        "parser_version": "ca-session-collision-repair/1",
        "license_note": f"official ETag {OFFICIAL_ETAG}; Last-Modified {OFFICIAL_LAST_MODIFIED}; scope {plan.scope.source_fingerprint}",
    }


def _payload_checksum(payload: dict[str, Any]) -> str:
    """Stable checksum for explicit repair provenance, not a normal-sync key."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _delete_children(db: OrmSession, bill_ids: list[uuid.UUID]) -> int:
    version_ids = select(BillVersion.id).where(BillVersion.bill_id.in_(bill_ids))
    vote_ids = select(VoteEvent.id).where(VoteEvent.bill_id.in_(bill_ids))
    extracted = db.execute(select(func.count()).select_from(BillDocument).where(BillDocument.bill_version_id.in_(version_ids), BillDocument.extracted_text.is_not(None))).scalar_one()
    db.execute(delete(VoteRecord).where(VoteRecord.vote_event_id.in_(vote_ids)))
    db.execute(delete(VoteEvent).where(VoteEvent.bill_id.in_(bill_ids)))
    db.execute(delete(BillAction).where(BillAction.bill_id.in_(bill_ids)))
    db.execute(delete(Sponsorship).where(Sponsorship.bill_id.in_(bill_ids)))
    db.execute(delete(BillSubject).where(BillSubject.bill_id.in_(bill_ids)))
    db.execute(delete(BillIdentifier).where(BillIdentifier.bill_id.in_(bill_ids)))
    db.execute(delete(RelatedBill).where(RelatedBill.bill_id.in_(bill_ids)))
    db.execute(delete(BillDocument).where(BillDocument.bill_version_id.in_(version_ids)))
    db.execute(delete(BillVersion).where(BillVersion.bill_id.in_(bill_ids)))
    db.execute(delete(SearchDocument).where(SearchDocument.bill_id.in_(bill_ids)))
    return int(extracted)


def _active_fetch_jobs_for_bills(db: OrmSession, bill_ids: list[uuid.UUID]) -> int:
    if not bill_ids:
        return 0
    return int(db.execute(
        select(func.count())
        .select_from(IngestJob)
        .join(
            BillDocument,
            IngestJob.payload["document_id"].astext == cast(BillDocument.id, String),
        )
        .join(BillVersion, BillVersion.id == BillDocument.bill_version_id)
        .where(
            BillVersion.bill_id.in_(bill_ids),
            IngestJob.kind == "fetch_text",
            IngestJob.status.in_(("queued", "running")),
        )
    ).scalar_one())


def _entity_openstates_id(container: dict[str, Any], key: str) -> str | None:
    nested = container.get(key)
    if nested is None:
        return None
    if not isinstance(nested, dict):
        raise CollisionRepairError(f"invalid nested {key!r} entity")
    upstream_id = nested.get("id")
    if not isinstance(upstream_id, str) or not upstream_id:
        raise CollisionRepairError(f"nested {key!r} entity lacks an Open States id")
    return upstream_id


def _required_entity_maps(
    db: OrmSession, plan: RepairPlan
) -> tuple[dict[str, uuid.UUID], dict[str, uuid.UUID]]:
    organization_ids: set[str] = set()
    person_ids: set[str] = set()
    for staged in plan.bills:
        payload = staged.payload
        for action in payload["actions"]:
            if not isinstance(action, dict):
                raise CollisionRepairError("invalid action payload")
            if value := _entity_openstates_id(action, "organization"):
                organization_ids.add(value)
        for sponsorship in payload["sponsorships"]:
            if not isinstance(sponsorship, dict):
                raise CollisionRepairError("invalid sponsorship payload")
            if value := _entity_openstates_id(sponsorship, "person"):
                person_ids.add(value)
            if value := _entity_openstates_id(sponsorship, "organization"):
                organization_ids.add(value)
        for vote in payload["votes"]:
            if not isinstance(vote, dict):
                raise CollisionRepairError("invalid vote payload")
            if value := _entity_openstates_id(vote, "organization"):
                organization_ids.add(value)
            for record in _records(vote):
                if value := _entity_openstates_id(record, "voter"):
                    person_ids.add(value)
    organizations = {
        upstream_id: row_id
        for upstream_id, row_id in db.execute(
            select(Organization.openstates_id, Organization.id).where(
                Organization.openstates_id.in_(organization_ids)
            )
        )
    }
    people = {
        upstream_id: row_id
        for upstream_id, row_id in db.execute(
            select(Person.openstates_id, Person.id).where(Person.openstates_id.in_(person_ids))
        )
    }
    missing_organizations = organization_ids - set(organizations)
    if missing_organizations:
        raise CollisionRepairError(
            "staged payload references entities absent from the normalized corpus: "
            f"organizations={len(missing_organizations)}"
        )
    return organizations, people


def _subjects(payload: dict[str, Any]) -> list[str]:
    result: set[str] = set()
    raw_subjects = payload.get("subject", payload.get("subjects", []))
    if raw_subjects is None:
        return []
    if isinstance(raw_subjects, str):
        raw_subjects = [raw_subjects]
    if not isinstance(raw_subjects, list):
        raise CollisionRepairError("invalid subject payload")
    for subject in raw_subjects:
        if isinstance(subject, str):
            value = subject.strip()
        elif isinstance(subject, dict):
            raw = subject.get("name") or subject.get("subject")
            value = raw.strip() if isinstance(raw, str) else ""
        else:
            raise CollisionRepairError("invalid subject payload")
        if value:
            result.add(value)
    return sorted(result)


def _records(vote: dict[str, Any]) -> Iterable[dict[str, Any]]:
    raw = vote.get("records")
    if raw is None:
        raw = vote.get("votes")
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(row, dict) for row in raw):
        raise CollisionRepairError("invalid vote records payload")
    return raw


def _vote_tally(vote: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, int]:
    """Prefer official aggregate counts; fall back to the individual roll call.

    Open States commonly returns both.  The aggregate is authoritative even
    where a roll call is omitted (voice votes), while records still become
    VoteRecord rows when available.
    """
    tally = {"yes": 0, "no": 0, "other": 0}
    counts = vote.get("counts")
    if counts is not None:
        if not isinstance(counts, list):
            raise CollisionRepairError("invalid vote counts payload")
        for count in counts:
            if not isinstance(count, dict) or not isinstance(count.get("option"), str):
                raise CollisionRepairError("invalid vote count row")
            value = count.get("value", 0)
            if isinstance(value, bool):
                raise CollisionRepairError("invalid boolean vote count")
            try:
                numeric = int(value)
            except (TypeError, ValueError) as exc:
                raise CollisionRepairError("invalid vote count value") from exc
            if numeric < 0:
                raise CollisionRepairError("negative vote count")
            key = count["option"].lower().strip()
            tally["yes" if key == "yes" else "no" if key == "no" else "other"] += numeric
        return tally
    for record in records:
        option = record.get("option")
        if not isinstance(option, str) or not option.strip():
            raise CollisionRepairError("vote record has blank option")
        key = option.lower().strip()
        tally["yes" if key == "yes" else "no" if key == "no" else "other"] += 1
    return tally


def _rebuild_bill(
    db: OrmSession,
    *,
    bill: Bill,
    staged: StagedBill,
    plan: RepairPlan,
    session: SessionModel,
    retrieved_at: datetime,
    organizations: dict[str, uuid.UUID],
    people: dict[str, uuid.UUID],
) -> None:
    payload = staged.payload
    provenance = _provenance(plan)
    identifier = payload["identifier"].strip()
    identifier_norm = _normalise_identifier(identifier)
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise CollisionRepairError(f"Open States payload {identifier!r} has blank title")
    chamber = payload.get("organization_classification")
    if chamber is None and isinstance(payload.get("from_organization"), dict):
        chamber = payload["from_organization"].get("classification")
    bill.session_id = session.id
    bill.identifier = identifier
    bill.identifier_norm = identifier_norm
    bill.title = title.strip()
    bill.description = _abstract(payload)
    bill.chamber = chamber if isinstance(chamber, str) else None
    bill.bill_type = _classification(payload.get("classification"))
    bill.openstates_id = payload["id"]
    bill.upstream_id = payload["id"]
    bill.source_url = _source_url(payload)
    bill.retrieved_at = retrieved_at
    bill.upstream_updated_at = _parse_datetime(payload.get("updated_at"))
    bill.checksum = _payload_checksum(payload)
    for key, value in provenance.items():
        setattr(bill, key, value)

    actions: list[BillAction] = []
    for fallback_order, action in enumerate(payload["actions"]):
        if not isinstance(action, dict):
            raise CollisionRepairError("invalid action payload")
        description = action.get("description")
        if not isinstance(description, str) or not description.strip():
            raise CollisionRepairError(f"bill {identifier!r} has blank action description")
        raw_order = action.get("order")
        order = raw_order if isinstance(raw_order, int) and not isinstance(raw_order, bool) else fallback_order
        row = BillAction(
            bill_id=bill.id, description=description.strip(), action_date=_parse_date(action.get("date")),
            classification=_classification(action.get("classification")), order=order,
            organization_id=organizations.get(_entity_openstates_id(action, "organization")),
            source_name=SOURCE_NAME, retrieved_at=retrieved_at, raw_ref=provenance["raw_ref"],
        )
        db.add(row)
        actions.append(row)
    latest = max(actions, key=lambda item: (item.action_date or date.min, item.order if item.order is not None else 0), default=None)
    bill.latest_action_date = latest.action_date if latest is not None else None
    bill.latest_action_text = latest.description if latest is not None else None
    introduced = [item for item in actions if "introduction" in (item.classification or "")]
    bill.introduced_date = min((item.action_date for item in introduced if item.action_date is not None), default=None)
    bill.status = status.apply_session_outcome(status.derive_status(actions), session_end_date=session.end_date, session_active=session.active, today=retrieved_at.date())
    bill.status_date = bill.latest_action_date

    for sponsor in payload["sponsorships"]:
        if not isinstance(sponsor, dict):
            raise CollisionRepairError("invalid sponsorship payload")
        name = sponsor.get("name")
        if name is not None and not isinstance(name, str):
            raise CollisionRepairError("invalid sponsor name")
        db.add(Sponsorship(bill_id=bill.id, name=name.strip() if isinstance(name, str) else None,
                           classification=_classification(sponsor.get("classification")), primary=bool(sponsor.get("primary")),
                           person_id=people.get(_entity_openstates_id(sponsor, "person")),
                           organization_id=organizations.get(_entity_openstates_id(sponsor, "organization")),
                           source_name=SOURCE_NAME, retrieved_at=retrieved_at, raw_ref=provenance["raw_ref"]))
    for subject_name in _subjects(payload):
        db.add(BillSubject(bill_id=bill.id, subject=subject_name))
    for alternate in payload["other_identifiers"]:
        if not isinstance(alternate, dict) or not isinstance(alternate.get("identifier"), str):
            raise CollisionRepairError("invalid alternate identifier payload")
        value = alternate["identifier"].strip()
        if not value:
            raise CollisionRepairError("blank alternate identifier payload")
        db.add(BillIdentifier(
            bill_id=bill.id,
            identifier=value,
            identifier_norm=_normalise_alternate_identifier(value),
            scheme=alternate.get("scheme") if isinstance(alternate.get("scheme"), str) else None,
            source_name=SOURCE_NAME,
            retrieved_at=retrieved_at,
            raw_ref=provenance["raw_ref"],
        ))
    for related in payload["related_bills"]:
        if not isinstance(related, dict):
            raise CollisionRepairError("invalid related bill payload")
        related_identifier = related.get("identifier")
        if not isinstance(related_identifier, str) or not related_identifier.strip():
            raise CollisionRepairError("related bill lacks an identifier")
        related_upstream_id = related.get("id")
        related_bill_id = None
        if isinstance(related_upstream_id, str) and related_upstream_id:
            related_bill_id = db.execute(
                select(Bill.id).where(Bill.openstates_id == related_upstream_id)
            ).scalar_one_or_none()
        db.add(RelatedBill(
            bill_id=bill.id,
            related_bill_id=related_bill_id,
            related_identifier=related_identifier.strip(),
            relation_type=_classification(related.get("relation_type") or related.get("classification")),
        ))

    versions: dict[str, BillVersion] = {}
    for version in payload["versions"]:
        if not isinstance(version, dict):
            raise CollisionRepairError("invalid version payload")
        note = version.get("note")
        if note is not None and not isinstance(note, str):
            raise CollisionRepairError("invalid version note")
        row = BillVersion(bill_id=bill.id, note=note.strip() if isinstance(note, str) else None,
                          date=_parse_date(version.get("date")), source_name=SOURCE_NAME, retrieved_at=retrieved_at,
                          raw_ref=provenance["raw_ref"], upstream_id=version.get("id") if isinstance(version.get("id"), str) else None)
        db.add(row)
        if isinstance(version.get("id"), str):
            if version["id"] in versions:
                raise CollisionRepairError("duplicate upstream version id in staged payload")
            versions[version["id"]] = row
        for link in version.get("links") or []:
            if not isinstance(link, dict) or not isinstance(link.get("url"), str) or not link["url"].strip():
                raise CollisionRepairError("invalid version document link")
            db.add(BillDocument(version=row, url=link["url"].strip(), media_type=link.get("media_type") if isinstance(link.get("media_type"), str) else None,
                                fetch_attempts=0, source_name=SOURCE_NAME, retrieved_at=retrieved_at, raw_ref=provenance["raw_ref"]))
    standalone = payload["documents"]
    if standalone:
        placeholder = BillVersion(bill_id=bill.id, note="(document, no version)", date=None, source_name=SOURCE_NAME,
                                  retrieved_at=retrieved_at, raw_ref=provenance["raw_ref"], license_note="synthetic placeholder")
        db.add(placeholder)
        for document in standalone:
            if not isinstance(document, dict):
                raise CollisionRepairError("invalid standalone document payload")
            links = document.get("links") or ([document] if document.get("url") else [])
            if not isinstance(links, list):
                raise CollisionRepairError("invalid standalone document links")
            for link in links:
                if not isinstance(link, dict) or not isinstance(link.get("url"), str) or not link["url"].strip():
                    raise CollisionRepairError("invalid standalone document link")
                db.add(BillDocument(version=placeholder, url=link["url"].strip(), media_type=link.get("media_type") if isinstance(link.get("media_type"), str) else None,
                                    fetch_attempts=0, source_name=SOURCE_NAME, retrieved_at=retrieved_at, raw_ref=provenance["raw_ref"]))

    for vote in payload["votes"]:
        if not isinstance(vote, dict):
            raise CollisionRepairError("invalid vote payload")
        records = list(_records(vote))
        for record in records:
            option = record.get("option")
            if not isinstance(option, str) or not option.strip():
                raise CollisionRepairError("vote record has blank option")
        tally = _vote_tally(vote, records)
        event = VoteEvent(bill_id=bill.id, motion_text=vote.get("motion_text") if isinstance(vote.get("motion_text"), str) else None,
                          motion_classification=_classification(vote.get("motion_classification")), start_date=_parse_date(vote.get("start_date")),
                          result=vote.get("result") if isinstance(vote.get("result"), str) else None,
                          organization_id=organizations.get(_entity_openstates_id(vote, "organization")),
                          yes_count=tally["yes"], no_count=tally["no"], other_count=tally["other"], source_name=SOURCE_NAME,
                          upstream_id=vote.get("id") if isinstance(vote.get("id"), str) else None, retrieved_at=retrieved_at,
                          raw_ref=provenance["raw_ref"], checksum=_payload_checksum(vote))
        db.add(event)
        for record in records:
            db.add(VoteRecord(vote_event=event, voter_name=record.get("voter_name") if isinstance(record.get("voter_name"), str) else None,
                              person_id=people.get(_entity_openstates_id(record, "voter")),
                              option=record["option"].lower().strip()))

    detail = _event_detail(staged, plan)
    if staged.kind == "retained_regular":
        # Existing consumers may cache these collections independently. A
        # metadata-only event would leave their action/sponsor/vote caches
        # carrying the very special-session contamination this repair removes.
        events.record_event(db, bill.id, events.METADATA, detail)
        events.record_event(db, bill.id, events.ACTIONS, detail)
        events.record_event(db, bill.id, events.SPONSORS, detail)
        if payload["votes"]:
            events.record_event(db, bill.id, events.VOTES, detail)
    else:
        events.record_event(db, bill.id, events.CREATED, detail)


def _rebuild_search_document(db: OrmSession, bill: Bill, refreshed_at: datetime) -> None:
    subjects = sorted(db.execute(select(BillSubject.subject).where(BillSubject.bill_id == bill.id)).scalars())
    sponsors = sorted({name for name in db.execute(select(Sponsorship.name).where(Sponsorship.bill_id == bill.id)).scalars() if name})
    db.add(SearchDocument(bill_id=bill.id, jurisdiction_id=bill.jurisdiction_id, identifier_norm=bill.identifier_norm,
                          title=bill.title, summary=bill.description, subjects=subjects, sponsors=sponsors,
                          status=bill.status, latest_action_date=bill.latest_action_date, refreshed_at=refreshed_at))


def _event_detail(staged: StagedBill, plan: RepairPlan) -> str:
    return f"CA session-collision repair ({staged.kind}); scope={plan.scope.source_fingerprint[:12]}"


def _repair_already_applied(
    db: OrmSession,
    plan: RepairPlan,
    *,
    regular: SessionModel,
    special: SessionModel | None,
) -> bool:
    marker = f"%; scope={plan.scope.source_fingerprint[:12]}"
    marked_ids = set(db.execute(
        select(BillEvent.bill_id).where(BillEvent.detail.like(marker))
    ).scalars())
    if not marked_ids:
        return False
    if len(marked_ids) != 61 or special is None:
        raise CollisionRepairError("repair completion markers are partial/inconsistent")
    expected: dict[uuid.UUID, tuple[uuid.UUID, str, str]] = {}
    retained_ids = plan.scope.retained_uuid_by_identifier
    for staged in plan.bills:
        norm = _normalise_identifier(staged.measure.identifier)
        target_session = special if staged.kind == "special" else regular
        if staged.kind == "retained_regular":
            bill_id = retained_ids[norm]
        else:
            bill_id = db.execute(
                select(Bill.id).where(
                    Bill.session_id == target_session.id,
                    Bill.identifier_norm == norm,
                )
            ).scalar_one_or_none()
            if bill_id is None:
                raise CollisionRepairError("repair marker exists but a created target bill is missing")
        expected[bill_id] = (target_session.id, norm, staged.payload["id"])
    rows = db.execute(select(Bill).where(Bill.id.in_(expected))).scalars().all()
    if len(rows) != 61 or any(
        (row.session_id, row.identifier_norm, row.openstates_id) != expected[row.id]
        for row in rows
    ):
        raise CollisionRepairError("repair marker exists but target bill identities do not match the staged plan")
    search_count = db.execute(
        select(func.count()).select_from(SearchDocument).where(SearchDocument.bill_id.in_(expected))
    ).scalar_one()
    if search_count != 61:
        raise CollisionRepairError("repair marker exists but target search materialization is incomplete")
    return True


def preflight_database(db: OrmSession, plan: RepairPlan) -> int:
    """Read-only DB proof that this exact plan still has the expected shape."""
    ca = db.execute(select(Jurisdiction).where(Jurisdiction.abbreviation == CA_ABBREVIATION)).scalar_one_or_none()
    if ca is None:
        raise CollisionRepairError("California jurisdiction is missing")
    regular = db.execute(select(SessionModel).where(SessionModel.jurisdiction_id == ca.id, SessionModel.identifier == REGULAR_SESSION_IDENTIFIER)).scalar_one_or_none()
    if regular is None:
        raise CollisionRepairError("California regular session row is missing")
    if regular.active is not True:
        raise CollisionRepairError("California regular session must remain active during this floor-deadline sweep")
    special = db.execute(select(SessionModel).where(
        SessionModel.jurisdiction_id == ca.id,
        SessionModel.identifier == SPECIAL_SESSION_IDENTIFIER,
    )).scalar_one_or_none()
    if _repair_already_applied(db, plan, regular=regular, special=special):
        return 0
    expected = {_normalise_identifier(item.identifier) for item in plan.scope.contaminated_regular}
    rows = db.execute(select(Bill).where(Bill.session_id == regular.id, Bill.identifier_norm.in_(expected))).scalars().all()
    if len(rows) != 24 or {row.identifier_norm for row in rows} != expected:
        raise CollisionRepairError("existing regular UUID invariant failed: expected exactly 24 contaminated public bills")
    if {row.identifier_norm: row.id for row in rows} != plan.scope.retained_uuid_by_identifier:
        raise CollisionRepairError("existing regular public UUIDs differ from the frozen pre-sweep manifest")
    if {
        row.identifier_norm: row.openstates_id for row in rows
    } != plan.scope.retained_old_openstates_id_by_identifier:
        raise CollisionRepairError("existing regular Open States ids differ from the frozen contaminated mapping")
    bill_ids = [row.id for row in rows]
    if _active_fetch_jobs_for_bills(db, bill_ids):
        raise CollisionRepairError("target documents still have queued/running fetch_text jobs")
    incoming_relations = db.execute(
        select(func.count()).select_from(RelatedBill).where(RelatedBill.related_bill_id.in_(bill_ids))
    ).scalar_one()
    if incoming_relations:
        raise CollisionRepairError("target bills have inbound related_bills rows outside repair ownership")
    frozen_existing_ids = set(plan.scope.retained_old_openstates_id_by_identifier.values())
    desired_new_ids = [
        item.payload["id"] for item in plan.bills
        if item.payload["id"] not in frozen_existing_ids
    ]
    if db.execute(
        select(func.count()).select_from(Bill).where(Bill.openstates_id.in_(desired_new_ids))
    ).scalar_one():
        raise CollisionRepairError("one or more desired regular Open States ids already exist")
    _required_entity_maps(db, plan)
    return int(db.execute(select(func.count()).select_from(BillDocument).join(BillVersion).where(BillVersion.bill_id.in_(bill_ids), BillDocument.extracted_text.is_not(None))).scalar_one())


def apply_repair(db: OrmSession, plan: RepairPlan, *, allow_fulltext_reset: bool = False, now: datetime | None = None) -> RepairResult:
    """Apply a fully staged plan atomically.  Caller must *not* have begun a transaction."""
    if len(plan.bills) != 61:
        raise CollisionRepairError("refusing to apply a plan whose staged count is not exactly 61")
    now = now or datetime.now(timezone.utc)
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
        # PostgreSQL does not accept a bind parameter in ``SET LOCAL``.  Its
        # parameterized ``set_config`` equivalent keeps this bounded without
        # interpolating operator input into SQL.
        db.execute(text("SELECT set_config('lock_timeout', :timeout, true)"), {"timeout": f"{LOCK_TIMEOUT_MS}ms"})
        db.execute(text("SELECT set_config('statement_timeout', :timeout, true)"), {"timeout": f"{STATEMENT_TIMEOUT_MS}ms"})
        locked = db.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": CA_SESSION_COLLISION_REPAIR_LOCK}).scalar_one()
        if not locked:
            raise CollisionRepairError("another CA collision repair holds the advisory lock")
        ca = db.execute(select(Jurisdiction).where(Jurisdiction.abbreviation == CA_ABBREVIATION).with_for_update()).scalar_one()
        regular = db.execute(select(SessionModel).where(SessionModel.jurisdiction_id == ca.id, SessionModel.identifier == REGULAR_SESSION_IDENTIFIER).with_for_update()).scalar_one_or_none()
        if regular is None:
            raise CollisionRepairError("California regular session disappeared after preflight")
        if regular.active is not True:
            raise CollisionRepairError("California regular session is no longer active")
        regular.start_date = REGULAR_SESSION_START
        regular.end_date = REGULAR_SESSION_END
        special = db.execute(select(SessionModel).where(SessionModel.jurisdiction_id == ca.id, SessionModel.identifier == SPECIAL_SESSION_IDENTIFIER).with_for_update()).scalar_one_or_none()
        if _repair_already_applied(db, plan, regular=regular, special=special):
            return RepairResult(24, 13, 24, 61, 0, already_applied=True)
        if special is None:
            special = SessionModel(jurisdiction_id=ca.id, identifier=SPECIAL_SESSION_IDENTIFIER, classification="special", active=False,
                                   start_date=SPECIAL_SESSION_START, end_date=SPECIAL_SESSION_END,
                                   source_name=SOURCE_NAME, source_url=OFFICIAL_SOURCE_URL, retrieved_at=now,
                                   raw_ref=f"ca-pubinfo:{OFFICIAL_ZIP_SHA256}")
            db.add(special)
            db.flush()
        elif special.classification not in (None, "special"):
            raise CollisionRepairError("existing special session has unexpected classification")
        else:
            special.classification = "special"
            special.start_date = SPECIAL_SESSION_START
            special.end_date = SPECIAL_SESSION_END
            special.active = False
            special.source_name = SOURCE_NAME
            special.source_url = OFFICIAL_SOURCE_URL
            special.retrieved_at = now
            special.raw_ref = f"ca-pubinfo:{OFFICIAL_ZIP_SHA256}"
        expected = {_normalise_identifier(item.identifier) for item in plan.scope.contaminated_regular}
        retained = db.execute(select(Bill).where(Bill.session_id == regular.id, Bill.identifier_norm.in_(expected)).with_for_update()).scalars().all()
        if len(retained) != 24 or {row.identifier_norm for row in retained} != expected:
            raise CollisionRepairError("regular UUID invariant changed after preflight")
        if {row.identifier_norm: row.id for row in retained} != plan.scope.retained_uuid_by_identifier:
            raise CollisionRepairError("regular public UUID invariant changed after preflight")
        if {
            row.identifier_norm: row.openstates_id for row in retained
        } != plan.scope.retained_old_openstates_id_by_identifier:
            raise CollisionRepairError("regular Open States id invariant changed after preflight")
        existing_ids = [row.id for row in retained]
        if _active_fetch_jobs_for_bills(db, existing_ids):
            raise CollisionRepairError("target documents gained queued/running fetch_text jobs")
        incoming_relations = db.execute(
            select(func.count()).select_from(RelatedBill).where(RelatedBill.related_bill_id.in_(existing_ids))
        ).scalar_one()
        if incoming_relations:
            raise CollisionRepairError("target bills gained inbound related_bills rows")
        organizations, people = _required_entity_maps(db, plan)
        reset_fulltext = _delete_children(db, existing_ids)
        if reset_fulltext and not allow_fulltext_reset:
            raise CollisionRepairError(
                f"repair would reset extracted text on {reset_fulltext} documents; rerun only with explicit --allow-fulltext-reset"
            )
        retained_by_norm = {row.identifier_norm: row for row in retained}
        rebuilt: list[Bill] = []
        # These UPDATEs release the globally-unique old special OpenStates
        # ids before we INSERT the corresponding special bills below.  An
        # ORM flush is required here: PostgreSQL does not defer the unique
        # constraint, and relying on its incidental UPDATE/INSERT ordering
        # would make this repair nondeterministically fail.
        for staged in (item for item in plan.bills if item.kind == "retained_regular"):
            bill = retained_by_norm[_normalise_identifier(staged.measure.identifier)]
            _rebuild_bill(
                db, bill=bill, staged=staged, plan=plan, session=regular,
                retrieved_at=now, organizations=organizations, people=people,
            )
            rebuilt.append(bill)
        db.flush()
        for staged in (item for item in plan.bills if item.kind != "retained_regular"):
            target_session = special if staged.kind == "special" else regular
            norm = _normalise_identifier(staged.measure.identifier)
            exists = db.execute(
                select(Bill.id).where(Bill.session_id == target_session.id, Bill.identifier_norm == norm)
            ).scalar_one_or_none()
            if exists is not None:
                raise CollisionRepairError(f"expected missing {staged.kind} {staged.measure.identifier!r} already exists")
            bill = Bill(
                jurisdiction_id=ca.id,
                session_id=target_session.id,
                identifier="pending",
                identifier_norm=f"PENDING-{uuid.uuid4()}",
                title="pending",
            )
            db.add(bill)
            db.flush()
            _rebuild_bill(
                db, bill=bill, staged=staged, plan=plan, session=target_session,
                retrieved_at=now, organizations=organizations, people=people,
            )
            rebuilt.append(bill)
        db.flush()
        for bill in rebuilt:
            _rebuild_search_document(db, bill, now)
        db.flush()
        if len(rebuilt) != 61:
            raise CollisionRepairError("repair reconstruction count changed unexpectedly")
    return RepairResult(24, 13, 24, 61, reset_fulltext)
