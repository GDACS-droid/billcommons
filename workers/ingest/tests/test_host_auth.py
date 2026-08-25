"""Mock-transport coverage for per-host full-text authorization."""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from billcommons_ingest import host_auth as host_auth_mod
from billcommons_ingest.fulltext import FullTextFetcher, process_fetch_text_job
from billcommons_schema.models import Bill, BillDocument, BillVersion, Jurisdiction, Session as SessionModel


class _DisallowRobots:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def can_fetch(self, url: str) -> bool:
        self.calls.append(url)
        return False


class _AllowRobots(_DisallowRobots):
    def can_fetch(self, url: str) -> bool:
        self.calls.append(url)
        return True


def _auth_from_env(monkeypatch, config: dict):
    monkeypatch.setenv("BILLCOMMONS_HOST_AUTH_JSON", json.dumps(config))
    return host_auth_mod.HostAuth.from_environment()


def _make_document(db_session, url: str) -> BillDocument:
    jurisdiction = Jurisdiction(
        name="Host auth test state", abbreviation=f"ZQ_HA_{uuid.uuid4().hex[:8].upper()}", classification="state"
    )
    db_session.add(jurisdiction)
    db_session.flush()
    session_row = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026 Session", active=True)
    db_session.add(session_row)
    db_session.flush()
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 1",
        identifier_norm="HB 1",
        title="Host auth test bill",
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


def test_configured_host_gets_headers_and_skips_robots_with_auditable_status(
    db_session, rawstore, monkeypatch
):
    auth = _auth_from_env(
        monkeypatch,
        {
            "lims.dccouncil.gov": {
                "headers": {"Authorization": "${TEST_DC_LIMS_TOKEN}"},
                "robots_exempt": True,
            }
        },
    )
    monkeypatch.setenv("TEST_DC_LIMS_TOKEN", "mock-dc-token")
    # Rebuild after the token environment variable is present.
    auth = host_auth_mod.HostAuth.from_environment()
    seen_headers: list[dict[str, str]] = []

    def handler(request):
        seen_headers.append(dict(request.headers))
        assert request.url.path != "/robots.txt", "authorized host must skip RobotsCache"
        return httpx.Response(200, text="official bill text", headers={"content-type": "text/plain"})

    document = _make_document(db_session, "https://lims.dccouncil.gov/downloads/bill.txt")
    fetcher = FullTextFetcher(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        robots_cache=_DisallowRobots(),
        host_auth=auth,
        rate_per_sec=1000.0,
    )

    process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert seen_headers[0]["authorization"] == "mock-dc-token"
    assert fetcher.last_fetch_robots_exempt is True
    assert document.license_note == "fulltext_status=ok robots=api_token_exempt"


def test_unknown_host_has_no_auth_headers_and_still_obeys_robots(monkeypatch):
    auth = _auth_from_env(
        monkeypatch,
        {"lims.dccouncil.gov": {"headers": {"Authorization": "configured"}, "robots_exempt": True}},
    )
    robots = _AllowRobots()
    seen_headers: list[dict[str, str]] = []

    def handler(request):
        seen_headers.append(dict(request.headers))
        return httpx.Response(200, text="public bill text")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = FullTextFetcher(client=client, robots_cache=robots, host_auth=auth, rate_per_sec=1000.0)

    response = fetcher.fetch("https://unknown.example/bill.pdf")

    assert response.status_code == 200
    assert robots.calls == ["https://unknown.example/bill.pdf"]
    assert auth.headers_for("https://unknown.example/bill.pdf") == {}
    assert auth.robots_exempt("https://unknown.example/bill.pdf") is False
    assert "authorization" not in seen_headers[0]


