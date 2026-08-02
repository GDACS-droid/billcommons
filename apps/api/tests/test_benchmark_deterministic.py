"""The deterministic half of docs/quality/adversarial-benchmark.md, as a gate.

A published benchmark that nothing runs is a brochure. Every assertion here
corresponds to a numbered question in that document, and each one is a
regression test for a defect that was live in production on 2026-08-02 -- the
benchmark found them while it was being written.

Questions whose correct answer requires judging an agent's prose (the
hallucination-bait and refusal-quality ones) are NOT here; they need a
transcript grader. These are the ones a machine can settle.
"""
import pytest
from fastapi.testclient import TestClient

from billcommons_api.app import create_app


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


# --- 1.1 / 1.4  ambiguity is not not-found -----------------------------------

def test_q1_1_ambiguous_bill_number_returns_all_candidates(client):
    """TX HB 1 resolves to multiple sessions. Returning one is the failure."""
    body = client.get(
        "/api/v1/bills", params={"jurisdiction": "TX", "identifier": "HB 1"}
    ).json()
    assert body["pagination"]["total"] > 1, (
        "TX HB 1 collapsed to a single bill -- ambiguity was flattened"
    )


def test_q1_4_ambiguous_is_never_reported_as_absent(client):
    """The batch endpoint must separate `ambiguous` from `not_found`.

    Conflating them tells a consumer to DELETE a bill the corpus holds.
    """
    body = client.get("/api/v1/bills/batch", params={"keys": "TX:HB1"}).json()
    assert "ambiguous" in body and "not_found" in body, (
        "batch envelope lost the ambiguous/not_found distinction"
    )
    ambiguous_keys = {k for k in body["ambiguous"]}
    not_found_keys = set(body["not_found"])
    assert not (ambiguous_keys & not_found_keys), (
        "a key was reported as BOTH ambiguous and not_found"
    )


# --- 3.2  passed_both is never assigned --------------------------------------

def test_q3_2_passed_both_is_never_assigned(client):
    """No evidence source supports it, so it must never appear."""
    body = client.get(
        "/api/v1/bills", params={"status": "passed_both", "per_page": 1}
    ).json()
    assert body["pagination"]["total"] == 0, (
        "passed_both was assigned to bills -- there is no evidence source for it"
    )


# --- 3.3  the mortality split is not cross-state comparable ------------------

def test_q3_3_mortality_publishes_a_comparable_figure(client):
    body = client.get("/api/v1/stats/mortality").json()
    for row in body["data"]:
        assert row["did_not_pass"] == row["died_on_adjournment"] + row["killed"]
    assert body["totals"]["did_not_pass_pct"] is not None


def test_q3_3_states_with_a_meaningless_split_are_flagged(client):
    """CA/WI/NY file a death action for every bill; MA/MO/IA file none.

    If nothing is flagged, either the flag broke or the corpus changed shape --
    both are worth failing on, because this table is published as citable.
    """
    rows = client.get("/api/v1/stats/mortality").json()["data"]
    flagged = [r for r in rows if r["terminal_split_is_degenerate"]]
    assert flagged, "no jurisdiction flagged -- the caveat has silently stopped applying"
    for r in flagged:
        assert r["died_on_adjournment"] == 0 or r["killed"] == 0


# --- 2.3  enrolled does not wait forever -------------------------------------

def test_q2_3_stale_enrolled_bills_are_marked_uncaptured(client):
    """A bill enrolled in a long-adjourned session is not "awaiting signature".

    Every state bounds executive action at roughly 5-45 days from presentment.
    """
    from datetime import date, timedelta

    from billcommons_shared.enrollment import (
        ENROLLED_PENDING_GRACE_DAYS,
        enrolled_outcome_is_uncaptured,
    )

    today = date(2026, 8, 2)
    long_ago = today - timedelta(days=ENROLLED_PENDING_GRACE_DAYS + 1)
    just_now = today - timedelta(days=ENROLLED_PENDING_GRACE_DAYS - 1)

    assert enrolled_outcome_is_uncaptured("enrolled", long_ago, today)
    assert not enrolled_outcome_is_uncaptured("enrolled", just_now, today)
    # 2.4: an unknown end date must never trigger it -- those are carryover biennia.
    assert not enrolled_outcome_is_uncaptured("enrolled", None, today)
    # 2.2: a bill that already resolved is untouched.
    assert not enrolled_outcome_is_uncaptured("enacted", long_ago, today)


# --- 6.2  status_date is not a date we have ----------------------------------

def test_q6_2_status_date_is_not_silently_backfilled(client):
    """It is unpopulated corpus-wide. If that ever changes, the docs and the
    web fallback both need revisiting -- so fail loudly rather than drift."""
    body = client.get("/api/v1/bills", params={"per_page": 50}).json()
    populated = [b for b in body["data"] if b.get("status_date")]
    assert not populated, (
        "status_date is now populated -- update BillDetail's note and the web "
        "asOf fallback, which both document it as always null"
    )
