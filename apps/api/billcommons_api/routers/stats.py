from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.deps import get_db
from billcommons_api.schemas import (
    MortalityJurisdictionRow,
    MortalityReportEnvelope,
    MortalityTotals,
    ToolUsageRow,
    UsageStatsOut,
)
from billcommons_schema.models import Bill, Jurisdiction, Session, ToolInvocation
from billcommons_shared.telemetry_constants import PROBE_FAMILY

router = APIRouter(prefix="/stats", tags=["stats"])

# Buckets the report rolls the raw status vocabulary into. Kept explicit so a
# new status value shows up as a loud KeyError in tests rather than silently
# vanishing from the report's totals.
_ENACTED = {"enacted"}
_DIED_ON_ADJOURNMENT = {"died_on_adjournment"}
_KILLED = {"dead", "vetoed", "withdrawn"}
# A substituted print remains procedurally live until its survivor reaches an
# outcome.  Treating it as unknown makes the mortality table fail to partition
# the ingest status vocabulary and turns an ordinary legislative transition
# into a misleading "unclassified" outcome.
_PENDING = {"introduced", "in_committee", "substituted", "passed_one_chamber", "passed_both", "enrolled"}


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


@router.get("/usage", response_model=UsageStatsOut)
def usage_stats(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    db: OrmSession = Depends(get_db),
) -> UsageStatsOut:
    """Aggregate MCP tool usage. Public, because the honest number is the point.

    This exists because "is anyone using this?" was unanswerable. Over one log
    window the MCP server served 139 successful POSTs and exactly ONE tool
    call: everything else connected, listed the tools, and disconnected --
    directory health probers, not researchers. Connections are not usage, and
    only tool CALLS distinguish them.

    Published rather than kept private: a project whose pitch is that it
    refuses to dress up what it does not know should not quietly sit on its own
    adoption numbers. Aggregate only -- no IP, no query text, no bill ids.

    Our own read-path monitor calls a real tool every two minutes, on purpose:
    a handshake succeeds against a dead database, so nothing cheaper detects
    the outage that prompted it. That makes it ~720 synthetic calls a day, and
    for the first hours of this table's life it was 61 of 63 rows. Those calls
    are tagged at the MCP edge and excluded here, and the count we subtracted
    is reported as `self_probe_calls` so the arithmetic is checkable.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    is_probe = ToolInvocation.client_family == PROBE_FAMILY
    real_traffic = ToolInvocation.client_family.is_distinct_from(PROBE_FAMILY)

    self_probe_calls = (
        db.execute(
            select(func.count())
            .select_from(ToolInvocation)
            .where(ToolInvocation.occurred_at >= since, is_probe)
        ).scalar_one()
        or 0
    )

    rows = db.execute(
        select(
            ToolInvocation.tool,
            ToolInvocation.outcome,
            func.count().label("n"),
            func.percentile_disc(0.5)
            .within_group(ToolInvocation.duration_ms)
            .label("median_ms"),
        )
        .where(ToolInvocation.occurred_at >= since, real_traffic)
        .group_by(ToolInvocation.tool, ToolInvocation.outcome)
    ).all()

    by_tool: dict[str, dict] = {}
    for tool, outcome, n, median_ms in rows:
        b = by_tool.setdefault(
            tool, {"tool": tool, "ok": 0, "error": 0, "median_ms": None}
        )
        b[outcome] = n
        if outcome == "ok" and median_ms is not None:
            b["median_ms"] = int(median_ms)

    errors = db.execute(
        select(ToolInvocation.error_code, func.count())
        .where(
            ToolInvocation.occurred_at >= since,
            ToolInvocation.outcome == "error",
            real_traffic,
        )
        .group_by(ToolInvocation.error_code)
        .order_by(func.count().desc())
    ).all()

    total_calls = sum(b["ok"] + b["error"] for b in by_tool.values())
    return UsageStatsOut(
        window_days=days,
        total_tool_calls=total_calls,
        tools=[
            ToolUsageRow(**b)
            for b in sorted(by_tool.values(), key=lambda b: -(b["ok"] + b["error"]))
        ],
        error_codes={code or "unknown": n for code, n in errors},
        self_probe_calls=self_probe_calls,
        note=(
            "MCP tool calls only. Connections and tool listings are excluded -- "
            "they are not usage. Calls from our own uptime monitor are excluded "
            "too, and counted separately as self_probe_calls. No IP, query text "
            "or bill ids are recorded."
        ),
        meta={"api_version": "v1", "request_id": request.state.request_id},
    )