def test_missing_config_is_a_noop_and_log_output_never_contains_token(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("BILLCOMMONS_HOST_AUTH_JSON", raising=False)
    monkeypatch.setenv("BILLCOMMONS_HOST_AUTH_FILE", str(tmp_path / "missing-host-auth.json"))
    caplog.set_level("INFO")

    missing = host_auth_mod.HostAuth.from_environment()

    assert missing.headers_for("https://lims.dccouncil.gov/api/v2") == {}
    assert missing.robots_exempt("https://lims.dccouncil.gov/api/v2") is False
    assert "mock-dc-token" not in caplog.text


def test_redirect_to_a_non_exempt_host_does_not_mislabel_that_hosts_failure_as_exempt(
    db_session, rawstore, monkeypatch
):
    """R1 fixlist #3: `last_fetch_robots_exempt` must reflect the CURRENT
    hop, not stick True from an earlier exempt hop in the same redirect
    chain. Hop 1 (the exempt host) redirects to hop 2 (a different,
    non-exempt host) which then fails with a non-terminal status; the
    resulting license_note must NOT carry `robots=api_token_exempt` -- that
    audit trail belongs to the host whose exemption actually applied, not
    to whichever host happened to be fetched first.
    """
    auth = _auth_from_env(
        monkeypatch,
        {"lims.dccouncil.gov": {"headers": {"Authorization": "configured"}, "robots_exempt": True}},
    )

    def handler(request):
        if request.url.host == "lims.dccouncil.gov":
            return httpx.Response(302, headers={"location": "https://other-host.example/bill.pdf"})
        return httpx.Response(500, text="upstream error")

    document = _make_document(db_session, "https://lims.dccouncil.gov/downloads/bill.pdf")
    fetcher = FullTextFetcher(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        robots_cache=_AllowRobots(),
        host_auth=auth,
        rate_per_sec=1000.0,
    )

    from billcommons_ingest.fulltext import DocumentFetchError

    with pytest.raises(DocumentFetchError):
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert document.license_note is not None
    assert "robots=api_token_exempt" not in document.license_note


def test_http_url_for_a_configured_host_never_gets_auth_headers(monkeypatch):
    """R1 fixlist #5: `headers_for`/`_hostname` match on hostname only, with
    no scheme check -- an http:// URL (or an https->http downgrade redirect)
    for a configured host must not receive the same Authorization/x-api-key
    headers as https."""
    auth = _auth_from_env(
        monkeypatch,
        {"lims.dccouncil.gov": {"headers": {"Authorization": "configured"}, "robots_exempt": True}},
    )
    seen_headers: list[dict[str, str]] = []

    def handler(request):
        seen_headers.append(dict(request.headers))
        return httpx.Response(200, text="plain http response")

    fetcher = FullTextFetcher(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        robots_cache=_AllowRobots(),
        host_auth=auth,
        rate_per_sec=1000.0,
    )

    response = fetcher.fetch("http://lims.dccouncil.gov/downloads/bill.pdf")

    assert response.status_code == 200
    assert "authorization" not in seen_headers[0]


def test_http_url_for_a_configured_host_is_never_robots_exempt(monkeypatch):
    """R2 fixlist #1: the robots exemption is hostname-only with no scheme
    check, unlike header attachment (which correctly requires https). An
    http:// URL (or an https->http downgrade redirect) for a configured host
    must still be subject to the NORMAL robots.txt check -- it gets neither
    credentials nor the exemption."""
    auth = _auth_from_env(
        monkeypatch,
        {"lims.dccouncil.gov": {"headers": {"Authorization": "configured"}, "robots_exempt": True}},
    )

    assert auth.robots_exempt("https://lims.dccouncil.gov/downloads/bill.pdf") is True
    assert auth.robots_exempt("http://lims.dccouncil.gov/downloads/bill.pdf") is False

    robots = _DisallowRobots()
    fetcher = FullTextFetcher(
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
        robots_cache=robots,
        host_auth=auth,
        rate_per_sec=1000.0,
    )

    from billcommons_ingest.fulltext import UnfetchableDocument

    with pytest.raises(UnfetchableDocument):
        fetcher.fetch("http://lims.dccouncil.gov/downloads/bill.pdf")

    assert fetcher.last_fetch_robots_exempt is False
    assert robots.calls == ["http://lims.dccouncil.gov/downloads/bill.pdf"]


def test_https_to_http_downgrade_hop_loses_robots_exemption_mid_chain(db_session, rawstore, monkeypatch):
    """Same gap, exercised via a redirect hop: an exempt https first hop
    that redirects to a plain http:// URL on the SAME configured host must
    have the second hop's robots.txt actually checked, not skipped just
    because the hostname still matches."""
    auth = _auth_from_env(
        monkeypatch,
        {"lims.dccouncil.gov": {"headers": {"Authorization": "configured"}, "robots_exempt": True}},
    )

    def handler(request):
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"location": "http://lims.dccouncil.gov/downloads/bill.pdf"})
        return httpx.Response(200, text="should never be reached")

    document = _make_document(db_session, "https://lims.dccouncil.gov/downloads/redirect.pdf")
    robots = _DisallowRobots()
    fetcher = FullTextFetcher(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        robots_cache=robots,
        host_auth=auth,
        rate_per_sec=1000.0,
    )

    from billcommons_ingest.fulltext import UnfetchableDocument

    with pytest.raises(UnfetchableDocument):
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert fetcher.last_fetch_robots_exempt is False
    assert "http://lims.dccouncil.gov/downloads/bill.pdf" in robots.calls


