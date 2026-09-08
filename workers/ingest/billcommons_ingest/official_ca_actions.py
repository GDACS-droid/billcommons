"""CA official action transport adapter with a shared pure parser.

The parser lives in :mod:`billcommons_shared.ca_official_actions` so Scout and
this ingest adapter validate the same exact retained archive bytes.  This
module retains the established transport entrypoints for ingest callers.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Callable

import httpx

from billcommons_shared.ca_official_actions import (
    ADAPTER_VERSION,
    CA_SESSION_YEAR,
    DOWNLOADS_BASE_URL,
    HISTORY_COLUMNS,
    BILL_COLUMNS,
    MAX_COMPRESSION_RATIO,
    MAX_MEMBER_UNCOMPRESSED_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_RESPONSE_CHUNK_BYTES,
    MAX_SAFE_NUMERIC,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    MAX_ZIP_MEMBERS,
    PER_READ_TIMEOUT_SECONDS,
    REGULAR_SESSION_IDENTIFIER,
    SPECIAL_SESSION_IDENTIFIER,
    TOTAL_RESPONSE_DEADLINE_SECONDS,
    CapturedCaOfficialActionsResponse,
    OfficialBillMapping,
    OfficialCaActionEvent,
    OfficialCaActionsError,
    ParsedCaOfficialActionsBatch,
    ParsedHistoryAction,
    _require_aware_utc,
    _safe_diagnostic_details,
    ca_delta_url,
    map_official_bill_id,
    parse_ca_official_actions_zip,
    parse_ca_pubinfo_action_archive,
)
from billcommons_shared.httpc import new_client

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
