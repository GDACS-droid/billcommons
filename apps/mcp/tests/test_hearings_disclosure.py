"""get_upcoming_hearings must not describe a dataset we never collect as late.

The shipped note read:

    "Hearing/calendar data has a daily refresh target; absence here may
     reflect ingestion lag rather than an empty calendar."

There is no daily refresh, no ingestion, and no lag: nothing has ever written a
`legislative_events` row. An agent reading that note tells the user the
calendar may just be behind -- which is a claim about the legislature's
schedule, invented by us, in the one surface no human reviews.

The website's /hearings page said this correctly the whole time. The machine
surface is the one that drifted, so it is the one that gets a test.
"""
from __future__ import annotations

import billcommons_mcp.tools as tools

FORBIDDEN = ("ingestion lag", "refresh target", "check back", "coming soon")


def test_empty_hearings_are_labelled_not_collected():
    result = tools.get_upcoming_hearings()
    assert result["hearings"] == [], "hearings exist now -- revisit this test"
    assert result["data_status"] == "not_collected"
    assert result["note"]


def test_note_does_not_blame_a_delay():
    note = tools.get_upcoming_hearings()["note"].lower()
    for phrase in FORBIDDEN:
        assert phrase not in note, f"hearings note still implies a delay: {phrase!r}"


def test_note_states_the_inference_not_to_draw():
    """The load-bearing half of the disclosure. Saying "we do not collect this"
    is not enough on its own -- the note has to close off the specific wrong
    conclusion, because that conclusion is the useful-sounding one."""
    note = tools.get_upcoming_hearings()["note"].lower()
    assert "not" in note
    assert "scheduled" in note, (
        "the note should explicitly deny that this means 'none are scheduled'"
    )


def test_docstring_warns_the_agent_before_it_calls():
    """Tool descriptions are what an agent reads when DECIDING to call. A
    correct payload disclosure arrives too late to stop the tool being offered
    as a way to answer "when is the hearing"."""
    doc = (tools.get_upcoming_hearings.__doc__ or "").lower()
    assert "not collected" in doc
    assert "never" in doc


def test_jurisdiction_filter_still_discloses():
    result = tools.get_upcoming_hearings(jurisdiction="FL")
    assert result["data_status"] == "not_collected"
    assert result["note"]
