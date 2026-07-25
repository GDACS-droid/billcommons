#!/usr/bin/env python3
"""Bill Commons crawl-stall monitor: alerts to Telegram when the crawl dies.

Runs on the box from a systemd timer. Calls billcommons_ingest.healthcheck
and pings Telegram when the crawl has claimable work but has stopped
producing text.

Why this exists: the crawl has now gone silently dead twice while every
conventional signal reported healthy -- Railway `Online`, logs scrolling with
job claims, queue depth comfortable. On 2026-07-25 that cost ~2 hours and was
only noticed because a human asked for a status update. Uptime is not
liveness; extracted text is.

Alert policy is deliberately conservative:
  * Only alerts on a STATE CHANGE (healthy -> stalled, or recovery), so a
    long stall produces one alert rather than one every ten minutes. An alert
    stream that repeats gets muted, and a muted alert is worse than none.
  * Re-alerts if a stall persists past RENOTIFY_HOURS, so a muted-by-silence
    stall can't be forgotten either.
  * Never restarts or mutates anything. Remediation on a signal this coarse
    risks restart-looping a worker that is merely slow.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PYTHON = REPO / ".venv/bin/python"
STATE_FILE = Path.home() / ".local/state/billcommons/stall-monitor.json"
VTS_CONFIG = Path.home() / ".config/voice-to-ship/config.json"
RENOTIFY_HOURS = 6


def telegram(text: str) -> bool:
    """Best-effort Telegram alert (compliance_monitor.py pattern). Never raises."""
    try:
        cfg = json.loads(VTS_CONFIG.read_text())
        body = json.dumps({"chat_id": cfg["allowed_user_id"], "text": text}).encode()
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


def main() -> int:
    proc = subprocess.run(
        [str(PYTHON), "-m", "billcommons_ingest.healthcheck", "--json"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )

    # Exit 2 means the check itself failed (DB unreachable, etc). That is NOT
    # a healthy result and must not be swallowed -- it is its own alertable
    # condition, because a monitor that silently stops checking looks exactly
    # like a monitor reporting all-clear.
    if proc.returncode == 2:
        state = load_state()
        if state.get("last_status") != "check-failed":
            telegram(f"⚠️ Bill Commons: crawl healthcheck FAILED to run\n\n{proc.stderr[:600]}")
        save_state({"last_status": "check-failed", "since": datetime.now(timezone.utc).isoformat()})
        return 2

    try:
        health = json.loads(proc.stdout)
    except Exception:  # noqa: BLE001
        print(f"unparseable healthcheck output: {proc.stdout[:400]}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    state = load_state()
    previous = state.get("last_status")
    status = "healthy" if health["healthy"] else "stalled"

    should_alert = False
    if status == "stalled":
        if previous != "stalled":
            should_alert = True
        else:
            since = state.get("since")
            if since:
                try:
                    if now - datetime.fromisoformat(since) >= timedelta(hours=RENOTIFY_HOURS):
                        should_alert = True
                except ValueError:
                    should_alert = True
    elif previous in ("stalled", "check-failed"):
        should_alert = True  # recovery is worth exactly one message

    if should_alert:
        if status == "stalled":
            telegram(
                "🔴 Bill Commons: FULL-TEXT CRAWL STALLED\n\n"
                f"{health['reason']}\n\n"
                f"last text: {health['last_text_at']} ({health['minutes_since_text']} min ago)\n"
                f"claimable now: {health['claimable_now']:,}\n"
                f"queued/dead: {health['queued_total']:,}/{health['dead_total']:,}\n\n"
                "Worker reporting Online is not evidence it is working -- "
                "check extracted_text, not uptime."
            )
        else:
            telegram(
                "✅ Bill Commons: crawl recovered\n\n"
                f"{health['reason']}\n"
                f"claimable now: {health['claimable_now']:,}"
            )

    # Preserve the original stall start across repeated stalled checks so
    # RENOTIFY_HOURS measures the stall, not the last poll.
    since = state.get("since") if previous == status else now.isoformat()
    save_state({"last_status": status, "since": since, "last_checked": now.isoformat()})

    print(f"[{status}] {health['reason']}")
    return 0 if status == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
