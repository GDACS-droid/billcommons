"""Contracts for the bounded CA official action-delta adapter.

The tests use only synthetic pubinfo-shaped archives and an injected
``httpx.MockTransport``.  They never contact California or write a corpus.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from billcommons_ingest import official_ca_actions as adapter


FIXTURE = Path(__file__).parent / "fixtures" / "official_ca_actions_expected.json"
RETRIEVED_AT = datetime(2026, 9, 7, 15, 30, tzinfo=timezone.utc)


def _tsv(rows: list[list[str]]) -> bytes:
    stream = io.StringIO(newline="")
    csv.writer(stream, delimiter="\t", quotechar="`", lineterminator="\n").writerows(rows)
    return stream.getvalue().encode("utf-8")


def _bill(bill_id: str, *, session_year: str = "20252026", session_num: str = "0") -> list[str]:
    return [bill_id, session_year, session_num, "AB", "12"] + [""] * 14


def _history(
    bill_id: str,
    history_id: str,
    action: str,
    sequence: str,
    *,
    action_date: str = "2026-09-01 00:00:00",
) -> list[str]:
    return [
        bill_id, history_id, action_date, action, "SOURCE", "2026-09-01 12:00:00",
        sequence, "77", "Applied", "Assembly", "E&E", "Enrollment", "Passed",
    ]


def _zip(
    bill_rows: list[list[str]],
    history_rows: list[list[str]],
    *,
    members: dict[str, bytes] | None = None,
    compression: int = zipfile.ZIP_STORED,
) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=compression) as archive:
        archive.writestr("BILL_TBL.dat", _tsv(bill_rows))
        archive.writestr("BILL_HISTORY_TBL.dat", _tsv(history_rows))
        for name, content in (members or {}).items():
            archive.writestr(name, content)
    return out.getvalue()


def _valid_zip() -> bytes:
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    bills = list(expected["bills"])
    return _zip(
        [_bill(bills[0]), _bill(bills[1], session_num="1")],
        [
            _history(bills[0], "1002", "Ordered to third reading.", "4"),
            _history(bills[0], "1001", "Read  first time.", "3"),
            _history(bills[1], "2001", "Chaptered by Secretary of State.", "1"),
            # A retained row with no BILL_TBL scope must never become an event.
            _history("historical-only", "old-1", "Not in scope.", "1"),
        ],
    )


def test_parse_valid_delta_retains_exact_response_and_canonical_events():
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw = _valid_zip()

    batch = adapter.parse_ca_official_actions_zip(
        raw,
        source_url=expected["source_url"],
        retrieved_at=RETRIEVED_AT,
        upstream_modified="Mon, 07 Sep 2026 14:00:00 GMT",
    )

    assert batch.raw_bytes == raw
    assert batch.sha256 == hashlib.sha256(raw).hexdigest()
    assert batch.retrieved_at == RETRIEVED_AT
    assert batch.upstream_modified == "Mon, 07 Sep 2026 14:00:00 GMT"
    assert batch.scoped_bill_ids == ("202520260AB12", "202520261SB2")
    assert batch.event_count == 3
    first = batch.events_by_official_bill_id["202520260AB12"][0]
    assert first.occurrence_id == "ca-history:1001"
    assert first.description == expected["bills"]["202520260AB12"]["description"]
    assert first.raw_fields["action"] == "Read  first time."
    assert first.as_reconciliation_event()["date"] == "2026-09-01"
    assert adapter.map_official_bill_id("202520260AB12").identifier == "AB 12"
    assert adapter.map_official_bill_id("202520261SB2").session_identifier == adapter.SPECIAL_SESSION_IDENTIFIER


def test_fetch_uses_exact_delta_url_mock_transport_and_only_last_modified_for_freshness():
    raw = _valid_zip()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            content=raw,
            headers={
                "Content-Length": str(len(raw)),
                "Last-Modified": "Mon, 07 Sep 2026 14:00:00 GMT",
                "Date": "Mon, 07 Sep 2026 15:00:00 GMT",
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        batch = adapter.fetch_ca_official_actions_delta("Mon", client=client, retrieved_at=RETRIEVED_AT)

    assert seen == ["https://downloads.leginfo.legislature.ca.gov/pubinfo_Mon.zip"]
    assert batch.upstream_modified == "Mon, 07 Sep 2026 14:00:00 GMT"


def test_fetch_does_not_infer_upstream_freshness_from_http_date():
    raw = _valid_zip()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw, headers={"Date": "Mon, 07 Sep 2026 15:00:00 GMT"}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        batch = adapter.fetch_ca_official_actions_delta("Mon", client=client, retrieved_at=RETRIEVED_AT)

    assert batch.upstream_modified is None


def test_fetch_rejects_redirect_without_fetching_its_destination():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://example.test/delta.zip"}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(adapter.OfficialCaActionsError, match="HTTP 302"):
            adapter.fetch_ca_official_actions_response("Mon", client=client, retrieved_at=RETRIEVED_AT)

    assert requests == ["https://downloads.leginfo.legislature.ca.gov/pubinfo_Mon.zip"]


def test_capture_returns_malformed_zip_evidence_before_parser_failure():
    raw = b"not a ZIP archive"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=raw,
            headers={"Last-Modified": "Mon, 07 Sep 2026 14:00:00 GMT"},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        captured = adapter.fetch_ca_official_actions_response("Mon", client=client, retrieved_at=RETRIEVED_AT)

    assert isinstance(captured, adapter.CapturedCaOfficialActionsResponse)
    assert captured.raw_bytes == raw
    assert captured.sha256 == hashlib.sha256(raw).hexdigest()
    assert captured.upstream_modified == "Mon, 07 Sep 2026 14:00:00 GMT"
    with pytest.raises(adapter.OfficialCaActionsError, match="valid ZIP archive"):
        adapter.parse_ca_official_actions_zip(
            captured.raw_bytes,
            source_url=captured.source_url,
            retrieved_at=captured.retrieved_at,
            upstream_modified=captured.upstream_modified,
        )

    # Compatibility wrapper still gives callers the former fetch-and-parse
    # behavior; orchestration that needs durable evidence uses capture first.
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(adapter.OfficialCaActionsError, match="valid ZIP archive"):
            adapter.fetch_ca_official_actions_delta("Mon", client=client, retrieved_at=RETRIEVED_AT)


def test_capture_fails_a_slow_drip_at_the_total_monotonic_deadline():
    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial response"

        def close(self) -> None:
            return None

    ticks = iter((0.0, 0.0, adapter.TOTAL_RESPONSE_DEADLINE_SECONDS + 0.1))

    def clock() -> float:
        return next(ticks)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=SlowStream(), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(adapter.OfficialCaActionsError, match="total deadline"):
            adapter.fetch_ca_official_actions_response(
                "Mon",
                client=client,
                retrieved_at=RETRIEVED_AT,
                clock=clock,
            )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            _zip(
                [_bill("202520260AB12"), _bill("202520260AB12")],
                [_history("202520260AB12", "1001", "Read first time.", "1")],
            ),
            "duplicate official bill ID",
        ),
        (
            _zip(
                [_bill("202320240AB12", session_year="20232024")],
                [_history("202320240AB12", "1001", "Read first time.", "1")],
            ),
            "unknown CA source scope",
        ),
        (
            _zip(
                [["202520260AB12"] + [""] * 17],
                [_history("202520260AB12", "1001", "Read first time.", "1")],
            ),
            "expected 19",
        ),
    ],
)
def test_parse_fails_closed_for_duplicate_unknown_or_malformed_source_contract(raw: bytes, message: str):
    with pytest.raises(adapter.OfficialCaActionsError, match=message):
        adapter.parse_ca_official_actions_zip(
            raw,
            source_url=adapter.ca_delta_url("Mon"),
            retrieved_at=RETRIEVED_AT,
        )


def test_parse_rejects_duplicate_history_occurrence_id():
    raw = _zip(
        [_bill("202520260AB12"), _bill("202520260AB13")],
        [
            _history("202520260AB12", "1001", "Read first time.", "1"),
            _history("202520260AB13", "1001", "Read first time.", "1"),
        ],
    )

    with pytest.raises(adapter.OfficialCaActionsError, match="duplicate official history ID 1001"):
        adapter.parse_ca_official_actions_zip(raw, source_url=adapter.ca_delta_url("Mon"), retrieved_at=RETRIEVED_AT)


def test_parse_accepts_observed_auxiliary_member_counts_but_keeps_member_cap():
    base_rows = [_bill("202520260AB12")]
    history_rows = [_history("202520260AB12", "1001", "Read first time.", "1")]
    for auxiliary_member_count in (102, 144):  # Two required tables make 104 and 146.
        observed_shape = _zip(
            base_rows,
            history_rows,
            members={f"BILL_VERSION_TBL_{index}.lob": b"x" for index in range(auxiliary_member_count)},
        )
        batch = adapter.parse_ca_official_actions_zip(
            observed_shape,
            source_url=adapter.ca_delta_url("Mon"),
            retrieved_at=RETRIEVED_AT,
        )
        assert batch.event_count == 1

    over_cap = _zip(
        base_rows,
        history_rows,
        members={f"BILL_VERSION_TBL_{index}.lob": b"x" for index in range(255)},
    )
    with pytest.raises(adapter.OfficialCaActionsError, match="257 members; cap is 256"):
        adapter.parse_ca_official_actions_zip(
            over_cap,
            source_url=adapter.ca_delta_url("Mon"),
            retrieved_at=RETRIEVED_AT,
        )


def test_parse_rejects_zip_bomb_and_crc_failure_before_table_parsing():
    bomb = _zip(
        [_bill("202520260AB12")],
        [_history("202520260AB12", "1001", "Read first time.", "1")],
        members={"padding.dat": b"0" * (1024 * 1024)},
        compression=zipfile.ZIP_DEFLATED,
    )
    with pytest.raises(adapter.OfficialCaActionsError, match="compression ratio cap"):
        adapter.parse_ca_official_actions_zip(bomb, source_url=adapter.ca_delta_url("Mon"), retrieved_at=RETRIEVED_AT)

    corrupted = bytearray(_valid_zip())
    offset = corrupted.find(b"Read  first time.")
    assert offset > 0
    corrupted[offset] ^= 1
    with pytest.raises(adapter.OfficialCaActionsError, match="valid ZIP archive"):
        adapter.parse_ca_official_actions_zip(bytes(corrupted), source_url=adapter.ca_delta_url("Mon"), retrieved_at=RETRIEVED_AT)


def test_fetch_rejects_http_status_and_stream_bytes_when_content_length_lies():
    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    with httpx.Client(transport=httpx.MockTransport(unavailable)) as client:
        with pytest.raises(adapter.OfficialCaActionsError, match="HTTP 503"):
            adapter.fetch_ca_official_actions_delta("Mon", client=client, retrieved_at=RETRIEVED_AT)

    oversized = b"x" * (adapter.MAX_RESPONSE_BYTES + 1)

    def lying_length(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized, headers={"Content-Length": "1"}, request=request)

    with httpx.Client(transport=httpx.MockTransport(lying_length)) as client:
        with pytest.raises(adapter.OfficialCaActionsError, match="streamed response exceeds"):
            adapter.fetch_ca_official_actions_delta("Mon", client=client, retrieved_at=RETRIEVED_AT)
