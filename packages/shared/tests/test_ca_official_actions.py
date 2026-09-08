from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone

import pytest

from billcommons_shared.ca_official_actions import (
    BILL_COLUMNS, HISTORY_COLUMNS, OfficialCaActionsError, ca_delta_url,
    parse_ca_official_actions_zip,
)


def _archive(compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    def row(columns, values):
        return "\t".join(values.get(column, "") for column in columns).encode() + b"\n"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr("BILL_TBL.dat", row(BILL_COLUMNS, {
            "bill_id": "202520260AB123", "session_year": "20252026",
            "session_num": "0", "measure_type": "AB", "measure_num": "123",
        }))
        archive.writestr("BILL_HISTORY_TBL.dat", row(HISTORY_COLUMNS, {
            "bill_id": "202520260AB123", "bill_history_id": "1001",
            "action_date": "2026-01-15", "action": "Read first time.",
            "trans_update_dt": "2026-01-15T12:00:00", "action_sequence": "1",
        }))
    return output.getvalue()


@pytest.mark.parametrize("compression", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_parser_translates_corrupt_supported_zip_decoders(compression):
    raw = bytearray(_archive(compression))
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        member = archive.getinfo("BILL_TBL.dat")
    payload_start = member.header_offset + 30 + len(member.filename.encode()) + len(member.extra)
    # Keep the ZIP framing valid while corrupting the actual decoder payload.
    # LZMA uses a four-byte ZIP prefix before its properties/data.
    corrupt_at = payload_start + (4 if compression == zipfile.ZIP_LZMA else 0)
    raw[corrupt_at] ^= 0xFF
    with pytest.raises(OfficialCaActionsError) as failure:
        parse_ca_official_actions_zip(
            bytes(raw), source_url=ca_delta_url("Mon"), retrieved_at=datetime.now(timezone.utc),
        )
    assert failure.value.diagnostic_code == "archive_crc_or_invalid_zip"


def test_parser_translates_unsupported_compression_and_honors_deadline():
    raw = bytearray(_archive())
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        member = archive.getinfo("BILL_TBL.dat")
    # Patch the local and central ZIP headers to an unsupported method. This
    # exercises zipfile's real decoder path instead of a monkeypatched error.
    raw[member.header_offset + 8:member.header_offset + 10] = (99).to_bytes(2, "little")
    central = raw.index(b"PK\x01\x02")
    raw[central + 10:central + 12] = (99).to_bytes(2, "little")
    with pytest.raises(OfficialCaActionsError) as compression_error:
        parse_ca_official_actions_zip(
            bytes(raw), source_url=ca_delta_url("Mon"), retrieved_at=datetime.now(timezone.utc),
        )
    assert compression_error.value.diagnostic_code == "archive_crc_or_invalid_zip"

    corrupt = bytearray(_archive())
    with zipfile.ZipFile(io.BytesIO(corrupt)) as archive:
        member = archive.getinfo("BILL_TBL.dat")
    payload_start = member.header_offset + 30 + len(member.filename.encode()) + len(member.extra)
    corrupt[payload_start] ^= 0xFF
    with pytest.raises(OfficialCaActionsError) as corrupt_error:
        parse_ca_official_actions_zip(
            bytes(corrupt), source_url=ca_delta_url("Mon"), retrieved_at=datetime.now(timezone.utc),
        )
    assert corrupt_error.value.diagnostic_code == "archive_crc_or_invalid_zip"

    with pytest.raises(OfficialCaActionsError) as deadline_error:
        parse_ca_official_actions_zip(
            _archive(), source_url=ca_delta_url("Mon"), retrieved_at=datetime.now(timezone.utc),
            deadline=0.0,
        )
    assert deadline_error.value.diagnostic_code == "archive_parse_deadline_exceeded"
