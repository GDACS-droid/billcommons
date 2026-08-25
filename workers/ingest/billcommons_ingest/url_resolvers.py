"""Data-driven URL resolver layer for stale-but-not-blocked upstream fetch
URLs.

Some jurisdictions' `bill_documents.url` values were correct at capture time
but the UPSTREAM site's URL shape has since drifted, so the document now
404s even though the bill/report is still published:

  * Massachusetts (malegislature.gov) stores a "docket" URL
    (`/Bills/{court}/HD854.pdf`) at capture time, but the docket-style path
    stops resolving once the docket is assigned a bill number -- the live
    page/PDF moves to the bill-style id (`/Bills/{court}/H854.pdf`).

    Docket ids (HD/SD) and bill ids (H/S) are INDEPENDENT sequences at MA
    -- HD177's bill is NOT H177 (H177 is a real, unrelated bill whose own
    docket is HD4189; verified live 2026-08-21). So this module does NOT
    derive/guess a bill id from a docket id at all anymore -- it only
    recognizes the URL SHAPE (`ma_docket_from_url`) and hands the
    (court, id) pair to `fulltext._resolve_ma_document`, which looks up the
    AUTHORITATIVE bill number via MA's keyless public JSON document API
    (`/api/GeneralCourts/{court}/Documents/{id}`, see
    https://malegislature.gov/api/swagger) and cross-checks the resolved
    bill's own `DocketNumber` against the original docket before accepting
    its `DocumentText` -- that lookup is inherently a multi-step, live
    process (fetch the docket's record, read its `BillNumber`, only then
    fetch the bill's own record), not a static list of candidate URLs, so
    it cannot live in this module's data-driven, network-free
    `RESOLVER_RULES` table. `ma_api_url`/`ma_docket_from_url` here are pure
    URL-shape helpers shared by that resolution logic.
  * Iowa (legis.iowa.gov) renamed the publication-code path segment from
    `LGEG` to `LGI`; every other path segment is unchanged. This one IS a
    pure, static rewrite, so it stays in `RESOLVER_RULES` below.

This module holds ONLY pure URL-shape logic -- no network/DB access -- kept
deliberately separate from the fetch pipeline (`fulltext.py`) so it is
testable without any network/DB/httpx fixture -- see
`tests/test_url_resolvers.py`. The multi-step, network-dependent MA
resolution itself is tested in `tests/test_fulltext.py` alongside the rest
of the fetch pipeline, via the same `httpx.MockTransport` fixtures used
everywhere else there.

`RESOLVER_RULES` stays data-driven for jurisdictions (today, only Iowa)
whose rewrite is a pure function of the URL: adding one means adding one
`ResolverRule` entry, not another branch buried in the fetch path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

CandidateFn = Callable[[str, "str | None", "str | None"], list[str]]


# ---------------------------------------------------------------------------
# Massachusetts: URL-shape helpers only (see module docstring -- the actual
# docket -> bill-number resolution is a live, multi-step lookup that lives in
# fulltext._resolve_ma_document, not here).
# ---------------------------------------------------------------------------

# Matches a malegislature.gov bill-document path, e.g.
# ".../Bills/194/HD177.pdf" or ".../Bills/194/HD177" (no extension).
_MA_BILLS_PATH_RE = re.compile(
    r"^(?P<prefix>.*/Bills/)(?P<court>\d+)/(?P<docid>[A-Za-z]+\d+)(?P<suffix>\.[A-Za-z0-9]+)?$"
)
_MA_HOSTS = frozenset({"malegislature.gov", "www.malegislature.gov"})
# A House/Senate DOCKET id (assigned at filing) has the shape HD854/SD123 --
# as opposed to a House/Senate BILL id (assigned once a docket is taken up),
# e.g. H854/S123. Used only to decide WHICH resolution path to take (docket
# lookup first vs. treat the id as already a bill number); never to derive
# one id from the other.
_MA_DOCKET_SHAPE_RE = re.compile(r"^[HS]D\d+$", re.IGNORECASE)


def is_ma_docket_id(doc_id: str) -> bool:
    """True if `doc_id` has the DOCKET shape (HD854/SD123), as opposed to
    an already-assigned BILL id (H854/S123). Never implies anything about
    what the corresponding bill id (if any) actually is -- that is looked
    up live, never derived from the docket id's shape."""
    return bool(_MA_DOCKET_SHAPE_RE.match(doc_id))


def ma_api_url(court: str, doc_id: str) -> str:
    """The malegislature.gov keyless JSON document-API URL for `doc_id`
    (accepts both docket ids and bill ids) within `court`."""
    return f"https://malegislature.gov/api/GeneralCourts/{court}/Documents/{doc_id}"


