"""Unit tests for billcommons_ingest.session_match.resolve_session.

Business intent: Open States bulk-zip session slugs (e.g. "2026rs") never
match the registry-seeded human session identifiers (e.g. "2026 Regular
Session") by exact string equality -- these tests encode the real slug
families seen across the 51-jurisdiction bulk-zip inventory
(data/bulkzips/*.zip) so a regression in the normalization/classification
logic breaks a specific real state's bootstrap, not just an abstract case.
"""
from __future__ import annotations

from billcommons_ingest.session_match import (
    MatchPath,
    SessionCandidate,
    resolve_session,
)


def test_al_2026rs_matches_2026_regular_session():
    # AL_2026rs.zip against registry's "2026 Regular Session".
    candidates = [SessionCandidate(identifier="2026 Regular Session", classification="regular")]
    result = resolve_session("2026rs", candidates)
    assert result.path == MatchPath.FUZZY
    assert result.candidate.identifier == "2026 Regular Session"


def test_ar_2026f_matches_2026_fiscal_session():
    # AR_2026F.zip -- the *regular* seeded session is "2026 Fiscal Session".
    candidates = [SessionCandidate(identifier="2026 Fiscal Session", classification="regular")]
    result = resolve_session("2026F", candidates)
    assert result.path == MatchPath.FUZZY
    assert result.candidate.identifier == "2026 Fiscal Session"


def test_ar_2026s1_does_not_match_fiscal_session():
    # AR_2026S1.zip is the special "Tax rates" session -- must NOT match the
    # regular fiscal session even though both mention 2026.
    candidates = [SessionCandidate(identifier="2026 Fiscal Session", classification="regular")]
    result = resolve_session("2026S1", candidates)
    assert result.path == MatchPath.NONE


def test_ar_2026s1_matches_seeded_special_session():
    candidates = [
        SessionCandidate(identifier="2026 Fiscal Session", classification="regular"),
        SessionCandidate(
            identifier="Arkansas special: Tax rates (2026-05-04)", classification="special"
        ),
    ]
    result = resolve_session("2026S1", candidates)
    assert result.path == MatchPath.FUZZY
    assert result.candidate.identifier == "Arkansas special: Tax rates (2026-05-04)"


def test_ca_20252026_matches_2025_2026_regular_session():
    # CA_20252026.zip (no separators) against "2025-2026 Regular Session".
    candidates = [SessionCandidate(identifier="2025-2026 Regular Session", classification="regular")]
    result = resolve_session("20252026", candidates)
    assert result.path == MatchPath.FUZZY
    assert result.candidate.identifier == "2025-2026 Regular Session"


def test_ca_special_session_slug_matches_special_not_regular():
    candidates = [
        SessionCandidate(identifier="2025-2026 Regular Session", classification="regular"),
        SessionCandidate(identifier="California special: redistricting (2025-11-01)", classification="special"),
    ]
    result = resolve_session("20252026-Special-Session-1", candidates)
    assert result.path == MatchPath.FUZZY
    assert "special" in result.candidate.identifier.lower()


def test_nc_2025_matches_2025_2026_regular_session():
    # NC_2025.zip -- registry uses a biennium identifier "2025-2026 Regular
    # Session"; slug only carries the first year.
    candidates = [SessionCandidate(identifier="2025-2026 Regular Session", classification="regular")]
    result = resolve_session("2025", candidates)
    assert result.path == MatchPath.FUZZY
    assert result.candidate.identifier == "2025-2026 Regular Session"


def test_tx_89r_matches_89th_legislature():
    # TX_89R.zip against "89th Legislature (2025 Regular Session)".
    candidates = [
        SessionCandidate(
            identifier="89th Legislature (2025 Regular Session)", classification="special"
        )
    ]
    result = resolve_session("89R", candidates)
    assert result.path == MatchPath.FUZZY
    assert result.candidate.identifier == "89th Legislature (2025 Regular Session)"


def test_tx_891_first_called_special_does_not_match_regular_session():
    # TX_891.zip / TX_892.zip are called special sessions ("1st Called
    # Session", "2nd Called Session") -- the bare numeric slug "891" (89th
    # Legislature, 1st special) should not silently collide with the base
    # 89R regular-session row once specials are also seeded.
    candidates = [
        SessionCandidate(identifier="89th Legislature (2025 Regular Session)", classification="regular"),
        SessionCandidate(
            identifier="Texas special: 1st Called Session (2025-07-21)", classification="special"
        ),
    ]
    result = resolve_session("891", candidates)
    assert result.path == MatchPath.FUZZY
    assert result.candidate.identifier == "Texas special: 1st Called Session (2025-07-21)"


def test_dc_26_matches_council_period_26():
    # DC_26.zip against "Council Period 26 (2025-2026)".
    candidates = [SessionCandidate(identifier="Council Period 26 (2025-2026)", classification="regular")]
    result = resolve_session("26", candidates)
    assert result.path == MatchPath.FUZZY
    assert result.candidate.identifier == "Council Period 26 (2025-2026)"


def test_ak_34_matches_34th_legislature():
    # AK_34.zip against "2026 Regular Session (34th Legislature, 2nd Session)".
    candidates = [
        SessionCandidate(
            identifier="2026 Regular Session (34th Legislature, 2nd Session)",
            classification="special",
        )
    ]
    result = resolve_session("34", candidates)
    assert result.path == MatchPath.FUZZY
    assert result.candidate.identifier == "2026 Regular Session (34th Legislature, 2nd Session)"


def test_ambiguous_candidates_never_guessed():
    # Two regular sessions both mentioning 2026 for the same jurisdiction:
    # must return NONE, never pick one arbitrarily.
    candidates = [
        SessionCandidate(identifier="2026 Regular Session", classification="regular"),
        SessionCandidate(identifier="2026 Special Session A", classification="regular"),
    ]
    result = resolve_session("2026", candidates)
    # "2026 Special Session A" contains the word "special" so it's
    # classified as special and excluded; only "2026 Regular Session"
    # survives classification filtering -> unique match, not ambiguous.
    assert result.path == MatchPath.FUZZY
    assert result.candidate.identifier == "2026 Regular Session"


def test_truly_ambiguous_two_regular_candidates_returns_none():
    candidates = [
        SessionCandidate(identifier="2026 Regular Session", classification="regular"),
        SessionCandidate(identifier="2026 Regular Session (redux)", classification="regular"),
    ]
    result = resolve_session("2026", candidates)
    assert result.path == MatchPath.NONE


def test_no_candidates_returns_none():
    result = resolve_session("2026rs", [])
    assert result.path == MatchPath.NONE
