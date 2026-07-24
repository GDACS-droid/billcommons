"""Tests for billcommons_shared.normalize.normalize_bill_number.

Each case encodes a real-world bill-number formatting quirk seen across
state legislature sites/Open States data. If normalization logic changes
(e.g. stops stripping leading zeros, or starts dropping alpha suffixes),
these tests should fail — that's the point: they pin down *why* the format
matters (dedup on identifier_norm, cross-source matching), not just what a
snapshot happens to look like today.
"""
import pytest

from billcommons_shared.normalize import normalize_bill_number


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Standard already-clean form.
        ("HB 123", "HB 123"),
        # Punctuated prefix ("H.B.") must collapse to "HB".
        ("H.B. 123", "HB 123"),
        # Lowercase + hyphen separator.
        ("hb-123", "HB 123"),
        # No separator at all.
        ("HB123", "HB 123"),
        # Joint resolution abbreviation, already spaced.
        ("SJR 7", "SJR 7"),
        # Joint resolution, no space, lowercase.
        ("sjr7", "SJR 7"),
        # NJ-style single-letter prefix directly touching digits.
        ("A1234", "A 1234"),
        # Maine-style "LD" (Legislative Document) prefix.
        ("LD 55", "LD 55"),
        # Leading zeros must be stripped from the numeric part.
        ("SB0001", "SB 1"),
        ("SB 0007", "SB 7"),
        # Multiple internal spaces collapse to one.
        ("HB   123", "HB 123"),
        # Trailing alpha suffix (amendment/companion marker) is preserved.
        ("HB 123A", "HB 123A"),
        ("hb123a", "HB 123A"),
        # Periods-only prefix punctuation variant.
        ("H.J.R. 4", "HJR 4"),
        # Mixed-case input with extra whitespace padding.
        ("  Hb 42  ", "HB 42"),
    ],
)
def test_normalize_bill_number(raw: str, expected: str) -> None:
    assert normalize_bill_number(raw) == expected


def test_normalize_bill_number_rejects_none() -> None:
    with pytest.raises(ValueError):
        normalize_bill_number(None)


def test_normalize_bill_number_rejects_unparseable_input() -> None:
    with pytest.raises(ValueError):
        normalize_bill_number("completely not a bill number !!!")
