"""Bounded, read-only California official action-delta adapter.

California publishes small ``pubinfo_<weekday>.zip`` delta archives on its
official downloads host.  This module fetches one exact archive, retains its
complete response bytes for the caller's durable raw store, and turns the
``BILL_HISTORY_TBL.dat`` ledger into explicitly identified action events.  It
does not open a database session or write a corpus row.

The public parser is deliberately stricter than the one-off reconciliation
script: an autonomous incremental importer must stop when the publisher
changes the current-session scope or archive shape instead of silently
attributing an unknown record to a California bill.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import MappingProxyType
from typing import Iterable, Mapping
from urllib.parse import urlsplit

import httpx

from billcommons_shared.httpc import new_client


ADAPTER_VERSION = "ca-official-actions/1"
DOWNLOADS_BASE_URL = "https://downloads.leginfo.legislature.ca.gov"
_DOWNLOADS_HOST = "downloads.leginfo.legislature.ca.gov"
_DELTA_DAYS = frozenset({"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"})
_DELTA_PATH_RE = re.compile(r"^/pubinfo_(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\.zip$")

CA_SESSION_YEAR = "20252026"
REGULAR_SESSION_IDENTIFIER = "2025-2026 Regular Session"
SPECIAL_SESSION_IDENTIFIER = "2025-2026 Special Session 1"

BILL_COLUMNS = (
    "bill_id", "session_year", "session_num", "measure_type", "measure_num",
    "measure_state", "chapter_year", "chapter_type", "chapter_session_num",
    "chapter_num", "latest_bill_version_id", "active_flg", "trans_uid",
    "trans_update", "current_location", "current_secondary_loc", "current_house",
    "current_status", "days_31st_in_print",
)
HISTORY_COLUMNS = (
    "bill_id", "bill_history_id", "action_date", "action", "trans_uid",
    "trans_update_dt", "action_sequence", "action_code", "action_status",
    "primary_location", "secondary_location", "ternary_location", "end_status",
)

# The delta is normally only a few MB.  These values deliberately bound both
# wire bytes and every decompressed member before any CSV parsing occurs.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ZIP_MEMBERS = 32
MAX_MEMBER_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100


class OfficialCaActionsError(RuntimeError):
    """A source-contract failure that must not be treated as partial input."""


@dataclass(frozen=True)
class OfficialBillMapping:
    """The local session and displayed identifier represented by a CA BILL_ID."""

    official_bill_id: str
    session_identifier: str
    session_classification: str
    identifier: str


@dataclass(frozen=True)
class ParsedHistoryAction:
    """A normalized ledger row shared with the offline reconciliation script."""

    official_bill_id: str
    history_id: str
    action_date: date | None
    description: str
    sequence: int | None
    updated_at: str
    raw_fields: Mapping[str, str]

    @property
    def occurrence_id(self) -> str:
        return f"ca-history:{self.history_id}"


@dataclass(frozen=True)
class OfficialCaActionEvent:
    """Canonical action event plus the exact source columns used to derive it."""

    occurrence_id: str
    official_bill_id: str
    history_id: str
    action_date: date | None
    description: str
    sequence: int | None
    updated_at: str
    source_url: str
    raw_fields: Mapping[str, str]

    def as_reconciliation_event(self) -> dict[str, object]:
        """Return the portable event shape consumed by reconciliation code."""

        return {
            "occurrence_id": self.occurrence_id,
            "source_identity": self.occurrence_id,
            "source_namespace": "ca-leginfo-pubinfo-history",
            "bill_id": self.official_bill_id,
            "description": self.description,
            "date": self.action_date.isoformat() if self.action_date else None,
            "date_precision": "day" if self.action_date else "unknown",
            "source_url": self.source_url,
            "raw_fields": dict(self.raw_fields),
        }


@dataclass(frozen=True)
class ParsedCaOfficialActionsBatch:
    """A complete immutable result for one exact fetched response."""

    source_url: str
    raw_bytes: bytes
    sha256: str
    retrieved_at: datetime
    upstream_modified: str | None
    adapter_version: str
    scoped_bill_ids: tuple[str, ...]
    events_by_official_bill_id: Mapping[str, tuple[OfficialCaActionEvent, ...]]

    @property
    def event_count(self) -> int:
        return sum(len(events) for events in self.events_by_official_bill_id.values())


def ca_delta_url(day: str) -> str:
    """Return the one allowed URL for a CA weekly delta archive."""

    if day not in _DELTA_DAYS:
        raise ValueError(f"day must be one of {sorted(_DELTA_DAYS)}, got {day!r}")
    return f"{DOWNLOADS_BASE_URL}/pubinfo_{day}.zip"


def _require_delta_url(source_url: str) -> str:
    if not isinstance(source_url, str):
        raise OfficialCaActionsError("official CA source URL must be a string")
    parsed = urlsplit(source_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != _DOWNLOADS_HOST
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not _DELTA_PATH_RE.fullmatch(parsed.path)
    ):
        raise OfficialCaActionsError(
            "official CA delta URL must be an exact https downloads.leginfo.legislature.ca.gov/pubinfo_<Day>.zip URL"
        )
    return f"{DOWNLOADS_BASE_URL}{parsed.path}"


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retrieved_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _normal_description(value: str | None) -> str:
    return " ".join((value or "").split())


def _parse_date(value: str) -> date | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise OfficialCaActionsError(f"invalid official action date {value!r}") from exc


def _parse_sequence(value: str) -> int | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise OfficialCaActionsError(f"invalid official action sequence {value!r}") from exc


def _history_sort_key(action: ParsedHistoryAction) -> tuple[int, str, str]:
    return (
        action.sequence if action.sequence is not None else -1,
        action.action_date.isoformat() if action.action_date else "",
        action.history_id,
    )


def map_official_bill_id(official_bill_id: str) -> OfficialBillMapping:
    """Map a current CA official BILL_ID to the exact local session identity."""

    match = re.fullmatch(r"20252026([01])([A-Z][A-Z0-9]*?)([0-9]+)", official_bill_id)
    if match is None:
        raise OfficialCaActionsError(f"unknown CA official bill ID scope {official_bill_id!r}")
    session_number, measure_type, measure_number = match.groups()
    if session_number == "0":
        session_identifier, session_classification = REGULAR_SESSION_IDENTIFIER, "regular"
    else:
        session_identifier, session_classification = SPECIAL_SESSION_IDENTIFIER, "special"
    return OfficialBillMapping(
        official_bill_id=official_bill_id,
        session_identifier=session_identifier,
        session_classification=session_classification,
        identifier=f"{measure_type} {int(measure_number)}",
    )


def _validate_zip(archive: zipfile.ZipFile) -> None:
    members = archive.infolist()
    if len(members) > MAX_ZIP_MEMBERS:
        raise OfficialCaActionsError(f"official CA ZIP has {len(members)} members; cap is {MAX_ZIP_MEMBERS}")
    names = [member.filename for member in members]
    if len(set(names)) != len(names):
        raise OfficialCaActionsError("official CA ZIP has duplicate member names")
    if "BILL_TBL.dat" not in names or "BILL_HISTORY_TBL.dat" not in names:
        raise OfficialCaActionsError("official CA ZIP is missing required BILL_TBL.dat or BILL_HISTORY_TBL.dat")

    total_uncompressed = 0
    for member in members:
        if member.flag_bits & 0x1:
            raise OfficialCaActionsError(f"official CA ZIP member is encrypted: {member.filename}")
        if member.file_size > MAX_MEMBER_UNCOMPRESSED_BYTES:
            raise OfficialCaActionsError(f"official CA ZIP member exceeds uncompressed cap: {member.filename}")
        total_uncompressed += member.file_size
        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise OfficialCaActionsError("official CA ZIP exceeds total uncompressed cap")
        if member.file_size and not member.compress_size:
            raise OfficialCaActionsError(f"official CA ZIP member has invalid compressed size: {member.filename}")
        if member.compress_size and member.file_size / member.compress_size > MAX_COMPRESSION_RATIO:
            raise OfficialCaActionsError(f"official CA ZIP member exceeds compression ratio cap: {member.filename}")

    # Force decompression and CRC verification before parsing.  Bytes are not
    # retained for unrelated members, and metadata caps above bound this work.
    for member in members:
        consumed = 0
        with archive.open(member) as stream:
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                consumed += len(chunk)
                if consumed > MAX_MEMBER_UNCOMPRESSED_BYTES:
                    raise OfficialCaActionsError(f"official CA ZIP member exceeds read cap: {member.filename}")
        if consumed != member.file_size:
            raise OfficialCaActionsError(f"official CA ZIP member size changed while reading: {member.filename}")


def _table_rows(archive: zipfile.ZipFile, member_name: str, columns: tuple[str, ...]) -> Iterable[tuple[int, dict[str, str]]]:
    try:
        raw = archive.open(member_name)
    except KeyError as exc:
        raise OfficialCaActionsError(f"official CA ZIP is missing {member_name}") from exc
    with raw, io.TextIOWrapper(raw, encoding="utf-8", errors="strict", newline="") as stream:
        try:
            reader = csv.reader(stream, delimiter="\t", quotechar="`", strict=True)
            for line_number, values in enumerate(reader, start=1):
                if len(values) != len(columns):
                    raise OfficialCaActionsError(
                        f"{member_name}:{line_number} has {len(values)} fields; expected {len(columns)}"
                    )
                yield line_number, dict(zip(columns, values, strict=True))
        except csv.Error as exc:
            raise OfficialCaActionsError(f"{member_name} is malformed TSV") from exc


def parse_ca_pubinfo_action_archive(
    archive: zipfile.ZipFile,
    *,
    strict_current_scope: bool,
    reject_duplicate_history_ids: bool,
) -> dict[str, tuple[ParsedHistoryAction, ...]]:
    """Parse the two action tables from an already validated ZIP archive.

    ``strict_current_scope=False`` preserves the offline sweep's historical
    behavior: unrelated retained rows are skipped.  The delta adapter sets it
    true, so a source-scope change stops automation before any caller writes.
    """

    bill_ids: set[str] = set()
    for line_number, row in _table_rows(archive, "BILL_TBL.dat", BILL_COLUMNS):
        official_bill_id = row["bill_id"].strip()
        if not official_bill_id:
            raise OfficialCaActionsError(f"BILL_TBL.dat:{line_number} has blank bill ID")
        in_current_scope = row["session_year"].strip() == CA_SESSION_YEAR and row["session_num"].strip() in {"0", "1"}
        if not in_current_scope:
            if strict_current_scope:
                raise OfficialCaActionsError(
                    f"BILL_TBL.dat:{line_number} has unknown CA source scope "
                    f"{row['session_year']!r}/{row['session_num']!r}"
                )
            continue
        if strict_current_scope:
            try:
                mapped = map_official_bill_id(official_bill_id)
            except OfficialCaActionsError as exc:
                raise OfficialCaActionsError(f"BILL_TBL.dat:{line_number} {exc}") from exc
            if mapped.session_classification == "regular" and row["session_num"].strip() != "0":
                raise OfficialCaActionsError(f"BILL_TBL.dat:{line_number} session number conflicts with bill ID")
            if mapped.session_classification == "special" and row["session_num"].strip() != "1":
                raise OfficialCaActionsError(f"BILL_TBL.dat:{line_number} session number conflicts with bill ID")
        if official_bill_id in bill_ids:
            raise OfficialCaActionsError(f"duplicate official bill ID {official_bill_id}")
        bill_ids.add(official_bill_id)

    if not bill_ids:
        raise OfficialCaActionsError("official CA ZIP contains no current regular/special bills")

    actions: dict[str, list[ParsedHistoryAction]] = defaultdict(list)
    history_ids: set[str] = set()
    for line_number, row in _table_rows(archive, "BILL_HISTORY_TBL.dat", HISTORY_COLUMNS):
        official_bill_id = row["bill_id"].strip()
        if official_bill_id not in bill_ids:
            # Official archives can retain history-only rows from a different
            # extract.  They have no BILL_TBL source scope and cannot be
            # attributed safely, so they are intentionally not emitted.
            continue
        history_id = row["bill_history_id"].strip()
        if not history_id:
            raise OfficialCaActionsError(f"BILL_HISTORY_TBL.dat:{line_number} has blank history ID")
        if reject_duplicate_history_ids and history_id in history_ids:
            raise OfficialCaActionsError(f"duplicate official history ID {history_id}")
        history_ids.add(history_id)
        description = _normal_description(row["action"])
        if not description:
            raise OfficialCaActionsError(f"BILL_HISTORY_TBL.dat:{line_number} has blank action text")
        actions[official_bill_id].append(
            ParsedHistoryAction(
                official_bill_id=official_bill_id,
                history_id=history_id,
                action_date=_parse_date(row["action_date"]),
                description=description,
                sequence=_parse_sequence(row["action_sequence"]),
                updated_at=row["trans_update_dt"].strip(),
                raw_fields=MappingProxyType(dict(row)),
            )
        )
    return {bill_id: tuple(sorted(actions[bill_id], key=_history_sort_key)) for bill_id in sorted(bill_ids)}


def parse_ca_official_actions_zip(
    raw_bytes: bytes,
    *,
    source_url: str,
    retrieved_at: datetime,
    upstream_modified: str | None = None,
) -> ParsedCaOfficialActionsBatch:
    """Purely validate and parse one exact official CA delta response."""

    canonical_url = _require_delta_url(source_url)
    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw_bytes must be bytes")
    if len(raw_bytes) > MAX_RESPONSE_BYTES:
        raise OfficialCaActionsError(f"official CA response exceeds {MAX_RESPONSE_BYTES} byte cap")
    if upstream_modified is not None and not isinstance(upstream_modified, str):
        raise TypeError("upstream_modified must be a string or None")
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
            _validate_zip(archive)
            parsed = parse_ca_pubinfo_action_archive(
                archive,
                strict_current_scope=True,
                reject_duplicate_history_ids=True,
            )
    except zipfile.BadZipFile as exc:
        raise OfficialCaActionsError("official CA response is not a valid ZIP archive") from exc

    events = {
        bill_id: tuple(
            OfficialCaActionEvent(
                occurrence_id=action.occurrence_id,
                official_bill_id=action.official_bill_id,
                history_id=action.history_id,
                action_date=action.action_date,
                description=action.description,
                sequence=action.sequence,
                updated_at=action.updated_at,
                source_url=canonical_url,
                raw_fields=action.raw_fields,
            )
            for action in actions
        )
        for bill_id, actions in parsed.items()
    }
    return ParsedCaOfficialActionsBatch(
        source_url=canonical_url,
        raw_bytes=raw_bytes,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        retrieved_at=_require_aware_utc(retrieved_at),
        upstream_modified=upstream_modified,
        adapter_version=ADAPTER_VERSION,
        scoped_bill_ids=tuple(parsed),
        events_by_official_bill_id=MappingProxyType(events),
    )


def fetch_ca_official_actions_delta(
    day: str,
    *,
    client: httpx.Client | None = None,
    retrieved_at: datetime | None = None,
) -> ParsedCaOfficialActionsBatch:
    """Fetch and parse one exact CA delta URL with a hard streaming cap.

    Only ``Last-Modified`` is retained as upstream freshness evidence.  The
    response ``Date`` header is transport metadata and is never promoted to
    upstream freshness.
    """

    source_url = ca_delta_url(day)
    owns_client = client is None
    client = client or new_client(timeout=httpx.Timeout(30.0, read=120.0))
    try:
        with client.stream("GET", source_url) as response:
            if str(response.url) != source_url:
                raise OfficialCaActionsError("official CA delta fetch was redirected away from its exact source URL")
            if response.status_code != 200:
                raise OfficialCaActionsError(
                    f"official CA delta fetch failed with HTTP {response.status_code}"
                )
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise OfficialCaActionsError("official CA delta has invalid Content-Length") from exc
                if declared_length < 0 or declared_length > MAX_RESPONSE_BYTES:
                    raise OfficialCaActionsError("official CA delta Content-Length exceeds response cap")
            chunks: list[bytes] = []
            received = 0
            for chunk in response.iter_bytes():
                received += len(chunk)
                if received > MAX_RESPONSE_BYTES:
                    raise OfficialCaActionsError("official CA delta streamed response exceeds response cap")
                chunks.append(chunk)
            raw_bytes = b"".join(chunks)
            upstream_modified = response.headers.get("Last-Modified")
    finally:
        if owns_client:
            client.close()
    return parse_ca_official_actions_zip(
        raw_bytes,
        source_url=source_url,
        retrieved_at=retrieved_at or datetime.now(timezone.utc),
        upstream_modified=upstream_modified,
    )
