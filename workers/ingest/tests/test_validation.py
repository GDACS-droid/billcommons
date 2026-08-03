"""Tests for billcommons_ingest.validation using injected httpx transports
for both the deployed search API and the cross-source fetch -- no real
network calls in this file (per BRIEF-wave2.md's transport-injection
pattern, mirrored from test_openstates_api.py / test_fulltext.py).

Business intent per docs/SPEC.md "QA per jurisdiction" + "Coverage state
machine + GREEN criteria": validation must independently re-check ingested
rows (not just re-read them), a leg that hits a network error must be
excluded from the pass-rate denominator rather than silently counted as a
pass or a fail, and a clean validation run must NOT be enough by itself to
promote a jurisdiction to GREEN when full-text coverage is still zero.
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest
from sqlalchemy import select, text

from billcommons_ingest.fulltext import RobotsCache
from billcommons_ingest.validation import (
    ADVISORY_FAIL,
    ADVISORY_PASS,
    FAIL,
    PASS,
    SKIPPED_ROBOTS,
    UNVERIFIABLE,
    UNVERIFIABLE_JS_PAGE,
    ValidationSummary,
    BillValidationResult,
    LegResult,
    apply_validation_result,
    validate_and_record_txnfree,
    validate_jurisdiction_txnfree,
    _check_cross_source,
    _check_search_retrieval,
    _check_structural,
    _distinctive_title_word,
    _rarest_title_word,
    sample_bills,
    validate_and_record,
)
from billcommons_schema.models import (
    Bill,
    BillAction,
    Jurisdiction,
    JurisdictionCoverage,
    Session as SessionModel,
    ValidationRun,
)
from billcommons_shared.db import get_session

# Set by conftest via PGAPPNAME before the engine is built, so every
# connection this test process opens is labelled and can be told apart from
# the production workers sharing this database.
TEST_APPLICATION_NAME = os.environ.get("PGAPPNAME", "billcommons-tests")


def _make_jurisdiction_with_bills(db_session, n=3, abbr=None):
    if abbr is None:
        abbr = f"ZQ_VAL_{uuid.uuid4().hex[:8].upper()}"
    jurisdiction = Jurisdiction(name="Validation Test State", abbreviation=abbr, classification="state")
    db_session.add(jurisdiction)
    db_session.flush()
    session_row = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026 Session", active=True)
    db_session.add(session_row)
    db_session.flush()
    bills = []
    for i in range(n):
        bill = Bill(
            jurisdiction_id=jurisdiction.id,
            session_id=session_row.id,
            identifier=f"HB {i + 1}",
            identifier_norm=f"HB {i + 1}",
            title=f"An act concerning transportation infrastructure funding number {i + 1}",
            source_url=f"https://example-legislature.gov/bills/hb{i + 1}",
        )
        db_session.add(bill)
        bills.append(bill)
    db_session.flush()
    return jurisdiction, session_row, bills


# ---------------------------------------------------------------------------
# Leg 1: structural
# ---------------------------------------------------------------------------


def test_structural_leg_passes_for_complete_bill(db_session):
    _, _, bills = _make_jurisdiction_with_bills(db_session, n=1)
    result = _check_structural(db_session, bills[0])
    assert result.status == PASS


def test_structural_leg_fails_when_source_url_missing(db_session):
    _, _, bills = _make_jurisdiction_with_bills(db_session, n=1)
    bills[0].source_url = None
    db_session.flush()
    result = _check_structural(db_session, bills[0])
    assert result.status == FAIL
    assert "source_url" in result.detail


def test_structural_leg_fails_when_latest_action_date_inconsistent(db_session):
    """Business intent: a bill claiming latest_action_date=X while its own
    bill_actions rows disagree is a real data-integrity bug (a stale field
    that didn't get updated when new actions came in), not a false alarm."""
    _, _, bills = _make_jurisdiction_with_bills(db_session, n=1)
    bill = bills[0]
    import datetime as dt

    db_session.add(
        BillAction(bill_id=bill.id, description="Introduced", action_date=dt.date(2026, 1, 1), order=0)
    )
    db_session.add(
        BillAction(bill_id=bill.id, description="Passed House", action_date=dt.date(2026, 3, 1), order=1)
    )
    db_session.flush()
    bill.latest_action_date = dt.date(2026, 1, 1)  # stale -- real max is 2026-03-01
    db_session.flush()

    result = _check_structural(db_session, bill)
    assert result.status == FAIL
    assert "latest_action_date" in result.detail


# ---------------------------------------------------------------------------
# Leg 2: search retrieval (injected transport)
# ---------------------------------------------------------------------------


def test_distinctive_title_word_skips_stopwords():
    word = _distinctive_title_word("An act relating to transportation infrastructure")
    assert word == "infrastructure" or word == "transportation"
    assert word not in ("relating",)


def test_search_retrieval_passes_when_bill_found_both_queries(db_session):
    _, _, bills = _make_jurisdiction_with_bills(db_session, n=1)
    bill = bills[0]

    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [{"id": str(bill.id)}],
                "pagination": {"page": 1, "per_page": 10, "total": 1, "total_pages": 1},
                "meta": {"api_version": "v1", "request_id": "test"},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.billcommons.org/api/v1")
    legs = _check_search_retrieval(client, bill, "ZQ", db=db_session)
    by_leg = {leg.leg: leg for leg in legs}
    assert by_leg["bill_number_search"].status == PASS
    assert by_leg["keyword_search"].status == ADVISORY_PASS


def test_search_retrieval_fails_when_bill_number_search_empty(db_session):
    """bill_number_search is MANDATORY -- its failure is a real leg FAIL,
    unlike keyword_search (advisory, see test above/below)."""
    _, _, bills = _make_jurisdiction_with_bills(db_session, n=1)
    bill = bills[0]

    def handler(request):
        return httpx.Response(
            200,
            json={"data": [], "pagination": {"page": 1, "per_page": 10, "total": 0, "total_pages": 0},
                  "meta": {"api_version": "v1", "request_id": "test"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.billcommons.org/api/v1")
    legs = _check_search_retrieval(client, bill, "ZQ", db=db_session)
    assert len(legs) == 1
    assert legs[0].leg == "bill_number_search"
    assert legs[0].status == FAIL


def test_search_retrieval_keyword_miss_is_advisory_not_fail(db_session):
    """Business intent (Finding 3): a keyword-search miss must NOT fail the
    leg by itself -- it's recorded as advisory_fail, distinct from a real
    FAIL, and excluded from the pass-rate denominator."""
    _, _, bills = _make_jurisdiction_with_bills(db_session, n=1)
    bill = bills[0]

    def handler(request):
        q = request.url.params.get("q")
        if q == bill.identifier:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": str(bill.id)}],
                    "pagination": {"page": 1, "per_page": 10, "total": 1, "total_pages": 1},
                    "meta": {"api_version": "v1", "request_id": "test"},
                },
            )
        return httpx.Response(
            200,
            json={"data": [], "pagination": {"page": 1, "per_page": 25, "total": 0, "total_pages": 0},
                  "meta": {"api_version": "v1", "request_id": "test"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.billcommons.org/api/v1")
    legs = _check_search_retrieval(client, bill, "ZQ", db=db_session)
    by_leg = {leg.leg: leg for leg in legs}
    assert by_leg["bill_number_search"].status == PASS
    assert by_leg["keyword_search"].status == ADVISORY_FAIL

    summary = ValidationSummary(jurisdiction_abbr="ZQ", session_id=None)
    bill_result = BillValidationResult(bill_id=str(bill.id), identifier=bill.identifier)
    bill_result.legs = legs
    summary.bills.append(bill_result)
    assert summary.checks_failed == 0, "advisory_fail must not count toward checks_failed"


def test_search_retrieval_is_unverifiable_on_network_error(db_session):
    _, _, bills = _make_jurisdiction_with_bills(db_session, n=1)
    bill = bills[0]

    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.billcommons.org/api/v1")
    legs = _check_search_retrieval(client, bill, "ZQ", db=db_session)
    assert len(legs) == 1
    assert legs[0].leg == "bill_number_search"
    assert legs[0].status == UNVERIFIABLE


def test_rarest_title_word_prefers_less_common_over_longer(db_session):
    """Business intent (Finding 3): the keyword probe should prefer a word
    that is RARE across ingested titles, not just the longest candidate --
    a common word like 'amendments' can appear in many titles and produce a
    false-alarm advisory_fail even when the search index is fine."""
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    from billcommons_schema.models import Bill as BillModel

    common_titles = [
        f"An act concerning amendments to statute number {i}" for i in range(5)
    ]
    for i, t in enumerate(common_titles):
        db_session.add(
            BillModel(
                jurisdiction_id=jurisdiction.id,
                session_id=session_row.id,
                identifier=f"SB {100 + i}",
                identifier_norm=f"SB {100 + i}",
                title=t,
                source_url=f"https://example-legislature.gov/bills/sb{100 + i}",
            )
        )
    db_session.flush()

    word = _rarest_title_word(db_session, "An act concerning amendments to cryptocurrency taxation")
    assert word == "cryptocurrency" or word == "taxation"
    assert word != "amendments"


# ---------------------------------------------------------------------------
# Leg 3: cross-source (injected transport + robots cache)
# ---------------------------------------------------------------------------


def test_cross_source_passes_when_identifier_on_page(db_session):
    _, _, bills = _make_jurisdiction_with_bills(db_session, n=1)
    bill = bills[0]

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=f"<html>Bill {bill.identifier} status: passed</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == PASS


def test_cross_source_fails_when_identifier_absent_but_page_has_real_content(db_session):
    """A genuine mismatch: substantial visible text AND other bill-number
    tokens present, just not ours -- this must stay a real FAIL, not get
    reclassified as a JS-page false-negative (Finding 2 says the heuristic
    should only catch near-empty/no-bill-number-token pages)."""
    _, _, bills = _make_jurisdiction_with_bills(db_session, n=1)
    bill = bills[0]

    page = (
        "<html><body><h1>Legislature Bill Tracker</h1>"
        + "<p>This session includes SB 42, HB 200, and SR 7 among the bills "
        "under consideration by the committee this term. " * 8
        + "</p></body></html>"
    )

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=page)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == FAIL


def test_cross_source_passes_co_year_insert_surface_form(db_session):
    """CO real-world rendering: 'HB 1057' shows up on the official page as
    'HB26-1057' -- a 2-digit session-year inserted between the letter prefix
    and the number."""
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 1057",
        identifier_norm="HB 1057",
        title="An act",
        source_url="https://leg.colorado.gov/bills/hb26-1057",
    )
    db_session.add(bill)
    db_session.flush()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<html><body>See HB26-1057 for the full text of this bill.</body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == PASS


def test_cross_source_passes_co_year_insert_with_zero_padded_number(db_session):
    """Regression for a real fail found running `validate --all` after this
    round's initial fix: CO's year-insert form ALSO zero-pads the bill
    number when it's below the site's fixed display width, even in the
    year-prefixed form -- "SB 24" (identifier, no leading zeros) renders on
    leg.colorado.gov as literally "SB26-024" (3-digit zero-padded), not
    "SB2624". The initial year-insert regex only matched the EXACT digit
    string with no padding allowance and missed this real page."""
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="SB 24",
        identifier_norm="SB 24",
        title="An act",
        source_url="https://leg.colorado.gov/bills/sb26-024",
    )
    db_session.add(bill)
    db_session.flush()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<html><body>SB26-024 State &amp; Local Unmanned Aircraft Regulation</body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == PASS


def test_cross_source_passes_spelled_out_prefix_surface_form(db_session):
    """Regression for real fails found running `validate --all`: NC
    (www.ncleg.gov) and PA (www.palegis.us) page titles never render the
    abbreviated prefix at all, only the spelled-out form -- "SB 362" shows
    up as "Senate Bill 362", "SR 14" as "Senate Resolution 14"."""
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="SB 362",
        identifier_norm="SB 362",
        title="An act",
        source_url="https://www.ncleg.gov/BillLookUp/2025/S362",
    )
    db_session.add(bill)
    db_session.flush()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text="<html><body>Senate Bill 362 (2025-2026 Session) - North Carolina General Assembly</body></html>"
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == PASS


def test_cross_source_passes_nj_bare_surface_form(db_session):
    """NJ real-world rendering: 'S 907' shows up bare/concatenated as 'S907'."""
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="S 907",
        identifier_norm="S 907",
        title="An act",
        source_url="https://www.njleg.state.nj.us/bill/s907",
    )
    db_session.add(bill)
    db_session.flush()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<html><body>Bill S907 was introduced in the Senate.</body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == PASS


