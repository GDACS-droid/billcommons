"""Replayable evidence for semantic mutations from fetched bill documents.

A fetched document is useful corpus input, but its URL alone does not prove
that the bytes came from a reviewed authoritative source.  This module keeps
that distinction explicit while atomically retaining the exact response and
local before/after document states for real content mutations.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as OrmSession

from billcommons_schema.models import (
    Bill,
    BillDocument,
    BillVersion,
    CorpusUpdateEvidence,
    OfficialRawBlob,
)

SOURCE_NAME = "official_document_fetch"
PROCESSING_VERSION = "official_document_fetch_evidence/1"
MAX_BLOB_BYTES = 8 * 1024 * 1024
_AUTHORITY_UNVERIFIED = "retrieved_document_not_authority_verified"


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sanitize_source_url(url: str | None) -> str | None:
    """Retain a fetch location without credentials or request-only data."""
    if url is None:
        return None
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("document evidence requires an absolute http(s) source URL")
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("document evidence source URL has an invalid port") from exc
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))


def snapshot_document(document: BillDocument) -> dict[str, Any]:
    """Serialize document corpus fields, excluding operational bookkeeping.

    ``retrieved_at``, retry state, archive paths, and status notes change on
    successful crawler bookkeeping even when the corpus text did not.  They
    are deliberately not part of a semantic evidence transition.
    """
    return {
        "document": {
            "id": str(document.id),
            "bill_version_id": str(document.bill_version_id),
            "media_type": document.media_type,
            "url": sanitize_source_url(document.url),
            "extracted_text": document.extracted_text,
            "source_name": document.source_name,
            "checksum": document.checksum,
            "parser_version": document.parser_version,
        }
    }


def _store_blob(db: OrmSession, data: bytes, *, content_type: str) -> str:
    """Content-address bounded evidence bytes and verify the stored value."""
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_BLOB_BYTES:
        raise ValueError("document update evidence must be non-empty bytes within the storage cap")
    sha256 = hashlib.sha256(data).hexdigest()
    db.execute(
        insert(OfficialRawBlob)
        .values(sha256=sha256, data=data, content_type=content_type)
        .on_conflict_do_nothing(index_elements=(OfficialRawBlob.sha256,))
    )
    stored = db.get(OfficialRawBlob, sha256)
    if stored is None or hashlib.sha256(stored.data).hexdigest() != sha256:
        raise RuntimeError("document evidence blob integrity check failed")
    return sha256


def record_document_update_evidence(
    db: OrmSession,
    *,
    document: BillDocument,
    before: dict[str, Any],
    raw: bytes,
    content_type: str | None,
    fetched_url: str,
    resolver: str | None,
    retrieved_at: datetime,
) -> CorpusUpdateEvidence | None:
    """Write immutable evidence for a semantic document update.

    This function intentionally has no transaction handling.  Call it in the
    document update transaction so failed relational evidence storage rolls
    back the document mutation too.  A fetch is not recorded merely because
    crawler timestamps or retry metadata changed.
    """
    after = snapshot_document(document)
    before_bytes = _canonical_json_bytes(before)
    after_bytes = _canonical_json_bytes(after)
    if before_bytes == after_bytes:
        return None

    bill = db.execute(
        select(Bill).join(BillVersion, BillVersion.bill_id == Bill.id).where(
            BillVersion.id == document.bill_version_id
        )
    ).scalar_one()
    source_url = sanitize_source_url(fetched_url)
    if source_url is None:  # Defensive for the type checker; fetched URLs are required.
        raise ValueError("document evidence requires a fetched source URL")
    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
    else:
        retrieved_at = retrieved_at.astimezone(timezone.utc)
    if resolver is not None and (not isinstance(resolver, str) or len(resolver) > 255):
        raise ValueError("document evidence resolver must be a short string")

    response_sha256 = _store_blob(
        db, raw, content_type=content_type.split(";", 1)[0].strip().lower() if content_type else "application/octet-stream"
    )
    before_sha256 = _store_blob(db, before_bytes, content_type="application/json")
    after_sha256 = _store_blob(db, after_bytes, content_type="application/json")
    evidence = CorpusUpdateEvidence(
        bill_id=bill.id,
        original_bill_upstream_id=bill.upstream_id,
        source_name=SOURCE_NAME,
        source_url=source_url,
        request_scope={
            "document_id": str(document.id),
            "resolver": resolver,
            "authority": _AUTHORITY_UNVERIFIED,
        },
        response_sha256=response_sha256,
        before_snapshot_sha256=before_sha256,
        after_snapshot_sha256=after_sha256,
        processing_version=PROCESSING_VERSION,
        mutation_kind="updated",
        changed_components=["documents"],
        retrieved_at=retrieved_at,
    )
    db.add(evidence)
    db.flush()
    return evidence
