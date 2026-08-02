from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.deps import get_db
from billcommons_api.schemas import (
    MortalityJurisdictionRow,
    MortalityReportEnvelope,
    MortalityTotals,
)
from billcommons_schema.models import Bill, Jurisdiction, Session

router = APIRouter(prefix="/stats", tags=["stats"])

# Buckets the report rolls the raw status vocabulary into. Kept explicit so a
# new status value shows up as a loud KeyError in tests rather than silently
# vanishing from the report's totals.
_ENACTED = {"enacted"}
_DIED_ON_ADJOURNMENT = {"died_on_adjournment"}
_KILLED = {"dead", "vetoed", "withdrawn"}
_PENDING = {"introduced", "in_committee", "passed_one_chamber", "passed_both", "enrolled"}


@router.get("/mortality", response_model=MortalityReportEnvelope)
def mortality_report(
    request: Request,
    db: OrmSession = Depends(get_db),
) -> MortalityReportEnvelope:
    """How bills actually end, per jurisdiction.

    The headline finding this endpoint exists to serve: the most common way a
    state bill ends is not a vote -- it is the session adjourning with the bill
    still in committee. Nothing is filed, so trackers that read only the action
    record report those bills as alive indefinitely. `died_on_adjournment` is
    broken out from `killed` because the consumer action differs: a voted-down
    bill is finished, an out-of-clock bill is a reintroduction candidate.
    """
    rows = db.execute(
        select(
            Jurisdiction.abbreviation,
            Jurisdiction.name,
            Bill.status,
            func.count(),
        )
        .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
        .group_by(Jurisdiction.abbreviation, Jurisdiction.name, Bill.status)
    ).all()

    active = set(
        db.execute(
            select(Jurisdiction.abbreviation)
            .join(Session, Session.jurisdiction_id == Jurisdiction.id)
            .where(Session.active.is_(True))
            .distinct()
        ).scalars()
    )

    by_code: dict[str, dict] = {}
    for code, name, status, count in rows:
        bucket = by_code.setdefault(
            code,
            {
                "jurisdiction_code": code,
                "jurisdiction_name": name,
                "total": 0,
                "enacted": 0,
                "died_on_adjournment": 0,
                "killed": 0,
                "pending": 0,
                "unknown": 0,
            },
        )
        bucket["total"] += count
        if status in _ENACTED:
            bucket["enacted"] += count
        elif status in _DIED_ON_ADJOURNMENT:
            bucket["died_on_adjournment"] += count
        elif status in _KILLED:
            bucket["killed"] += count
        elif status in _PENDING:
            bucket["pending"] += count
        else:
            # None (stage could not be determined) or a value this report does
            # not know. Both belong in "unknown" -- inventing a bucket for an
            # unrecognized status would misstate the corpus.
            bucket["unknown"] += count

    items = []
    totals = {
        "total": 0,
        "enacted": 0,
        "died_on_adjournment": 0,
        "killed": 0,
        "pending": 0,
        "unknown": 0,
    }
    for bucket in sorted(by_code.values(), key=lambda b: b["jurisdiction_code"]):
        for key in totals:
            totals[key] += bucket[key]
        total = bucket["total"]
        # The cross-state comparable figure. See MortalityJurisdictionRow:
        # which of the two terminal buckets a state uses is decided by whether
        # its clerk files a death action, so only their SUM is comparable.
        did_not_pass = bucket["died_on_adjournment"] + bucket["killed"]
        items.append(
            MortalityJurisdictionRow(
                **bucket,
                enacted_pct=round(100 * bucket["enacted"] / total, 1) if total else None,
                died_on_adjournment_pct=(
                    round(100 * bucket["died_on_adjournment"] / total, 1) if total else None
                ),
                killed_pct=round(100 * bucket["killed"] / total, 1) if total else None,
                did_not_pass=did_not_pass,
                did_not_pass_pct=round(100 * did_not_pass / total, 1) if total else None,
                # Zero in one bucket means the split reflects only this
                # jurisdiction's recording convention.
                terminal_split_is_degenerate=(
                    did_not_pass > 0
                    and (bucket["died_on_adjournment"] == 0 or bucket["killed"] == 0)
                ),
                has_active_session=bucket["jurisdiction_code"] in active,
            )
        )

    grand_total = totals["total"]
    return MortalityReportEnvelope(
        data=items,
        totals=MortalityTotals(
            **totals,
            did_not_pass=totals["died_on_adjournment"] + totals["killed"],
            did_not_pass_pct=(
                round(
                    100 * (totals["died_on_adjournment"] + totals["killed"]) / grand_total, 1
                )
                if grand_total
                else None
            ),
            enacted_pct=round(100 * totals["enacted"] / grand_total, 1) if grand_total else None,
            died_on_adjournment_pct=(
                round(100 * totals["died_on_adjournment"] / grand_total, 1)
                if grand_total
                else None
            ),
        ),
        meta={"api_version": "v1", "request_id": request.state.request_id},
    )
