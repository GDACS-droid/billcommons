"""Regression coverage for session-scoped MCP bill lookups and warnings."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from billcommons_mcp import common, tools
from billcommons_schema.models import Bill, Jurisdiction, Session as SessionModel


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def unique(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return self.value


class _Session:
    def __init__(self, session_row, bills, jurisdiction):
        self.session_row = session_row
        self.bills = bills
        self.jurisdiction = jurisdiction
        self.statements = []
        self.closed = False

    def execute(self, statement):
        self.statements.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        if entity is SessionModel:
            return _Result(self.session_row)
        assert entity is Bill
        return _Result(self.bills)

    def get(self, entity, identity):
        assert entity is Jurisdiction
        assert identity == self.jurisdiction.id
        return self.jurisdiction

    def close(self):
        self.closed = True


def _patch_tool_dependencies(monkeypatch, db, jurisdiction, coverage_sessions):
    monkeypatch.setattr(tools, "get_session", lambda: db)
    monkeypatch.setattr(tools, "find_jurisdiction", lambda _db, _state: jurisdiction)
    monkeypatch.setattr(tools, "serialize_bill_full", lambda bill, include_text: {"id": str(bill.id)})
    monkeypatch.setattr(tools, "record_invocation", lambda **_kwargs: None)
    monkeypatch.setattr(
        tools,
        "coverage_warning_for_jurisdiction",
        lambda _db, _jurisdiction, min_state="METADATA_SEARCHABLE", session_id=None: (
            coverage_sessions.append(session_id) or None
        ),
    )


def _query_params(statement):
    return statement.compile().params.values()


def test_coverage_rows_query_limits_to_requested_session():
    jurisdiction_id = uuid.uuid4()
    session_id = uuid.uuid4()
    row = SimpleNamespace()

    class _CoverageSession:
        statement = None

        def execute(self, statement):
            self.statement = statement
            return _Result([row])

    db = _CoverageSession()

    assert common.coverage_rows_for_jurisdiction(db, jurisdiction_id, session_id) == [row]
    assert session_id in _query_params(db.statement)


def test_scoped_coverage_uses_only_the_served_session_row(monkeypatch):
    jurisdiction_id = uuid.uuid4()
    served_session_id = uuid.uuid4()
    sibling_session_id = uuid.uuid4()
    jurisdiction = SimpleNamespace(id=jurisdiction_id, abbreviation="TS")
    served = SimpleNamespace(
        jurisdiction_id=jurisdiction_id,
        session_id=served_session_id,
        status="GREEN",
        bill_count=1,
        full_text_count=1,
        last_attempt_at=None,
        last_success_at=None,
        validation_pass_rate=None,
        known_gaps=None,
        notes=None,
    )
    sibling = SimpleNamespace(
        jurisdiction_id=jurisdiction_id,
        session_id=sibling_session_id,
        status="BLOCKED",
        bill_count=0,
        full_text_count=0,
        last_attempt_at=None,
        last_success_at=None,
        validation_pass_rate=None,
        known_gaps=None,
        notes=None,
    )
    calls = []

    def coverage_rows(_db, received_jurisdiction_id, session_id=None):
        calls.append((received_jurisdiction_id, session_id))
        return [served] if session_id == served_session_id else [served, sibling]

    monkeypatch.setattr(common, "coverage_rows_for_jurisdiction", coverage_rows)

    assert common.coverage_warning_for_jurisdiction(
        object(), jurisdiction, session_id=served_session_id
    ) is None
    aggregate_warning = common.coverage_warning_for_jurisdiction(object(), jurisdiction)

    assert aggregate_warning["status"] == "BLOCKED"
    assert calls == [
        (jurisdiction_id, served_session_id),
        (jurisdiction_id, None),
    ]


def test_get_bill_record_accepts_exact_session_identifier_and_scopes_warning(monkeypatch):
    jurisdiction = SimpleNamespace(id=uuid.uuid4(), abbreviation="TS")
    session_row = SimpleNamespace(id=uuid.uuid4(), jurisdiction_id=jurisdiction.id)
    bill = SimpleNamespace(
        id=uuid.uuid4(), jurisdiction_id=jurisdiction.id, session_id=session_row.id
    )
    db = _Session(session_row, [bill], jurisdiction)
    coverage_sessions = []
    _patch_tool_dependencies(monkeypatch, db, jurisdiction, coverage_sessions)

    result = tools.get_bill_record(
        jurisdiction="TS", session="2026 Regular Session", identifier="HB 1"
    )

    assert result["bill"]["id"] == str(bill.id)
    assert "2026 Regular Session" in _query_params(db.statements[0])
    assert coverage_sessions == [session_row.id]
    assert db.closed


def test_get_bill_record_accepts_same_jurisdiction_session_uuid(monkeypatch):
    jurisdiction = SimpleNamespace(id=uuid.uuid4(), abbreviation="TS")
    session_row = SimpleNamespace(id=uuid.uuid4(), jurisdiction_id=jurisdiction.id)
    bill = SimpleNamespace(
        id=uuid.uuid4(), jurisdiction_id=jurisdiction.id, session_id=session_row.id
    )
    db = _Session(session_row, [bill], jurisdiction)
    coverage_sessions = []
    _patch_tool_dependencies(monkeypatch, db, jurisdiction, coverage_sessions)

    result = tools.get_bill_record(jurisdiction="TS", session=str(session_row.id), identifier="HB 1")

    assert result["bill"]["id"] == str(bill.id)
    assert session_row.id in _query_params(db.statements[0])
    assert coverage_sessions == [session_row.id]


@pytest.mark.parametrize("session_value", ["not a stored session", str(uuid.uuid4())])
def test_get_bill_record_rejects_nonmatching_or_cross_jurisdiction_session_without_candidates(
    monkeypatch,
    session_value,
):
    jurisdiction = SimpleNamespace(id=uuid.uuid4(), abbreviation="TS")
    db = _Session(None, [], jurisdiction)
    coverage_sessions = []
    _patch_tool_dependencies(monkeypatch, db, jurisdiction, coverage_sessions)

    result = tools.get_bill_record(
        jurisdiction="TS", session=session_value, identifier="HB 1"
    )

    assert result == {
        "error": {
            "code": "invalid_session",
            "message": "The session is not valid for the specified jurisdiction.",
        }
    }
    assert len(db.statements) == 1
    assert db.statements[0].column_descriptions[0]["entity"] is SessionModel
    assert coverage_sessions == []
    assert db.closed
