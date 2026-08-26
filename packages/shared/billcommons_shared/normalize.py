"""Bill-number normalization.

Canonical form: uppercase alpha prefix, single space, digits with leading
zeros stripped, optional trailing alpha suffix preserved (e.g. "HB 123A").

Examples:
    "H.B. 123"  -> "HB 123"
    "hb-123"    -> "HB 123"
    "SB0001"    -> "SB 1"
    "A1234"     -> "A 1234"   (NJ-style single-letter prefix)
    "LD 55"     -> "LD 55"

The result is stored alongside the as-published `identifier` in the `bills`
table as `identifier_norm`, used for lookup/dedup and trigram search.
"""
from __future__ import annotations

import re

# Matches an optional alpha prefix (letters only) followed by digits and an
# optional trailing alpha suffix, after punctuation has been stripped and
# whitespace collapsed. Examples this must split correctly:
#   "HB123"   -> prefix="HB", digits="123", suffix=""
#   "A1234"   -> prefix="A",  digits="1234", suffix=""
#   "HB123A"  -> prefix="HB", digits="123", suffix="A"
_PATTERN = re.compile(r"^([A-Z]*)\s*0*(\d+)([A-Z]*)$")


def normalize_bill_number(raw: str) -> str:
    """Normalize a bill number/identifier to canonical "PREFIX NUM[SUFFIX]" form.

    Raises ValueError if the input doesn't contain a recognizable
    prefix+number pattern (callers should treat that as a data-quality issue
    to surface, not silently swallow).
    """
    if raw is None:
        raise ValueError("bill number is None")

    # Strip punctuation (periods, hyphens, etc.) but keep letters/digits/space,
    # uppercase, then collapse whitespace.
    cleaned = re.sub(r"[^A-Za-z0-9\s]", "", raw).upper()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Remove all internal spaces before pattern matching so "H B 123" and
    # "HB123" normalize identically; we reinsert the single canonical space.
    compact = cleaned.replace(" ", "")

    match = _PATTERN.match(compact)
    if not match:
        raise ValueError(f"could not parse bill number: {raw!r}")

    prefix, digits, suffix = match.groups()
    number = str(int(digits))  # strips leading zeros
    result = f"{prefix} {number}{suffix}" if prefix else f"{number}{suffix}"
    return result


# A single uppercase letter trailing a digit is NY's print/amendment version
# (e.g. "A 10008C"), never part of the bill's identity -- the corpus stores
# the bill as "A 10008". Lookups that resolve a survivor/substitution
# identifier against `bills.identifier_norm` need both forms, exact first.
# NY-only: FL ("HB 1A", a special-session print) and CA ("AB 1X") use the
# same trailing-letter shape to mean something else -- there the letter IS
# part of identity, so stripping it would resolve to the wrong bill. Mirrors
# workers/ingest/billcommons_ingest/status.py's `substitution_lookup_candidates`
# (kept independent/duplicated on purpose -- the API doesn't have ingest on
# its import path, and ingest is left untouched by this helper's existence).
_TRAILING_PRINT_VERSION_RE = re.compile(r"\d[A-Z]$")


def identifier_lookup_candidates(identifier: str, *, print_suffix: bool = False) -> list[str]:
    """Identifiers to try, in order, when resolving `identifier` (already
    normalized, e.g. via `normalize_bill_number`) against `bills.identifier_norm`.
    Always includes `identifier` itself; when `print_suffix` is True (NY
    only -- see module note above) and it ends in a digit followed by one
    uppercase letter (an NY print version), also includes that identifier
    with the trailing letter stripped."""
    candidates = [identifier]
    if print_suffix and _TRAILING_PRINT_VERSION_RE.search(identifier):
        candidates.append(identifier[:-1])
    return candidates
