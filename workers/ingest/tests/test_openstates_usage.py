from __future__ import annotations

import json

import httpx
import pytest

from billcommons_ingest import openstates_api
from billcommons_ingest.openstates_usage import observe_openstates_requests


class NoWait:
    def acquire(self, _url):
        pass


def test_usage_counts_retries_and_denials_across_clients_without_recording_request_data(monkeypatch, capsys):
    monkeypatch.setattr(openstates_api.time, "sleep", lambda _seconds: None)
    responses = [500, 429, "timeout", 200, 200]
    reservations = []

    def handle(request):
        result = responses.pop(0)
        if result == "timeout":
            raise httpx.ReadTimeout("fixture transport detail", request=request)
        return httpx.Response(result, json={"results": []})

    def client():
        return openstates_api.OpenStatesClient(
            client=httpx.Client(transport=httpx.MockTransport(handle), base_url="https://example.test"),
            api_key="fixture-secret-do-not-log", rate_limiter=NoWait(),
            consume_budget=lambda: reservations.append(1),
        )

    with pytest.raises(RuntimeError, match="rolled back"):
        with observe_openstates_requests(cycle_id="cycle", phase="api_sync", state="AK"):
            first = client()
            first.search_bills(identifier="fixture-query-do-not-log")
            client().get_jurisdiction("fixture-path-do-not-log")

            def deny():
                raise openstates_api.OpenStatesDailyBudgetExceeded("fixture quota detail")

            first.consume_budget = deny
            with pytest.raises(openstates_api.OpenStatesDailyBudgetExceeded):
                first.search_bills(page=2)
            raise RuntimeError("rolled back")

    raw = capsys.readouterr().out
    record = json.loads(raw)
    assert record["counts"] == {
        "logical_bills_requests": 2, "logical_jurisdictions_requests": 1,
        "attempted_requests": 5, "retry_attempts": 3,
        "http_5xx": 1, "http_4xx": 1, "http_2xx": 2,
        "transport_errors": 1, "budget_denials": 1,
    }
    assert record["phase_returned"] is False
    assert record["cycle_id"] == "cycle" and record["state"] == "AK"
    assert len(reservations) == 5 and not responses
    for private in ("fixture-secret", "fixture-query", "fixture-path", "fixture quota", "fixture transport"):
        assert private not in raw

    # Exception unwinding must restore the context; later calls are not
    # attributed to a failed phase, and the next phase starts empty.
    with observe_openstates_requests(cycle_id="next", phase="session_dates"):
        pass
    assert json.loads(capsys.readouterr().out)["counts"] == {}


def test_unavailable_budget_does_not_count_as_an_http_attempt(capsys):
    def unavailable():
        raise openstates_api.OpenStatesBudgetUnavailable("unavailable")

    def forbidden(_request):
        pytest.fail("an unavailable budget must prevent HTTP")

    client = openstates_api.OpenStatesClient(
        client=httpx.Client(transport=httpx.MockTransport(forbidden)),
        api_key="fixture", rate_limiter=NoWait(), consume_budget=unavailable,
    )
    with pytest.raises(openstates_api.OpenStatesBudgetUnavailable):
        with observe_openstates_requests(cycle_id="cycle", phase="session_dates"):
            client.get_jurisdiction("fixture")
    assert json.loads(capsys.readouterr().out)["counts"] == {
        "logical_jurisdictions_requests": 1, "budget_unavailable": 1,
    }
