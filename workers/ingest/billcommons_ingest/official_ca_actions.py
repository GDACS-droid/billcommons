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
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import MappingProxyType
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit

import httpx

from billcommons_shared.httpc import new_client
from billcommons_ingest.official_diagnostics import MAX_SAFE_NUMERIC, validate_error_metadata


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
MAX_RESPONSE_CHUNK_BYTES = 64 * 1024
PER_READ_TIMEOUT_SECONDS = 30.0
TOTAL_RESPONSE_DEADLINE_SECONDS = 180.0
# Retained CA deltas had 104 and 146 members because they include auxiliary
# bill-version LOBs.  A 256-entry directory remains bounded while admitting
# those observed shapes; wire, aggregate decompression, per-member, CRC,
# encryption, and compression-ratio limits below still apply.
MAX_ZIP_MEMBERS = 256
MAX_MEMBER_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100


class OfficialCaActionsError(RuntimeError):
    """A source-contract failure that must not be treated as partial input."""

    def __init__(
        self,
        *args: object,
        code: str | None = None,
        details: Mapping[str, int] | None = None,
        http_status: int | None = None,
    ) -> None:
        # Keep RuntimeError's argument and stringification behavior intact for
        # existing callers; the structured fields are strictly additive.
        super().__init__(*args)
        if http_status is not None:
            if not isinstance(http_status, int) or isinstance(http_status, bool) or not 100 <= http_status <= 599:
                raise ValueError("http_status must be an HTTP status integer")
            details = {**(details or {}), "http_status": http_status}
        self.diagnostic_code, self.diagnostic_details = validate_error_metadata(code, details)

    @property
    def http_status(self) -> int | None:
        """Return an observed non-success HTTP status, if this error has one."""

        value = self.diagnostic_details.get("http_status")
        return value if isinstance(value, int) else None


def _safe_diagnostic_details(**values: int) -> dict[str, int]:
    """Omit unrepresentable metadata without changing the source failure."""

    return {key: value for key, value in values.items() if 0 <= value <= MAX_SAFE_NUMERIC}


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


@dataclass(frozen=True)
class CapturedCaOfficialActionsResponse:
    """Exact response evidence captured before any ZIP or table parsing.

    Callers persist this object before parsing so a malformed publisher
    response remains independently inspectable alongside its failed parser
    observation.
    """

    source_url: str
    raw_bytes: bytes
    sha256: str
    retrieved_at: datetime
    upstream_modified: str | None


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
        raise OfficialCaActionsError(
            f"official CA ZIP has {len(members)} members; cap is {MAX_ZIP_MEMBERS}",
            code="archive_member_count_limit_exceeded",
            details=_safe_diagnostic_details(observed=len(members), limit=MAX_ZIP_MEMBERS),
        )
    names = [member.filename for member in members]
    if len(set(names)) != len(names):
        raise OfficialCaActionsError("official CA ZIP has duplicate member names", code="archive_duplicate_member_names")
    if "BILL_TBL.dat" not in names or "BILL_HISTORY_TBL.dat" not in names:
        raise OfficialCaActionsError(
            "official CA ZIP is missing required BILL_TBL.dat or BILL_HISTORY_TBL.dat",
            code="archive_missing_required_tables",
        )

    total_uncompressed = 0
    for member in members:
        if member.flag_bits & 0x1:
            raise OfficialCaActionsError(f"official CA ZIP member is encrypted: {member.filename}", code="archive_encrypted_member")
        if member.file_size > MAX_MEMBER_UNCOMPRESSED_BYTES:
            raise OfficialCaActionsError(
                f"official CA ZIP member exceeds uncompressed cap: {member.filename}",
                code="archive_member_size_limit_exceeded",
                details=_safe_diagnostic_details(observed=member.file_size, limit=MAX_MEMBER_UNCOMPRESSED_BYTES),
            )
        total_uncompressed += member.file_size
        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise OfficialCaActionsError(
                "official CA ZIP exceeds total uncompressed cap",
                code="archive_total_size_limit_exceeded",
                details=_safe_diagnostic_details(observed=total_uncompressed, limit=MAX_TOTAL_UNCOMPRESSED_BYTES),
            )
        if member.file_size and not member.compress_size:
            raise OfficialCaActionsError(
                f"official CA ZIP member has invalid compressed size: {member.filename}",
                code="archive_invalid_compressed_size",
            )
        if member.compress_size and member.file_size / member.compress_size > MAX_COMPRESSION_RATIO:
            raise OfficialCaActionsError(
                f"official CA ZIP member exceeds compression ratio cap: {member.filename}",
                code="archive_compression_ratio_limit_exceeded",
                details=_safe_diagnostic_details(
                    observed=(member.file_size + member.compress_size - 1) // member.compress_size,
                    limit=MAX_COMPRESSION_RATIO,
                ),
            )

    # Force decompression and CRC verification before parsing.  Bytes are not
    # retained for unrelated members, and metadata caps above bound this work.
    for member in members:
        consumed = 0
        with archive.open(member) as stream:
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                consumed += len(chunk)
                if consumed > MAX_MEMBER_UNCOMPRESSED_BYTES:
                    raise OfficialCaActionsError(
                        f"official CA ZIP member exceeds read cap: {member.filename}",
                        code="archive_member_size_limit_exceeded",
                        details=_safe_diagnostic_details(observed=consumed, limit=MAX_MEMBER_UNCOMPRESSED_BYTES),
                    )
        if consumed != member.file_size:
            raise OfficialCaActionsError(
                f"official CA ZIP member size changed while reading: {member.filename}",
                code="archive_member_size_changed",
                details=_safe_diagnostic_details(observed=consumed, limit=member.file_size),
            )


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
        raise OfficialCaActionsError(
            f"official CA response exceeds {MAX_RESPONSE_BYTES} byte cap",
            code="parser_response_size_limit_exceeded",
            details=_safe_diagnostic_details(observed=len(raw_bytes), limit=MAX_RESPONSE_BYTES),
        )
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
        raise OfficialCaActionsError(
            "official CA response is not a valid ZIP archive",
            code="archive_crc_or_invalid_zip",
        ) from exc

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


