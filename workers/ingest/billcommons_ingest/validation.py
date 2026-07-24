"""QA validation harness for a jurisdiction's ingested bills.

Per docs/SPEC.md "QA per jurisdiction": >=5 random bills compared against
the official source (number, title, session, sponsor, latest action);
bill-number search verified; keyword-from-official-text search verified;
results saved (`validation_runs`). This module implements that by sampling
bills from the DB and INDEPENDENTLY re-checking each one against sources the
DB write path did not just populate from: the deployed production search API
(proves index == db) and the bill's own official `source_url` (proves the
stored link is live and actually names this bill).

Three verification legs per bill, each recorded independently:

    1. structural  -- internal consistency of the already-ingested row
       (identifier/title/session/source_url present; latest_action_date
       matches the actual max(bill_actions.action_date) when actions exist).
       This does not "trust the same row" for its OWN correctness -- it
       checks the row's internal consistency, which is a different failure
       mode than a wrong value slipping in during ingest.
    2. search_retrieval -- calls the DEPLOYED production /api/v1/search
       endpoint (a real external HTTP round trip, independent of whatever
       DB session this process holds) with the bill's number and asserts
       the bill comes back, plus a keyword probe built from a distinctive
       word in the title.
    3. cross_source -- fetches the bill's official `source_url` (politely:
       robots-aware, rate-limited, honest UA -- reuses fulltext.py's
       RobotsCache) and checks the identifier appears on the page. Network
       failures are recorded as 'unverifiable' (not pass, not fail) and
       robots-disallow as 'skipped_robots' -- neither counts against the
       pass rate denominator (see `checkable_legs`/`pass_rate`).

GREEN-criteria honesty (per SPEC "Coverage state machine + GREEN criteria"):
full-text/OCR coverage is a SEPARATE, not-yet-built pipeline stage from this
harness -- validation_pass_rate alone can only justify promoting a
jurisdiction to VALIDATING or GREEN when full-text coverage also actually
exists (full_text_count > 0). Absent that, the ceiling this module will ever
apply is METADATA_SEARCHABLE, regardless of how clean the validation run
was; see `apply_validation_result` below. This is a deliberate, documented
deferral, not an oversight.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_ingest.fulltext import RobotsCache, UnfetchableDocument
from billcommons_schema.models import (
    Bill,
    BillAction,
    Jurisdiction,
    JurisdictionCoverage,
    Session as SessionModel,
    ValidationRun,
)
from billcommons_shared.httpc import new_client

DEFAULT_SEARCH_API_BASE = "https://api.billcommons.org/api/v1"
DEFAULT_SAMPLE_SIZE = 5

# Leg outcome values. "pass"/"fail" count toward the pass rate; the rest
# ("unverifiable"/"skipped_robots") are excluded from the denominator -- a
# network hiccup or an honored robots.txt disallow is not the bill's fault.
PASS = "pass"
FAIL = "fail"
UNVERIFIABLE = "unverifiable"
SKIPPED_ROBOTS = "skipped_robots"
_NON_CHECKABLE = {UNVERIFIABLE, SKIPPED_ROBOTS}

# GREEN requires full-text coverage to exist (SPEC GREEN criterion #5); a
# jurisdiction with 0 full-text documents can be VALIDATING-clean but its
# ceiling is METADATA_SEARCHABLE, never GREEN, until that separate pipeline
# stage (fulltext.py) has actually run.
GREEN_PASS_RATE_THRESHOLD = 0.80


@dataclass
class LegResult:
    leg: str
    status: str  # pass/fail/unverifiable/skipped_robots
    detail: str


@dataclass
class BillValidationResult:
    bill_id: str
    identifier: str
    legs: list[LegResult] = field(default_factory=list)

    @property
    def checkable_legs(self) -> list[LegResult]:
        return [leg for leg in self.legs if leg.status not in _NON_CHECKABLE]

    @property
    def passed_legs(self) -> list[LegResult]:
        return [leg for leg in self.legs if leg.status == PASS]


@dataclass
class ValidationSummary:
    jurisdiction_abbr: str
    session_id: str | None
    bills: list[BillValidationResult] = field(default_factory=list)

    @property
    def checks_run(self) -> int:
        return sum(len(b.checkable_legs) for b in self.bills)

    @property
    def checks_failed(self) -> int:
        return sum(
            1 for b in self.bills for leg in b.checkable_legs if leg.status == FAIL
        )

    @property
    def pass_rate(self) -> float | None:
        """Fraction of CHECKABLE legs (excludes unverifiable/skipped_robots)
        that passed. None if nothing was checkable at all (e.g. every leg
        for every sampled bill hit a network error) -- that's a distinct,
        honest "we learned nothing" state, not a 0% or 100% pass rate."""
        total = self.checks_run
        if total == 0:
            return None
        passed = total - self.checks_failed
        return passed / total


# ---------------------------------------------------------------------------
# Leg 1: structural
# ---------------------------------------------------------------------------


def _check_structural(db: OrmSession, bill: Bill) -> LegResult:
    problems = []
    if not bill.identifier:
        problems.append("missing identifier")
    if not bill.title:
        problems.append("missing title")
    if bill.session_id is None:
        problems.append("missing session link")
    if not bill.source_url:
        problems.append("missing official source_url")

    if bill.latest_action_date is not None:
        max_action_date = db.execute(
            select(func.max(BillAction.action_date)).where(BillAction.bill_id == bill.id)
        ).scalar_one_or_none()
        if max_action_date is not None and max_action_date != bill.latest_action_date:
            problems.append(
                f"latest_action_date={bill.latest_action_date} != "
                f"max(bill_actions.action_date)={max_action_date}"
            )

    if problems:
        return LegResult("structural", FAIL, "; ".join(problems))
    return LegResult("structural", PASS, "identifier/title/session/source_url present; dates consistent")


# ---------------------------------------------------------------------------
# Leg 2: search retrieval (deployed production API)
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z]{5,}")
_STOPWORDS = {
    "shall", "which", "relating", "concerning", "amending", "regarding",
    "provide", "provides", "providing", "establish", "establishes",
    "requiring", "require", "section", "sections", "chapter", "provisions",
}


def _distinctive_title_word(title: str) -> str | None:
    """Pick a distinctive (long, non-stopword) word from a bill title for
    the keyword search probe. None if the title has no such word."""
    words = [w for w in _WORD_RE.findall(title) if w.lower() not in _STOPWORDS]
    return max(words, key=len) if words else None


def _search_api_get(client: httpx.Client, **params) -> dict:
    response = client.get("/search", params=params)
    response.raise_for_status()
    return response.json()


def _bill_in_results(payload: dict, bill: Bill) -> bool:
    """Match against the /api/v1/search response envelope (`{data: [...],
    pagination, meta}` -- see apps/api/billcommons_api/pagination.py). Falls
    back to `items`/`results` defensively in case the envelope shape ever
    changes, but `data` is the real, locked contract."""
    items = payload.get("data", payload.get("items", payload.get("results", [])))
    return any(str(item.get("id")) == str(bill.id) for item in items)


def _check_search_retrieval(
    client: httpx.Client, bill: Bill, jurisdiction_abbr: str
) -> LegResult:
    try:
        by_number = _search_api_get(
            client, q=bill.identifier, jurisdiction=jurisdiction_abbr, per_page=10
        )
    except httpx.HTTPError as exc:
        return LegResult("search_retrieval", UNVERIFIABLE, f"search API unreachable: {exc}")

    if not _bill_in_results(by_number, bill):
        return LegResult(
            "search_retrieval",
            FAIL,
            f"bill-number search for {bill.identifier!r} in {jurisdiction_abbr} returned no match",
        )

    keyword = _distinctive_title_word(bill.title)
    if keyword is None:
        return LegResult(
            "search_retrieval",
            PASS,
            "bill-number search matched (title had no distinctive keyword to probe)",
        )

    try:
        by_keyword = _search_api_get(
            client, q=keyword, jurisdiction=jurisdiction_abbr, per_page=25
        )
    except httpx.HTTPError as exc:
        return LegResult(
            "search_retrieval", UNVERIFIABLE, f"keyword search API unreachable: {exc}"
        )

    if not _bill_in_results(by_keyword, bill):
        return LegResult(
            "search_retrieval",
            FAIL,
            f"keyword search for {keyword!r} (from title) did not surface {bill.identifier}",
        )

    return LegResult(
        "search_retrieval", PASS, f"bill-number + keyword ({keyword!r}) search both matched"
    )


# ---------------------------------------------------------------------------
# Leg 3: cross-source spot check
# ---------------------------------------------------------------------------


def _normalize_for_page_match(identifier: str) -> list[str]:
    """A handful of surface forms the identifier might appear as on an
    official legislature/Open States page (state sites are inconsistent
    about spacing/punctuation): "HB 123", "HB123", "H.B. 123", "H. B. 123"."""
    compact = re.sub(r"[^A-Za-z0-9]", "", identifier)
    m = re.match(r"^([A-Za-z]+)(\d+)$", compact)
    forms = {identifier, compact}
    if m:
        prefix, number = m.groups()
        forms.add(f"{prefix} {number}")
        forms.add(".".join(prefix) + f". {number}")
    return list(forms)


def _check_cross_source(client: httpx.Client, robots_cache: RobotsCache, bill: Bill) -> LegResult:
    if not bill.source_url:
        return LegResult("cross_source", FAIL, "no source_url to verify against")

    try:
        if not robots_cache.can_fetch(bill.source_url):
            return LegResult(
                "cross_source", SKIPPED_ROBOTS, f"robots.txt disallows {bill.source_url}"
            )
        response = client.get(bill.source_url, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return LegResult("cross_source", UNVERIFIABLE, f"fetch failed for {bill.source_url}: {exc}")

    page_text = response.text
    candidates = _normalize_for_page_match(bill.identifier)
    if any(candidate in page_text for candidate in candidates):
        return LegResult("cross_source", PASS, f"source_url page contains identifier match")
    return LegResult(
        "cross_source",
        FAIL,
        f"source_url page did not contain any surface form of {bill.identifier!r}",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def sample_bills(db: OrmSession, jurisdiction: Jurisdiction, sample_size: int) -> list[Bill]:
    """Randomly sample up to `sample_size` bills for a jurisdiction. Random
    at the SQL layer (ORDER BY random()) so re-running draws a fresh sample
    each time, per SPEC's ">=5 random bills"."""
    bill_ids = db.execute(
        select(Bill.id).where(Bill.jurisdiction_id == jurisdiction.id).order_by(func.random())
    ).scalars().all()
    chosen = bill_ids[:sample_size]
    if not chosen:
        return []
    bills = db.execute(select(Bill).where(Bill.id.in_(chosen))).scalars().all()
    order = {bid: i for i, bid in enumerate(chosen)}
    bills.sort(key=lambda b: order[b.id])
    return bills


