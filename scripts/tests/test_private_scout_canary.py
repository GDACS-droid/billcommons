from __future__ import annotations

import io
import json
import sys
import uuid
from argparse import Namespace
from pathlib import Path
from urllib.error import HTTPError

import pytest

# ``scripts`` is intentionally not an importable production package.  Add its
# parent explicitly so this test runs from the repository root as documented.
scripts_directory = str(Path(__file__).resolve().parents[1])
if scripts_directory not in sys.path:
    sys.path.insert(0, scripts_directory)

import private_scout_canary as canary


def _args(**overrides):
    values = {
        "ack_production_canary": True,
        "email": "operator@example.test",
        "query": "HB 625",
        "api_base": "https://api.example.test",
        "origin": "https://billcommons.org",
        "jurisdiction": "FL",
        "poll_interval_seconds": 0.1,
        "poll_timeout_seconds": 10.0,
    }
    values.update(overrides)
    return Namespace(**values)


@pytest.fixture(autouse=True)
def private_cohort(monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_SCOUT_CANARY_EMAILS", "operator@example.test")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ENABLED", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ALLOW_PUBLIC", "0")


def _config(**overrides):
    return canary._config_from_args(_args(**overrides))


def _job(job_id: uuid.UUID, status: str, **extra):
    return {
        "id": str(job_id),
        "status": status,
        "partial_success": status == "partial",
        "finding_count": 2,
        "usage": {"external_requests": 1, "browser_sessions": 0},
        **extra,
    }


class _Client:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def request(self, method, path, *, body=None):
        self.calls.append((method, path, body))
        return next(self.replies)


def _runner(config, client, out):
    return canary.run_canary(
        config,
        ensure_customer=lambda email: uuid.UUID("00000000-0000-0000-0000-000000000011"),
        sign_session=lambda customer_id: "signed-session-secret",
        client_factory=lambda session: client,
        out=out,
        clock=iter((0.0, 0.0, 0.1)).__next__,
        sleep=lambda seconds: None,
    )


def test_config_requires_explicit_acknowledgement_before_any_external_work():
    with pytest.raises(canary.CanaryError, match="ack-production-canary"):
        canary._config_from_args(_args(ack_production_canary=False))


def test_config_never_permits_a_non_allowlisted_identity():
    with pytest.raises(canary.CanaryError, match="not in BILLCOMMONS_SCOUT_CANARY_EMAILS"):
        _config(email="outsider@example.test")


def test_config_uses_the_existing_normalized_email_identity():
    assert _config(email=" Operator@Example.Test ").email == "operator@example.test"


def test_config_rejects_public_rollout_even_with_a_named_cohort(monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ALLOW_PUBLIC", "true")
    with pytest.raises(canary.CanaryError, match="public Scout rollout"):
        _config()


def test_config_rejects_a_dark_scout_before_customer_or_api_work(monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ENABLED", "0")
    with pytest.raises(canary.CanaryError, match="BILLCOMMONS_SCOUT_ENABLED"):
        _config()


def test_config_requires_an_origin_accepted_by_the_existing_csrf_policy(monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_ALLOWED_ORIGINS", "https://allowed.example.test")
    with pytest.raises(canary.CanaryError, match="BILLCOMMONS_ALLOWED_ORIGINS"):
        _config()


@pytest.mark.parametrize("api_base", ["http://api.example.test", "https://user:pass@api.example.test", "https://api.example.test/path"])
def test_config_requires_a_clean_https_api_origin(api_base):
    with pytest.raises(canary.CanaryError, match="API base must be an HTTPS origin"):
        _config(api_base=api_base)


def test_completed_job_is_polled_then_identically_resubmitted_as_a_fresh_cache_hit():
    config = _config()
    job_id = uuid.uuid4()
    client = _Client(
        [
            (201, {"job": _job(job_id, "queued")}),
            (200, _job(job_id, "completed")),
            (200, {"coalesced": True, "cached": True, "job": _job(job_id, "completed")}),
        ]
    )
    out = io.StringIO()

    assert _runner(config, client, out) is True
    assert client.calls[0] == ("POST", "/api/v1/scout/jobs", {"query": "HB 625", "jurisdiction": "FL"})
    assert client.calls[2] == client.calls[0]
    report = json.loads(out.getvalue())
    assert report["canary"] == {"outcome": "cache_reuse_verified", "cache_reused": True}
    assert report["result"]["usage"] == {"browser_sessions": 0, "external_requests": 1}


def test_partial_job_is_reported_truthfully_and_may_reuse_a_fresh_cache():
    config = _config()
    job_id = uuid.uuid4()
    client = _Client(
        [
            (201, {"job": _job(job_id, "partial")}),
            (200, {"coalesced": True, "cached": True, "job": _job(job_id, "partial")}),
        ]
    )
    out = io.StringIO()

    assert _runner(config, client, out) is True
    report = json.loads(out.getvalue())
    assert report["result"]["observed_status"] == "partial"
    assert report["result"]["partial_success"] is True


def test_failed_job_is_reported_truthfully_without_creating_a_second_noncacheable_job():
    config = _config()
    job_id = uuid.uuid4()
    client = _Client([(201, {"job": _job(job_id, "failed", error_class="provider_failure")})])
    out = io.StringIO()

    assert _runner(config, client, out) is False
    assert len(client.calls) == 1
    assert json.loads(out.getvalue())["canary"]["outcome"] == "terminal_without_cache_reuse"


def test_api_failure_discards_the_mocked_response_body(monkeypatch):
    config = _config()
    leaked_response_text = "TOP-SECRET-API-ERROR-BODY"

    def failing_urlopen(request, timeout):
        raise HTTPError(request.full_url, 503, "unavailable", hdrs=None, fp=io.BytesIO(leaked_response_text.encode()))

    monkeypatch.setattr(canary, "urlopen", failing_urlopen)
    client = canary.UrllibApiClient(config, "signed-session-secret")
    with pytest.raises(canary.CanaryError, match="HTTP 503") as raised:
        client.request("POST", "/api/v1/scout/jobs", body={"query": "HB 625", "jurisdiction": "FL"})
    assert leaked_response_text not in str(raised.value)


def test_output_redacts_session_job_source_and_untrusted_response_body_content():
    config = _config()
    job_id = uuid.uuid4()
    source_body = "TOP-SECRET-SOURCE-BODY"
    client = _Client(
        [
            (201, {"job": _job(job_id, "completed", sources=[{"body": source_body}], cookie="server-cookie", api_key="bc_live_secret")}),
            (200, {"coalesced": True, "cached": True, "job": _job(job_id, "completed")}),
        ]
    )
    out = io.StringIO()

    assert _runner(config, client, out) is True
    rendered = out.getvalue()
    for forbidden in (source_body, "signed-session-secret", str(job_id), "server-cookie", "bc_live_secret", "HB 625"):
        assert forbidden not in rendered
