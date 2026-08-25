"""Unit coverage for the separate, CDP-assisted browser fetch path.

The browser callable is injected throughout: these tests never connect to
Chrome/CDP and use only the isolated pytest database fixture.
"""
from __future__ import annotations

import argparse
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from billcommons_ingest import browser_fetch, fulltext
from billcommons_ingest.cli import build_parser, cmd_browser_fetch
from billcommons_schema.models import Bill, BillDocument, BillVersion, Jurisdiction, Session as SessionModel
from billcommons_shared.rawstore import FilesystemRawStore


HOST = "capitol.tn.gov"


def _document(db_session, *, url: str = f"https://{HOST}/bills/test.pdf") -> BillDocument:
    jurisdiction = Jurisdiction(
        name="Browser Fetch Test",
        abbreviation=f"ZQ_BF_{uuid.uuid4().hex[:8].upper()}",
        classification="state",
    )
    db_session.add(jurisdiction)
    db_session.flush()
    session = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026", active=True)
    db_session.add(session)
    db_session.flush()
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session.id,
        identifier="HB 1",
        identifier_norm="HB 1",
        title="Browser fetch test bill",
    )
    db_session.add(bill)
    db_session.flush()
    version = BillVersion(bill_id=bill.id, note="introduced")
    db_session.add(version)
    db_session.flush()
    document = BillDocument(bill_version_id=version.id, url=url)
    db_session.add(document)
    db_session.flush()
    return document


def test_queue_selection_excludes_document_with_text(db_session):
    pending = _document(db_session)
    pending.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    already_text = _document(db_session, url=f"https://{HOST}/bills/already.pdf")
    already_text.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    already_text.extracted_text = "Already extracted."
    db_session.flush()

    selected = browser_fetch.select_browser_documents(db_session, HOST, limit=10)

    assert [document.id for document in selected] == [pending.id]


def test_non_200_charges_fetch_attempt(db_session, tmp_path):
    document = _document(db_session)
    document.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    db_session.flush()

    summary = browser_fetch.run_browser_fetch(
        db_session,
        hosts=(HOST,),
        limit=1,
        pace=0,
        dry_run=False,
        rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
        fetch_via_browser=lambda _url: (503, "text/plain", b"down"),
    )

    assert summary.fetched == 1
    assert summary.errors == 1
    assert document.fetch_attempts == 1
    assert document.license_note == f"fulltext_status={fulltext.STATUS_FETCH_ERROR}"


def test_fetch_error_document_is_reselected_by_browser_fetch_without_normal_worker(db_session, tmp_path):
    """R3-3: `fetch_error` is deliberately non-terminal, but before this fix
    `select_browser_documents` had no clause for it, so browser-fetch could
    not reclaim its own charged non-200 on its own cadence -- recovery
    depended on the normal robots-aware worker bouncing it back indirectly."""
    document = _document(db_session)
    document.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    db_session.flush()

    browser_fetch.run_browser_fetch(
        db_session,
        hosts=(HOST,),
        limit=1,
        pace=0,
        dry_run=False,
        rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
        fetch_via_browser=lambda _url: (503, "text/plain", b"down"),
    )
    assert document.license_note == f"fulltext_status={fulltext.STATUS_FETCH_ERROR}"

    reselected = browser_fetch.select_browser_documents(db_session, HOST, limit=10)
    assert [row.id for row in reselected] == [document.id]


