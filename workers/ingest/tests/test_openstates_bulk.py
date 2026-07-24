"""Tests for openstates_bulk.ingest_session_csv_zip against the synthetic
fixture zip (tests/fixtures/build_fixture_zip.py).

Business-logic intent encoded here (per Rule 9 -- these should fail if the
underlying semantics regress, not just if syntax breaks):
    - parsing populates every entity type BRIEF-wave2.md calls out
      (bills/actions/sponsorships/versions/sources), plus subjects/votes,
      from real column names read off the Open States export source.
    - running the same zip twice is idempotent: identical row counts, no
      duplicate rows, and the second run reports bills as "unchanged" via
      the checksum short-circuit (proving we didn't re-derive/re-write
      data that hadn't actually changed upstream).
    - every touched entity carries provenance (source_name/retrieved_at/
      raw_ref at minimum) -- this is what lets Bill Commons show
      attribution + last-updated per SPEC.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import func, select

from billcommons_ingest.openstates_bulk import ingest_session_csv_zip
from billcommons_schema.models import (
    Bill,
    BillAction,
    BillDocument,
    BillSubject,
    BillVersion,
    Jurisdiction,
    Session as SessionModel,
    Sponsorship,
    VoteEvent,
    VoteRecord,
)
from tests.fixtures.build_fixture_zip import build_fixture_zip_bytes


def _make_session_row(db_session) -> SessionModel:
    jurisdiction = Jurisdiction(
        name="Test State", abbreviation=f"ZZ_TEST_{uuid.uuid4().hex[:8].upper()}", classification="state"
    )
    db_session.add(jurisdiction)
    db_session.flush()
    session_row = SessionModel(
        jurisdiction_id=jurisdiction.id,
        identifier="2026 Test Session",
        active=True,
    )
    db_session.add(session_row)
    db_session.flush()
    return session_row


def test_ingest_creates_bills_with_normalized_identifiers(db_session, rawstore):
    session_row = _make_session_row(db_session)
    zip_bytes = build_fixture_zip_bytes()

    result = ingest_session_csv_zip(
        db_session, zip_bytes, session_row=session_row, rawstore=rawstore
    )
    db_session.flush()

    assert result.bills_created == 3
    assert result.bills_updated == 0
    assert result.bills_unchanged == 0

    bills = db_session.execute(
        select(Bill).where(Bill.session_id == session_row.id).order_by(Bill.identifier_norm)
    ).scalars().all()
    identifiers_norm = {b.identifier_norm for b in bills}
    # "HB 3A" exercises the trailing-suffix path in normalize_bill_number;
    # if that normalization regressed, this bill would fail to dedupe
    # correctly against itself on the second pass below.
    assert identifiers_norm == {"HB 1", "SB 22", "HB 3A"}


def test_ingest_populates_actions_sponsorships_versions_sources(db_session, rawstore):
    session_row = _make_session_row(db_session)
    zip_bytes = build_fixture_zip_bytes()
    ingest_session_csv_zip(db_session, zip_bytes, session_row=session_row, rawstore=rawstore)
    db_session.flush()

    bill_hb1 = db_session.execute(
        select(Bill).where(Bill.session_id == session_row.id, Bill.identifier_norm == "HB 1")
    ).scalar_one()

    # Actions: two rows for HB 1, and latest_action_* derived from max-order.
    actions = db_session.execute(
        select(BillAction).where(BillAction.bill_id == bill_hb1.id)
    ).scalars().all()
    assert len(actions) == 2
    assert bill_hb1.latest_action_text == "Passed House"
    assert bill_hb1.introduced_date is not None

    # Sponsorships: name-based sponsor captured even without a resolved person_id.
    sponsorships = db_session.execute(
        select(Sponsorship).where(Sponsorship.bill_id == bill_hb1.id)
    ).scalars().all()
    assert len(sponsorships) == 1
    assert sponsorships[0].name == "Jane Testperson"
    assert sponsorships[0].primary is True

    # Versions + documents (from bill_versions.csv + bill_version_links.csv).
    versions = db_session.execute(
        select(BillVersion).where(BillVersion.bill_id == bill_hb1.id)
    ).scalars().all()
    assert len(versions) == 1
    documents = db_session.execute(
        select(BillDocument).where(BillDocument.bill_version_id == versions[0].id)
    ).scalars().all()
    assert len(documents) == 1
    assert documents[0].url == "https://example.test/bills/hb1/introduced.html"

    # Sources: bill_sources.csv populates bills.source_url.
    assert bill_hb1.source_url == "https://example.test/bills/hb1"

    # Abstract fallback description.
    assert "synthetic abstract" in (bill_hb1.description or "")

    # Subjects parsed from the Python-list-repr array column.
    subjects = db_session.execute(
        select(BillSubject.subject).where(BillSubject.bill_id == bill_hb1.id)
    ).scalars().all()
    assert set(subjects) == {"testing", "fixtures"}


def test_ingest_derives_latest_action_from_max_date_not_csv_order(db_session, rawstore):
    """Regression for the 51-state validation sweep finding (WY/NE/NC/ME/
    LA/KS/HI/CO, 5/5 sampled bills each failing structural validation):
    some states' bulk CSVs do not list bill_actions rows in chronological
    order, so latest_action_date/text must be derived from
    max(action_date) -- NOT from the last row by CSV `order` column. The
    fixture's SB 22 (bill-2) actions are deliberately out of `order` vs.
    `date` order to prove this."""
    session_row = _make_session_row(db_session)
    zip_bytes = build_fixture_zip_bytes()
    ingest_session_csv_zip(db_session, zip_bytes, session_row=session_row, rawstore=rawstore)
    db_session.flush()

    bill_sb22 = db_session.execute(
        select(Bill).where(Bill.session_id == session_row.id, Bill.identifier_norm == "SB 22")
    ).scalar_one()

    # True max(action_date) is 2026-03-01 ("Passed Senate", at `order` 0 --
    # the LOWEST order value, proving order-position is not being used as
    # a proxy for recency).
    max_action_date = db_session.execute(
        select(func.max(BillAction.action_date)).where(BillAction.bill_id == bill_sb22.id)
    ).scalar_one()
    assert max_action_date == date(2026, 3, 1)
    assert bill_sb22.latest_action_date == date(2026, 3, 1)
    assert bill_sb22.latest_action_text == "Passed Senate"

    # introduced_date still correctly picks the min-dated introduction
    # action (2026-01-10), unaffected by the later out-of-order rows.
    assert bill_sb22.introduced_date == date(2026, 1, 10)


def test_ingest_populates_vote_events_and_records(db_session, rawstore):
    """Also the regression case for the natural-key collision bug: the
    fixture's vote-1 and vote-1b share (bill_id, motion_text, start_date)
    but have distinct upstream `id`s -- if vote_events were still keyed on
    that coarse tuple alone, this would wrongly collapse to ONE vote_event
    (losing vote-1b's counts/vote_record entirely), instead of two."""
    session_row = _make_session_row(db_session)
    zip_bytes = build_fixture_zip_bytes()
    ingest_session_csv_zip(db_session, zip_bytes, session_row=session_row, rawstore=rawstore)
    db_session.flush()

    bill_hb1 = db_session.execute(
        select(Bill).where(Bill.session_id == session_row.id, Bill.identifier_norm == "HB 1")
    ).scalar_one()
    vote_events = db_session.execute(
        select(VoteEvent).where(VoteEvent.bill_id == bill_hb1.id).order_by(VoteEvent.upstream_id)
    ).scalars().all()
    assert len(vote_events) == 2, (
        "vote-1 and vote-1b share (bill_id, motion_text, start_date) but are "
        "distinct upstream votes -- they must not collapse into one row"
    )
    vote_event, vote_event_b = vote_events
    assert vote_event.upstream_id == "vote-1"
    assert vote_event.yes_count == 60
    assert vote_event.no_count == 40
    assert vote_event.result == "pass"
    assert vote_event_b.upstream_id == "vote-1b"
    assert vote_event_b.yes_count == 10
    assert vote_event_b.no_count == 5

    vote_records = db_session.execute(
        select(VoteRecord).where(VoteRecord.vote_event_id == vote_event.id)
    ).scalars().all()
    assert len(vote_records) == 1
    assert vote_records[0].voter_name == "Jane Testperson"
    assert vote_records[0].option == "yes"

    vote_records_b = db_session.execute(
        select(VoteRecord).where(VoteRecord.vote_event_id == vote_event_b.id)
    ).scalars().all()
    assert len(vote_records_b) == 1
    assert vote_records_b[0].voter_name == "John Fixtureman"


def test_ingest_provenance_columns_populated(db_session, rawstore):
    session_row = _make_session_row(db_session)
    zip_bytes = build_fixture_zip_bytes()
    result = ingest_session_csv_zip(db_session, zip_bytes, session_row=session_row, rawstore=rawstore)
    db_session.flush()

    assert result.raw_ref is not None
    assert rawstore.exists(result.raw_ref)

    bills = db_session.execute(select(Bill).where(Bill.session_id == session_row.id)).scalars().all()
    for bill in bills:
        assert bill.source_name == "openstates_bulk_csv"
        assert bill.retrieved_at is not None
        assert bill.raw_ref == result.raw_ref
        assert bill.checksum is not None
        assert bill.parser_version == "openstates_bulk_csv/1"


def test_ingest_is_idempotent_on_repeat_run(db_session, rawstore):
    """The core idempotency guarantee from BRIEF-wave2.md: bootstrapping
    the SAME zip twice must yield identical final counts with zero
    duplicate rows, and the second pass must report bills as unchanged
    (proving the checksum short-circuit actually fired rather than
    silently re-writing identical data).

    All counts below are scoped to `session_row.id` (via a join through
    Bill where the child table has no direct session_id column) -- this
    test runs against a real shared live DB (see conftest.py), which can
    carry rows from concurrent real ingests (other jurisdictions/sessions);
    an unscoped whole-table count would flake whenever that concurrent
    activity changes the global count mid-test (FIX 3)."""
    session_row = _make_session_row(db_session)
    zip_bytes = build_fixture_zip_bytes()

    def _scoped_count(model, join_bill: bool = True):
        stmt = select(func.count(model.id))
        if join_bill:
            stmt = stmt.join(Bill, model.bill_id == Bill.id).where(Bill.session_id == session_row.id)
        else:
            stmt = stmt.where(model.session_id == session_row.id)
        return db_session.execute(stmt).scalar_one()

    first = ingest_session_csv_zip(db_session, zip_bytes, session_row=session_row, rawstore=rawstore)
    db_session.flush()

    bill_count_after_first = _scoped_count(Bill, join_bill=False)
    action_count_after_first = _scoped_count(BillAction)
    sponsorship_count_after_first = _scoped_count(Sponsorship)
    version_count_after_first = _scoped_count(BillVersion)
    document_count_after_first = db_session.execute(
        select(func.count(BillDocument.id))
        .join(BillVersion, BillDocument.bill_version_id == BillVersion.id)
        .join(Bill, BillVersion.bill_id == Bill.id)
        .where(Bill.session_id == session_row.id)
    ).scalar_one()
    subject_count_after_first = _scoped_count(BillSubject)
    vote_event_count_after_first = _scoped_count(VoteEvent)
    vote_record_count_after_first = db_session.execute(
        select(func.count(VoteRecord.id))
        .join(VoteEvent, VoteRecord.vote_event_id == VoteEvent.id)
        .join(Bill, VoteEvent.bill_id == Bill.id)
        .where(Bill.session_id == session_row.id)
    ).scalar_one()

    second = ingest_session_csv_zip(
        db_session,
        zip_bytes,
        session_row=session_row,
        rawstore=rawstore,
        retrieved_at=datetime.now(timezone.utc),
    )
    db_session.flush()

    assert second.bills_created == 0, "re-ingesting the same zip must not create new bills"
    assert second.bills_unchanged == first.bills_created, (
        "unchanged bills on the second pass must equal all bills created on the "
        "first pass -- proves the checksum short-circuit fired instead of "
        "silently rewriting identical rows"
    )

    bill_count_after_second = _scoped_count(Bill, join_bill=False)
    assert bill_count_after_second == bill_count_after_first

    assert action_count_after_first == _scoped_count(BillAction)
    assert sponsorship_count_after_first == _scoped_count(Sponsorship)
    assert version_count_after_first == _scoped_count(BillVersion)
    assert (
        document_count_after_first
        == db_session.execute(
            select(func.count(BillDocument.id))
            .join(BillVersion, BillDocument.bill_version_id == BillVersion.id)
            .join(Bill, BillVersion.bill_id == Bill.id)
            .where(Bill.session_id == session_row.id)
        ).scalar_one()
    )
    assert subject_count_after_first == _scoped_count(BillSubject)
    assert vote_event_count_after_first == _scoped_count(VoteEvent)
    assert (
        vote_record_count_after_first
        == db_session.execute(
            select(func.count(VoteRecord.id))
            .join(VoteEvent, VoteRecord.vote_event_id == VoteEvent.id)
            .join(Bill, VoteEvent.bill_id == Bill.id)
            .where(Bill.session_id == session_row.id)
        ).scalar_one()
    )


def test_ingest_missing_optional_csv_is_zero_rows_not_error(db_session, rawstore):
    """A session zip that legitimately has no votes (Open States'
    export_csv() skips writing empty files entirely) must not raise --
    missing file means zero rows for that entity, per
    docs/sources/openstates-csv.md."""
    import io
    import zipfile

    session_row = _make_session_row(db_session)
    # Build a minimal zip with only bills.csv, no votes/actions/etc.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "ZZ/2026 Test Session/ZZ_2026 Test Session_bills.csv",
            "id,identifier,title,classification,subject,session_identifier,jurisdiction,organization_classification\n"
            "ocd-bill/solo,HB 99,Solo bill with no children,['bill'],[],2026 Test Session,Test State,lower\n",
        )

    result = ingest_session_csv_zip(db_session, buf.getvalue(), session_row=session_row, rawstore=rawstore)
    assert result.bills_created == 1
    assert result.actions == 0
    assert result.vote_events == 0