def test_cross_source_passes_nh_zero_padded_first_letter_surface_form(db_session):
    """NH/MA real-world rendering: 'HB 914' shows up zero-padded using just
    the FIRST letter of the (multi-letter) prefix, as 'H0914' -- not
    'HB0914'."""
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 914",
        identifier_norm="HB 914",
        title="An act",
        source_url="https://www.gencourt.state.nh.us/bill_status/h0914",
    )
    db_session.add(bill)
    db_session.flush()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<html><body>Bill status for H0914 as of today.</body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == PASS


def test_cross_source_does_not_match_bare_number_without_alpha_prefix(db_session):
    """The tolerant surface-form matching must NOT loosen so far that any
    bare 3-4 digit number on the page counts as a match -- the alpha prefix
    (or its first letter) must be immediately adjacent. A page containing
    an unrelated 4-digit number (e.g. a year, zip code, or another bill's
    number with a DIFFERENT prefix) must not false-positive a match for our
    bill."""
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 1057",
        identifier_norm="HB 1057",
        title="An act",
        source_url="https://example-legislature.gov/other-page",
    )
    db_session.add(bill)
    db_session.flush()

    page = (
        "<html><body><h1>Legislature Bill Tracker</h1>"
        "<p>This session includes SB 1057 and AB 1057 among the bills "
        "under consideration by the committee. Filed in session 1057. "
        "Zip code 10571057 on the letterhead. This page has substantial "
        "visible content well past the minimum-length threshold so it is "
        "not misclassified as a JS app shell, repeated several more times "
        "to be safe about length requirements for this specific check. "
        * 4
        + "</p></body></html>"
    )

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=page)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == FAIL, (
        "a bare '1057' or a DIFFERENT prefix's '1057' (SB/AB, not HB) must never satisfy "
        "the match for HB 1057 -- the alpha prefix must be adjacent to the number"
    )


