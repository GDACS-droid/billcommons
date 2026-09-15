from datetime import date

from billcommons_shared.fl_senate_meetings import (
    MeetingRequest,
    discover_meeting_document,
    discover_meeting_request,
    meeting_evidence_excerpt,
)


PARENT_URL = "https://www.flsenate.gov/Session/Bill/2026/624"


def test_request_uses_latest_agenda_with_its_matching_committee_anchor():
    body = b"""
      <a href="/Committees/Show/CF">Children, Families, and Elder Affairs</a>
      <a href="/Committees/Show/JU">Judiciary</a>
      <a href="/Committees/Show/RC">Rules</a>
      <table><tr><td>On Committee agenda-- Children, Families, and Elder Affairs,
      01/12/26, 4:00 pm</td></tr>
      <tr><td>On Committee agenda-- Judiciary, 01/20/26, 9:30 am</td></tr>
      <tr><td>On Committee agenda-- Rules, 01/27/26, 9:00 am</td></tr></table>
    """

    request = discover_meeting_request(PARENT_URL, body)

    assert request == MeetingRequest(
        canonical_url="https://www.flsenate.gov/Committees/Show/RC/2026",
        session_year=2026,
        committee_code="RC",
        meeting_date=date(2026, 1, 27),
    )


def test_request_requires_exact_matching_anchor_and_bounded_parent():
    no_anchor = b"On Committee agenda-- Rules, 01/27/26, 9:00 am"
    assert discover_meeting_request(PARENT_URL, no_anchor) is None

    mismatched = b"""
      <a href="/Committees/Show/RC?next=https://example.test">Rules</a>
      On Committee agenda-- Rules, 01/27/26, 9:00 am
    """
    assert discover_meeting_request(PARENT_URL, mismatched) is None
    assert discover_meeting_request(PARENT_URL, b"x" * 20, max_html_bytes=19) is None


def test_request_rejects_route_normalization_and_permanently_marks_ambiguous_labels():
    traversal = b"""
      <a href="/Committees/Show/CF/../RC">Rules</a>
      On Committee agenda-- Rules, 01/27/26, 9:00 am
    """
    assert discover_meeting_request(PARENT_URL, traversal) is None
    ambiguous = b"""
      <a href="/Committees/Show/RC">Rules</a><a href="/Committees/Show/CF">Rules</a>
      <a href="/Committees/Show/RC">Rules</a>
      On Committee agenda-- Rules, 01/27/26, 9:00 am
    """
    assert discover_meeting_request(PARENT_URL, ambiguous) is None


def test_request_accepts_prior_calendar_year_meeting_in_a_regular_session_index():
    body = b"""
      <a href="/Committees/Show/RC">Rules</a>
      On Committee agenda-- Rules, 12/18/25, 9:00 am
    """
    request = discover_meeting_request(PARENT_URL, body)
    assert request is not None
    assert request.meeting_date == date(2025, 12, 18)


def test_request_refuses_a_latest_date_tie_between_different_committees():
    body = b"""
      <a href="/Committees/Show/RC">Rules</a><a href="/Committees/Show/JU">Judiciary</a>
      On Committee agenda-- Rules, 01/27/26, 9:00 am
      On Committee agenda-- Judiciary, 01/27/26, 10:00 am
    """
    assert discover_meeting_request(PARENT_URL, body) is None


def test_document_requires_matching_caption_row_date_and_code_and_prefers_expanded_agenda():
    request = MeetingRequest(
        canonical_url="https://www.flsenate.gov/Committees/Show/RC/2026",
        session_year=2026,
        committee_code="RC",
        meeting_date=date(2026, 1, 27),
    )
    index = b"""
      <table id="meetingsTbl"><caption>2026 Meeting Records</caption><tbody>
        <tr><td>1/20/2026</td><td><a href="/Committees/Show/RC/ExpandedAgenda/6750">Wrong date</a></td></tr>
        <tr><td>1/27/2026</td>
          <td><a href="/Committees/Show/RC/MeetingNotice/6787">Meeting Notice</a></td>
          <td><a href="/Committees/Show/RC/ExpandedAgenda/6787">Expanded Agenda</a></td>
        </tr>
      </tbody></table>
    """

    document = discover_meeting_document(request, index)

    assert document is not None
    assert document.canonical_url == "https://www.flsenate.gov/Committees/Show/RC/ExpandedAgenda/6787"
    assert document.artifact_type == "committee expanded agenda"
    assert document.session_year == 2026