def test_200_pdf_writes_text_with_ok_browser_status(db_session, tmp_path, monkeypatch):
    document = _document(db_session)
    document.license_note = f"fulltext_status={fulltext.STATUS_PERMANENTLY_FAILED}"
    document.fetch_attempts = 4
    db_session.flush()
    pdf = b"%PDF-browser-test"
    monkeypatch.setattr(
        fulltext,
        "extract_document_text",
        lambda _content_type, raw: fulltext.ExtractionOutcome(
            status=fulltext.STATUS_OK,
            extracted_text="Section 1. Browser-fetched public record.",
            checksum=hashlib.sha256(raw).hexdigest(),
        ),
    )

    summary = browser_fetch.run_browser_fetch(
        db_session,
        hosts=(HOST,),
        limit=1,
        pace=0,
        dry_run=False,
        rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
        fetch_via_browser=lambda _url: (200, "application/pdf", pdf),
    )

    assert summary.ok == 1
    assert document.extracted_text == "Section 1. Browser-fetched public record."
    assert document.license_note == f"fulltext_status={fulltext.STATUS_OK_BROWSER} via=browser"
    assert document.fetch_attempts == 0


def test_tunnel_down_exits_without_opening_a_database_session(capsys):
    args = argparse.Namespace(host=HOST, all_hosts=False, limit=1, pace=0.0, dry_run=False)

    result = cmd_browser_fetch(
        args,
        tunnel_check=lambda: False,
        session_factory=lambda: (_ for _ in ()).throw(AssertionError("database was touched")),
    )

    assert result == 0
    assert "tunnel down" in capsys.readouterr().out


def test_dry_run_does_not_check_tunnel_and_uses_only_mocked_command_dependencies(monkeypatch):
    args = argparse.Namespace(
        host=HOST, all_hosts=False, limit=1, pace=0.0, max_seconds=1500.0, dry_run=True
    )
    calls: list[str] = []

    class FakeDb:
        def commit(self):
            calls.append("commit")

        def rollback(self):
            calls.append("rollback")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(
        browser_fetch,
        "run_browser_fetch",
        lambda *_args, **_kwargs: browser_fetch.BrowserFetchSummary(skipped=1),
    )

    assert cmd_browser_fetch(
        args,
        tunnel_check=lambda: (_ for _ in ()).throw(AssertionError("tunnel was checked")),
        session_factory=FakeDb,
        rawstore_factory=object,
    ) == 0
    assert calls == ["commit", "close"]


def test_dry_run_prints_selected_document_details(db_session, tmp_path, capsys):
    """R3-8: `--dry-run`'s help text and docs promise the queue's contents
    ("show matching documents without fetching"), not just a count."""
    document = _document(db_session)
    document.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    db_session.flush()

    summary = browser_fetch.run_browser_fetch(
        db_session,
        hosts=(HOST,),
        limit=10,
        pace=0,
        dry_run=True,
        rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
        fetch_via_browser=lambda _url: (_ for _ in ()).throw(AssertionError("dry run fetched")),
    )

    assert summary.skipped == 1
    out = capsys.readouterr().out
    assert str(document.id) in out
    assert document.url in out
    assert HOST in out


def test_limit_parser_rejects_non_positive_values():
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["browser-fetch", "--host", HOST, "--limit", "0"])
    assert excinfo.value.code == 2


@pytest.mark.parametrize("bad_pace", ["-1", "nan", "inf", "-inf"])
def test_pace_parser_rejects_negative_and_non_finite_values(bad_pace):
    """R3-9: a negative/non-finite --pace used to reach
    run_browser_fetch's own ValueError/sleep() and surface as a raw
    traceback via cmd_browser_fetch's generic except, instead of a clean
    argparse-level rejection (like --limit/--max-seconds already get)."""
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["browser-fetch", "--host", HOST, "--pace", bad_pace])
    assert excinfo.value.code == 2


def test_pace_parser_accepts_zero_and_positive_values():
    args = build_parser().parse_args(["browser-fetch", "--host", HOST, "--pace", "0"])
    assert args.pace == 0.0


def test_tunnel_is_up_reports_down_for_any_exception_from_get():
    """R3-9: `tunnel_is_up` used to catch only `httpx.HTTPError`, but it
    runs at cmd_browser_fetch's call site OUTSIDE that function's own
    try/except -- any other exception type from an injected `get` callable
    (or a future httpx internal change) would otherwise escape uncaught."""

    def get(*_args, **_kwargs):
        raise RuntimeError("dns resolution blew up, not an httpx.HTTPError")

    assert browser_fetch.tunnel_is_up(get=get) is False