def test_cross_source_js_page_heuristic_strips_style_block_contents(db_session):
    """Regression for a real gap found live against wyoleg.gov during the
    51-state sweep: a JS app shell's <style> block (inline font-face CSS)
    can be several KB on its own, which would make a near-empty page look
    "substantial" if only the <style> TAGS were stripped and not their
    contents -- silently defeating the whole unverifiable_js_page
    heuristic and leaving it a real (wrong) fail."""
    _, _, bills = _make_jurisdiction_with_bills(db_session, n=1)
    bill = bills[0]

    huge_css = "@font-face{font-family:'Roboto';src:url(x.woff2);} " * 60
    page = (
        "<!doctype html><html><head><title>Legislative Service Office</title>"
        f"<style>{huge_css}</style></head>"
        '<body><div id="app"></div></body></html>'
    )

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=page)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == UNVERIFIABLE_JS_PAGE


def test_cross_source_is_unverifiable_js_page_when_shell_has_no_content(db_session):
    """Finding 2: a JS-rendered legislature site (e.g. wyoleg.gov) returns
    200 with an app shell that has no server-rendered bill text -- the
    identifier's absence proves nothing about our data, so this must be
    classified as unverifiable_js_page, not a real fail, and excluded from
    the pass-rate denominator."""
    _, _, bills = _make_jurisdiction_with_bills(db_session, n=1)
    bill = bills[0]

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            text='<html><body><div id="app"></div>'
            '<script src="/static/app.js"></script></body></html>',
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == UNVERIFIABLE_JS_PAGE

    summary = ValidationSummary(jurisdiction_abbr="ZQ", session_id=None)
    bill_result = BillValidationResult(bill_id=str(bill.id), identifier=bill.identifier)
    bill_result.legs = [result]
    summary.bills.append(bill_result)
    assert summary.checks_run == 0, "unverifiable_js_page must be excluded from the denominator"


def test_cross_source_skips_when_robots_disallows(db_session):
    _, _, bills = _make_jurisdiction_with_bills(db_session, n=1)
    bill = bills[0]

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        return httpx.Response(200, text="should never reach here")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == SKIPPED_ROBOTS


def test_cross_source_does_not_false_positive_on_shb_substring(db_session):
    """Regression for Finding 2: un-anchored substring matching let 'hb 1057'
    match inside 'shb 1057' (a DIFFERENT bill's prefix, e.g. a "Substitute
    House Bill", that merely contains ours as a substring) -- a real false
    PASS in the one tool whose job is exact verification."""
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 1057",
        identifier_norm="HB 1057",
        title="An act",
        source_url="https://example-legislature.gov/other-bill",
    )
    db_session.add(bill)
    db_session.flush()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            text="<html><body><h1>Legislature Bill Tracker</h1>"
            "<p>SHB 1057 was substituted and passed the committee with amendments, "
            "unrelated to the bill under test here today. This page has substantial "
            "visible content well past the minimum-length threshold so it is not "
            "misclassified as a JS app shell, repeated to be safe. " * 4
            + "</p></body></html>",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == FAIL, "'shb 1057' must NOT satisfy a match for 'hb 1057'"


def test_cross_source_does_not_false_positive_on_h1_inside_graph1_or_march1(db_session):
    """Regression for Finding 2: the single-digit-bill surface form 'h1' (for
    identifier 'HB 1') must not match as a substring of unrelated words like
    'graph1' or 'march1' on the page."""
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 1",
        identifier_norm="HB 1",
        title="An act",
        source_url="https://example-legislature.gov/other-bill",
    )
    db_session.add(bill)
    db_session.flush()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            text="<html><body><h1>Legislature Bill Tracker</h1>"
            "<p>See graph1 in the appendix, filed on march1 2026, for unrelated "
            "committee data such as SB 42 and HB 200, with substantial page content "
            "well past the minimum-length threshold, repeated to be safe. " * 4
            + "</p></body></html>",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == FAIL, "'h1' must NOT match inside 'graph1'/'march1'"


