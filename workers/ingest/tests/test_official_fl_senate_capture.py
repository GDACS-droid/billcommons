"""Contracts for the bounded Florida Senate detail-page capture wrapper."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from billcommons_ingest import official_fl_senate_actions as actions
from billcommons_ingest import official_fl_senate_capture as capture
from billcommons_shared.safe_http import SafeResponse


SOURCE_URL = "https://www.flsenate.gov/Session/Bill/2025/7031"
NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def test_invalid_detail_url_is_rejected_before_budget_or_http():
    forbidden = lambda *_args: pytest.fail("invalid URL must not reach capture side effects")
    with pytest.raises(actions.OfficialFloridaSenateActionsError):
        capture.capture_florida_senate_bill_detail(
            "\n" + SOURCE_URL,
            fetch=forbidden,
            budget=forbidden,
        )


def test_detail_capture_reuses_robots_budget_and_bounded_html_flow():
    calls: list[str] = []
    budgets: list[str] = []
    raw = b"<html><body>captured source evidence</body></html>"

    def fetch(url: str, byte_cap: int) -> SafeResponse:
        calls.append(url)
        if url.endswith("robots.txt"):
            assert byte_cap == capture.discovery.MAX_ROBOTS_BYTES
            return SafeResponse(404, {}, b"not found")
        assert byte_cap == capture.discovery.MAX_PAGE_BYTES
        return SafeResponse(200, {"content-type": "text/html", "last-modified": "Mon, 07 Sep 2026 01:00:00 GMT"}, raw)

    result = capture.capture_florida_senate_bill_detail(
        SOURCE_URL,
        fetch=fetch,
        budget=budgets.append,
        now=NOW,
    )

    assert result.error_class is None
    assert result.source_url == SOURCE_URL
    assert result.raw_bytes == raw
    assert result.upstream_modified == "Mon, 07 Sep 2026 01:00:00 GMT"
    assert calls == ["https://www.flsenate.gov/robots.txt", SOURCE_URL]
    assert budgets == calls


def test_detail_capture_retains_robots_denial_without_fetching_page():
    calls: list[str] = []

    def fetch(url: str, _byte_cap: int) -> SafeResponse:
        calls.append(url)
        return SafeResponse(200, {}, b"User-agent: *\nDisallow: /\n")

    result = capture.capture_florida_senate_bill_detail(
        SOURCE_URL,
        fetch=fetch,
        budget=lambda _url: None,
        now=NOW,
    )

    assert result.error_class == "robots_disallowed"
    assert result.raw_bytes is None
    assert result.robots_bytes == b"User-agent: *\nDisallow: /\n"
    assert calls == ["https://www.flsenate.gov/robots.txt"]
