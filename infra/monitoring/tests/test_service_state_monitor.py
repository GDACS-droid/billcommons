from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "service_state_monitor", ROOT / "infra/monitoring/service_state_monitor.py"
)
assert SPEC and SPEC.loader
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


def test_expected_services_include_the_dedicated_scout_worker():
    assert "scout-worker" in monitor.EXPECTED_SERVICES
    assert len(monitor.EXPECTED_SERVICES) == len(set(monitor.EXPECTED_SERVICES))


@pytest.mark.parametrize(
    ("deployments", "expected_detail"),
    [
        ([], "no deployments found"),
        (
            [{"status": "CRASHED", "createdAt": "2026-09-01T00:00:00Z", "id": "scout-dead"}],
            "latest deployment is CRASHED",
        ),
    ],
)
def test_scout_worker_missing_or_failing_deployment_is_an_outage(monkeypatch, deployments, expected_detail):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=monitor.json.dumps(deployments), stderr="")

    monkeypatch.setattr(monitor.subprocess, "run", fake_run)
    ok, stale, detail = monitor.probe_service(
        "scout-worker", datetime(2026, 9, 1, tzinfo=timezone.utc)
    )

    assert (ok, stale) == (False, False)
    assert expected_detail in detail
    assert calls[0][0][5:7] == ["-s", "scout-worker"]


def test_service_state_systemd_unit_runs_the_tracked_monitor():
    service = (ROOT / "infra/systemd/com.gdacs.billcommons-services.service").read_text()
    timer = (ROOT / "infra/systemd/com.gdacs.billcommons-services.timer").read_text()
    assert "service_state_monitor.py" in service
    assert "SuccessExitStatus=0 1" in service
    assert "OnUnitActiveSec=15min" in timer