def test_cross_source_true_positive_single_digit_bill_h1(db_session):
    """Same single-digit bill (HB 1) must still genuinely PASS when 'H1'
    appears as its own real token, boundary-delimited."""
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 1",
        identifier_norm="HB 1",
        title="An act",
        source_url="https://example-legislature.gov/hb1",
    )
    db_session.add(bill)
    db_session.flush()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<html><body>Bill H1 was introduced in the House.</body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == PASS


def test_cross_source_does_not_false_positive_on_senate_bill_362_inside_3625(db_session):
    """Regression for Finding 2: 'senate bill 362' must not match as a
    substring of 'senate bill 3625' -- a genuinely different bill number that
    merely starts with ours."""
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="SB 362",
        identifier_norm="SB 362",
        title="An act",
        source_url="https://www.ncleg.gov/other-bill",
    )
    db_session.add(bill)
    db_session.flush()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            text="<html><body><h1>North Carolina General Assembly</h1>"
            "<p>Senate Bill 3625 (2025-2026 Session) status and history, along with "
            "HB 200 and SR 14, substantial page content well "
            "past the minimum-length threshold, repeated to be safe. " * 4
            + "</p></body></html>",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == FAIL, "'senate bill 362' must NOT match inside 'senate bill 3625'"


def test_cross_source_true_positive_senate_bill_362(db_session):
    """Same bill (SB 362) must still genuinely PASS against the real
    boundary-delimited spelled-out surface form."""
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="SB 362",
        identifier_norm="SB 362",
        title="An act",
        source_url="https://www.ncleg.gov/BillLookUp/2025/S362",
    )
    db_session.add(bill)
    db_session.flush()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text="<html><body>Senate Bill 362 (2025-2026 Session) - North Carolina General Assembly</body></html>"
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == PASS


def test_cross_source_does_not_false_positive_on_h0914_inside_h09140(db_session):
    """Regression for Finding 2: 'h0914' must not match as a substring of
    'h09140' -- a different, longer bill number on the page."""
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 914",
        identifier_norm="HB 914",
        title="An act",
        source_url="https://www.gencourt.state.nh.us/other-bill",
    )
    db_session.add(bill)
    db_session.flush()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            text="<html><body><h1>NH General Court Bill Status</h1>"
            "<p>Bill status for H09140 as of today, along with SB 42 and HB 200, "
            "substantial page content well past the minimum-length threshold, "
            "repeated to be safe. " * 4
            + "</p></body></html>",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == FAIL, "'h0914' must NOT match inside 'h09140'"


def test_cross_source_is_unverifiable_on_fetch_error(db_session):
    _, _, bills = _make_jurisdiction_with_bills(db_session, n=1)
    bill = bills[0]

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ConnectTimeout("timed out")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots_cache = RobotsCache(client=client)
    result = _check_cross_source(client, robots_cache, bill)
    assert result.status == UNVERIFIABLE


# ---------------------------------------------------------------------------
# Summary math + non-checkable-leg exclusion
# ---------------------------------------------------------------------------


def test_pass_rate_excludes_unverifiable_and_skipped_robots():
    summary = ValidationSummary(jurisdiction_abbr="ZQ", session_id=None)
    bill_result = BillValidationResult(bill_id="1", identifier="HB 1")
    bill_result.legs = [
        LegResult("structural", PASS, "ok"),
        LegResult("search_retrieval", FAIL, "no match"),
        LegResult("cross_source", UNVERIFIABLE, "network down"),
    ]
    summary.bills.append(bill_result)

    # 1 pass + 1 fail out of 2 CHECKABLE legs -- the unverifiable leg must
    # not inflate or deflate the denominator.
    assert summary.checks_run == 2
    assert summary.checks_failed == 1
    assert summary.pass_rate == 0.5


def test_pass_rate_is_none_when_nothing_checkable():
    summary = ValidationSummary(jurisdiction_abbr="ZQ", session_id=None)
    bill_result = BillValidationResult(bill_id="1", identifier="HB 1")
    bill_result.legs = [
        LegResult("search_retrieval", UNVERIFIABLE, "network down"),
        LegResult("cross_source", SKIPPED_ROBOTS, "robots disallow"),
    ]
    summary.bills.append(bill_result)
    assert summary.pass_rate is None


# ---------------------------------------------------------------------------
# sample_bills
# ---------------------------------------------------------------------------


def test_sample_bills_caps_at_sample_size(db_session):
    jurisdiction, _, bills = _make_jurisdiction_with_bills(db_session, n=8)
    sampled = sample_bills(db_session, jurisdiction, sample_size=5)
    assert len(sampled) == 5
    assert len(set(b.id for b in sampled)) == 5


def test_sample_bills_returns_all_when_fewer_than_sample_size(db_session):
    jurisdiction, _, bills = _make_jurisdiction_with_bills(db_session, n=2)
    sampled = sample_bills(db_session, jurisdiction, sample_size=5)
    assert len(sampled) == 2


# ---------------------------------------------------------------------------
# GREEN-criteria honesty: apply_validation_result
# ---------------------------------------------------------------------------


def test_clean_validation_with_no_fulltext_caps_at_validating_not_green(db_session):
    """The core GREEN-honesty requirement from the task brief: a perfect
    pass rate must NOT promote a jurisdiction to GREEN while
    full_text_count is still 0 -- GREEN criterion #5 (full text searchable
    wherever available) is a separate, unmet condition.

    `full_text_available_count=1` is what makes this the "text exists but we
    don't have it" case; leaving it 0 would mean nothing is obtainable, which
    is the vacuous-criterion case covered separately below.
    """
    jurisdiction, session_row, bills = _make_jurisdiction_with_bills(db_session, n=1)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        status="METADATA_SEARCHABLE",
        bill_count=1,
        full_text_count=0,
        full_text_available_count=1,
    )
    db_session.add(coverage)
    db_session.flush()

    summary = ValidationSummary(jurisdiction_abbr=jurisdiction.abbreviation, session_id=str(session_row.id))
    bill_result = BillValidationResult(bill_id=str(bills[0].id), identifier=bills[0].identifier)
    bill_result.legs = [LegResult("structural", PASS, "ok"), LegResult("search_retrieval", PASS, "ok")]
    summary.bills.append(bill_result)

    apply_validation_result(db_session, jurisdiction, summary)
    db_session.flush()
    db_session.refresh(coverage)

    assert coverage.status == "VALIDATING"
    assert coverage.status != "GREEN"
    assert "full-text" in (coverage.known_gaps or "").lower()