def test_browser_transient_error_does_not_charge_or_remove_candidate(db_session, tmp_path):
    document = _document(db_session)
    document.fetch_attempts = 0
    document.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    db_session.flush()
    db_session.commit()

    summary = browser_fetch.run_browser_fetch(
        db_session,
        hosts=(HOST,),
        limit=1,
        pace=0,
        dry_run=False,
        rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
        fetch_via_browser=lambda _url: (_ for _ in ()).throw(
            browser_fetch.BrowserTransientError("evaluate timed out")
        ),
    )

    assert summary.fetched == 1
    assert summary.errors == 1
    assert document.fetch_attempts == 0
    # R3-4: a single transient failure is tracked (below the backoff
    # threshold) but keeps the document's real status/reselectability
    # unchanged -- only repeated CONSECUTIVE failures defer reselection.
    assert document.license_note == (
        f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED} browser_transient_count=1"
    )
    assert [row.id for row in browser_fetch.select_browser_documents(db_session, HOST, limit=1)] == [document.id]


def test_charge_fetch_error_treats_missing_attempt_count_as_zero():
    document = SimpleNamespace(fetch_attempts=None, license_note=None)

    browser_fetch._charge_fetch_error(document)

    assert document.fetch_attempts == 1
    assert document.license_note == f"fulltext_status={fulltext.STATUS_FETCH_ERROR}"


def test_tunnel_loss_stops_run_without_charging_document(db_session, tmp_path):
    first = _document(db_session)
    first.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    second = _document(db_session, url=f"https://{HOST}/bills/second.pdf")
    second.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    db_session.flush()

    summary = browser_fetch.run_browser_fetch(
        db_session,
        hosts=(HOST,),
        limit=2,
        pace=0,
        dry_run=False,
        rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
        fetch_via_browser=lambda _url: (_ for _ in ()).throw(browser_fetch.BrowserTunnelLost("gone")),
    )

    assert summary.tunnel_lost is True
    assert summary.fetched == 1
    assert first.fetch_attempts in (None, 0)
    assert second.fetch_attempts in (None, 0)


def test_tunnel_loss_does_not_sleep_before_exit(db_session, tmp_path):
    """R3-5: `attempted` is set True before `fetch_via_browser` is called;
    on BrowserTunnelLost the `finally` on that SAME iteration used to still
    run the pacing sleep before the loop actually broke, holding the DB
    session open pointlessly after tunnel loss is already known. Needs a
    SECOND eligible document so the `index < len(documents) - 1` guard
    doesn't itself skip the sleep for an unrelated reason (last-item case)."""
    first = _document(db_session, url=f"https://{HOST}/bills/first.pdf")
    first.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    second = _document(db_session, url=f"https://{HOST}/bills/second.pdf")
    second.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    db_session.flush()
    sleeps: list[float] = []

    browser_fetch.run_browser_fetch(
        db_session,
        hosts=(HOST,),
        limit=2,
        pace=5,
        dry_run=False,
        rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
        fetch_via_browser=lambda _url: (_ for _ in ()).throw(browser_fetch.BrowserTunnelLost("gone")),
        sleep=sleeps.append,
    )

    assert sleeps == []


