"""The evidence packet must not present derived conclusions as official record.

This packet is the product's flagship honesty claim -- "explicit
official-vs-derived labelling" -- and it was mislabelling the two things most
likely to mislead an agent citing it:

  * `status` is DERIVED (classification -> narrow text fallback -> None), and
    `died_on_adjournment` is inferred from the session calendar with no filed
    action behind it. It sat inside a block labelled "official" beside the
    jurisdiction's own source_url.
  * `hearings` was labelled "official" over an empty list, when
    legislative_events is empty corpus-wide -- turning "we do not collect this"
    into "the legislature scheduled none".
"""
import inspect

from billcommons_mcp import tools

SRC = inspect.getsource(tools)


def test_official_record_does_not_claim_plain_official():
    assert '"label": "official",\n                    "data": serialize_bill_full' not in SRC, (
        "official_record labels a block containing the DERIVED status as plain 'official'"
    )


def test_official_record_names_its_derived_fields():
    assert '"derived_fields": ["status", "status_date"]' in SRC
    assert "derived_note" in SRC


def test_derived_note_says_status_is_not_reported_by_the_jurisdiction():
    assert "not reported by the" in SRC and "jurisdiction" in SRC


def test_empty_hearings_are_labelled_not_collected():
    assert "not collected -- Bill Commons has no hearing data" in SRC


def test_empty_hearings_carry_an_absence_note():
    assert "absence_note" in SRC
    assert "It does NOT mean no" in SRC, (
        "the absence note must distinguish ignorance from absence explicitly"
    )