def test_clean_validation_with_fulltext_present_promotes_to_green(db_session):
    jurisdiction, session_row, bills = _make_jurisdiction_with_bills(db_session, n=1)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        status="METADATA_SEARCHABLE",
        bill_count=1,
        full_text_count=1,
        full_text_available_count=1,
    )
    db_session.add(coverage)
    db_session.flush()

    summary = ValidationSummary(jurisdiction_abbr=jurisdiction.abbreviation, session_id=str(session_row.id))
    bill_result = BillValidationResult(bill_id=str(bills[0].id), identifier=bills[0].identifier)
    bill_result.legs = [LegResult("structural", PASS, "ok"), LegResult("search_retrieval", PASS, "ok")]
    summary.bills.append(bill_result)

    apply_validation_result(db_session, jurisdiction, summary)
    db_session.flush()
    db_session.refresh(coverage)

    assert coverage.status == "GREEN"


def _clean_summary(jurisdiction, session_row, bills):
    summary = ValidationSummary(
        jurisdiction_abbr=jurisdiction.abbreviation, session_id=str(session_row.id)
    )
    bill_result = BillValidationResult(bill_id=str(bills[0].id), identifier=bills[0].identifier)
    bill_result.legs = [LegResult("structural", PASS, "ok"), LegResult("search_retrieval", PASS, "ok")]
    summary.bills.append(bill_result)
    return summary


def test_partial_fulltext_coverage_is_not_green(db_session):
    """A clean sample plus SOME text must not earn GREEN.

    Business intent: this is the bug that let 19 jurisdictions wear GREEN
    while holding text for 1-2% of their obtainable bills. SPEC criterion #5
    is "full text searchable WHEREVER technically available", so the bar is a
    ratio, not `full_text_count > 0`. Revert the gate to a >0 check and this
    fails.
    """
    jurisdiction, session_row, bills = _make_jurisdiction_with_bills(db_session, n=1)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        status="FULL_TEXT_SEARCHABLE",
        bill_count=100,
        full_text_count=37,  # 37% of what's obtainable -- the real PA shape
        full_text_available_count=100,
    )
    db_session.add(coverage)
    db_session.flush()

    apply_validation_result(db_session, jurisdiction, _clean_summary(jurisdiction, session_row, bills))
    db_session.flush()
    db_session.refresh(coverage)

    assert coverage.status == "VALIDATING"
    # A crawl still in progress is not a fault -- it must not read as DEGRADED.
    assert coverage.status != "DEGRADED"
    assert "37/100" in (coverage.known_gaps or "")


def test_stale_green_is_demoted_when_coverage_is_below_threshold(db_session):
    """An already-GREEN row must lose the badge once measured honestly --
    otherwise the 19 pre-existing GREENs would keep it forever."""
    jurisdiction, session_row, bills = _make_jurisdiction_with_bills(db_session, n=1)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        status="GREEN",
        bill_count=100,
        full_text_count=2,
        full_text_available_count=100,
    )
    db_session.add(coverage)
    db_session.flush()

    apply_validation_result(db_session, jurisdiction, _clean_summary(jurisdiction, session_row, bills))
    db_session.flush()
    db_session.refresh(coverage)

    assert coverage.status == "VALIDATING"


def test_nothing_obtainable_earns_green_but_states_the_limitation(db_session):
    """The DC/TN case: every source is robots-blocked, so criterion #5 is
    vacuous and a clean sample earns GREEN -- but known_gaps must say so, or
    GREEN would imply full-text search users will never get."""
    jurisdiction, session_row, bills = _make_jurisdiction_with_bills(db_session, n=1)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        status="METADATA_SEARCHABLE",
        bill_count=50,
        full_text_count=0,
        full_text_available_count=0,
    )
    db_session.add(coverage)
    db_session.flush()

    apply_validation_result(db_session, jurisdiction, _clean_summary(jurisdiction, session_row, bills))
    db_session.flush()
    db_session.refresh(coverage)

    assert coverage.status == "GREEN"
    assert "no full text obtainable" in (coverage.known_gaps or "").lower()
    assert "metadata-searchable" in (coverage.known_gaps or "").lower()


def test_uncomputed_available_count_never_promotes_to_green(db_session):
    """NULL means "not measured yet", NOT "nothing obtainable".

    Business intent: migration 0003 adds this column NULL rather than 0
    precisely because 0 reads as the vacuous-criterion case. If NULL were
    treated as 0, every un-recomputed row on deploy would be handed a free
    GREEN -- all 51 at once.
    """
    jurisdiction, session_row, bills = _make_jurisdiction_with_bills(db_session, n=1)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        status="METADATA_SEARCHABLE",
        bill_count=100,
        full_text_count=100,
        full_text_available_count=None,
    )
    db_session.add(coverage)
    db_session.flush()

    apply_validation_result(db_session, jurisdiction, _clean_summary(jurisdiction, session_row, bills))
    db_session.flush()
    db_session.refresh(coverage)

    assert coverage.status != "GREEN"
    assert "not yet computed" in (coverage.known_gaps or "").lower()


def test_degraded_recovers_when_validation_passes_again(db_session):
    """DEGRADED must clear on a passing sample, or it is a roach motel.

    Business intent: DEGRADED is set automatically by a failing sample, so it
    has to clear automatically too. It didn't -- the promote list omitted it,
    which stranded 31 production rows in DEGRADED, several sitting at
    validation_pass_rate 1.0, unable to ever go GREEN again.
    """
    jurisdiction, session_row, bills = _make_jurisdiction_with_bills(db_session, n=1)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        status="DEGRADED",
        bill_count=10,
        full_text_count=10,
        full_text_available_count=10,
    )
    db_session.add(coverage)
    db_session.flush()

    apply_validation_result(db_session, jurisdiction, _clean_summary(jurisdiction, session_row, bills))
    db_session.flush()
    db_session.refresh(coverage)

    assert coverage.status == "GREEN"