def test_empty_env_var_disables_a_prefixed_header_template_and_the_exemption(monkeypatch, caplog):
    """R3 fixlist #1: an empty (not just missing) env var must not survive
    inside a prefixed template like "Bearer ${VAR}" -- the whole host entry
    must be treated as unconfigured: no headers, and no robots exemption."""
    monkeypatch.setenv("TEST_EMPTY_DC_LIMS_TOKEN", "")
    caplog.set_level("INFO")

    auth = _auth_from_env(
        monkeypatch,
        {
            "lims.dccouncil.gov": {
                "headers": {"Authorization": "Bearer ${TEST_EMPTY_DC_LIMS_TOKEN}"},
                "robots_exempt": True,
            }
        },
    )

    assert auth.headers_for("https://lims.dccouncil.gov/downloads/bill.txt") == {}
    assert auth.robots_exempt("https://lims.dccouncil.gov/downloads/bill.txt") is False
    assert "host auth for lims.dccouncil.gov skipped: unresolved token" in caplog.text


def test_empty_env_var_disables_a_prefixed_token_template(tmp_path, monkeypatch):
    """Same gap via the {token} substitution path with a literal prefix
    ("iga-api-client-{token}") when the token file resolves to an empty
    value for the configured key."""
    config_dir = tmp_path / ".config" / "billcommons"
    config_dir.mkdir(parents=True)
    token_path = config_dir / "empty-token.json"
    token_path.write_text('{"api_token": ""}', encoding="utf-8")
    monkeypatch.setattr(host_auth_mod.Path, "home", staticmethod(lambda: tmp_path))

    auth = _auth_from_env(
        monkeypatch,
        {
            "api.iga.in.gov": {
                "headers": {"User-Agent": "iga-api-client-{token}"},
                "token_file": str(token_path),
                "token_key": "api_token",
                "robots_exempt": True,
            }
        },
    )

    assert auth.headers_for("https://api.iga.in.gov/v1/bills") == {}
    assert auth.robots_exempt("https://api.iga.in.gov/v1/bills") is False


def test_empty_host_auth_json_env_var_falls_through_to_the_file(tmp_path, monkeypatch):
    """R3 fixlist #2: BILLCOMMONS_HOST_AUTH_JSON present-but-empty (the
    standard artifact of a cleared Railway var) must fall through to
    BILLCOMMONS_HOST_AUTH_FILE, not silently resolve to {}."""
    config_path = tmp_path / "host-auth.json"
    config_path.write_text(
        json.dumps({"lims.dccouncil.gov": {"headers": {"Authorization": "configured"}, "robots_exempt": True}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BILLCOMMONS_HOST_AUTH_JSON", "   ")
    monkeypatch.setenv("BILLCOMMONS_HOST_AUTH_FILE", str(config_path))

    auth = host_auth_mod.HostAuth.from_environment()

    assert auth.headers_for("https://lims.dccouncil.gov/downloads/bill.txt") == {
        "Authorization": "configured"
    }
    assert auth.robots_exempt("https://lims.dccouncil.gov/downloads/bill.txt") is True


def test_token_file_template_and_logs_expose_only_hostname(tmp_path, monkeypatch, caplog):
    config_dir = tmp_path / ".config" / "billcommons"
    config_dir.mkdir(parents=True)
    token_path = config_dir / "test-token.json"
    token_path.write_text('{"api_token": "token-must-not-appear-in-logs"}', encoding="utf-8")
    monkeypatch.setattr(host_auth_mod.Path, "home", staticmethod(lambda: tmp_path))
    caplog.set_level("INFO")

    auth = _auth_from_env(
        monkeypatch,
        {
            "API.IGA.IN.GOV": {
                "headers": {"x-api-key": "{token}", "User-Agent": "iga-api-client-{token}"},
                "token_file": str(token_path),
                "token_key": "api_token",
                "robots_exempt": True,
            }
        },
    )

    assert auth.headers_for("https://api.iga.in.gov/v1/bills") == {
        "x-api-key": "token-must-not-appear-in-logs",
        "User-Agent": "iga-api-client-token-must-not-appear-in-logs",
    }
    assert auth.robots_exempt("https://api.iga.in.gov/v1/bills") is True
    assert "api.iga.in.gov" in caplog.text
    assert "token-must-not-appear-in-logs" not in caplog.text