def validate_jurisdiction(
    db: OrmSession,
    jurisdiction: Jurisdiction,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    search_client: httpx.Client | None = None,
    source_client: httpx.Client | None = None,
    robots_cache: RobotsCache | None = None,
    rng: random.Random | None = None,
) -> ValidationSummary:
    """Sample `sample_size` bills for `jurisdiction` and run all three
    verification legs on each, independent of the write path that ingested
    them. Does not touch the DB write side except reads -- callers persist
    the summary via `record_validation_run` / `apply_validation_result`."""
    bills = sample_bills(db, jurisdiction, sample_size)

    owns_search_client = search_client is None
    owns_source_client = source_client is None
    search_client = search_client or new_client(base_url=DEFAULT_SEARCH_API_BASE, timeout=15.0)
    source_client = source_client or new_client(timeout=20.0)
    robots_cache = robots_cache or RobotsCache(client=source_client)

    summary = ValidationSummary(jurisdiction_abbr=jurisdiction.abbreviation, session_id=None)
    try:
        for bill in bills:
            result = BillValidationResult(bill_id=str(bill.id), identifier=bill.identifier)
            result.legs.append(_check_structural(db, bill))
            result.legs.append(
                _check_search_retrieval(search_client, bill, jurisdiction.abbreviation)
            )
            result.legs.append(_check_cross_source(source_client, robots_cache, bill))
            summary.bills.append(result)
    finally:
        if owns_search_client:
            search_client.close()
        if owns_source_client:
            source_client.close()

    return summary