def test_degraded_with_clean_sample_but_thin_fulltext_lifts_to_validating(db_session):
    """Recovery is not all-or-nothing: a clean sample lifts DEGRADED out even
    when full text is still landing -- to VALIDATING, not straight to GREEN."""
    jurisdiction, session_row, bills = _make_jurisdiction_with_bills(db_session, n=1)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        status="DEGRADED",
        bill_count=100,
        full_text_count=5,
        full_text_available_count=100,
    )
    db_session.add(coverage)
    db_session.flush()

    apply_validation_result(db_session, jurisdiction, _clean_summary(jurisdiction, session_row, bills))
    db_session.flush()
    db_session.refresh(coverage)

    assert coverage.status == "VALIDATING"


def test_blocked_is_never_lifted_by_a_passing_sample(db_session):
    """BLOCKED is operator-set; only an operator clears it. A clean sample
    must not silently un-block a jurisdiction taken out of service."""
    jurisdiction, session_row, bills = _make_jurisdiction_with_bills(db_session, n=1)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        status="BLOCKED",
        bill_count=10,
        full_text_count=10,
        full_text_available_count=10,
    )
    db_session.add(coverage)
    db_session.flush()

    apply_validation_result(db_session, jurisdiction, _clean_summary(jurisdiction, session_row, bills))
    db_session.flush()
    db_session.refresh(coverage)

    assert coverage.status == "BLOCKED"


def test_failing_validation_demotes_to_degraded(db_session):
    jurisdiction, session_row, bills = _make_jurisdiction_with_bills(db_session, n=1)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        status="VALIDATING",
        bill_count=1,
        full_text_count=1,
        full_text_available_count=1,
    )
    db_session.add(coverage)
    db_session.flush()

    summary = ValidationSummary(jurisdiction_abbr=jurisdiction.abbreviation, session_id=str(session_row.id))
    bill_result = BillValidationResult(bill_id=str(bills[0].id), identifier=bills[0].identifier)
    bill_result.legs = [LegResult("structural", FAIL, "broken"), LegResult("search_retrieval", FAIL, "broken")]
    summary.bills.append(bill_result)

    apply_validation_result(db_session, jurisdiction, summary)
    db_session.flush()
    db_session.refresh(coverage)

    assert coverage.status == "DEGRADED"


def test_zero_bill_count_coverage_rows_are_left_untouched(db_session):
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=1)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id, session_id=session_row.id, status="SOURCE_IDENTIFIED", bill_count=0
    )
    db_session.add(coverage)
    db_session.flush()

    summary = ValidationSummary(jurisdiction_abbr=jurisdiction.abbreviation, session_id=None)
    apply_validation_result(db_session, jurisdiction, summary)
    db_session.flush()
    db_session.refresh(coverage)

    assert coverage.status == "SOURCE_IDENTIFIED"
    assert coverage.validation_pass_rate is None


# ---------------------------------------------------------------------------
# End-to-end validate_and_record with fully injected clients
# ---------------------------------------------------------------------------


def test_validate_job_kind_dispatch_updates_jurisdiction_coverage_status(db_session):
    """Business intent: cli.py's worker `validate` job-kind dispatch calls
    exactly this function (validate_and_record) with the jurisdiction
    resolved from the job payload -- if this stops updating
    jurisdiction_coverage.status, the periodic validation scheduler could
    enqueue jobs forever without ever actually promoting a jurisdiction to
    GREEN (the whole point of wiring `validate` into the worker loop)."""
    jurisdiction, session_row, bills = _make_jurisdiction_with_bills(db_session, n=2)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        status="FULL_TEXT_SEARCHABLE",
        bill_count=2,
        full_text_count=2,
        # Text held for everything obtainable -- the state that should reach
        # GREEN. NULL would mean "denominator not yet measured".
        full_text_available_count=2,
    )
    db_session.add(coverage)
    db_session.flush()

    def search_handler(request):
        return httpx.Response(
            200,
            json={
                "data": [{"id": str(b.id)} for b in bills],
                "pagination": {"page": 1, "per_page": 25, "total": len(bills), "total_pages": 1},
                "meta": {"api_version": "v1", "request_id": "test"},
            },
        )

    def source_handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="HB 1 HB 2 An act concerning transportation infrastructure funding")

    search_client = httpx.Client(
        transport=httpx.MockTransport(search_handler), base_url="https://api.billcommons.org/api/v1"
    )
    source_client = httpx.Client(transport=httpx.MockTransport(source_handler))

    # Mirrors cli.py cmd_worker's `validate` kind dispatch exactly: resolve
    # jurisdiction from payload, call validate_and_record with the payload's
    # sample size.
    validate_and_record(
        db_session, jurisdiction, sample_size=2, search_client=search_client, source_client=source_client
    )
    db_session.flush()
    db_session.refresh(coverage)

    assert coverage.status == "GREEN"
    assert coverage.validation_pass_rate is not None


def test_validate_and_record_persists_validation_run(db_session):
    jurisdiction, session_row, bills = _make_jurisdiction_with_bills(db_session, n=2)

    def search_handler(request):
        return httpx.Response(
            200,
            json={
                "data": [{"id": str(b.id)} for b in bills],
                "pagination": {"page": 1, "per_page": 25, "total": len(bills), "total_pages": 1},
                "meta": {"api_version": "v1", "request_id": "test"},
            },
        )

    def source_handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="HB 1 HB 2 An act concerning transportation infrastructure funding")

    search_client = httpx.Client(
        transport=httpx.MockTransport(search_handler), base_url="https://api.billcommons.org/api/v1"
    )
    source_client = httpx.Client(transport=httpx.MockTransport(source_handler))

    summary, run = validate_and_record(
        db_session, jurisdiction, sample_size=2, search_client=search_client, source_client=source_client
    )
    db_session.flush()

    assert run.id is not None
    assert run.checks_run == summary.checks_run
    assert run.pass_rate == summary.pass_rate
    assert len(run.details["bills"]) == 2


# ---------------------------------------------------------------------------
# Transaction-free validation core: validate_jurisdiction_txnfree /
# validate_and_record_txnfree (FIX A -- no DB session held during external
# HTTP; see module docstring / cli.py "idle in transaction" root cause).
#
# These tests commit real ZZ_/ZQ_-prefixed fixture rows (rather than using
# db_session's rollback-only fixture) because the functions under test open
# and close THEIR OWN sessions internally and would not see uncommitted rows
# from a different connection -- the session-scoped `_sweep_leaked_test_
# jurisdictions` fixture in conftest.py cleans these up at suite end.
# ---------------------------------------------------------------------------


