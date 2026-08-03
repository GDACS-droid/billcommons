"""Disclosures for endpoints that are live, documented, and hold no rows.

Three endpoints -- /people, /committees, /events -- ship in the OpenAPI schema
and return `{"data": [], "pagination": {"total": 0}}` for every query, because
nothing in the ingest pipeline has ever written to those tables. A bare empty
200 is the worst possible answer: an agent reads "no legislators in Florida"
and reports it to a user as a fact about Florida.

The rule this module enforces is that an empty result must say WHY it is empty,
and must distinguish the two cases that look identical in a payload:

    not_collected  -- the table has no rows at all; we do not hold this dataset
    no_match       -- we hold the dataset and nothing matched this query

The distinction is probed at request time rather than hardcoded. Hardcoding
"not collected" would quietly become a lie the day someone wires up an
ingester, which is exactly the failure mode this module exists to prevent. The
probe runs only when `total == 0`, so the normal path pays nothing for it.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession


@dataclass(frozen=True)
class Dataset:
    """A dataset whose absence needs explaining, and the explanation."""

    name: str
    #: Why we hold none of it, in the second person, ending in a full stop.
    #: Written to be read by an agent that is about to summarise the empty
    #: result to a human, so it states the wrong inference explicitly.
    not_collected: str


LEGISLATORS = Dataset(
    name="legislators",
    not_collected=(
        "Bill Commons does not collect legislator records. This empty list means "
        "the data is not held, NOT that the jurisdiction has no legislators. "
        "Sponsor names ARE available per bill via /api/v1/bills/{id}/full, but "
        "as bare names without party, district, or chamber."
    ),
)

COMMITTEES = Dataset(
    name="committees",
    not_collected=(
        "Bill Commons does not collect committee records or membership rosters. "
        "This empty list means the data is not held, NOT that the jurisdiction "
        "has no committees. Committee referrals are visible in each bill's "
        "action history via /api/v1/bills/{id}/actions."
    ),
)

HEARINGS = Dataset(
    name="hearings",
    not_collected=(
        "Bill Commons does not collect hearing or committee-calendar data for any "
        "jurisdiction. This empty list means the data is not held, NOT that no "
        "hearings are scheduled. Consult the chamber's own calendar."
    ),
)


def describe_empty(db: OrmSession, model, dataset: Dataset) -> tuple[str, str] | None:
    """Return `(data_status, notice)` for an empty result, or None if non-empty.

    Call only when the caller has already established that its own query
    returned zero rows -- this answers the follow-up question ("is the whole
    table empty, or just my filter?"), not the original one.
    """
    probe = db.execute(select(model.id).limit(1)).scalar_one_or_none()
    if probe is None:
        return "not_collected", dataset.not_collected
    return (
        "no_match",
        f"No {dataset.name} matched this query. Coverage is partial; an empty "
        "result is not evidence that none exist.",
    )