def test_repeated_transient_failure_does_not_block_progress_on_other_documents(
    db_session, tmp_path, monkeypatch
):
    """R3-4: two eligible docs, the first always throws BrowserTransientError
    -- the second must still be attempted within one run (a static,
    once-selected document list already guarantees this structurally), AND
    across repeated runs the first document must eventually stop consuming
    a queue slot once its consecutive-failure backoff kicks in."""
    first = _document(db_session, url=f"https://{HOST}/bills/first.pdf")
    first.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    second = _document(db_session, url=f"https://{HOST}/bills/second.pdf")
    second.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    db_session.flush()
    db_session.commit()

    def fetch_via_browser(url: str):
        if "first.pdf" in url:
            raise browser_fetch.BrowserTransientError("evaluate timed out")
        return (200, "text/plain", b"ok")

    monkeypatch.setattr(
        fulltext,
        "extract_document_text",
        lambda _content_type, raw: fulltext.ExtractionOutcome(
            status=fulltext.STATUS_OK, extracted_text=raw.decode(), checksum=hashlib.sha256(raw).hexdigest()
        ),
    )

    summary = browser_fetch.run_browser_fetch(
        db_session,
        hosts=(HOST,),
        limit=2,
        pace=0,
        dry_run=False,
        rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
        fetch_via_browser=fetch_via_browser,
    )
    assert summary.fetched == 2
    assert summary.errors == 1
    assert summary.ok == 1
    assert second.extracted_text == "ok"

    # Run again BROWSER_TRANSIENT_BACKOFF_AFTER - 1 more times: still
    # consecutively transient-failing, still selectable each time.
    for _ in range(browser_fetch.BROWSER_TRANSIENT_BACKOFF_AFTER - 1):
        browser_fetch.run_browser_fetch(
            db_session,
            hosts=(HOST,),
            limit=1,
            pace=0,
            dry_run=False,
            rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
            fetch_via_browser=lambda _url: (_ for _ in ()).throw(
                browser_fetch.BrowserTransientError("evaluate timed out")
            ),
        )

    assert first.fetch_attempts in (None, 0)
    assert first.license_note.startswith(f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}")
    # Threshold reached: the document is now deferred and stops consuming a
    # queue slot, freeing it up for genuinely new candidates.
    assert first.id not in [
        row.id for row in browser_fetch.select_browser_documents(db_session, HOST, limit=10)
    ]


def test_browser_evaluate_uses_abort_controller_and_document_timeout():
    # `Page.evaluate`'s real signature (playwright==1.61.0) is
    # `(self, expression, arg=None)` -- no `timeout` kwarg. This fake mirrors
    # that exactly so a `timeout=` kwarg regression fails this test
    # immediately instead of being silently accepted by a too-permissive
    # double (R3-2: the real API raised TypeError on every call).
    class FakePage:
        def evaluate(self, expression, arg=None):
            assert "AbortController" in expression
            assert "controller.abort()" in expression
            assert arg == {"url": "https://capitol.tn.gov/bill.pdf", "timeoutMs": 60000}
            return {"status": 200, "contentType": "text/plain", "base64": "b2s="}

    fetcher = browser_fetch._HostBrowserFetcher(HOST)
    fetcher._page = FakePage()
    assert fetcher.fetch("https://capitol.tn.gov/bill.pdf") == (200, "text/plain", b"ok")


def test_each_document_is_committed_before_pacing_sleep(db_session, tmp_path, monkeypatch):
    first = _document(db_session)
    second = _document(db_session, url=f"https://{HOST}/bills/second.pdf")
    for document in (first, second):
        document.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    db_session.flush()
    commits: list[int] = []
    original_commit = db_session.commit

    def commit():
        commits.append(1)
        original_commit()

    monkeypatch.setattr(db_session, "commit", commit)
    monkeypatch.setattr(
        fulltext,
        "extract_document_text",
        lambda _content_type, raw: fulltext.ExtractionOutcome(
            status=fulltext.STATUS_OK, extracted_text=raw.decode(), checksum=hashlib.sha256(raw).hexdigest()
        ),
    )
    sleeps: list[float] = []
    browser_fetch.run_browser_fetch(
        db_session,
        hosts=(HOST,),
        limit=2,
        pace=1,
        dry_run=False,
        rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
        fetch_via_browser=lambda _url: (200, "text/plain", b"text"),
        sleep=sleeps.append,
        uniform=lambda _low, _high: 1,
    )

    assert len(commits) == 2
    assert sleeps == [1]


