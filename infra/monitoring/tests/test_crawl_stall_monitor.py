"""Focused notification-policy tests with no subprocess, state-file, or network calls."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


MODULE = Path(__file__).parents[1] / "crawl_stall_monitor.py"
SPEC = importlib.util.spec_from_file_location("crawl_stall_monitor_test", MODULE)
assert SPEC and SPEC.loader
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def _health(status: str, *, last_text_at: datetime | None = None) -> dict:
    return {
        "healthy": status != "stalled",
        "status": status,
        "reason": status,
        "last_text_at": last_text_at.isoformat() if last_text_at else None,
        "minutes_since_text": 90,
        "texted_last_hour": 1 if status == "producing" else 0,
        "claimable_now": 5,
        "queued_total": 5,
        "dead_total": 0,
        "actionable_queued": 0,
    }


def _open_incident() -> dict:
    state, actions = monitor.evaluate(_health("stalled", last_text_at=NOW - timedelta(hours=2)), {}, now=NOW)
    assert actions == []
    state, actions = monitor.evaluate(
        _health("stalled", last_text_at=NOW - timedelta(hours=2)),
        state,
        now=NOW + timedelta(minutes=10),
    )
    assert actions == ["stalled"]
    return state


def test_requires_two_consecutive_bad_checks_before_first_red():
    first, actions = monitor.evaluate(_health("stalled"), {}, now=NOW)
    assert actions == []
    assert first["last_status"] == "stalled_pending"

    second, actions = monitor.evaluate(_health("stalled"), first, now=NOW + timedelta(minutes=10))
    assert actions == ["stalled"]
    assert second["last_status"] == "stalled"
    assert second["incident_since"] == NOW.isoformat()


def test_waiting_upstream_does_not_send_recovery_and_productive_new_text_does_once():
    state = _open_incident()
    waiting, actions = monitor.evaluate(
        _health("waiting_upstream", last_text_at=NOW - timedelta(hours=2)),
        state,
        now=NOW + timedelta(minutes=20),
    )
    assert actions == []
    assert waiting["incident_since"] == state["incident_since"]

    unchanged, actions = monitor.evaluate(
        _health("producing", last_text_at=NOW - timedelta(hours=2)),
        waiting,
        now=NOW + timedelta(minutes=30),
    )
    assert actions == []
    assert "incident_since" in unchanged

    recovered, actions = monitor.evaluate(
        _health("producing", last_text_at=NOW + timedelta(minutes=1)),
        unchanged,
        now=NOW + timedelta(minutes=40),
    )
    assert actions == ["recovered"]
    assert "incident_since" in recovered
    recovered = monitor.acknowledge_deliveries(recovered, {"recovered": True}, now=NOW)
    assert "incident_since" not in recovered

    again, actions = monitor.evaluate(
        _health("producing", last_text_at=NOW + timedelta(minutes=2)),
        recovered,
        now=NOW + timedelta(minutes=50),
    )
    assert actions == []
    assert again["last_status"] == "producing"


def test_six_hour_reminders_are_spaced_from_last_attempt_not_incident_start():
    state = _open_incident()
    at_six, actions = monitor.evaluate(
        _health("stalled"), state, now=NOW + timedelta(hours=6, minutes=10)
    )
    assert actions == ["reminder"]

    shortly_after, actions = monitor.evaluate(
        _health("stalled"), at_six, now=NOW + timedelta(hours=6, minutes=20)
    )
    assert actions == []

    at_twelve, actions = monitor.evaluate(
        _health("stalled"), shortly_after, now=NOW + timedelta(hours=12, minutes=10)
    )
    assert actions == ["reminder"]
    assert at_twelve["last_attempt_at"] == (NOW + timedelta(hours=12, minutes=10)).isoformat()


def test_legacy_state_preserves_existing_incident_and_check_failure_does_not_clear_it():
    legacy = {"last_status": "stalled", "since": (NOW - timedelta(hours=2)).isoformat()}
    waiting, actions = monitor.evaluate(_health("waiting_upstream"), legacy, now=NOW)
    assert actions == []
    assert waiting["incident_since"] == legacy["since"]
    assert waiting["last_attempt_at"] == legacy["since"]

    failed, actions = monitor.evaluate_check_failed(waiting, now=NOW + timedelta(minutes=10))
    assert actions == ["check_failed"]
    assert failed["incident_since"] == legacy["since"]
    repeated, actions = monitor.evaluate_check_failed(failed, now=NOW + timedelta(minutes=20))
    assert actions == []
    assert repeated["incident_since"] == legacy["since"]


def test_legacy_incident_needs_text_newer_than_its_start_to_recover():
    legacy = {"last_status": "stalled", "since": NOW.isoformat()}
    unchanged, actions = monitor.evaluate(
        _health("producing", last_text_at=NOW), legacy, now=NOW + timedelta(minutes=10)
    )
    assert actions == []
    assert "incident_since" in unchanged

    recovered, actions = monitor.evaluate(
        _health("producing", last_text_at=NOW + timedelta(seconds=1)),
        unchanged,
        now=NOW + timedelta(minutes=20),
    )
    assert actions == ["recovered"]
    recovered = monitor.acknowledge_deliveries(recovered, {"recovered": True}, now=NOW)
    assert "incident_since" not in recovered


def test_send_failure_does_not_raise_or_reopen_a_notification_loop(monkeypatch):
    state = _open_incident()
    assert "last_attempt_at" in state
    sent: list[str] = []
    monkeypatch.setattr(monitor, "telegram", lambda message: sent.append(message) and False)
    monitor._dispatch(["stalled"], _health("stalled"), dry_run=False)
    assert len(sent) == 1
    unchanged, actions = monitor.evaluate(
        _health("stalled"), state, now=NOW + timedelta(minutes=20)
    )
    assert actions == []
    assert unchanged["last_attempt_at"] == state["last_attempt_at"]
    assert "last_alert_at" not in unchanged


def test_failed_recovery_retries_from_waiting_with_its_original_evidence(monkeypatch):
    state = _open_incident()
    pending, actions = monitor.evaluate(
        _health("producing", last_text_at=NOW + timedelta(minutes=1)),
        state,
        now=NOW + timedelta(minutes=20),
    )
    assert actions == ["recovered"]
    pending = monitor.acknowledge_deliveries(pending, {"recovered": False}, now=NOW)
    assert "incident_since" in pending
    assert "pending_recovery_at" in pending

    soon, actions = monitor.evaluate(
        _health("waiting_upstream", last_text_at=NOW + timedelta(minutes=1)),
        pending,
        now=NOW + timedelta(minutes=30),
    )
    assert actions == []

    retry, actions = monitor.evaluate(
        _health("waiting_upstream", last_text_at=NOW + timedelta(minutes=1)),
        soon,
        now=NOW + timedelta(hours=6, minutes=20),
    )
    assert actions == ["recovered"]
    sent: list[str] = []
    monkeypatch.setattr(monitor, "telegram", lambda message: sent.append(message) and True)
    monitor._dispatch(actions, _health("waiting_upstream"), dry_run=False, state=retry)
    assert "recovery observed" in sent[0]
    assert (NOW + timedelta(minutes=1)).isoformat() in sent[0]
    acknowledged = monitor.acknowledge_deliveries(retry, {"recovered": True}, now=NOW)
    assert "incident_since" not in acknowledged


def test_new_stall_cancels_an_undelivered_recovery():
    state = _open_incident()
    pending, actions = monitor.evaluate(
        _health("producing", last_text_at=NOW + timedelta(minutes=1)),
        state,
        now=NOW + timedelta(minutes=20),
    )
    assert actions == ["recovered"]
    red_again, actions = monitor.evaluate(
        _health("stalled", last_text_at=NOW - timedelta(hours=2)),
        pending,
        now=NOW + timedelta(minutes=30),
    )
    assert actions == []
    assert "pending_recovery_at" not in red_again
    assert "recovery_observed_at" not in red_again
    assert "recovery_reason" not in red_again
    assert "incident_since" in red_again

    newer, actions = monitor.evaluate(
        _health("producing", last_text_at=NOW + timedelta(minutes=2)),
        red_again,
        now=NOW + timedelta(minutes=40),
    )
    assert actions == ["recovered"]
    assert newer["recovery_observed_at"] == (NOW + timedelta(minutes=2)).isoformat()
    assert newer["recovery_reason"] == "producing"


def test_hyphenated_legacy_check_failure_migrates_and_restoration_is_not_a_crawl_recovery(monkeypatch):
    legacy, actions = monitor.evaluate_check_failed({"last_status": "check-failed"}, now=NOW)
    assert actions == []
    assert legacy["last_status"] == "check_failed"

    failed, actions = monitor.evaluate_check_failed({}, now=NOW)
    assert actions == ["check_failed"]
    failed = monitor.acknowledge_deliveries(failed, {"check_failed": True}, now=NOW)
    restored, actions = monitor.evaluate(
        _health("idle_or_backoff", last_text_at=NOW - timedelta(hours=2)),
        failed,
        now=NOW + timedelta(minutes=10),
    )
    assert actions == ["monitor_restored"]
    sent: list[str] = []
    monkeypatch.setattr(monitor, "telegram", lambda message: sent.append(message) and True)
    monitor._dispatch(actions, _health("idle_or_backoff"), dry_run=False)
    assert "does not claim crawl recovery" in sent[0]
    restored = monitor.acknowledge_deliveries(restored, {"monitor_restored": True}, now=NOW)
    assert "monitor_restore_attempt_at" in restored
    assert "check_failed_attempt_at" not in restored

    flapping, actions = monitor.evaluate_check_failed(restored, now=NOW + timedelta(minutes=20))
    assert actions == ["check_failed"]
    assert flapping["last_status"] == "check_failed"
    flapping = monitor.acknowledge_deliveries(flapping, {"check_failed": True}, now=NOW)
    assert "monitor_restore_attempt_at" not in flapping

    next_restoration, actions = monitor.evaluate(
        _health("idle_or_backoff", last_text_at=NOW - timedelta(hours=2)),
        flapping,
        now=NOW + timedelta(minutes=30),
    )
    assert actions == ["monitor_restored"]
    assert "monitor_restore_attempt_at" in next_restoration


def test_failed_check_failure_and_restoration_messages_retry_on_their_own_clocks():
    failed, actions = monitor.evaluate_check_failed({}, now=NOW)
    assert actions == ["check_failed"]
    failed = monitor.acknowledge_deliveries(failed, {"check_failed": False}, now=NOW)

    soon, actions = monitor.evaluate_check_failed(failed, now=NOW + timedelta(minutes=10))
    assert actions == []
    retry, actions = monitor.evaluate_check_failed(soon, now=NOW + timedelta(hours=6))
    assert actions == ["check_failed"]
    retry = monitor.acknowledge_deliveries(retry, {"check_failed": True}, now=NOW)

    pending_restore, actions = monitor.evaluate(
        _health("idle_or_backoff", last_text_at=NOW - timedelta(hours=2)),
        retry,
        now=NOW + timedelta(hours=6, minutes=10),
    )
    assert actions == ["monitor_restored"]
    pending_restore = monitor.acknowledge_deliveries(
        pending_restore, {"monitor_restored": False}, now=NOW
    )
    soon, actions = monitor.evaluate(
        _health("idle_or_backoff", last_text_at=NOW - timedelta(hours=2)),
        pending_restore,
        now=NOW + timedelta(hours=6, minutes=20),
    )
    assert actions == []
    retried_restore, actions = monitor.evaluate(
        _health("idle_or_backoff", last_text_at=NOW - timedelta(hours=2)),
        soon,
        now=NOW + timedelta(hours=12, minutes=10),
    )
    assert actions == ["monitor_restored"]
    acknowledged = monitor.acknowledge_deliveries(
        retried_restore, {"monitor_restored": True}, now=NOW
    )
    assert "monitor_restore_attempt_at" in acknowledged
    assert "check_failed_attempt_at" not in acknowledged
    assert "check_failed_alerted" not in acknowledged


def test_health_payload_validation_rejects_incomplete_and_inconsistent_data():
    valid = _health("producing", last_text_at=NOW)
    valid.update(
        {
            "checked_at": NOW.isoformat(),
            "backlog_remains": False,
            "awaiting_upstream": 0,
            "running_total": 0,
            "stale_running": 0,
        }
    )
    assert monitor.validate_health_payload(valid) is valid

    for invalid in (
        {},
        {**valid, "healthy": False},
        {**valid, "status": "unknown"},
        {**valid, "claimable_now": "5"},
        {**valid, "minutes_since_text": float("nan")},
        {**valid, "texted_last_hour": 0},
    ):
        try:
            monitor.validate_health_payload(invalid)
        except ValueError:
            pass
        else:  # pragma: no cover - assertion is clearer than pytest.raises in this table
            raise AssertionError(f"invalid payload accepted: {invalid}")


def test_main_treats_bad_json_and_unexpected_exit_codes_as_check_failed(monkeypatch):
    saved: list[dict] = []
    monkeypatch.setattr(monitor, "load_state", lambda: {})
    monkeypatch.setattr(monitor, "save_state", saved.append)
    monkeypatch.setattr(monitor, "telegram", lambda message: False)
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="{}"),
    )
    assert monitor.main([]) == 2
    assert saved[-1]["last_status"] == "check_failed"

    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=3, stdout="not-used"),
    )
    assert monitor.main([]) == 2


def test_main_records_last_alert_only_after_successful_stall_notification(monkeypatch):
    health = _health("stalled", last_text_at=NOW - timedelta(hours=2))
    health.update(
        {
            "checked_at": NOW.isoformat(),
            "backlog_remains": True,
            "awaiting_upstream": 0,
            "running_total": 0,
            "stale_running": 0,
        }
    )
    legacy = {"last_status": "stalled", "since": (NOW - timedelta(hours=8)).isoformat()}
    saved: list[dict] = []
    monkeypatch.setattr(monitor, "load_state", lambda: legacy)
    monkeypatch.setattr(monitor, "save_state", saved.append)
    monkeypatch.setattr(monitor, "telegram", lambda message: True)
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=json.dumps(health)),
    )
    assert monitor.main([]) == 1
    assert "last_attempt_at" in saved[-1]
    assert "last_alert_at" in saved[-1]

    saved.clear()
    monkeypatch.setattr(monitor, "telegram", lambda message: False)
    assert monitor.main([]) == 1
    assert "last_attempt_at" in saved[-1]
    assert "last_alert_at" not in saved[-1]
