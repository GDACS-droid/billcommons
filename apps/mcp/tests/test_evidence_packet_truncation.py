"""Regression test for Finding 5: build_legislative_evidence_packet must
report explicit truncation metadata, not silently truncate votes/hearings/
history at their caps.

Business intent: a data consumer (often an AI agent, per this server's
citation-ready design) reading a `votes` section that stops at exactly 100
entries has no way to tell "this bill really only had 100 votes" from "there
were 500 votes and this packet silently dropped 400 of them" -- the second
case, unflagged, is a materially misleading citation source. This test
inserts more vote/hearing rows than a deliberately-low, env-tuned cap and
asserts the packet says so explicitly.

Runs the tool function directly against the real shared DB (matching this
project's live-DB test convention -- see workers/ingest/tests/conftest.py),
inserting its own fixture rows and cleaning them up in a `finally`. Env caps
must be set BEFORE `billcommons_mcp.tools` is imported (its MAX_EVIDENCE_*
constants are computed once at module import time), so this runs the actual
check in a subprocess with the env pre-set, matching
test_env_int_fallback.py's pattern.

Run: .venv/bin/python -m pytest apps/mcp/tests/test_evidence_packet_truncation.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

MCP_APP_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = MCP_APP_DIR.parent.parent

_SCRIPT = """
import json
import os
import sys
import uuid
from datetime import date

from billcommons_mcp.tools import build_legislative_evidence_packet, MAX_EVIDENCE_VOTES, MAX_EVIDENCE_HEARINGS
from billcommons_schema.models import (
    Bill,
    Jurisdiction,
    LegislativeEvent,
    Session as SessionModel,
    VoteEvent,
)
from billcommons_shared.db import get_session

# TEST_VOTE_ROW_COUNT/TEST_HEARING_ROW_COUNT are set by the test harness to
# either (cap + 1) -- proving truncation is flagged -- or a value strictly
# under the cap -- proving truncation is NOT falsely flagged. Defaults to
# cap+1 so a stray direct run of this script still exercises something
# sensible.
vote_row_count = int(os.environ.get("TEST_VOTE_ROW_COUNT", str(MAX_EVIDENCE_VOTES + 1)))
hearing_row_count = int(os.environ.get("TEST_HEARING_ROW_COUNT", str(MAX_EVIDENCE_HEARINGS + 1)))

abbr = "ZQ_EVID_" + uuid.uuid4().hex[:8].upper()
db = get_session()
bill_id = None
jurisdiction_id = None
try:
    jurisdiction = Jurisdiction(name="Evidence Packet Test State", abbreviation=abbr, classification="state")
    db.add(jurisdiction)
    db.flush()
    jurisdiction_id = jurisdiction.id
    session_row = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026 Session", active=True)
    db.add(session_row)
    db.flush()
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 1",
        identifier_norm="HB 1",
        title="An evidence-packet test bill",
        source_url="https://example-legislature.gov/hb1",
    )
    db.add(bill)
    db.flush()
    bill_id = bill.id

    # Bulk-insert (not one db.add()/flush per row) to stay fast over the
    # live DB's network latency.
    db.execute(
        VoteEvent.__table__.insert(),
        [
            {"bill_id": bill.id, "motion_text": f"Motion {i}", "start_date": date(2026, 1, 1)}
            for i in range(vote_row_count)
        ],
    )
    db.execute(
        LegislativeEvent.__table__.insert(),
        [
            {"jurisdiction_id": jurisdiction.id, "bill_id": bill.id, "name": f"Hearing {i}"}
            for i in range(hearing_row_count)
        ],
    )
    db.commit()

    packet = build_legislative_evidence_packet(str(bill_id))
    print(json.dumps(packet))
finally:
    db.rollback()
    if bill_id is not None:
        db.execute(VoteEvent.__table__.delete().where(VoteEvent.bill_id == bill_id))
        db.execute(LegislativeEvent.__table__.delete().where(LegislativeEvent.bill_id == bill_id))
        db.execute(Bill.__table__.delete().where(Bill.id == bill_id))
    if jurisdiction_id is not None:
        db.execute(SessionModel.__table__.delete().where(SessionModel.jurisdiction_id == jurisdiction_id))
        db.execute(Jurisdiction.__table__.delete().where(Jurisdiction.id == jurisdiction_id))
    db.commit()
    db.close()
"""


def _run(extra_env: dict[str, str]) -> dict:
    env = dict(os.environ)
    env.update(extra_env)
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=MCP_APP_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"fixture/tool-call subprocess failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # Last non-empty line is the packet JSON (any print noise goes above it).
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    return json.loads(lines[-1])


def test_evidence_packet_flags_truncated_votes_and_hearings():
    packet = _run({"MCP_MAX_EVIDENCE_VOTES": "3", "MCP_MAX_EVIDENCE_HEARINGS": "3"})

    assert "error" not in packet, f"tool returned an error: {packet}"
    assert packet["truncated"]["votes"] is True, (
        "a bill with MORE votes than the cap must be flagged truncated=True for votes"
    )
    assert packet["truncated"]["hearings"] is True, (
        "a bill with MORE hearings than the cap must be flagged truncated=True for hearings"
    )
    assert len(packet["votes"]["data"]) == 3, "the data list itself must still respect the cap"
    assert len(packet["hearings"]["data"]) == 3
    assert packet["truncation_caps"]["votes"] == 3
    assert packet["truncation_caps"]["hearings"] == 3


def test_evidence_packet_does_not_flag_truncation_when_under_cap():
    """Sanity check the other direction: a bill with FEWER records than the
    cap must not be falsely flagged as truncated."""
    packet = _run(
        {
            "MCP_MAX_EVIDENCE_VOTES": "10",
            "MCP_MAX_EVIDENCE_HEARINGS": "10",
            "TEST_VOTE_ROW_COUNT": "3",
            "TEST_HEARING_ROW_COUNT": "3",
        }
    )

    assert "error" not in packet, f"tool returned an error: {packet}"
    assert packet["truncated"]["votes"] is False, (
        "a bill with fewer records than the cap must not be falsely flagged truncated"
    )
    assert packet["truncated"]["hearings"] is False
