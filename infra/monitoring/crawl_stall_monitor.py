#!/usr/bin/env python3
"""Alert when the full-text crawl has persistently stopped producing text.

The healthcheck is read-only. This wrapper adds conservative notification
policy: two consecutive bad ten-minute checks establish an incident, a
productive observation with a newer text timestamp resolves it, and reminders
are spaced from the most recent alert attempt. Waiting for an upstream
assignment is neither a stall nor a recovery.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
PYTHON = REPO / ".venv/bin/python"
STATE_FILE = Path.home() / ".local/state/billcommons/stall-monitor.json"
VTS_CONFIG = Path.home() / ".config/voice-to-ship/config.json"
RENOTIFY_HOURS = 6
VALID_STATUSES = frozenset(
    {"waiting_upstream", "producing", "idle_or_backoff", "running", "stalled"}
)
HEALTH_COUNT_FIELDS = (
    "texted_last_hour",
    "claimable_now",
    "queued_total",
    "dead_total",
    "awaiting_upstream",
    "actionable_queued",
    "running_total",
    "stale_running",
)


def telegram(message: str) -> bool:
    """Best-effort Telegram alert. It deliberately never exposes config data."""
    try:
        cfg = json.loads(VTS_CONFIG.read_text())
        body = json.dumps({"chat_id": cfg["allowed_user_id"], "text": message}).encode()
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage",
                data=body,
                headers={"Content-Type": "application/json"},
            ),
            timeout=15,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"telegram failed: {type(exc).__name__}", file=sys.stderr)
        return False


def load_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict[str, Any]) -> None:
    """Atomically replace state so an interrupted timer cannot corrupt it."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=STATE_FILE.parent, prefix=f".{STATE_FILE.name}.", delete=False
    ) as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = handle.name
    os.replace(temporary, STATE_FILE)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def _state_count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _normalise_state(state: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Migrate the old ``last_status/since`` file without losing an incident."""
    result = dict(state)
    if result.get("last_status") == "check-failed":
        result["last_status"] = "check_failed"
        if not _parse_time(result.get("check_failed_attempt_at")):
            result["check_failed_attempt_at"] = (
                result.get("last_attempt_at")
                or result.get("since")
                or result.get("last_checked")
                or _iso(now)
            )
    if result.get("last_status") == "stalled" and not result.get("incident_since"):
        since = result.get("since") if _parse_time(result.get("since")) else _iso(now)
        result["incident_since"] = since
        # Old versions sent the initial alert before writing this state, so
        # its incident start is the best available last-attempt value.
        result["last_attempt_at"] = result.get("last_alert_at") or since
        result["bad_checks"] = max(_state_count(result.get("bad_checks")), 2)
    return result


def _health_status(health: dict[str, Any]) -> str:
    return health["status"]


def _queue_monitor_restored(
    state: dict[str, Any], actions: list[str], *, now: datetime
) -> None:
    """Retry a monitoring-restored notice only while its yellow alert is open."""
    if not state.get("check_failed_alerted"):
        return
    last_attempt = _parse_time(state.get("monitor_restore_attempt_at"))
    if last_attempt is None or now - last_attempt >= timedelta(hours=RENOTIFY_HOURS):
        state["monitor_restore_attempt_at"] = _iso(now)
        actions.append("monitor_restored")


def validate_health_payload(value: object) -> dict[str, Any]:
    """Reject incomplete or contradictory health JSON as a check failure."""
    if not isinstance(value, dict):
        raise ValueError("healthcheck JSON was not an object")
    required = {
        "healthy",
        "status",
        "reason",
        "checked_at",
        "last_text_at",
        "minutes_since_text",
        "backlog_remains",
        *HEALTH_COUNT_FIELDS,
    }
    if not required.issubset(value):
        raise ValueError("healthcheck JSON was incomplete")
    if type(value["healthy"]) is not bool or type(value["backlog_remains"]) is not bool:
        raise ValueError("healthcheck boolean fields were invalid")
    status = value["status"]
    if status not in VALID_STATUSES or value["healthy"] != (status != "stalled"):
        raise ValueError("healthcheck status and healthy fields disagreed")
    if not isinstance(value["reason"], str) or not value["reason"]:
        raise ValueError("healthcheck reason was invalid")
    if _parse_time(value["checked_at"]) is None:
        raise ValueError("healthcheck checked_at was invalid")
    last_text = value["last_text_at"]
    if last_text is not None and _parse_time(last_text) is None:
        raise ValueError("healthcheck last_text_at was invalid")
    minutes = value["minutes_since_text"]
    if minutes is not None and (
        type(minutes) not in (int, float) or not math.isfinite(minutes) or minutes < 0
    ):
        raise ValueError("healthcheck minutes_since_text was invalid")
    if (last_text is None) != (minutes is None):
        raise ValueError("healthcheck last_text_at and minutes_since_text disagreed")
    for field in HEALTH_COUNT_FIELDS:
        if type(value[field]) is not int or value[field] < 0:
            raise ValueError(f"healthcheck {field} was invalid")
    if status == "producing" and value["texted_last_hour"] == 0:
        raise ValueError("healthcheck producing state had no new text")
    return value


def evaluate(
    health: dict[str, Any], state: dict[str, Any] | None, *, now: datetime
) -> tuple[dict[str, Any], list[str]]:
    """Purely decide state transitions and notification actions.

    Actions are attempted delivery. A recovery remains pending until delivery
    succeeds, so a failed green message cannot silently close the incident.
    """
    state = _normalise_state(state or {}, now)
    result = dict(state)
    actions: list[str] = []
    now_text = _iso(now)
    status = _health_status(health)
    incident_since = _parse_time(result.get("incident_since"))

    if status == "stalled":
        # A new bad sample supersedes an undelivered recovery. Never send a
        # stale green after the crawl has become red again.
        result.pop("pending_recovery_at", None)
        result.pop("recovery_attempt_at", None)
        result.pop("recovery_observed_at", None)
        result.pop("recovery_reason", None)
        if incident_since is None:
            if result.get("last_status") == "stalled_pending":
                bad_checks = _state_count(result.get("bad_checks")) + 1
                pending_since = result.get("pending_since") or now_text
                pending_text = result.get("pending_last_text_at")
            else:
                bad_checks = 1
                pending_since = now_text
                pending_text = health.get("last_text_at")
                # Notification timestamps belong to a prior, resolved
                # incident and must not be mistaken for this one.
                result.pop("last_attempt_at", None)
                result.pop("last_alert_at", None)
            result.update(
                {
                    "last_status": "stalled_pending",
                    "bad_checks": bad_checks,
                    "pending_since": pending_since,
                    "pending_last_text_at": pending_text,
                    "last_checked": now_text,
                }
            )
            if bad_checks >= 2:
                result.update(
                    {
                        "last_status": "stalled",
                        "incident_since": pending_since,
                        "incident_last_text_at": pending_text,
                        "last_attempt_at": now_text,
                        "bad_checks": bad_checks,
                    }
                )
                result.pop("pending_since", None)
                result.pop("pending_last_text_at", None)
                actions.append("stalled")
        else:
            result.update({"last_status": "stalled", "bad_checks": 2, "last_checked": now_text})
            last_attempt = _parse_time(result.get("last_attempt_at"))
            if last_attempt is None or now - last_attempt >= timedelta(hours=RENOTIFY_HOURS):
                result["last_attempt_at"] = now_text
                actions.append("reminder")
        _queue_monitor_restored(result, actions, now=now)
        return result, actions

    # A non-stalled observation breaks a pending two-check sequence. It does
    # not close a confirmed incident: waiting/backoff is not proof of output.
    result.pop("pending_since", None)
    result.pop("pending_last_text_at", None)
    result["bad_checks"] = 0
    result["last_status"] = status
    result["last_checked"] = now_text

    if incident_since is not None:
        if status == "producing":
            current_text = _parse_time(health.get("last_text_at"))
            incident_text = _parse_time(result.get("incident_last_text_at"))
            # The legacy fallback is the incident start, never an arbitrary
            # existing text timestamp. That prevents a historical text row
            # from converting red -> green when the monitor state is migrated.
            baseline = incident_text or incident_since
            if current_text is not None and current_text > baseline:
                result.setdefault("pending_recovery_at", now_text)
                result.setdefault("recovery_observed_at", health.get("last_text_at"))
                result.setdefault("recovery_reason", health.get("reason"))

        # Once evidence exists, delivery can be retried after a bounded delay
        # on any later non-stalled valid health sample. The source may have
        # returned to upstream waiting before Telegram recovers.
        if result.get("pending_recovery_at"):
            recovery_attempt = _parse_time(result.get("recovery_attempt_at"))
            if recovery_attempt is None or now - recovery_attempt >= timedelta(hours=RENOTIFY_HOURS):
                result["recovery_attempt_at"] = now_text
                actions.append("recovered")
    _queue_monitor_restored(result, actions, now=now)
    return result, actions


def evaluate_check_failed(
    state: dict[str, Any] | None, *, now: datetime
) -> tuple[dict[str, Any], list[str]]:
    """Record a failed healthcheck without resolving any open crawl incident."""
    result = _normalise_state(state or {}, now)
    actions: list[str] = []
    last_attempt = _parse_time(result.get("check_failed_attempt_at"))
    if last_attempt is None or now - last_attempt >= timedelta(hours=RENOTIFY_HOURS):
        result["check_failed_attempt_at"] = _iso(now)
        actions.append("check_failed")
    result["last_status"] = "check_failed"
    result["last_checked"] = _iso(now)
    return result, actions


def acknowledge_deliveries(
    state: dict[str, Any], delivered: dict[str, bool], *, now: datetime
) -> dict[str, Any]:
    """Apply successful notification delivery without treating attempts as success."""
    result = dict(state)
    if any(delivered.get(action) for action in ("stalled", "reminder")):
        result["last_alert_at"] = _iso(now)
    if delivered.get("recovered"):
        for key in (
            "incident_since",
            "incident_last_text_at",
            "last_attempt_at",
            "last_alert_at",
            "since",
            "pending_recovery_at",
            "recovery_attempt_at",
            "recovery_observed_at",
            "recovery_reason",
        ):
            result.pop(key, None)
    if delivered.get("check_failed"):
        result["check_failed_alerted"] = True
        # A fresh yellow episode must not inherit a completed restoration's
        # retry window.
        result.pop("monitor_restore_attempt_at", None)
    if delivered.get("monitor_restored"):
        result.pop("check_failed_alerted", None)
        # A new failure after this successful restoration is a new episode,
        # so it should alert promptly instead of waiting six more hours.
        result.pop("check_failed_attempt_at", None)
    return result


def _stall_message(health: dict[str, Any], *, reminder: bool) -> str:
    heading = (
        "🔴 Bill Commons: FULL-TEXT CRAWL STILL STALLED"
        if reminder
        else "🔴 Bill Commons: FULL-TEXT CRAWL STALLED"
    )
    return (
        f"{heading}\n\n{health.get('reason', 'no reason supplied')}\n\n"
        f"last text: {health.get('last_text_at')} ({health.get('minutes_since_text')} min ago)\n"
        f"claimable now: {int(health.get('claimable_now') or 0):,}\n"
        f"queued/dead: {int(health.get('queued_total') or 0):,}/{int(health.get('dead_total') or 0):,}\n\n"
        "Worker reporting Online is not evidence it is working -- check extracted_text, not uptime."
    )


def _dispatch(
    actions: list[str], health: dict[str, Any], *, dry_run: bool, state: dict[str, Any] | None = None
) -> dict[str, bool]:
    delivered: dict[str, bool] = {}
    for action in actions:
        if action in {"stalled", "reminder"}:
            message = _stall_message(health, reminder=action == "reminder")
        elif action == "recovered":
            observed_at = (state or {}).get("recovery_observed_at", health.get("last_text_at"))
            observed_reason = (state or {}).get("recovery_reason", health.get("reason"))
            message = (
                "✅ Bill Commons: crawl recovery observed\n\n"
                f"New extracted text was observed at {observed_at}.\n"
                f"Healthcheck evidence: {observed_reason or 'new text landed'}."
            )
        elif action == "check_failed":
            message = "⚠️ Bill Commons: crawl healthcheck FAILED to run\n\nInspect monitor and healthcheck logs."
        else:
            message = (
                "🟡 Bill Commons: healthcheck monitoring restored\n\n"
                "A valid health sample was received. This does not claim crawl recovery."
            )
        if dry_run:
            print(f"[dry-run {action}] {message}")
            delivered[action] = False
        else:
            delivered[action] = telegram(message)
    return delivered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bill Commons crawl-stall monitor")
    parser.add_argument("--dry-run", action="store_true", help="evaluate but do not send or write state")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    state = load_state()
    try:
        proc = subprocess.run(
            [str(PYTHON), "-m", "billcommons_ingest.healthcheck", "--json"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode not in (0, 1):
            raise RuntimeError("healthcheck returned an invalid exit code")
        health = validate_health_payload(json.loads(proc.stdout))
    except Exception as exc:  # noqa: BLE001
        next_state, actions = evaluate_check_failed(state, now=now)
        delivered = _dispatch(actions, {}, dry_run=args.dry_run, state=next_state)
        next_state = acknowledge_deliveries(next_state, delivered, now=now)
        if not args.dry_run:
            save_state(next_state)
        print(f"[check-failed] {type(exc).__name__}", file=sys.stderr)
        return 2

    next_state, actions = evaluate(health, state, now=now)
    delivered = _dispatch(actions, health, dry_run=args.dry_run, state=next_state)
    next_state = acknowledge_deliveries(next_state, delivered, now=now)
    if not args.dry_run:
        save_state(next_state)
    status = _health_status(health)
    print(f"[{status}] {health.get('reason', '')}")
    return 0 if health.get("healthy", status != "stalled") else 1


if __name__ == "__main__":
    raise SystemExit(main())