@dataclass(frozen=True)
class MaDocumentUrl:
    court: str
    doc_id: str
    path_prefix: str
    path_suffix: str


def ma_docket_from_url(source_url: str) -> MaDocumentUrl | None:
    """If `source_url` matches malegislature.gov's bill-document PDF/page
    path shape, return its (court, doc_id, path_prefix, path_suffix) so the
    fetch pipeline can resolve the AUTHORITATIVE bill number via the JSON
    document API instead of guessing one from the id's shape. `None` for
    any URL that doesn't match (nothing MA-specific to do for it)."""
    if urlparse(source_url).hostname not in _MA_HOSTS:
        return None
    match = _MA_BILLS_PATH_RE.match(source_url)
    if not match:
        return None
    return MaDocumentUrl(
        court=match.group("court"),
        doc_id=match.group("docid"),
        path_prefix=match.group("prefix"),
        path_suffix=match.group("suffix") or "",
    )


# ---------------------------------------------------------------------------
# Iowa: publication-code path segment renamed LGEG -> LGI
# ---------------------------------------------------------------------------

_IA_STALE_SEGMENT = "/publications/LGEG/"
_IA_CURRENT_SEGMENT = "/publications/LGI/"


def _ia_candidates(source_url: str, bill_identifier: str | None, session: str | None) -> list[str]:
    if _IA_STALE_SEGMENT not in source_url:
        return []
    return [source_url.replace(_IA_STALE_SEGMENT, _IA_CURRENT_SEGMENT)]


# ---------------------------------------------------------------------------
# Rules table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolverRule:
    #: lowercase jurisdiction abbreviation this rule applies to (e.g. "ia")
    jurisdiction: str
    #: recorded as `url_resolver=<name>` in `bill_documents.license_note`
    #: when a candidate this rule produced is the one that fetched
    #: successfully -- see fulltext.py's `_mark_status`.
    name: str
    candidates: CandidateFn


# Massachusetts is deliberately NOT in this table -- its resolution is a
# live, multi-step lookup (fulltext._resolve_ma_document), not a static
# candidate list a jurisdiction-keyed rule can produce. See module
# docstring.
RESOLVER_RULES: tuple[ResolverRule, ...] = (
    ResolverRule("ia", "ia_lgeg_to_lgi", _ia_candidates),
)


def _candidates_with_rule_names(
    jurisdiction_code: str | None,
    source_url: str,
    bill_identifier: str | None = None,
    session: str | None = None,
) -> list[tuple[str, str | None]]:
    """Original URL first (rule name `None`), then every additional
    candidate produced by rules matching `jurisdiction_code`, in rule-table
    order, de-duplicated (a rewrite that happens to equal an earlier
    candidate is dropped, not repeated)."""
    result: list[tuple[str, str | None]] = [(source_url, None)]
    code = (jurisdiction_code or "").strip().lower()
    for rule in RESOLVER_RULES:
        if rule.jurisdiction != code:
            continue
        for candidate in rule.candidates(source_url, bill_identifier, session):
            if not any(existing == candidate for existing, _name in result):
                result.append((candidate, rule.name))
    return result


def resolve_fetch_url(
    jurisdiction_code: str | None,
    source_url: str,
    bill_identifier: str | None = None,
    session: str | None = None,
) -> list[str]:
    """Return candidate URLs to try for `source_url`, ORIGINAL FIRST, then
    any jurisdiction-specific rewrites.

    A jurisdiction with no matching rule (the overwhelming majority,
    including Massachusetts -- see module docstring) gets back exactly
    `[source_url]` -- fetch behavior is unchanged for it.

    `bill_identifier` / `session` are accepted for rules that need more than
    the URL itself to build a candidate (none of the current rules do --
    Iowa's derives everything from the URL's own path); kept in the
    signature so a future rule (e.g. one needing the bill's legislative
    session where the URL doesn't carry a court/session number) doesn't
    require changing every call site.
    """
    return [url for url, _rule_name in _candidates_with_rule_names(jurisdiction_code, source_url, bill_identifier, session)]


def resolver_name_for_candidate(
    jurisdiction_code: str | None,
    source_url: str,
    fetched_url: str,
    bill_identifier: str | None = None,
    session: str | None = None,
) -> str | None:
    """Given the URL that ended up fetching successfully, return the name of
    the rule that produced it (for `license_note`), or `None` if it was the
    original `source_url` (no rewrite involved)."""
    if fetched_url == source_url:
        return None
    for url, rule_name in _candidates_with_rule_names(jurisdiction_code, source_url, bill_identifier, session):
        if url == fetched_url:
            return rule_name
    return None