def test_all_hosts_skips_landing_page_failure_and_keeps_other_host(monkeypatch):
    failed_host, live_host = browser_fetch.ALLOWLIST[:2]
    closed: list[str] = []

    class FakeHostFetcher:
        def __init__(self, host):
            self.host = host

        def __enter__(self):
            if self.host == failed_host:
                raise RuntimeError("landing page failed")
            return lambda _url: (200, "text/plain", b"ok")

        def __exit__(self, *_args):
            closed.append(self.host)

    monkeypatch.setattr(browser_fetch, "_HostBrowserFetcher", FakeHostFetcher)
    with browser_fetch.connected_browser_fetcher((failed_host, live_host)) as fetch:
        assert fetch.available_hosts == (live_host,)
        assert fetch(f"https://{live_host}/bill.pdf")[0] == 200
    assert closed == [live_host]


def test_missing_playwright_install_is_reported_distinctly(monkeypatch):
    """R3-6: a missing/broken Playwright install must fail loudly and
    distinctly, not fold into the generic per-host skip path -- which would
    make `available_hosts` empty and produce a summary
    (fetched=0 ok=0 ... errors=0) indistinguishable from a genuinely empty
    queue or a tunnel that's simply down."""

    class FakeHostFetcher:
        def __init__(self, host):
            self.host = host

        def __enter__(self):
            raise ImportError("No module named 'playwright'")

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(browser_fetch, "_HostBrowserFetcher", FakeHostFetcher)
    with pytest.raises(RuntimeError, match="Playwright is not installed"):
        with browser_fetch.connected_browser_fetcher((HOST,)):
            pass


def test_all_host_limit_is_split_and_selected_round_robin(db_session):
    first_host, second_host = browser_fetch.ALLOWLIST[:2]
    first = _document(db_session, url=f"https://{first_host}/bills/one.pdf")
    second = _document(db_session, url=f"https://{second_host}/bills/two.pdf")
    third = _document(db_session, url=f"https://{first_host}/bills/three.pdf")
    for document in (first, second, third):
        document.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    db_session.flush()

    selected = browser_fetch._round_robin_documents(
        db_session, (first_host, second_host), limit=3
    )

    assert [urlparse(document.url).hostname for document in selected] == [
        first_host,
        second_host,
        first_host,
    ]


def test_http_document_is_fetched_over_https_with_browser_provenance(db_session, tmp_path, monkeypatch):
    document = _document(db_session, url=f"http://{HOST}/bills/legacy.pdf")
    document.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    db_session.flush()
    seen: list[str] = []
    monkeypatch.setattr(
        fulltext,
        "extract_document_text",
        lambda _content_type, raw: fulltext.ExtractionOutcome(
            status=fulltext.STATUS_OK, extracted_text="legacy text", checksum=hashlib.sha256(raw).hexdigest()
        ),
    )

    browser_fetch.run_browser_fetch(
        db_session,
        hosts=(HOST,),
        limit=1,
        pace=0,
        dry_run=False,
        rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
        fetch_via_browser=lambda url: (seen.append(url) or (200, "application/pdf", b"pdf")),
    )

    assert seen == [f"https://{HOST}/bills/legacy.pdf"]
    assert document.url == f"http://{HOST}/bills/legacy.pdf"
    assert document.license_note == f"fulltext_status={fulltext.STATUS_OK_BROWSER} via=browser;scheme=https"


def test_partial_pdf_keeps_browser_provenance(db_session, tmp_path, monkeypatch):
    document = _document(db_session)
    document.license_note = f"fulltext_status={fulltext.STATUS_PERMANENTLY_FAILED}"
    db_session.flush()
    monkeypatch.setattr(
        fulltext,
        "extract_document_text",
        lambda _content_type, raw: fulltext.ExtractionOutcome(
            status=fulltext.STATUS_OK_PARTIAL_PDF,
            extracted_text="partial text",
            checksum=hashlib.sha256(raw).hexdigest(),
        ),
    )

    browser_fetch.run_browser_fetch(
        db_session,
        hosts=(HOST,),
        limit=1,
        pace=0,
        dry_run=False,
        rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
        fetch_via_browser=lambda _url: (200, "application/pdf", b"pdf"),
    )

    assert document.license_note == f"fulltext_status={fulltext.STATUS_OK_PARTIAL_PDF} via=browser"


