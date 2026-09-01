#!/usr/bin/env python3
"""Bill Commons Railway service-state monitor: alerts when a service's latest
deployment is not actually running.

Runs on the box from a systemd timer, alongside read_path_monitor.py,
crawl_stall_monitor.py, and freshness_monitor.py.

Why this exists: on 2026-08-09 `sync-worker` was deleted from Railway and
the corpus silently froze for twelve days. Nothing on this box watched
Railway's own view of service state -- the closest thing, read_path_monitor,
only watches the API and MCP services (the ones with public URLs to hit);
it has no way to notice that a background worker with no HTTP surface has no
running deployment at all. This monitor closes that gap directly: for every
service Bill Commons depends on, is the LATEST deployment actually SUCCESS,
or is it FAILED / CRASHED / REMOVED / missing entirely.

Deliberately conservative about what counts as an outage vs a warning:
  * latest deployment status != SUCCESS (FAILED, CRASHED, REMOVED, or no
    deployment found at all) -- OUTAGE. A service cannot be doing its job
    with no live deployment.
  * latest deployment is SUCCESS but its own age is > 30 days -- WARNING
    only, printed and included in alerts, but does not itself flip a probe
    to failing. An old-but-working deployment is a staleness/drift signal,
    not a live incident -- treating it as one would train the alert to be
    ignored.
  * the `railway` CLI itself times out or errors -- OUTAGE. Per
    monitor-hardening's core law ("exit codes lie -- assert effects"), a
    monitor that cannot reach its data source must never read that as
    healthy; a CLI hang would otherwise silently degrade into "all quiet"
    exactly like the incident this is meant to catch.

Uses a STABLE linked directory (~/.local/share/billcommons/railway-link)
rather than the working repo, so this monitor's Railway CLI session survives
worktree churn and is never accidentally pointed at a project link meant for
something else.

Alert policy mirrors read_path_monitor.py / freshness_monitor.py exactly:
state-change-only with a RENOTIFY_HOURS re-alert, FAILURES_BEFORE_ALERT
consecutive bad runs before an outage alert fires. Never restarts, redeploys,
or otherwise mutates a Railway service -- this reports, it does not remediate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = Path.home() / ".local/state/billcommons/service-state-monitor.json"
VTS_CONFIG = Path.home() / ".config/voice-to-ship/config.json"
LINK_DIR = Path.home() / ".local/share/billcommons/railway-link"

RENOTIFY_HOURS = 6
FAILURES_BEFORE_ALERT = 2
# 45s measured against a cold `npx -y @railway/cli` invocation on this box
# (first-run npm resolution + API round trip); env-tunable so a forced-failure
# drill can prove the timeout path without waiting on a genuinely wedged CLI.
CLI_TIMEOUT_SECONDS = float(os.environ.get("BILLCOMMONS_SERVICE_CLI_TIMEOUT_SECONDS", "45") or 45)

# Measured on the box 2026-08-21: sync-worker was already FAILED and
# webhooks-worker already CRASHED at write time -- this list is every
# service Bill Commons runs, not just the ones currently healthy.
EXPECTED_SERVICES = [
    "api",
    "mcp",
    "worker",
    "scout-worker",
    "sync-worker",
    "validate-worker",
    "webhooks-worker",
]

STALE_DAYS = float(os.environ.get("BILLCOMMONS_SERVICE_STALE_DAYS", "30") or 30)


def telegram(text_: str) -> bool:
    """Best-effort Telegram alert (read_path_monitor.py pattern). Never raises."""
    import urllib.request

    try:
        cfg = json.loads(VTS_CONFIG.read_text())
        body = json.dumps({"chat_id": cfg["allowed_user_id"], "text": text_}).encode()
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
        print(f"telegram failed: {exc}", file=sys.stderr)
        return False


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def probe_service(service: str, now: datetime) -> tuple[bool, bool, str]:
    """Returns (ok, stale, detail). `ok=False` is an outage; `stale=True` is
    a non-outage warning that only applies when ok is True.

    CLI failures of every shape (timeout, nonzero exit, unparseable JSON,
    empty deployment list) are `ok=False` -- never silently treated as
    healthy, per monitor-hardening's core law.
    """
    try:
        proc = subprocess.run(
            [
                "npx", "-y", "@railway/cli", "deployment", "list",
                "-s", service, "--json", "--limit", "3",
            ],
            cwd=str(LINK_DIR),
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, False, f"railway CLI timed out after {CLI_TIMEOUT_SECONDS}s"
    except Exception as exc:  # noqa: BLE001
        return False, False, f"railway CLI failed to run: {type(exc).__name__}: {exc}"

    if proc.returncode != 0:
        return False, False, f"railway CLI exit {proc.returncode}: {proc.stderr.strip()[:200]}"

    try:
        deployments = json.loads(proc.stdout)
    except Exception as exc:  # noqa: BLE001
        return False, False, f"unparseable railway CLI output: {type(exc).__name__}: {exc}"

    if not deployments:
        return False, False, "no deployments found -- service has never been deployed or was removed"

    latest = deployments[0]
    status = latest.get("status", "UNKNOWN")
    created_raw = latest.get("createdAt")
    try:
        created = datetime.fromisoformat((created_raw or "").replace("Z", "+00:00"))
    except ValueError:
        created = None
    age_days = (now - created).total_seconds() / 86400.0 if created else None
    age_str = f"{age_days:.1f}d ago" if age_days is not None else "unknown age"

    if status != "SUCCESS":
        return (
            False,
            False,
            f"latest deployment is {status} ({age_str}, id={latest.get('id', '?')})",
        )

    stale = age_days is not None and age_days > STALE_DAYS
    detail = f"SUCCESS ({age_str})"
    if stale:
        detail += f" -- STALE: no successful deploy in > {STALE_DAYS:.0f}d"
    return True, stale, detail


def main() -> int:
    now = datetime.now(timezone.utc)
    results = [(svc, *probe_service(svc, now)) for svc in EXPECTED_SERVICES]

    failures = [(svc, detail) for svc, ok, _stale, detail in results if not ok]
    stale = [(svc, detail) for svc, ok, is_stale, detail in results if ok and is_stale]
    healthy = not failures

    state = load_state()
    previous = state.get("last_status")
    consecutive = 0 if healthy else state.get("consecutive_failures", 0) + 1

    status = "healthy" if healthy else "failing"
    confirmed_outage = consecutive >= FAILURES_BEFORE_ALERT

    should_alert = False
    if confirmed_outage:
        if previous != "outage":
            should_alert = True
        else:
            since = state.get("since")
            if since:
                try:
                    if now - datetime.fromisoformat(since) >= timedelta(hours=RENOTIFY_HOURS):
                        should_alert = True
                except ValueError:
                    should_alert = True
    elif healthy and previous == "outage":
        should_alert = True  # recovery is worth exactly one message

    # Staleness is its own, separate, lower-severity state-change: alert once
    # when the SET of stale services changes, independent of the outage
    # alert above (a service can be simultaneously healthy and stale).
    previous_stale = set(state.get("stale_services", []))
    current_stale = {svc for svc, _ in stale}
    stale_changed = current_stale != previous_stale and current_stale

    if should_alert:
        if confirmed_outage:
            detail = "\n".join(f"  ✗ {svc}: {why}" for svc, why in failures)
            telegram(
                "🔴 Bill Commons: RAILWAY SERVICE STATE FAILURE\n\n"
                f"{len(failures)} of {len(results)} services failing "
                f"({consecutive} consecutive runs)\n\n"
                f"{detail}\n\n"
                "A service reporting no live SUCCESS deployment cannot be doing "
                "its job -- Railway 'Online' on a stale build is not evidence "
                "of this either; this checks the DEPLOYMENT, not the process."
            )
        else:
            telegram(
                "✅ Bill Commons: Railway service state recovered\n\n"
                + "\n".join(f"  ✓ {svc}: {detail}" for svc, _ok, _stale, detail in results)
            )
    elif stale_changed:
        detail = "\n".join(f"  ⚠️ {svc}: {why}" for svc, why in stale)
        telegram(
            f"⚠️ Bill Commons: {len(current_stale)} service(s) have not shipped "
            f"a new successful deploy in > {STALE_DAYS:.0f}d\n\n{detail}"
        )

    effective = "outage" if confirmed_outage else status
    since = state.get("since") if previous == effective else now.isoformat()
    save_state({
        "last_status": effective,
        "consecutive_failures": consecutive,
        "since": since,
        "stale_services": sorted(current_stale),
        "last_checked": now.isoformat(),
    })

    for svc, ok, _stale, detail in results:
        print(f"  [{'ok' if ok else 'FAIL'}] {svc}: {detail}")
    print(f"[{effective}] {len(failures)}/{len(results)} failing")
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