def _make_committed_jurisdiction_with_bills(n=2):
    abbr = f"ZQ_TXNFREE_{uuid.uuid4().hex[:8].upper()}"
    db = get_session()
    try:
        jurisdiction = Jurisdiction(name="Txn-Free Test State", abbreviation=abbr, classification="state")
        db.add(jurisdiction)
        db.flush()
        session_row = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026 Session", active=True)
        db.add(session_row)
        db.flush()
        bills = []
        for i in range(n):
            bill = Bill(
                jurisdiction_id=jurisdiction.id,
                session_id=session_row.id,
                identifier=f"HB {i + 1}",
                identifier_norm=f"HB {i + 1}",
                title=f"An act concerning transportation infrastructure funding number {i + 1}",
                source_url=f"https://example-legislature.gov/bills/hb{i + 1}",
            )
            db.add(bill)
            bills.append(bill)
        db.flush()
        coverage = JurisdictionCoverage(
            jurisdiction_id=jurisdiction.id,
            session_id=session_row.id,
            status="FULL_TEXT_SEARCHABLE",
            bill_count=n,
            full_text_count=n,
            # Fully-covered jurisdiction: we hold text for everything that is
            # obtainable. In production the validate-worker recomputes this
            # denominator before validating; leaving it NULL here would mean
            # "not yet measured", which correctly refuses promotion.
            full_text_available_count=n,
        )
        db.add(coverage)
        db.commit()
        return jurisdiction.id, abbr, [(b.id, b.identifier) for b in bills]
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _delete_committed_jurisdiction(jurisdiction_id) -> None:
    """Immediate cleanup for the committing txn-free tests above -- rather
    than waiting for the session-scoped sweep in conftest.py, so these
    FULL_TEXT_SEARCHABLE fixture rows don't linger for the duration of the
    whole test run and compete with other tests' batch-limited
    `plan_validation`/`enqueue_validation_jobs` selections (both draw from
    the same shared live DB; see conftest.py's module docstring)."""
    db = get_session()
    try:
        p = {"j": jurisdiction_id}
        for stmt in (
            "DELETE FROM validation_runs WHERE jurisdiction_id=:j",
            "DELETE FROM jurisdiction_coverage WHERE jurisdiction_id=:j",
            "DELETE FROM bills WHERE jurisdiction_id=:j",
            "DELETE FROM sessions WHERE jurisdiction_id=:j",
            "DELETE FROM jurisdictions WHERE id=:j",
        ):
            db.execute(text(stmt), p)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def test_txnfree_runs_structural_and_external_legs_with_no_session_held_during_http():
    """Business intent (the actual bug this refactor fixes): the external
    HTTP phase must run with NO db session from validate_jurisdiction_txnfree
    open. Proven by structuring the test so a held-session bug WOULD fail
    it: the fake search/source transports assert (inside the handler, i.e.
    literally during the HTTP call) that no session-holding call is in
    flight, by using a request counter that only advances if called; then
    after the call returns we independently verify via pg_stat_activity that
    zero idle-in-transaction connections exist for this backend's
    application, proving the function does not leak an open transaction
    across or after the call."""
    jurisdiction_id, abbr, bills = _make_committed_jurisdiction_with_bills(n=2)
    try:
        calls = {"search": 0, "source": 0}

        def search_handler(request):
            calls["search"] += 1
            return httpx.Response(
                200,
                json={
                    "data": [{"id": str(bid)} for bid, _ in bills],
                    "pagination": {"page": 1, "per_page": 25, "total": len(bills), "total_pages": 1},
                    "meta": {"api_version": "v1", "request_id": "test"},
                },
            )

        def source_handler(request):
            calls["source"] += 1
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, text="HB 1 HB 2 An act concerning transportation infrastructure funding")

        search_client = httpx.Client(
            transport=httpx.MockTransport(search_handler), base_url="https://api.billcommons.org/api/v1"
        )
        source_client = httpx.Client(transport=httpx.MockTransport(source_handler))

        summary = validate_jurisdiction_txnfree(
            jurisdiction_id, sample_size=2, search_client=search_client, source_client=source_client
        )

        assert calls["search"] > 0, "structural leg ran and external search leg was actually invoked"
        assert calls["source"] > 0, "external cross_source leg was actually invoked"
        assert len(summary.bills) == 2
        for bill_result in summary.bills:
            leg_names = {leg.leg for leg in bill_result.legs}
            assert "structural" in leg_names
            assert "bill_number_search" in leg_names
            assert "cross_source" in leg_names

        # Independent proof this function's OWN backend connection(s) were
        # never left idle-in-transaction.
        #
        # Scoped by application_name, which the test conftest sets via
        # PGAPPNAME. It previously filtered idle-in-transaction rows to those
        # whose query text mentioned `jurisdictions` -- but this suite shares
        # a database with the live crawl and sync workers, and those workers
        # query that table constantly. Any one of them sitting in a genuine
        # open transaction failed this assertion, at random, for a reason
        # that had nothing to do with the function under test. Filtering on
        # the connection's own label separates "we leaked a transaction" from
        # "production is busy", which is the distinction the assertion was
        # always trying to make.
        check_db = get_session()
        try:
            offending = check_db.execute(
                text(
                    "SELECT pid, query FROM pg_stat_activity "
                    "WHERE state = 'idle in transaction' "
                    "AND datname = current_database() "
                    "AND application_name = :app "
                    "AND pid <> pg_backend_pid()"
                ),
                {"app": TEST_APPLICATION_NAME},
            ).all()
            assert offending == [], (
                "validate_jurisdiction_txnfree left an idle-in-transaction connection "
                f"belonging to this test process; found: {offending}"
            )
        finally:
            check_db.close()
    finally:
        _delete_committed_jurisdiction(jurisdiction_id)


