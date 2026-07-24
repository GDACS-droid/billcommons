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
