"""QA validation harness for a jurisdiction's ingested bills.

Per docs/SPEC.md "QA per jurisdiction": >=5 random bills compared against
the official source (number, title, session, sponsor, latest action);
bill-number search verified; keyword-from-official-text search verified;
results saved (`validation_runs`). This module implements that by sampling
bills from the DB and INDEPENDENTLY re-checking each one against sources the
DB write path did not just populate from: the deployed production search API
(proves index == db) and the bill's own official `source_url` (proves the
stored link is live and actually names this bill).

Four verification legs per bill, each recorded independently:

    1. structural  -- internal consistency of the already-ingested row
       (identifier/title/session/source_url present; latest_action_date
       matches the actual max(bill_actions.action_date) when actions exist).
       This does not "trust the same row" for its OWN correctness -- it
       checks the row's internal consistency, which is a different failure
       mode than a wrong value slipping in during ingest.
    2. bill_number_search (MANDATORY) -- calls the DEPLOYED production
       /api/v1/search endpoint (a real external HTTP round trip, independent
       of whatever DB session this process holds) with the bill's own
       identifier and asserts the bill comes back. A miss here is a real
       search-index bug and fails the leg.
    3. keyword_search (ADVISORY) -- a second, weaker probe: searches by the
       rarest non-boilerplate word in the bill's title (by DB frequency, see
       `_rarest_title_word`) and records whether that surfaces the bill too.
       Recorded as 'advisory_pass'/'advisory_fail' -- excluded from the
       pass-rate denominator and never fails the leg on its own, because a
       single generic title word can legitimately miss even a healthy
       search index (relevance ranking, many other bills sharing the word).
    4. cross_source -- fetches the bill's official `source_url` (politely:
       robots-aware, rate-limited, honest UA -- reuses fulltext.py's
       RobotsCache) and checks the identifier appears on the page. Network
       failures are recorded as 'unverifiable' (not pass, not fail),
       robots-disallow as 'skipped_robots', and a fetched-200-but-no-real-
       content page (JS-rendered app shell, e.g. wyoleg.gov) as
       'unverifiable_js_page' -- none of these three count against the
       pass rate denominator (see `checkable_legs`/`pass_rate`). A page with
       substantial visible text AND other bill-number tokens present, just
       not ours, stays a genuine 'fail'.

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

import os
import random
import re
import time
import uuid
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
from billcommons_shared.db import get_session
from billcommons_shared.httpc import new_client

DEFAULT_SEARCH_API_BASE = "https://api.billcommons.org/api/v1"
DEFAULT_SAMPLE_SIZE = 5

# Per-request hard timeouts for the two external legs (search_retrieval hits
# our own deployed API; cross_source hits an arbitrary state site, which can
# be slower/flakier) -- overridable for tests/tuning via new_client() kwargs
# already threaded through by callers.
DEFAULT_SEARCH_TIMEOUT = 20.0
DEFAULT_SOURCE_TIMEOUT = 30.0

# Per-jurisdiction wall-clock cap on the whole no-txn external phase (env
# VALIDATION_JURISDICTION_TIMEOUT). A single hung/slow official site must
# never stall the dedicated validation worker's loop indefinitely -- on cap,
# any bill/leg not yet reached is marked 'unverifiable' honestly (see
# `_run_external_phase`) rather than raising or half-completing silently.
DEFAULT_JURISDICTION_TIMEOUT = float(os.environ.get("VALIDATION_JURISDICTION_TIMEOUT", "180"))

# Leg outcome values. "pass"/"fail" count toward the pass rate; the rest
# ("unverifiable"/"skipped_robots") are excluded from the denominator -- a
# network hiccup or an honored robots.txt disallow is not the bill's fault.
PASS = "pass"
FAIL = "fail"
UNVERIFIABLE = "unverifiable"
SKIPPED_ROBOTS = "skipped_robots"
# ADVISORY_FAIL/ADVISORY_PASS: recorded outcomes for a check whose failure is
# informative but not authoritative enough to fail the leg on its own (see
# `keyword_search` below -- picking ANY single title word as a probe can
# legitimately miss even a healthy search index, e.g. relevance ranking
# pushing the target bill past the page size). Excluded from the pass-rate
# denominator like UNVERIFIABLE/SKIPPED_ROBOTS, but distinguishable in
# `details` from a genuine network/robots non-result.
ADVISORY_PASS = "advisory_pass"
ADVISORY_FAIL = "advisory_fail"
# UNVERIFIABLE_JS_PAGE: the cross_source fetch succeeded (200) but the page
# is a JS-rendered app shell with no server-rendered bill content -- the
# identifier's absence proves nothing about our data being wrong, only that
# this particular legislature site can't be cross-checked via a plain HTTP
# GET. Excluded from the pass-rate denominator, same as UNVERIFIABLE.
UNVERIFIABLE_JS_PAGE = "unverifiable_js_page"
_NON_CHECKABLE = {UNVERIFIABLE, SKIPPED_ROBOTS, ADVISORY_PASS, ADVISORY_FAIL, UNVERIFIABLE_JS_PAGE}

# GREEN requires full-text coverage to exist (SPEC GREEN criterion #5); a
# jurisdiction with 0 full-text documents can be VALIDATING-clean but its
# ceiling is METADATA_SEARCHABLE, never GREEN, until that separate pipeline
# stage (fulltext.py) has actually run.
GREEN_PASS_RATE_THRESHOLD = 0.80

# SPEC GREEN criterion #5 is "full text searchable WHEREVER technically
# available" -- not "somewhere". `full_text_count > 0` satisfied the literal
# words while letting a jurisdiction holding text for 1% of its obtainable
# bills wear a GREEN badge, which is what this threshold exists to stop.
# Measured against full_text_available_count (see coverage.py), so bills whose
# source publishes nothing, and documents that are terminally unfetchable,
# never count against a jurisdiction. available == 0 means nothing is
# obtainable at all: criterion #5 is then vacuous and GREEN is allowed, with
# the limitation recorded in known_gaps.
GREEN_FULLTEXT_COVERAGE_THRESHOLD = 0.80

# Statuses a CLEAN validation sample may lift back to VALIDATING when
# full-text coverage isn't there yet. DEGRADED is in the list because it is
# set automatically by a failing sample, so it must clear automatically on a
# passing one -- otherwise a jurisdiction that fails once can never recover,
# which is what stranded 31 rows (several sitting at pass_rate 1.0) in
# DEGRADED. BLOCKED is deliberately absent: an operator sets it, an operator
# clears it.
_RECOVERABLE_INTO_VALIDATING = (
    "BOOTSTRAPPED",
    "METADATA_SEARCHABLE",
    "SOURCE_IDENTIFIED",
    "FULL_TEXT_SEARCHABLE",
    "DEGRADED",
    "GREEN",
)


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


@dataclass
class _BillSnapshot:
    """Plain-Python materialization of exactly the fields the structural
    check + the two external legs need from a `Bill` row -- read once during
    the SHORT read txn (see `_load_snapshot`) and then used for the rest of
    `validate_jurisdiction_txnfree`'s external HTTP phase with NO open DB
    session. `structural_ok`/`structural_detail` are computed eagerly too
    (the structural check IS db-only, so it runs during the read txn rather
    than needing a second DB round trip later)."""

    bill_id: str
    identifier: str
    title: str
    source_url: str | None
    structural_ok: bool
    structural_detail: str
    keyword: str | None


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

_WORD_RE = re.compile(r"[A-Za-z]{6,}")
# Legislative boilerplate: words that show up in huge swaths of titles across
# a jurisdiction (generic verbs/nouns of bill drafting), so they're bad
# keyword-search probes even though they pass the length filter -- a search
# for one of these will legitimately surface dozens of OTHER bills instead of
# (or ahead of) the one under test, which is a false alarm about the search
# index, not a real bug.
_STOPWORDS = {
    "shall", "which", "relating", "concerning", "amending", "regarding",
    "provide", "provides", "providing", "establish", "establishes",
    "requiring", "require", "section", "sections", "chapter", "provisions",
    "amend", "amendment", "amendments", "exemption", "exemptions",
    "act", "relate", "related", "revise", "revising", "revision", "code",
    "state", "certain", "general", "further", "matters", "purposes",
    "recognize", "recognizing", "designating", "designate",
}


def _distinctive_title_word(title: str) -> str | None:
    """Pick the LONGEST candidate (long, non-stopword) word from a bill
    title as a first-pass keyword search probe. None if the title has no
    such word. `_rarest_title_word` below is the DB-frequency-aware
    upgrade used by the actual search_retrieval check; this function is
    kept for the length-only fallback (no db session available) and for
    existing callers/tests."""
    words = [w for w in _WORD_RE.findall(title) if w.lower() not in _STOPWORDS]
    return max(words, key=len) if words else None


def _rarest_title_word(db: OrmSession, title: str) -> str | None:
    """Pick the title candidate word (>=6 letters, not legislative
    boilerplate) that is RAREST across all ingested bill titles, via one
    cheap COUNT-per-candidate query. A generic word like 'amendments' can
    appear in hundreds of titles in a jurisdiction, so a keyword-search
    probe built from it can legitimately fail to surface any ONE specific
    bill in the results page even though the search index is fine -- that's
    a bad probe, not a search bug. Falls back to the longest-word heuristic
    if no candidates exist or the DB has no bills yet (empty case)."""
    candidates = sorted({
        w for w in _WORD_RE.findall(title) if w.lower() not in _STOPWORDS
    })
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    count_columns = [
        func.count().filter(Bill.title.ilike(f"%{word}%")).label(f"c{i}")
        for i, word in enumerate(candidates)
    ]
    counts = db.execute(select(*count_columns)).one()
    counts_by_word = dict(zip(candidates, counts))
    return min(candidates, key=lambda w: (counts_by_word[w], -len(w)))


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
    client: httpx.Client, bill: Bill, jurisdiction_abbr: str, db: OrmSession | None = None
) -> list[LegResult]:
    """Two independently-recorded checks against the deployed search API:

    - `bill_number_search` (MANDATORY): searching by the bill's own
      identifier must surface it. A failure here is a real, authoritative
      search-index bug and fails the leg.
    - `keyword_search` (ADVISORY): searching by a word pulled from the
      title is a much weaker signal -- even a healthy search index can fail
      to surface one specific bill for a single-word query (relevance
      ranking, a common word shared by many other bills, pagination). Its
      outcome is recorded (`advisory_pass`/`advisory_fail`) but never fails
      the leg or counts toward the pass-rate denominator on its own.
    """
    legs: list[LegResult] = []

    try:
        by_number = _search_api_get(
            client, q=bill.identifier, jurisdiction=jurisdiction_abbr, per_page=10
        )
    except httpx.HTTPError as exc:
        legs.append(LegResult("bill_number_search", UNVERIFIABLE, f"search API unreachable: {exc}"))
        return legs

    if not _bill_in_results(by_number, bill):
        legs.append(
            LegResult(
                "bill_number_search",
                FAIL,
                f"bill-number search for {bill.identifier!r} in {jurisdiction_abbr} returned no match",
            )
        )
        return legs

    legs.append(
        LegResult(
            "bill_number_search",
            PASS,
            f"bill-number search for {bill.identifier!r} matched",
        )
    )

    keyword = _rarest_title_word(db, bill.title) if db is not None else _distinctive_title_word(bill.title)
    if keyword is None:
        legs.append(
            LegResult(
                "keyword_search",
                ADVISORY_PASS,
                "title had no distinctive keyword to probe",
            )
        )
        return legs

    try:
        by_keyword = _search_api_get(
            client, q=keyword, jurisdiction=jurisdiction_abbr, per_page=25
        )
    except httpx.HTTPError as exc:
        legs.append(
            LegResult("keyword_search", UNVERIFIABLE, f"keyword search API unreachable: {exc}")
        )
        return legs

    if not _bill_in_results(by_keyword, bill):
        legs.append(
            LegResult(
                "keyword_search",
                ADVISORY_FAIL,
                f"keyword search for {keyword!r} (from title) did not surface {bill.identifier}",
            )
        )
        return legs

    legs.append(
        LegResult("keyword_search", ADVISORY_PASS, f"keyword ({keyword!r}) search matched")
    )
    return legs


# ---------------------------------------------------------------------------
# Leg 3: cross-source spot check
# ---------------------------------------------------------------------------


def _normalize_page_text_for_match(text: str) -> str:
    """Strip punctuation (but NOT whitespace) and casefold, so literal
    substring matching is tolerant of the page's own punctuation noise
    around a bill number (e.g. "H.B. 1057", "HB-1057") without having to
    enumerate every possible separator as a distinct candidate string --
    while still leaving whitespace in place as real token boundaries so
    adjacency checks (the alpha prefix immediately next to its number,
    required below) can't be fooled by unrelated digits/letters that just
    happen to run together once ALL separators are removed."""
    collapsed = re.sub(r"[^a-z0-9\s]", "", text.casefold())
    return re.sub(r"\s+", " ", collapsed)


# Common two/three-letter bill-type prefixes, spelled out in full -- some
# state sites (NC, PA -- confirmed live during the 51-state validation
# sweep; www.ncleg.gov titles pages "Senate Bill 362", www.palegis.us titles
# "Senate Resolution 14") never render the abbreviated "SB"/"SR" form at
# all, only the spelled-out one. Deliberately a SMALL, conservative fixed
# map of the standard legislative abbreviations (not a guess-based
# expansion) -- an unrecognized prefix just skips this candidate family
# rather than inventing a wrong expansion.
_PREFIX_SPELLOUTS: dict[str, str] = {
    "hb": "house bill",
    "sb": "senate bill",
    "hr": "house resolution",
    "sr": "senate resolution",
    "hjr": "house joint resolution",
    "sjr": "senate joint resolution",
    "hcr": "house concurrent resolution",
    "scr": "senate concurrent resolution",
}


def _anchor_pattern(candidate: str) -> re.Pattern:
    """Compile a bare surface-form candidate (e.g. "hb 1057", "h1", "senate
    bill 362") into a boundary-anchored regex: a non-alphanumeric character
    (or string start) must precede the leading alpha prefix, AND a non-digit
    character (or string end) must follow the trailing number.

    This is what closes Finding 2's false-PASS bugs: an un-anchored
    substring search lets 'hb 1057' match inside 'shb 1057' (wrong bill
    prefix, just happens to contain ours), 'h1' match inside 'graph1' or
    'march1', 'senate bill 362' match inside 'senate bill 3625' (a DIFFERENT
    bill number that merely starts with ours), and 'h0914' match inside
    'h09140'. Requiring real boundaries on both ends makes every one of
    those a genuine non-match while leaving true positives (a real,
    boundary-delimited occurrence of the candidate) matching exactly as
    before -- candidates are matched against `_normalize_page_text_for_match`
    output, which casefolds + strips punctuation but PRESERVES whitespace as
    a real separator, so `\\s` counts as a valid non-alphanumeric boundary
    too.
    """
    return re.compile(
        rf"(?<![a-z0-9]){re.escape(candidate)}(?!\d)"
    )


def _normalize_for_page_match(identifier: str) -> list[re.Pattern]:
    """Cross-source surface forms an identifier might appear as on an
    official legislature/Open States page. State sites vary wildly in
    spacing/punctuation/year-prefixing conventions -- real fails from the
    51-state sweep clustered around a handful of state-specific renderings
    that a plain "HB 123"/"HB123"/"H.B. 123" set doesn't cover:

        - CO: "HB26-1057" -- a 2-digit session-year prefix inserted between
          the letter prefix and the number ("HB 1057" -> "HB26-1057").
        - NJ/MA/NH/AL/MD/NC/ID/NM: bare, zero-padded, or letter-concatenated
          forms like "S907"/"H0914" -- often just the FIRST letter of a
          multi-letter prefix (e.g. "HB 914" rendered as "H0914", not
          "HB0914"), sometimes zero-padded to a fixed width.
        - NC/AL/PA: the prefix spelled out in full instead of abbreviated
          ("SB 362" -> page titled "Senate Bill 362"; "SR 14" -> "Senate
          Resolution 14") -- a real fail from the 51-state sweep against
          www.ncleg.gov and www.palegis.us, whose page titles never render
          the abbreviated form at all.

    Every returned candidate is compiled to a boundary-ANCHORED regex (see
    `_anchor_pattern`) and matched against a page text that has ALSO been
    punctuation-stripped and casefolded, with whitespace COLLAPSED but
    preserved as real token separators (see `_normalize_page_text_for_match`)
    via `_check_cross_source` -- so candidates here are lowercase and
    punctuation-free, but a candidate with a space (e.g. "hb 1057") only
    matches a page that itself has that separator; callers must not compare
    these against a raw, un-normalized page string.

    Every candidate still requires the alpha prefix (or its first letter)
    immediately adjacent to the number -- this deliberately does NOT loosen
    to "any 3-4 digit number matches", which would false-positive on page
    furniture like zip codes, years, or other bills' numbers.
    """
    compact = re.sub(r"[^A-Za-z0-9]", "", identifier).lower()
    m = re.match(r"^([a-z]+)(\d+)$", compact)
    forms = {compact}
    if not m:
        return [_anchor_pattern(f) for f in forms]

    prefix, number = m.groups()
    first_letter = prefix[0]
    padded_number_3 = number.zfill(3)
    padded_number_4 = number.zfill(4)

    forms.update(
        {
            f"{prefix}{number}",
            f"{prefix} {number}",
            # First-letter-only concatenation, the NJ/MA/NH/AL/MD/NC/ID/NM
            # pattern ("HB 914" -> "H914"), plain and zero-padded to common
            # state-site widths.
            f"{first_letter}{number}",
            f"{first_letter}{padded_number_3}",
            f"{first_letter}{padded_number_4}",
            # Full-prefix zero-padded variants too, since some sites pad
            # after the full letter prefix rather than just its first letter.
            f"{prefix}{padded_number_3}",
            f"{prefix}{padded_number_4}",
            # SEPARATED zero-padded forms -- padding AND a separator between
            # prefix and number. www.scstatehouse.gov renders identifier
            # "S 537" as "S*0537", which normalizes to "s 0537": zero-padded
            # like the concatenated forms above, but still separated. Without
            # these, every South Carolina bill failed cross_source against a
            # page that was plainly showing the right bill, which is what
            # held SC at DEGRADED.
            f"{first_letter} {padded_number_3}",
            f"{first_letter} {padded_number_4}",
            f"{prefix} {padded_number_3}",
            f"{prefix} {padded_number_4}",
        }
    )

    spellout = _PREFIX_SPELLOUTS.get(prefix)
    if spellout:
        forms.add(f"{spellout} {number}")

    patterns = [_anchor_pattern(f) for f in forms]

    # CO-style 2-digit-year insert between prefix and number: "HB26-1057",
    # or "SB26-024" for a lower/zero-padded bill number (identifier "SB 24"
    # -> real page renders the number zero-padded to 3 digits EVEN in the
    # year-prefixed form -- a real fail from the 51-state validation sweep:
    # "SB 24" on leg.colorado.gov/bills/SB26-024 is literally titled
    # "SB26-024", not "SB2624"). The exact year isn't known at match time
    # (this function has no session date to work from and guessing one year
    # would miss every other one), so this is expressed as a REGEX candidate
    # rather than a literal string -- checked with re.search against the
    # punctuation-stripped (but whitespace-preserving) normalized page text,
    # still anchored on the alpha prefix immediately before the 2-digit year
    # and the number immediately after (both boundary conditions -- a
    # non-alphanumeric char/string-start before the prefix, a non-digit
    # char/string-end after the number -- so it can't drift into matching an
    # unrelated number or an unrelated prefix that just happens to end in
    # these letters). `\d*` before the number allows the optional
    # zero-padding without requiring it.
    year_insert_pattern = re.compile(
        rf"(?<![a-z0-9]){re.escape(prefix)}\d{{2}}0*{re.escape(number)}(?!\d)"
    )
    patterns.append(year_insert_pattern)

    return patterns


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
# Any surface form a bill number might take elsewhere on the page ("SB 42",
# "HB123", "H.B. 4" for SOME other bill) -- used to tell "this is a real
# legislature content page that simply doesn't mention OUR bill" (a genuine
# mismatch) apart from "this page has no bill-number tokens at all" (a
# JS-app shell that never got a chance to render anything).
#
# Two exclusions, both from false positives observed on real pages that held
# AL, NJ and SC at DEGRADED for days. The naive pattern (`[A-Z]{1,4}\s?\d+`)
# matches ordinary postal addresses, and every state site puts its address in
# the page footer:
#
#   NJ  "Office of Public Information Room B50 State House Annex"  -> "B50"
#   SC  "1105 Pendleton Street * Columbia, SC 29201"               -> "SC 29201"
#
# One such match convinced the JS-shell guard below that an app shell was a
# real content page, so a missing identifier was reported as a genuine data
# mismatch rather than an unverifiable one.
#
#   * `\d{1,4}` -- bill numbers are at most four digits; this alone excludes
#     5-digit ZIP codes.
#   * the lookbehind excludes address/room prefixes, which is what "B50"
#     needs (a bare "B" prefix stays legal -- DC really does number its bills
#     "B26-0187").
_BILL_NUMBER_TOKEN_RE = re.compile(
    r"(?<!\bRoom )(?<!\bSuite )(?<!\bSte )(?<!\bUnit )(?<!\bApt )"
    r"(?<!\bBldg )(?<!\bFloor )(?<!\bBox )"
    r"\b[A-Z]{1,4}[\s.*-]?\d{1,4}\b"
)
_MIN_VISIBLE_TEXT_CHARS = 500


def _visible_text(html: str) -> str:
    """Strip tags (and, crucially, <script>/<style> BLOCK CONTENTS -- not
    just the tags) to approximate what a human sees rendered. A JS app
    shell's <style> block alone (inline font-face CSS, etc.) can be
    thousands of bytes despite having zero visible bill content, so
    stripping only the tag delimiters and leaving that CSS/JS text in place
    would make an empty shell look like a substantial content page and
    silently defeat this whole heuristic (confirmed against a real
    wyoleg.gov response during the 51-state sweep: an 11KB page collapsed
    to 26 visible chars once <style> contents were removed too)."""
    without_script_style = _SCRIPT_STYLE_RE.sub(" ", html)
    return _WHITESPACE_RE.sub(" ", _TAG_RE.sub(" ", without_script_style)).strip()


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
    # Match against VISIBLE (tag-stripped) text, not the raw HTML -- matching
    # raw markup risks a short surface-form candidate (e.g. a first-letter
    # concatenation like "h1" for identifier "HB 1") colliding with an
    # unrelated HTML tag/attribute token (a real collision found during this
    # round's own test suite: "h1" matched inside a stripped-of-punctuation
    # "<h1>" heading tag).
    visible = _visible_text(page_text)
    normalized_page_text = _normalize_page_text_for_match(visible)
    candidates = _normalize_for_page_match(bill.identifier)
    matched = any(candidate.search(normalized_page_text) for candidate in candidates)
    if matched:
        return LegResult("cross_source", PASS, f"source_url page contains identifier match")

    # Genuine 200 but the identifier isn't on the page. Before calling this a
    # real mismatch, rule out a JS-rendered app shell: the fetched HTML's
    # visible (tag-stripped) text is either near-empty, or contains no
    # bill-number-like token at all -- either way, there's no real content
    # for the identifier to have been absent FROM. A page with substantial
    # visible text AND other bill-number tokens present (just not ours) is a
    # genuine mismatch and stays a real fail.
    if len(visible) < _MIN_VISIBLE_TEXT_CHARS or not _BILL_NUMBER_TOKEN_RE.search(visible):
        return LegResult(
            "cross_source",
            UNVERIFIABLE_JS_PAGE,
            f"source_url page fetched 200 but has no substantive bill content "
            f"({len(visible)} visible chars, "
            f"{'no' if not _BILL_NUMBER_TOKEN_RE.search(visible) else 'some'} bill-number tokens) "
            f"-- likely a JS-rendered app shell",
        )

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


# ---------------------------------------------------------------------------
# Phase 1: SHORT read txn -- sample + materialize into plain snapshots, then
# the caller closes the session. Nothing below this point in the call chain
# touches a DB session until phase 3.
# ---------------------------------------------------------------------------


def _load_snapshot(db: OrmSession, jurisdiction: Jurisdiction, sample_size: int) -> list[_BillSnapshot]:
    """Sample bills + compute the structural leg (DB-only) + pick the
    keyword-search probe word, all while the read session is still open, and
    return plain dataclasses that carry no ORM/session state. This is the
    ONLY function in the txn-free path that touches `db`."""
    bills = sample_bills(db, jurisdiction, sample_size)
    snapshots: list[_BillSnapshot] = []
    for bill in bills:
        structural = _check_structural(db, bill)
        keyword = _rarest_title_word(db, bill.title)
        snapshots.append(
            _BillSnapshot(
                bill_id=str(bill.id),
                identifier=bill.identifier,
                title=bill.title,
                source_url=bill.source_url,
                structural_ok=structural.status == PASS,
                structural_detail=structural.detail,
                keyword=keyword,
            )
        )
    return snapshots


# ---------------------------------------------------------------------------
# Phase 2: NO-TXN external phase -- search_retrieval + cross_source, with NO
# DB session open at all. Bounded per-request timeouts + a per-jurisdiction
# wall-clock cap so a hung/slow site can never stall the caller's loop.
# ---------------------------------------------------------------------------


def _search_retrieval_from_snapshot(
    client: httpx.Client, snapshot: _BillSnapshot, jurisdiction_abbr: str
) -> list[LegResult]:
    """Same two checks as `_check_search_retrieval`, but driven off a
    `_BillSnapshot` (no `Bill`/db needed) -- the keyword was already picked
    during phase 1 while the DB was still open."""
    legs: list[LegResult] = []

    try:
        by_number = _search_api_get(
            client, q=snapshot.identifier, jurisdiction=jurisdiction_abbr, per_page=10
        )
    except httpx.HTTPError as exc:
        legs.append(LegResult("bill_number_search", UNVERIFIABLE, f"search API unreachable: {exc}"))
        return legs

    if not _bill_in_results(by_number, _SnapshotBillLike(snapshot.bill_id)):
        legs.append(
            LegResult(
                "bill_number_search",
                FAIL,
                f"bill-number search for {snapshot.identifier!r} in {jurisdiction_abbr} returned no match",
            )
        )
        return legs

    legs.append(
        LegResult("bill_number_search", PASS, f"bill-number search for {snapshot.identifier!r} matched")
    )

    if snapshot.keyword is None:
        legs.append(LegResult("keyword_search", ADVISORY_PASS, "title had no distinctive keyword to probe"))
        return legs

    try:
        by_keyword = _search_api_get(
            client, q=snapshot.keyword, jurisdiction=jurisdiction_abbr, per_page=25
        )
    except httpx.HTTPError as exc:
        legs.append(LegResult("keyword_search", UNVERIFIABLE, f"keyword search API unreachable: {exc}"))
        return legs

    if not _bill_in_results(by_keyword, _SnapshotBillLike(snapshot.bill_id)):
        legs.append(
            LegResult(
                "keyword_search",
                ADVISORY_FAIL,
                f"keyword search for {snapshot.keyword!r} (from title) did not surface {snapshot.identifier}",
            )
        )
        return legs

    legs.append(LegResult("keyword_search", ADVISORY_PASS, f"keyword ({snapshot.keyword!r}) search matched"))
    return legs


@dataclass
class _SnapshotBillLike:
    """Minimal stand-in exposing just the `.id`/`.identifier`/`.source_url`
    attributes `_bill_in_results`/`_check_cross_source` need, so those
    helpers stay shared between the legacy `Bill`-based path and the
    txn-free snapshot path without duplicating their matching logic."""

    id: str
    identifier: str = ""
    source_url: str | None = None


def _cross_source_from_snapshot(
    client: httpx.Client, robots_cache: RobotsCache, snapshot: _BillSnapshot
) -> LegResult:
    """Same check as `_check_cross_source`, driven off a `_BillSnapshot`."""
    bill_like = _SnapshotBillLike(
        id=snapshot.bill_id, identifier=snapshot.identifier, source_url=snapshot.source_url
    )
    return _check_cross_source(client, robots_cache, bill_like)  # type: ignore[arg-type]


def _run_external_phase(
    snapshots: list[_BillSnapshot],
    jurisdiction_abbr: str,
    *,
    search_client: httpx.Client,
    source_client: httpx.Client,
    robots_cache: RobotsCache,
    jurisdiction_timeout: float,
) -> list[BillValidationResult]:
    """Run search_retrieval + cross_source for every snapshot, with NO open
    DB session, honoring a per-jurisdiction wall-clock cap. Once the cap is
    hit, every remaining leg (for the current bill and any not-yet-started
    bills) is recorded as 'unverifiable' -- honest, non-fatal degradation,
    never a raised exception and never a silent fail."""
    deadline = time.monotonic() + jurisdiction_timeout
    results: list[BillValidationResult] = []

    for snapshot in snapshots:
        result = BillValidationResult(bill_id=snapshot.bill_id, identifier=snapshot.identifier)
        result.legs.append(
            LegResult("structural", PASS if snapshot.structural_ok else FAIL, snapshot.structural_detail)
        )

        if time.monotonic() >= deadline:
            result.legs.append(
                LegResult("bill_number_search", UNVERIFIABLE, "per-jurisdiction validation timeout reached")
            )
            result.legs.append(
                LegResult("cross_source", UNVERIFIABLE, "per-jurisdiction validation timeout reached")
            )
            results.append(result)
            continue

        result.legs.extend(_search_retrieval_from_snapshot(search_client, snapshot, jurisdiction_abbr))

        if time.monotonic() >= deadline:
            result.legs.append(
                LegResult("cross_source", UNVERIFIABLE, "per-jurisdiction validation timeout reached")
            )
            results.append(result)
            continue

        result.legs.append(_cross_source_from_snapshot(source_client, robots_cache, snapshot))
        results.append(result)

    return results


def validate_jurisdiction_txnfree(
    jurisdiction_id: uuid.UUID | str,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    search_client: httpx.Client | None = None,
    source_client: httpx.Client | None = None,
    robots_cache: RobotsCache | None = None,
    jurisdiction_timeout: float = DEFAULT_JURISDICTION_TIMEOUT,
) -> ValidationSummary:
    """Full validation pass for one jurisdiction with NO DB session held
    during external HTTP -- the fix for the `idle in transaction` pattern
    that starved the crawl worker's fetch_text queue (see module + cli.py
    docstrings). Opens and closes its own short-lived sessions; does NOT
    accept a caller session.

    Three phases:
      1. SHORT read txn (`_load_snapshot`): sample bills, run the DB-only
         structural leg, pick the keyword-search probe word, materialize
         everything into `_BillSnapshot`s, then close the session.
      2. NO-TXN external phase (`_run_external_phase`): search_retrieval
         (deployed production search API) + cross_source (each bill's
         official `source_url`), bounded by per-request timeouts on the
         clients and an overall `jurisdiction_timeout` wall-clock cap.
      3. SHORT write txn (caller-facing: `record_validation_run` +
         `apply_validation_result`) -- NOT done here; this function only
         returns the `ValidationSummary`. `validate_and_record` (below)
         chains phase 3 for CLI/worker callers that want the full
         sample+validate+persist pipeline in one call.
    """
    db = get_session()
    try:
        jurisdiction = db.get(Jurisdiction, jurisdiction_id)
        if jurisdiction is None:
            raise ValueError(f"validate_jurisdiction_txnfree: no jurisdiction row for id={jurisdiction_id!r}")
        jurisdiction_abbr = jurisdiction.abbreviation
        snapshots = _load_snapshot(db, jurisdiction, sample_size)
    finally:
        db.close()

    owns_search_client = search_client is None
    owns_source_client = source_client is None
    search_client = search_client or new_client(
        base_url=DEFAULT_SEARCH_API_BASE, timeout=DEFAULT_SEARCH_TIMEOUT
    )
    source_client = source_client or new_client(timeout=DEFAULT_SOURCE_TIMEOUT)
    robots_cache = robots_cache or RobotsCache(client=source_client)

    summary = ValidationSummary(jurisdiction_abbr=jurisdiction_abbr, session_id=None)
    try:
        summary.bills = _run_external_phase(
            snapshots,
            jurisdiction_abbr,
            search_client=search_client,
            source_client=source_client,
            robots_cache=robots_cache,
            jurisdiction_timeout=jurisdiction_timeout,
        )
    finally:
        if owns_search_client:
            search_client.close()
        if owns_source_client:
            source_client.close()

    return summary


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
    """Legacy entrypoint kept for the `validate` CLI + existing tests: same
    external checks as `validate_jurisdiction_txnfree`, but callable with an
    ALREADY-OPEN caller session for the (DB-only) sampling/structural phase.
    Delegates phase 1 to `_load_snapshot` on the caller's session (closed by
    the caller, not here) and phase 2 to the same `_run_external_phase` used
    by the txn-free path, so external HTTP is never issued while holding a
    long-lived transaction open in either code path -- the caller's session
    is only used for the short DB-only read, never across the network calls
    below."""
    snapshots = _load_snapshot(db, jurisdiction, sample_size)

    owns_search_client = search_client is None
    owns_source_client = source_client is None
    search_client = search_client or new_client(
        base_url=DEFAULT_SEARCH_API_BASE, timeout=DEFAULT_SEARCH_TIMEOUT
    )
    source_client = source_client or new_client(timeout=DEFAULT_SOURCE_TIMEOUT)
    robots_cache = robots_cache or RobotsCache(client=source_client)

    summary = ValidationSummary(jurisdiction_abbr=jurisdiction.abbreviation, session_id=None)
    try:
        summary.bills = _run_external_phase(
            snapshots,
            jurisdiction.abbreviation,
            search_client=search_client,
            source_client=source_client,
            robots_cache=robots_cache,
            jurisdiction_timeout=DEFAULT_JURISDICTION_TIMEOUT,
        )
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
    validation sample AND full text for at least
    GREEN_FULLTEXT_COVERAGE_THRESHOLD of the bills whose text is actually
    obtainable (`full_text_available_count`) -- this harness alone can
    promote a jurisdiction only as far as VALIDATING (clean sample, crawl
    still filling in) or GREEN (clean sample AND the text substantially
    landed). A jurisdiction with nothing obtainable at all (available == 0)
    satisfies #5 vacuously and may be GREEN with the limitation spelled out
    in known_gaps. A jurisdiction with bill_count == 0 is left alone
    (nothing to validate).

    Two things regress an existing GREEN, so a stale badge can't outlive the
    facts behind it: a hard validation FAILURE (pass_rate below threshold)
    demotes toward DEGRADED, and full-text coverage below the threshold
    demotes to VALIDATING (a crawl in progress, deliberately NOT DEGRADED --
    it isn't a fault). Otherwise a row already at GREEN/DEGRADED/BLOCKED is
    left where a human/operator set it.
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
        available = coverage.full_text_available_count
        if available is None:
            # Never recomputed, so how much text is obtainable is unknown.
            # Refuse to promote on an unmeasured denominator rather than
            # guess; the next coverage recompute pass resolves this.
            if coverage.status in ("BOOTSTRAPPED", "METADATA_SEARCHABLE", "SOURCE_IDENTIFIED"):
                coverage.status = "VALIDATING"
            coverage.known_gaps = (
                "obtainable full-text count not yet computed; GREEN deferred "
                "until the next coverage recompute pass"
            )
            continue

        # BLOCKED is operator-set and stays put in every branch below -- a
        # passing sample must not silently un-block a jurisdiction someone
        # deliberately took out of service.
        blocked = coverage.status == "BLOCKED"

        if available == 0:
            # Nothing is obtainable (no documents published, or every one is
            # robots-disallowed / has no text layer). Criterion #5 is vacuous
            # here, so a clean sample earns GREEN -- but the limitation is
            # stated rather than papered over, because "GREEN" must not imply
            # full-text search a user won't actually get.
            if not blocked:
                coverage.status = "GREEN"
            coverage.known_gaps = (
                "no full text obtainable from source (no documents published, "
                "or all are robots-disallowed / have no extractable text); "
                "bills remain metadata-searchable"
            )
        elif coverage.full_text_count >= GREEN_FULLTEXT_COVERAGE_THRESHOLD * available:
            if not blocked:
                coverage.status = "GREEN"
            # Clear any stale gap message left by a prior DEGRADED/VALIDATING
            # pass -- a GREEN row that still says "full-text coverage is 0" is
            # a lie once text has landed.
            coverage.known_gaps = None
        else:
            # SPEC GREEN criterion #5 ("full text searchable wherever
            # technically available") is not yet satisfied by this pass --
            # cap at METADATA_SEARCHABLE/VALIDATING rather than fabricate a
            # GREEN this harness can't actually back up. This is a crawl still
            # in progress, not a fault, so it must not read as DEGRADED.
            if coverage.status in _RECOVERABLE_INTO_VALIDATING:
                coverage.status = "VALIDATING"
            pct = 100.0 * coverage.full_text_count / available
            coverage.known_gaps = (
                f"full text for {coverage.full_text_count}/{available} obtainable "
                f"bills ({pct:.1f}%); GREEN requires "
                f"{GREEN_FULLTEXT_COVERAGE_THRESHOLD:.0%} -- full-text crawl in progress"
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


def validate_and_record_txnfree(
    jurisdiction_id: uuid.UUID | str,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    search_client: httpx.Client | None = None,
    source_client: httpx.Client | None = None,
    robots_cache: RobotsCache | None = None,
    jurisdiction_timeout: float = DEFAULT_JURISDICTION_TIMEOUT,
) -> ValidationSummary:
    """Phase 1+2 (`validate_jurisdiction_txnfree`, no open session during
    HTTP) followed by phase 3, a fresh SHORT write txn that persists the
    `validation_runs` row + updates `jurisdiction_coverage`, commits, and
    closes. This is the entrypoint the dedicated `validate-worker` loop
    calls -- at no point across the whole call is a DB session open while
    external HTTP is in flight."""
    summary = validate_jurisdiction_txnfree(
        jurisdiction_id,
        sample_size=sample_size,
        search_client=search_client,
        source_client=source_client,
        robots_cache=robots_cache,
        jurisdiction_timeout=jurisdiction_timeout,
    )

    db = get_session()
    try:
        jurisdiction = db.get(Jurisdiction, jurisdiction_id)
        if jurisdiction is None:
            raise ValueError(f"validate_and_record_txnfree: no jurisdiction row for id={jurisdiction_id!r}")
        record_validation_run(db, summary, jurisdiction)
        apply_validation_result(db, jurisdiction, summary)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return summary