def _require_before_response_deadline(started_at: float, clock: Callable[[], float]) -> None:
    if clock() - started_at > TOTAL_RESPONSE_DEADLINE_SECONDS:
        raise OfficialCaActionsError(
            f"official CA delta response exceeded {TOTAL_RESPONSE_DEADLINE_SECONDS:g}s total deadline",
            code="response_deadline_exceeded",
            details=_safe_diagnostic_details(limit=int(TOTAL_RESPONSE_DEADLINE_SECONDS)),
        )


def fetch_ca_official_actions_response(
    day: str,
    *,
    client: httpx.Client | None = None,
    retrieved_at: datetime | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CapturedCaOfficialActionsResponse:
    """Capture one exact CA delta response without parsing it.

    Only ``Last-Modified`` is retained as upstream freshness evidence.  The
    response ``Date`` header is transport metadata and is never promoted to
    upstream freshness.  The request does not follow redirects, so a redirect
    cannot cause this adapter to fetch a government HTML page or another URL.
    """

    source_url = ca_delta_url(day)
    owns_client = client is None
    client = client or new_client(timeout=httpx.Timeout(PER_READ_TIMEOUT_SECONDS))
    started_at = clock()
    try:
        with client.stream(
            "GET",
            source_url,
            follow_redirects=False,
            timeout=httpx.Timeout(PER_READ_TIMEOUT_SECONDS),
        ) as response:
            _require_before_response_deadline(started_at, clock)
            if str(response.url) != source_url:
                raise OfficialCaActionsError(
                    "official CA delta fetch was redirected away from its exact source URL",
                    code="unexpected_response_url",
                )
            if response.status_code != 200:
                raise OfficialCaActionsError(
                    f"official CA delta fetch failed with HTTP {response.status_code}",
                    code="http_status_unexpected",
                    # Nonstandard wire statuses still fail as source errors;
                    # only valid HTTP status evidence enters the diagnosis.
                    http_status=response.status_code if 100 <= response.status_code <= 599 else None,
                )
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise OfficialCaActionsError(
                        "official CA delta has invalid Content-Length",
                        code="invalid_content_length",
                    ) from exc
                if declared_length < 0:
                    raise OfficialCaActionsError(
                        "official CA delta has invalid Content-Length",
                        code="invalid_content_length",
                    )
                if declared_length > MAX_RESPONSE_BYTES:
                    raise OfficialCaActionsError(
                        "official CA delta Content-Length exceeds response cap",
                        code="content_length_limit_exceeded",
                        details=_safe_diagnostic_details(observed=declared_length, limit=MAX_RESPONSE_BYTES),
                    )
            chunks: list[bytes] = []
            received = 0
            for chunk in response.iter_bytes(chunk_size=MAX_RESPONSE_CHUNK_BYTES):
                _require_before_response_deadline(started_at, clock)
                if len(chunk) > MAX_RESPONSE_CHUNK_BYTES:
                    raise OfficialCaActionsError(
                        "official CA delta response chunk exceeds chunk cap",
                        code="response_chunk_limit_exceeded",
                        details=_safe_diagnostic_details(observed=len(chunk), limit=MAX_RESPONSE_CHUNK_BYTES),
                    )
                received += len(chunk)
                if received > MAX_RESPONSE_BYTES:
                    raise OfficialCaActionsError(
                        "official CA delta streamed response exceeds response cap",
                        code="response_size_limit_exceeded",
                        details=_safe_diagnostic_details(observed=received, limit=MAX_RESPONSE_BYTES),
                    )
                chunks.append(chunk)
            raw_bytes = b"".join(chunks)
            upstream_modified = response.headers.get("Last-Modified")
    finally:
        if owns_client:
            client.close()
    return CapturedCaOfficialActionsResponse(
        source_url=source_url,
        raw_bytes=raw_bytes,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        retrieved_at=_require_aware_utc(retrieved_at or datetime.now(timezone.utc)),
        upstream_modified=upstream_modified,
    )


def fetch_ca_official_actions_delta(
    day: str,
    *,
    client: httpx.Client | None = None,
    retrieved_at: datetime | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ParsedCaOfficialActionsBatch:
    """Fetch and parse one exact CA delta URL with the capture contract."""

    captured = fetch_ca_official_actions_response(
        day,
        client=client,
        retrieved_at=retrieved_at,
        clock=clock,
    )
    return parse_ca_official_actions_zip(
        captured.raw_bytes,
        source_url=captured.source_url,
        retrieved_at=captured.retrieved_at,
        upstream_modified=captured.upstream_modified,
    )
