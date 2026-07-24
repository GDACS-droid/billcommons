"""Resolve an Open States bulk-zip session slug (e.g. "2026rs", "2026F",
"89R") against a jurisdiction's registry-seeded `sessions` rows (which carry
human identifiers like "2026 Regular Session", "89th Legislature (2025
Regular Session)").

Three resolution paths, tried in order (see cmd_bootstrap in cli.py):
    (a) EXACT   -- identifier string match (current/original behavior).
    (b) FUZZY   -- normalized year + classification match against existing
                   sessions for the jurisdiction. Requires a UNIQUE
                   candidate; 2+ survivors is treated as no-match (never
                   guess between ambiguous candidates).
    (c) CREATE  -- no match found; caller creates a new Session row directly
                   from the zip's own slug/metadata.

This module only implements resolution (a)+(b) as a pure function
(`resolve_session`) so it's unit-testable without a DB; cli.py owns the
create-on-no-match path (c) since that needs a live ORM session to insert.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class MatchPath(str, Enum):
    EXACT = "exact"
    FUZZY = "fuzzy"
    NONE = "none"


@dataclass
class SessionCandidate:
    """Minimal duck-typed view of a `sessions` row, so this module doesn't
    need to import the ORM model (keeps it dependency-free / easy to unit
    test with plain objects)."""

    identifier: str
    classification: str | None = None


@dataclass
class MatchResult:
    path: MatchPath
    candidate: SessionCandidate | None = None
    reason: str = ""


_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
# Matches a 2-3 digit number NOT immediately preceded/followed by another
# digit (so it never grabs a substring of a 4-digit year) and optionally
# followed by an ordinal suffix (st/nd/rd/th) -- e.g. "89R" -> "89",
# "57th-2nd-regular" -> "57", "2", "34" -> "34". Applied to the *original*
# (non-normalized) text so word boundaries from spaces/punctuation still
# anchor the digit runs before they get stripped by `_normalize`.
_ORDINAL_LEG_RE = re.compile(r"(?<!\d)(\d{1,3})(?:st|nd|rd|th)?(?!\d)")


def _normalize(text: str) -> str:
    """casefold + strip punctuation/spaces, per FIX 1b."""
    text = text.casefold()
    return re.sub(r"[^a-z0-9]", "", text)


def _extract_years(text: str) -> set[str]:
    """All 4-digit years mentioned anywhere in the string (slug or
    identifier), e.g. "20252026" -> {"2025", "2026"}, "2026rs" -> {"2026"}.
    Handled specially for the concatenated-year case ("20252026") since the
    generic regex only greedily grabs the first 4 digits ("2025") -- we also
    split any 8-digit run into two 4-digit years."""
    years: set[str] = set()
    for run in re.finditer(r"(?:19|20)\d{2}(?:(?:19|20)\d{2})?", text):
        chunk = run.group(0)
        if len(chunk) == 8:
            years.add(chunk[:4])
            years.add(chunk[4:])
        else:
            years.add(chunk)
    return years


_TX_CALLED_SESSION_RE = re.compile(r"^(\d{2})([12])$")


def _extract_ordinal_numbers(text: str) -> set[str]:
    """Extract bare 1-3 digit "ordinal legislature/session/period number"
    tokens from the ORIGINAL (non-normalized) text, e.g. "89R" -> {"89"},
    "57th-2nd-regular" -> {"57", "2"}, "Council Period 26" -> {"26"}. Used
    for numeric-legislature slugs (TX "89R", AZ "57th-2nd-regular", DC "26",
    AK "34") that carry no calendar year at all. 4-digit years are excluded
    by construction (the regex only matches 1-3 digit runs).

    Special-cased for Texas's "<legislature><called-session-index>" bare
    numeric convention (e.g. "891" = 89th Legislature, 1st Called Session):
    a plain 3-digit token ending in 1 or 2 is split into its two logical
    components {legislature_ordinal, called_session_index} rather than kept
    whole, since a single seeded special-session row is expected to mention
    the legislature number and/or "1st"/"2nd Called Session" separately, not
    the concatenated "891" token itself."""
    tx_called = _TX_CALLED_SESSION_RE.match(text.strip())
    if tx_called:
        return {tx_called.group(1), tx_called.group(2)}
    return set(_ORDINAL_LEG_RE.findall(text))


def _classify_slug(slug_norm: str) -> str:
    """Infer a coarse classification from a *normalized* slug: 'special',
    'fiscal', or 'regular' (default). Special markers are checked first
    since a fiscal/budget marker is a weaker/rarer signal and special-session
    slugs like 's1'/'1e' never also mean fiscal."""
    if re.search(r"(special|(?<![a-z])s{1,2}\d|\d[es]\d?$|\d[e]$)", slug_norm):
        return "special"
    for marker in ("special", "sp", "ss"):
        if marker in slug_norm:
            return "special"
    for marker in ("fiscal", "budget"):
        if marker in slug_norm:
            return "fiscal"
    # Bare trailing 'f' with no other letters (e.g. "2026f") is Open States'
    # fiscal-session slug convention (seen in AR_2026F.zip).
    if re.fullmatch(r"\d+f", slug_norm):
        return "fiscal"
    # Texas's bare "<legislature><1|2>" called-session convention (e.g.
    # "891"/"892") is always a special session -- the base regular session
    # is always the "<legislature>R" form (e.g. "89R"), never a bare digit.
    if _TX_CALLED_SESSION_RE.match(slug_norm):
        return "special"
    return "regular"


def _classify_identifier(identifier_norm: str, classification: str | None) -> str:
    """Same coarse classification, but for an existing session row.

    Identifier TEXT is the primary signal and wins outright whenever it
    carries an explicit fiscal/budget/special keyword (registry entries
    sometimes leave the `classification` column as e.g. "regular" even for
    what's textually a fiscal session, e.g. Arkansas's "2026 Fiscal
    Session"). The `classification` column is only consulted when the text
    itself carries no keyword at all -- but even then it is NOT trusted for
    "special" (some registry rows mark a plainly-regular-named session
    "special" for legal/procedural reasons unrelated to naming, e.g. Texas's
    "89th Legislature (2025 Regular Session)" and Alaska's "... 34th
    Legislature, 2nd Session" are both registry-flagged "special" despite
    textually reading as regular sessions) -- so a keyword-less identifier
    always defaults to "regular"."""
    ident_norm = _normalize(identifier_norm)
    if "fiscal" in ident_norm or "budget" in ident_norm:
        return "fiscal"
    if "special" in ident_norm:
        return "special"
    return "regular"


def resolve_session(
    slug: str,
    candidates: list[SessionCandidate],
) -> MatchResult:
    """Resolve `slug` (an Open States zip session slug, e.g. "2026rs") against
    `candidates` (a jurisdiction's existing sessions rows). Exact-match is
    NOT attempted here -- callers should try an exact `identifier == slug`
    query against the DB first (path a) and only fall back to this fuzzy
    resolver (path b) if that misses, since exact match is a trivial O(1)
    indexed lookup that doesn't need the candidate list at all.

    Returns MatchPath.FUZZY with a single candidate on a unique match,
    MatchPath.NONE otherwise (including "2+ candidates survived" -- we never
    guess between ambiguous matches).
    """
    slug_norm = _normalize(slug)
    if not slug_norm:
        return MatchResult(MatchPath.NONE, reason="empty slug")

    slug_years = _extract_years(slug)
    slug_classification = _classify_slug(slug_norm)
    slug_ordinals = _extract_ordinal_numbers(slug)

    survivors: list[SessionCandidate] = []
    for cand in candidates:
        cand_years = _extract_years(cand.identifier)
        cand_classification = _classify_identifier(cand.identifier, cand.classification)

        if slug_classification != cand_classification:
            continue

        year_overlap = bool(slug_years & cand_years) if slug_years and cand_years else False

        ordinal_overlap = False
        if slug_ordinals:
            cand_ordinals = _extract_ordinal_numbers(cand.identifier)
            ordinal_overlap = bool(slug_ordinals & cand_ordinals)

        if year_overlap or ordinal_overlap:
            survivors.append(cand)

    if len(survivors) == 1:
        return MatchResult(MatchPath.FUZZY, candidate=survivors[0])
    if len(survivors) == 0:
        return MatchResult(MatchPath.NONE, reason=f"no candidate matched slug {slug!r}")
    return MatchResult(
        MatchPath.NONE,
        reason=(
            f"{len(survivors)} ambiguous candidates for slug {slug!r}: "
            f"{[c.identifier for c in survivors]}"
        ),
    )


def infer_classification_for_new_session(slug: str) -> str:
    """Classification to store on a freshly-CREATED session row (path c),
    inferred from the zip's own slug: 'regular' unless the slug carries a
    special/fiscal marker."""
    coarse = _classify_slug(_normalize(slug))
    return coarse