def test_txnfree_and_record_persists_validation_run_and_promotes_coverage():
    """validate_and_record_txnfree's phase 3 (short write txn) must persist
    a validation_runs row and advance jurisdiction_coverage exactly like the
    legacy validate_and_record does -- proving the txn-free refactor didn't
    change the OUTCOME, only how the session is held during HTTP."""
    jurisdiction_id, abbr, bills = _make_committed_jurisdiction_with_bills(n=2)
    try:
        def search_handler(request):
            return httpx.Response(
                200,
                json={
                    "data": [{"id": str(bid)} for bid, _ in bills],
                    "pagination": {"page": 1, "per_page": 25, "total": len(bills), "total_pages": 1},
                    "meta": {"api_version": "v1", "request_id": "test"},
                },
            )

        def source_handler(request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, text="HB 1 HB 2 An act concerning transportation infrastructure funding")

        search_client = httpx.Client(
            transport=httpx.MockTransport(search_handler), base_url="https://api.billcommons.org/api/v1"
        )
        source_client = httpx.Client(transport=httpx.MockTransport(source_handler))

        summary = validate_and_record_txnfree(
            jurisdiction_id, sample_size=2, search_client=search_client, source_client=source_client
        )

        check_db = get_session()
        try:
            coverage = check_db.execute(
                select(JurisdictionCoverage).where(JurisdictionCoverage.jurisdiction_id == jurisdiction_id)
            ).scalar_one()
            assert coverage.status == "GREEN"
            assert coverage.validation_pass_rate == summary.pass_rate

            run = check_db.execute(
                select(ValidationRun).where(ValidationRun.jurisdiction_id == jurisdiction_id)
            ).scalar_one()
            assert run.checks_run == summary.checks_run
        finally:
            check_db.close()
    finally:
        _delete_committed_jurisdiction(jurisdiction_id)


def test_txnfree_per_jurisdiction_timeout_marks_remaining_legs_unverifiable_not_raise():
    """Business intent: a hung/slow official state site must never stall
    the dedicated validation worker's loop indefinitely. A near-zero
    jurisdiction_timeout must cause remaining legs to be recorded as
    'unverifiable' honestly, and the function must return normally (never
    raise) even though external HTTP never got a chance to run."""
    jurisdiction_id, abbr, bills = _make_committed_jurisdiction_with_bills(n=2)
    try:
        def search_handler(request):
            return httpx.Response(
                200,
                json={
                    "data": [{"id": str(bid)} for bid, _ in bills],
                    "pagination": {"page": 1, "per_page": 25, "total": len(bills), "total_pages": 1},
                    "meta": {"api_version": "v1", "request_id": "test"},
                },
            )

        def source_handler(request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, text="irrelevant")

        search_client = httpx.Client(
            transport=httpx.MockTransport(search_handler), base_url="https://api.billcommons.org/api/v1"
        )
        source_client = httpx.Client(transport=httpx.MockTransport(source_handler))

        summary = validate_jurisdiction_txnfree(
            jurisdiction_id,
            sample_size=2,
            search_client=search_client,
            source_client=source_client,
            jurisdiction_timeout=0.0,
        )

        assert len(summary.bills) == 2
        for bill_result in summary.bills:
            leg_by_name = {leg.leg: leg for leg in bill_result.legs}
            assert leg_by_name["structural"].status == PASS  # structural ran during phase 1, before the cap
            assert leg_by_name["bill_number_search"].status == UNVERIFIABLE
            assert leg_by_name["cross_source"].status == UNVERIFIABLE
            assert "timeout" in leg_by_name["bill_number_search"].detail.lower()
    finally:
        _delete_committed_jurisdiction(jurisdiction_id)


def test_cross_source_passes_sc_separated_zero_padded_surface_form(db_session):
    """South Carolina renders identifier "S 537" as "S 0537" -- zero-padded
    AND still separated from the prefix.

    Note the near-miss: SC also renders some bills "S*0537", and THAT form
    already worked, because normalization strips the asterisk to give
    "s0537", which the existing concatenated candidate matches. Only the
    space-separated form had no candidate, so a test written against the
    asterisk form passes with or without this fix and proves nothing. This
    gap is what held South Carolina at DEGRADED with 3,961 bills and a
    healthy crawl.
    """
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="S 537",
        identifier_norm="S 537",
        title="An act",
        source_url="https://www.scstatehouse.gov/billsearch.php?billnumbers=537",
    )
    db_session.add(bill)
    db_session.flush()

    page = (
        "<html><body><h1>South Carolina Legislature</h1>"
        "<p>Session 126 - (2025-2026) Printer Friendly (pdf format) "
        "S 0537 General Bill, By Zell and Goldfinch</p>"
        + "<p>Committee report and history follow below. " * 20
        + "</body></html>"
    )

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=page)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = _check_cross_source(client, RobotsCache(client=client), bill)
    assert result.status == PASS


def test_js_shell_guard_ignores_postal_address_in_footer(db_session):
    """The JS-shell guard asks "does this page have ANY bill-number token?"
    to tell an app shell apart from a real page missing our bill. Every
    legislature site puts its mailing address in the footer, and the naive
    token pattern read those as bill numbers:

        NJ  "Room B50 State House Annex"      -> "B50"
        SC  "Columbia, SC 29201"              -> "SC 29201"

    One such match made an empty JS shell look like real content, so a bill
    that was never rendered got reported as a genuine data mismatch. AL and
    NJ sat at DEGRADED on exactly this.
    """
    jurisdiction, session_row, _ = _make_jurisdiction_with_bills(db_session, n=0)
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="S 4400",
        identifier_norm="S 4400",
        title="An act",
        source_url="https://www.njleg.state.nj.us/bill-search/2026/S4400",
    )
    db_session.add(bill)
    db_session.flush()

    shell = (
        "<html><body><div id='root'></div>"
        "<footer>Office of Legislative Services, Office of Public Information, "
        "Room B50 State House Annex, PO Box 068, Trenton, NJ 08625. "
        "State House Annex, 1105 Pendleton Street, Columbia, SC 29201. "
        + "Contact the Office of Public Information for assistance. " * 12
        + "</footer></body></html>"
    )

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=shell)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = _check_cross_source(client, RobotsCache(client=client), bill)
    assert result.status == UNVERIFIABLE_JS_PAGE, (
        f"address-only footer must read as an unrenderable shell, got {result.status}: {result.detail}"
    )