def test_document_rejects_wrong_session_caption_foreign_code_query_and_traversal():
    request = MeetingRequest(
        canonical_url="https://www.flsenate.gov/Committees/Show/RC/2026",
        session_year=2026,
        committee_code="RC",
        meeting_date=date(2026, 1, 27),
    )
    for caption, href in (
        ("2025 Meeting Records", "/Committees/Show/RC/ExpandedAgenda/6787"),
        ("2026 Meeting Records", "/Committees/Show/CF/ExpandedAgenda/6787"),
        ("2026 Meeting Records", "/Committees/Show/RC/ExpandedAgenda/6787?download=1"),
        ("2026 Meeting Records", "/Committees/Show/RC/ExpandedAgenda/../6787"),
        ("2026 Meeting Records", "https://example.test/Committees/Show/RC/ExpandedAgenda/6787"),
    ):
        index = (
            f'<table id="meetingsTbl"><caption>{caption}</caption><tbody><tr>'
            f'<td>1/27/2026</td><td><a href="{href}">Agenda</a></td>'
            "</tr></tbody></table>"
        ).encode()
        assert discover_meeting_document(request, index) is None


def test_document_rejects_duplicate_meetings_tables_instead_of_cross_binding_rows():
    request = MeetingRequest(
        canonical_url="https://www.flsenate.gov/Committees/Show/RC/2026",
        session_year=2026,
        committee_code="RC",
        meeting_date=date(2026, 1, 27),
    )
    index = b"""
      <table id="meetingsTbl"><caption>2026 Meeting Records</caption><tbody></tbody></table>
      <table id="meetingsTbl"><caption>2026 Meeting Records</caption><tbody><tr>
        <td>1/27/2026</td><td><a href="/Committees/Show/RC/ExpandedAgenda/6787">Agenda</a></td>
      </tr></tbody></table>
    """
    assert discover_meeting_document(request, index) is None


def test_document_refuses_multiple_rows_for_the_requested_date_without_a_time_binding():
    request = MeetingRequest(
        canonical_url="https://www.flsenate.gov/Committees/Show/RC/2026",
        session_year=2026,
        committee_code="RC",
        meeting_date=date(2026, 1, 27),
    )
    index = b"""
      <table id="meetingsTbl"><caption>2026 Meeting Records</caption><tbody>
        <tr><td>1/27/2026</td><td><a href="/Committees/Show/RC/ExpandedAgenda/6787">Morning</a></td></tr>
        <tr><td>1/27/2026</td><td><a href="/Committees/Show/RC/ExpandedAgenda/6788">Afternoon</a></td></tr>
      </tbody></table>
    """
    assert discover_meeting_document(request, index) is None


def test_meeting_evidence_requires_the_regular_session_header_meeting_header_and_exact_bill():
    text = """
      Florida Senate 2026 Regular Session
      COMMITTEE MEETING EXPANDED AGENDA
      Bills to be considered: HB 12, HB 123
    """
    excerpt = meeting_evidence_excerpt(text, "HB 12", 2026)
    assert excerpt is not None
    assert excerpt == "Florida Senate 2026 Regular Session COMMITTEE MEETING EXPANDED AGENDA Bills to be considered: HB 12, HB 123"
    assert len(excerpt) <= 500

    assert meeting_evidence_excerpt(text.replace("HB 12, ", ""), "HB 12", 2026) is None
    assert meeting_evidence_excerpt(text.replace("2026 Regular Session", "2025 Regular Session"), "HB 12", 2026) is None
    assert meeting_evidence_excerpt(text.replace("COMMITTEE MEETING EXPANDED AGENDA", "Committee calendar"), "HB 12", 2026) is None
    assert meeting_evidence_excerpt(text.replace("HB 12, HB 123", "HB 012, HB 12A"), "HB 12", 2026) is None


def test_meeting_evidence_accepts_a_late_bill_after_a_valid_nearby_header_and_bounds_the_window():
    text = "2026 Regular Session COMMITTEE MEETING NOTICE " + ("x" * 15_000) + " SB 624"
    excerpt = meeting_evidence_excerpt(text, "SB 624", 2026)
    assert excerpt is not None
    assert "SB 624" in excerpt and len(excerpt) <= 500


def test_meeting_evidence_rejects_a_meeting_header_outside_the_first_thousand_normalized_chars():
    text = "2026 Regular Session " + ("x" * 1_001) + " COMMITTEE MEETING NOTICE HB 12"
    assert meeting_evidence_excerpt(text, "HB 12", 2026) is None


def test_document_requires_unique_preferred_url_but_allows_repeated_identical_links():
    request = MeetingRequest(
        canonical_url="https://www.flsenate.gov/Committees/Show/RC/2026",
        session_year=2026, committee_code="RC", meeting_date=date(2026, 1, 27),
    )
    for kind in ("ExpandedAgenda", "MeetingNotice"):
        for second_id in ("6787", "6788"):
            index = (
                '<table id="meetingsTbl"><caption>2026 Meeting Records</caption>'
                '<tr><td>1/27/2026</td><td>'
                f'<a href="/Committees/Show/RC/{kind}/6787">Document</a>'
                f'<a href="/Committees/Show/RC/{kind}/{second_id}">Document</a>'
                '</td></tr></table>'
            ).encode()
            document = discover_meeting_document(request, index)
            if second_id == "6787":
                assert document is not None
                assert document.canonical_url.endswith("/6787")
            else:
                assert document is None
