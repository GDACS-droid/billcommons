"""Builds a small synthetic Open States bulk-CSV session zip for tests.

Exercises bills / actions / sponsorships / versions / sources CSVs (the
five entity types explicitly called out in BRIEF-wave2.md's verification
gate), plus organizations (referenced by actions) and votes (to exercise
the vote_events/vote_records path too). Column names/layout mirror
docs/sources/openstates-csv.md exactly.

Run directly to write ZZ_2026_Test_Session_csv_fixture.zip next to this
file, or import `build_fixture_zip_bytes()` from tests.
"""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

STATE = "ZZ"
SESSION = "2026 Test Session"


def _csv_bytes(rows: list[dict]) -> bytes:
    if not rows:
        return b""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def build_fixture_zip_bytes() -> bytes:
    prefix = f"{STATE}/{SESSION}/{STATE}_{SESSION}"

    organizations = [
        {
            "id": "ocd-organization/org-house",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
            "extras": "{}",
            "name": "Test State House",
            "parent_id": "",
            "jurisdiction_id": "ocd-jurisdiction/zz",
            "classification": "lower",
        },
    ]

    bills = [
        {
            "id": "ocd-bill/bill-1",
            "identifier": "HB 1",
            "title": "An act relating to synthetic test fixtures",
            "classification": "['bill']",
            "subject": "['testing', 'fixtures']",
            "session_identifier": SESSION,
            "jurisdiction": "Test State",
            "organization_classification": "lower",
        },
        {
            "id": "ocd-bill/bill-2",
            "identifier": "SB 22",
            "title": "An act relating to zip parsing",
            "classification": "['bill']",
            "subject": "['testing']",
            "session_identifier": SESSION,
            "jurisdiction": "Test State",
            "organization_classification": "upper",
        },
        {
            "id": "ocd-bill/bill-3",
            "identifier": "HB 3A",
            "title": "An act relating to idempotent upserts",
            "classification": "['resolution']",
            "subject": "[]",
            "session_identifier": SESSION,
            "jurisdiction": "Test State",
            "organization_classification": "lower",
        },
    ]

    bill_actions = [
        {
            "id": "action-1",
            "created_at": "2026-01-05",
            "updated_at": "2026-01-05",
            "bill_id": "ocd-bill/bill-1",
            "organization_id": "ocd-organization/org-house",
            "description": "Introduced",
            "date": "2026-01-05",
            "classification": "['introduction']",
            "order": 1,
        },
        {
            "id": "action-2",
            "created_at": "2026-02-01",
            "updated_at": "2026-02-01",
            "bill_id": "ocd-bill/bill-1",
            "organization_id": "ocd-organization/org-house",
            "description": "Passed House",
            "date": "2026-02-01",
            "classification": "['passage']",
            "order": 2,
        },
        {
            "id": "action-3",
            "created_at": "2026-01-10",
            "updated_at": "2026-01-10",
            "bill_id": "ocd-bill/bill-2",
            "organization_id": "",
            "description": "Introduced",
            "date": "2026-01-10",
            "classification": "['introduction']",
            "order": 1,
        },
    ]

    bill_sponsorships = [
        {
            "id": "sp-1",
            "created_at": "2026-01-05",
            "updated_at": "2026-01-05",
            "name": "Jane Testperson",
            "entity_type": "person",
            "organization_id": "",
            "person_id": "",
            "bill_id": "ocd-bill/bill-1",
            "primary": "True",
            "classification": "primary",
        },
        {
            "id": "sp-2",
            "created_at": "2026-01-10",
            "updated_at": "2026-01-10",
            "name": "John Fixtureman",
            "entity_type": "person",
            "organization_id": "",
            "person_id": "",
            "bill_id": "ocd-bill/bill-2",
            "primary": "True",
            "classification": "primary",
        },
    ]

    bill_versions = [
        {
            "id": "ver-1",
            "created_at": "2026-01-05",
            "updated_at": "2026-01-05",
            "bill_id": "ocd-bill/bill-1",
            "note": "Introduced version",
            "date": "2026-01-05",
            "classification": "introduced",
            "extras": "{}",
        },
    ]

    bill_version_links = [
        {
            "id": "verlink-1",
            "version_id": "ver-1",
            "media_type": "text/html",
            "url": "https://example.test/bills/hb1/introduced.html",
        },
    ]

    bill_sources = [
        {
            "id": "src-1",
            "created_at": "2026-01-05",
            "updated_at": "2026-01-05",
            "bill_id": "ocd-bill/bill-1",
            "note": "",
            "url": "https://example.test/bills/hb1",
        },
        {
            "id": "src-2",
            "created_at": "2026-01-10",
            "updated_at": "2026-01-10",
            "bill_id": "ocd-bill/bill-2",
            "note": "",
            "url": "https://example.test/bills/sb22",
        },
    ]

    bill_abstracts = [
        {
            "id": "abs-1",
            "created_at": "2026-01-05",
            "updated_at": "2026-01-05",
            "bill_id": "ocd-bill/bill-1",
            "abstract": "This is a synthetic abstract for fixture bill HB 1.",
            "note": "",
        },
    ]

    votes = [
        {
            "id": "vote-1",
            "identifier": "Vote 1",
            "motion_text": "Passage",
            "motion_classification": "['passage']",
            "start_date": "2026-02-01",
            "result": "pass",
            "organization_id": "ocd-organization/org-house",
            "bill_id": "ocd-bill/bill-1",
            "bill_action_id": "action-2",
            "jurisdiction": "Test State",
            "session_identifier": SESSION,
        },
    ]

    vote_counts = [
        {"id": "vc-1", "created_at": "2026-02-01", "updated_at": "2026-02-01", "vote_event_id": "vote-1", "option": "yes", "value": "60"},
        {"id": "vc-2", "created_at": "2026-02-01", "updated_at": "2026-02-01", "vote_event_id": "vote-1", "option": "no", "value": "40"},
    ]

    vote_people = [
        {"id": "vp-1", "created_at": "2026-02-01", "updated_at": "2026-02-01", "vote_event_id": "vote-1", "option": "yes", "voter_name": "Jane Testperson", "voter_id": "", "note": ""},
    ]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "README",
            "Open States Data Export\n\nState: ZZ\nSession: 2026 Test Session\n"
            "Generated At: 2026-07-23T00:00:00Z\nCSV Format Version: 2.1\n",
        )
        zf.writestr(f"{prefix}_organizations.csv", _csv_bytes(organizations))
        zf.writestr(f"{prefix}_bills.csv", _csv_bytes(bills))
        zf.writestr(f"{prefix}_bill_actions.csv", _csv_bytes(bill_actions))
        zf.writestr(f"{prefix}_bill_sponsorships.csv", _csv_bytes(bill_sponsorships))
        zf.writestr(f"{prefix}_bill_versions.csv", _csv_bytes(bill_versions))
        zf.writestr(f"{prefix}_bill_version_links.csv", _csv_bytes(bill_version_links))
        zf.writestr(f"{prefix}_bill_sources.csv", _csv_bytes(bill_sources))
        zf.writestr(f"{prefix}_bill_abstracts.csv", _csv_bytes(bill_abstracts))
        zf.writestr(f"{prefix}_votes.csv", _csv_bytes(votes))
        zf.writestr(f"{prefix}_vote_counts.csv", _csv_bytes(vote_counts))
        zf.writestr(f"{prefix}_vote_people.csv", _csv_bytes(vote_people))
    return buf.getvalue()


if __name__ == "__main__":
    out = Path(__file__).parent / "ZZ_2026_Test_Session_csv_fixture.zip"
    out.write_bytes(build_fixture_zip_bytes())
    print(f"wrote {out}")