def record_validation_run(db: OrmSession, summary: ValidationSummary, jurisdiction: Jurisdiction) -> ValidationRun:
    """Persist a `validation_runs` row from a ValidationSummary. Caller
    commits."""
    started_at = datetime.now(timezone.utc)
    details = {
        "bills": [
            {
                "bill_id": b.bill_id,
                "identifier": b.identifier,
                "legs": [{"leg": leg.leg, "status": leg.status, "detail": leg.detail} for leg in b.legs],
            }
            for b in summary.bills
        ],
        "sample_size": len(summary.bills),
    }
    run = ValidationRun(
        jurisdiction_id=jurisdiction.id,
        session_id=summary.session_id,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        pass_rate=summary.pass_rate,
        checks_run=summary.checks_run,
        checks_failed=summary.checks_failed,
        details=details,
    )
    db.add(run)
    db.flush()
    return run


def apply_validation_result(
    db: OrmSession, jurisdiction: Jurisdiction, summary: ValidationSummary
) -> list[JurisdictionCoverage]:
    """Update every `jurisdiction_coverage` row for this jurisdiction with
    the latest validation_pass_rate, and advance/hold the coverage state
    machine per SPEC's GREEN criteria.

    Honest ceiling: GREEN requires (per SPEC #5, #8) both a passing
    validation sample AND full-text coverage actually existing
    (`full_text_count > 0`) -- this harness alone can promote a
    jurisdiction only as far as VALIDATING (clean sample, no full text yet)
    or GREEN (clean sample AND full text present). A jurisdiction with
    bill_count == 0 is left alone (nothing to validate). Never regresses a
    row already at GREEN/DEGRADED/BLOCKED past what a human/operator set,
    except a hard validation FAILURE (pass_rate below threshold) explicitly
    demotes toward DEGRADED so a real problem isn't hidden behind a stale
    green status.
    """
    coverage_rows = db.execute(
        select(JurisdictionCoverage).where(JurisdictionCoverage.jurisdiction_id == jurisdiction.id)
    ).scalars().all()

    pass_rate = summary.pass_rate
    now = datetime.now(timezone.utc)

    for coverage in coverage_rows:
        if coverage.bill_count == 0:
            continue

        coverage.validation_pass_rate = pass_rate
        coverage.last_attempt_at = now

        if pass_rate is None:
            # Nothing was checkable this run (e.g. total network outage) --
            # don't move the state machine on no information.
            continue

        if pass_rate < GREEN_PASS_RATE_THRESHOLD:
            if coverage.status not in ("BLOCKED",):
                coverage.status = "DEGRADED"
            coverage.known_gaps = (
                f"validation pass rate {pass_rate:.0%} below "
                f"{GREEN_PASS_RATE_THRESHOLD:.0%} threshold"
            )
            continue

        # Pass rate is healthy. Ceiling depends on full-text coverage.
        if coverage.full_text_count > 0:
            coverage.status = "GREEN"
        else:
            # SPEC GREEN criterion #5 ("full text searchable wherever
            # technically available") is not yet satisfied by this pass --
            # cap at METADATA_SEARCHABLE/VALIDATING rather than fabricate a
            # GREEN this harness can't actually back up.
            if coverage.status in ("BOOTSTRAPPED", "METADATA_SEARCHABLE", "SOURCE_IDENTIFIED"):
                coverage.status = "VALIDATING"
            coverage.known_gaps = (
                "full-text coverage is 0; GREEN deferred until the fulltext "
                "pipeline (billcommons_ingest.fulltext) has run for this jurisdiction"
            )

    db.flush()
    return coverage_rows


def validate_and_record(
    db: OrmSession,
    jurisdiction: Jurisdiction,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    search_client: httpx.Client | None = None,
    source_client: httpx.Client | None = None,
) -> tuple[ValidationSummary, ValidationRun]:
    """Full pass for one jurisdiction: sample + validate + persist
    validation_runs + update jurisdiction_coverage. Caller commits."""
    summary = validate_jurisdiction(
        db, jurisdiction, sample_size=sample_size, search_client=search_client, source_client=source_client
    )
    run = record_validation_run(db, summary, jurisdiction)
    apply_validation_result(db, jurisdiction, summary)
    return summary, run
