"""Capture one exact Florida Senate bill-detail page as retained evidence.

This module authorizes no new source inventory.  Its only admissible input is
the exact URL contract already enforced by ``official_fl_senate_actions``;
after that validation it reuses the reviewed robots, rate-budget, SafeHTTP,
TLS, redirect, and response-size controls from the official landing observer.
It never parses a legislative fact or writes a database row.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from billcommons_ingest import official_discovery as discovery
from billcommons_ingest import official_fl_senate_actions as actions
from billcommons_shared.safe_http import SafeResponse


ADAPTER_NAME = "fl_senate_bill_history"
ADAPTER_VERSION = actions.ADAPTER_VERSION
JURISDICTION = "FL"


@dataclass(frozen=True)
class FloridaSenateDetailScope:
    """The exact one-bill source scope carried by a canonical detail URL."""

    source_url: str
    source_session_year: str
    source_bill_number: str


def detail_scope(source_url: str) -> FloridaSenateDetailScope:
    """Validate source URL before any capture side effect or budget consumption."""

    canonical_url, source_session_year, source_bill_number = actions._canonical_source_url(source_url)
    return FloridaSenateDetailScope(canonical_url, source_session_year, source_bill_number)


def capture_florida_senate_bill_detail(
    source_url: str, *,
    fetch: Callable[[str, int], SafeResponse] = discovery._fetch,
    budget: Callable[[str], None] = discovery._budget,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> discovery.OfficialDiscoveryCapture:
    """Read robots then one exact Florida Senate bill-detail HTML response.

    Invalid detail URLs raise the parser's structured URL-contract error
    before this function invokes fetch, budget, or sleep.  Expected transport
    and robots failures are returned as bounded capture evidence just as they
    are for the landing-page observer.
    """

    scope = detail_scope(source_url)
    return discovery._capture_reviewed_html(
        JURISDICTION,
        scope.source_url,
        fetch=fetch,
        budget=budget,
        sleep=sleep,
        now=now,
    )