def test_permanently_failed_document_is_deferred_for_seven_days_and_restamped(db_session, tmp_path):
    document = _document(db_session)
    recent = datetime.now(timezone.utc).isoformat()
    document.fetch_attempts = fulltext.MAX_FETCH_ATTEMPTS
    document.license_note = f"fulltext_status={fulltext.STATUS_PERMANENTLY_FAILED} browser_attempted_at={recent}"
    db_session.flush()
    assert browser_fetch.select_browser_documents(db_session, HOST, limit=1) == []

    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    document.license_note = f"fulltext_status={fulltext.STATUS_PERMANENTLY_FAILED} browser_attempted_at={old}"
    db_session.flush()
    browser_fetch.run_browser_fetch(
        db_session,
        hosts=(HOST,),
        limit=1,
        pace=0,
        dry_run=False,
        rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
        fetch_via_browser=lambda _url: (503, "text/plain", b"down"),
    )
    assert document.license_note.startswith(
        f"fulltext_status={fulltext.STATUS_PERMANENTLY_FAILED} browser_attempted_at="
    )
    assert document.license_note != f"fulltext_status={fulltext.STATUS_PERMANENTLY_FAILED} browser_attempted_at={old}"


def test_wall_clock_cap_stops_before_the_next_document(db_session, tmp_path):
    document = _document(db_session)
    document.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    db_session.flush()
    clock = iter((0.0, 1.0, 1.0))

    summary = browser_fetch.run_browser_fetch(
        db_session,
        hosts=(HOST,),
        limit=1,
        pace=0,
        dry_run=False,
        rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
        fetch_via_browser=lambda _url: (_ for _ in ()).throw(AssertionError("fetch should not run")),
        max_seconds=1,
        monotonic=lambda: next(clock),
    )

    assert summary.wall_clock_capped is True
    assert summary.fetched == 0


def test_allowlist_rejects_other_hosts(db_session):
    with pytest.raises(ValueError, match="not allowlisted"):
        browser_fetch.select_browser_documents(db_session, "example.com", limit=1)


def test_ok_browser_document_is_never_reenqueued(db_session, tmp_path, monkeypatch):
    """R3-1: `run_browser_fetch` always decorates a success note with
    `via=...` (never the bare `fulltext_status=ok_browser`) -- this uses the
    ACTUAL note it produces, not a hand-set undecorated one, so it would
    catch the exact-match regression the fixlist flagged."""
    document = _document(db_session)
    document.license_note = f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}"
    db_session.flush()
    monkeypatch.setattr(
        fulltext,
        "extract_document_text",
        lambda _content_type, raw: fulltext.ExtractionOutcome(
            status=fulltext.STATUS_OK, extracted_text=raw.decode(), checksum=hashlib.sha256(raw).hexdigest()
        ),
    )

    browser_fetch.run_browser_fetch(
        db_session,
        hosts=(HOST,),
        limit=1,
        pace=0,
        dry_run=False,
        rawstore=FilesystemRawStore(root=tmp_path / "rawstore"),
        fetch_via_browser=lambda _url: (200, "text/plain", b"text"),
    )

    assert document.license_note == f"fulltext_status={fulltext.STATUS_OK_BROWSER} via=browser"
    # extracted_text was written by the fetch above, so the plain "already
    # has text" clause alone would also exclude it -- clear it to isolate
    # the terminal-note recognition this test is actually pinning.
    document.extracted_text = None
    db_session.flush()

    assert fulltext.enqueue_fulltext_jobs(db_session, document_ids=[document.id]) == 0
