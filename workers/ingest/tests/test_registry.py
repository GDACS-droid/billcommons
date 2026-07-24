"""Tests for registry.seed_registry against a small synthetic registry
fixture (never the live data/registry/sessions-2026.json -- that's
exercised for real by the live-DB verification step, not unit tests)."""
from __future__ import annotations

import json

from sqlalchemy import select

from billcommons_ingest.registry import seed_registry
from billcommons_schema.models import Jurisdiction, JurisdictionCoverage, Session as SessionModel

FIXTURE_REGISTRY = {
    "generated_at": "2026-07-23",
    "jurisdictions": [
        {
            "state_code": "ZQ",
            "jurisdiction_name": "Zzyzxland",
            "legislature_name": "Zzyzxland Legislature",
            "session_identifier": "2026 Regular Session",
            "session_status": "active",
            "regular_or_special": "regular",
            "convened_at": "2026-01-01",
            "expected_adjournment": "2026-06-01",
            "uses_biennium": False,
            "official_legislature_homepage": "https://example.test/zq",
            "source_url": "https://example.test/source",
            "last_verified_at": "2026-07-23",
            "notes": "fixture jurisdiction for tests",
        },
    ],
    "special_sessions": [
        {
            "state_code": "ZQ",
            "topic": "Test special topic",
            "convened_at": "2026-07-01",
            "expected_adjournment": None,
            "status": "active_as_of_2026-07-23",
            "source_url": "https://example.test/special",
        },
    ],
}


def test_seed_registry_creates_jurisdiction_session_and_coverage(db_session, tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(FIXTURE_REGISTRY))

    counts = seed_registry(db_session, registry_path)
    db_session.flush()

    assert counts["jurisdictions"] == 1
    assert counts["sessions"] == 2  # one regular + one special
    assert counts["coverage_rows"] == 2

    jurisdiction = db_session.execute(
        select(Jurisdiction).where(Jurisdiction.abbreviation == "ZQ")
    ).scalar_one()
    assert jurisdiction.name == "Zzyzxland"

    sessions = db_session.execute(
        select(SessionModel).where(SessionModel.jurisdiction_id == jurisdiction.id)
    ).scalars().all()
    assert len(sessions) == 2
    identifiers = {s.identifier for s in sessions}
    assert "2026 Regular Session" in identifiers
    assert any("special" in i.lower() for i in identifiers)

    coverage_rows = db_session.execute(
        select(JurisdictionCoverage).where(JurisdictionCoverage.jurisdiction_id == jurisdiction.id)
    ).scalars().all()
    assert len(coverage_rows) == 2
    assert all(c.status == "SOURCE_IDENTIFIED" for c in coverage_rows)


def test_seed_registry_is_idempotent(db_session, tmp_path):
    """Re-running the seed against the same registry must not create
    duplicate jurisdictions/sessions/coverage rows -- this is what lets
    seed-registry be run repeatedly (e.g. re-verified session dates) without
    ever double-seeding."""
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(FIXTURE_REGISTRY))

    seed_registry(db_session, registry_path)
    db_session.flush()
    seed_registry(db_session, registry_path)
    db_session.flush()

    jurisdictions = db_session.execute(
        select(Jurisdiction).where(Jurisdiction.abbreviation == "ZQ")
    ).scalars().all()
    assert len(jurisdictions) == 1

    sessions = db_session.execute(
        select(SessionModel).where(SessionModel.jurisdiction_id == jurisdictions[0].id)
    ).scalars().all()
    assert len(sessions) == 2

    coverage_rows = db_session.execute(
        select(JurisdictionCoverage).where(JurisdictionCoverage.jurisdiction_id == jurisdictions[0].id)
    ).scalars().all()
    assert len(coverage_rows) == 2


def test_seed_registry_raises_on_special_session_unknown_state(db_session, tmp_path):
    bad_registry = {
        "jurisdictions": [],
        "special_sessions": [
            {"state_code": "NOPE", "topic": "x", "convened_at": "2026-01-01", "status": "active"}
        ],
    }
    registry_path = tmp_path / "bad_registry.json"
    registry_path.write_text(json.dumps(bad_registry))

    import pytest

    with pytest.raises(ValueError):
        seed_registry(db_session, registry_path)
