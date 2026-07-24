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

import uuid

import httpx
import pytest

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
)


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
    wherever available) is a separate, unmet condition."""
    jurisdiction, session_row, bills = _make_jurisdiction_with_bills(db_session, n=1)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        status="METADATA_SEARCHABLE",
        bill_count=1,
        full_text_count=0,
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


def test_failing_validation_demotes_to_degraded(db_session):
    jurisdiction, session_row, bills = _make_jurisdiction_with_bills(db_session, n=1)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        status="VALIDATING",
        bill_count=1,
        full_text_count=1,
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
