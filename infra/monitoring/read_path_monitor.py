#!/usr/bin/env python3
"""Bill Commons read-path monitor: alerts to Telegram when the API stops serving.

Runs on the box from a systemd timer, alongside crawl_stall_monitor.py.

Why this exists: on 2026-08-02 the API was down for an unknown number of hours
and nobody noticed. Every signal we had said healthy -- Railway reported all
services `Online`, and the crawl-stall monitor stayed green the entire time
because the *crawl* was genuinely fine. It was watching the write path. The
read path had no monitor at all, and billcommons.org kept returning HTTP 200
because Next.js served cached pages over a dead API, so the failure was
invisible from the front door too.

Three deliberate design choices, each from that incident:

1. `/health` is NOT sufficient on its own, and a status-code check of it is
   actively misleading. `routers/health.py` catches the pool TimeoutError and
   returns **HTTP 200** with `{"status": "degraded", "database": "error"}`.
   A monitor asserting `200` would have reported UP for the whole outage. We
   assert on the BODY.

2. `/health` is also rate-limit exempt, so it cannot see a 429 storm. The
   second probe goes through the normal limited path and touches real data.

3. The third probe is the public website, end-to-end, because the API can be
   healthy while Vercel or a poisoned Data Cache still serves a broken page.

Timeouts are short (10s). A 30s timeout would have shown "up" during much of
the outage, when requests were merely queueing behind an exhausted pool.

Alert policy mirrors the stall monitor: state-change-only so a long outage
produces one message rather than one per run, with a re-notify after
RENOTIFY_HOURS so a muted-by-silence outage cannot be forgotten. Requires
FAILURES_BEFORE_ALERT consecutive bad runs so one blip stays quiet.
Never restarts or mutates anything.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = Path.home() / ".local/state/billcommons/read-path-monitor.json"
VTS_CONFIG = Path.home() / ".config/voice-to-ship/config.json"

RENOTIFY_HOURS = 6
FAILURES_BEFORE_ALERT = 2
TIMEOUT_SECONDS = 10
SLOW_SECONDS = 5.0

UA = "billcommons-read-path-monitor/1.0"

# (label, url, required substring in body or None, allow_slow)
PROBES = [
    (
        "api /health",
        "https://api.billcommons.org/api/v1/health",
        # The endpoint returns 200 even when the database is unreachable.
        # This substring is the ONLY thing that distinguishes up from degraded.
        '"database":"ok"',
    ),
    (
        "api /bills (limited path, real read)",
        "https://api.billcommons.org/api/v1/bills?per_page=1",
        '"pagination"',
    ),
    (
        "web /states/NC (end-to-end)",
        "https://billcommons.org/states/NC",
        None,
    ),
]

# The MCP server is a SEPARATE Railway service with its own container and its
# own dependencies. It stayed healthy through the API outage on 2026-08-02 --
# and then went down on its own hours later, on an unpinned `mcp` release, while
# every probe above stayed green. Watching the API does not watch this.
#
# A real tool call, not just a handshake: `initialize` succeeds without touching
# the database, so it cannot see the failure mode that matters.
#
# That has a cost: at a 2-minute cadence this manufactures ~720 tool calls a
# day, and the usage telemetry shipped the same day exists precisely to answer
# "is anyone actually calling these tools?". Within hours the monitor was 61 of
# 63 recorded calls. PROBE_HEADER tags every one so /stats/usage can subtract
# them -- see billcommons_mcp.telemetry.PROBE_FAMILY. Do not remove it, or the
# adoption number silently becomes a measurement of this file.
MCP_URL = "https://mcp.billcommons.org/mcp"
PROBE_HEADER = "x-billcommons-probe"
MCP_TOOL_CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "get_jurisdiction_coverage", "arguments": {}},
}


def telegram(text: str) -> bool:
    """Best-effort Telegram alert (crawl_stall_monitor.py pattern). Never raises."""
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


def probe(label: str, url: str, must_contain: str | None) -> tuple[bool, str, float]:
    """Returns (ok, detail, elapsed_seconds). Never raises."""
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:
            body = res.read(65536).decode("utf-8", "replace")
            elapsed = time.monotonic() - started
            if res.status != 200:
                return False, f"HTTP {res.status}", elapsed
            if must_contain:
                # Compare with whitespace stripped: JSON separators vary
                # between serializers and a cosmetic change must not page us.
                haystack = "".join(body.split())
                needle = "".join(must_contain.split())
                if needle not in haystack:
                    return False, f"body missing {must_contain!r}", elapsed
            if elapsed > SLOW_SECONDS:
                return False, f"slow: {elapsed:.1f}s", elapsed
            return True, f"{elapsed:.2f}s", elapsed
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}", time.monotonic() - started
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:120]}", time.monotonic() - started


def probe_mcp() -> tuple[bool, str, float]:
    """Call a real MCP tool. Never raises."""
    started = time.monotonic()
    try:
        req = urllib.request.Request(
            MCP_URL,
            data=json.dumps(MCP_TOOL_CALL).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": UA,
                PROBE_HEADER: "read-path-monitor",
            },
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:
            body = res.read(65536).decode("utf-8", "replace")
            elapsed = time.monotonic() - started
            if res.status != 200:
                return False, f"HTTP {res.status}", elapsed
            # A structured MCP error still comes back as HTTP 200.
            if '"isError":true' in "".join(body.split()):
                return False, "tool returned isError", elapsed
            if "jurisdiction_count" not in body:
                return False, "tool response missing expected payload", elapsed
            if elapsed > SLOW_SECONDS:
                return False, f"slow: {elapsed:.1f}s", elapsed
            return True, f"{elapsed:.2f}s", elapsed
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}", time.monotonic() - started
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:120]}", time.monotonic() - started


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:
        return json.loads(res.read(1_000_000).decode("utf-8", "replace"))


def probe_filter_is_applied() -> tuple[bool, str, float]:
    """A query filter that is silently ignored looks exactly like a working one.

    FastAPI drops unknown query parameters without complaint, so a filter that
    was never deployed -- or was renamed, or removed -- answers HTTP 200 with a
    perfectly well-formed payload. On 2026-08-03 `?sponsor=rouson` returned all
    1,964 Florida bills instead of 45, and every status-code and body-shape
    check passed. The only thing that distinguishes applied from ignored is
    that an applied filter CHANGES THE RESULT.

    Two requests, one assertion: a deliberately impossible sponsor must return
    zero, while the same query without it returns some. If those ever agree,
    the filter is not running.
    """
    started = time.monotonic()
    try:
        base = _get_json("https://api.billcommons.org/api/v1/bills?jurisdiction=FL&per_page=1")
        filtered = _get_json(
            "https://api.billcommons.org/api/v1/bills"
            "?jurisdiction=FL&per_page=1&sponsor=zzzznotarealsponsorname"
        )
        elapsed = time.monotonic() - started
        base_total = base["pagination"]["total"]
        filtered_total = filtered["pagination"]["total"]
        if base_total == 0:
            return False, "baseline query returned nothing", elapsed
        if filtered_total != 0:
            return (
                False,
                f"sponsor filter ignored: impossible name matched {filtered_total} "
                f"of {base_total} bills",
                elapsed,
            )
        return True, f"{elapsed:.2f}s", elapsed
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:120]}", time.monotonic() - started


def probe_topic_hubs_resolve() -> tuple[bool, str, float]:
    """Every topic the API advertises must have a page that renders.

    These are two different systems: the API computes the topic list, and the
    website resolves a slug against a SEPARATELY cached copy of it. On
    2026-08-03 they disagreed -- /topics listed seven trackers while four of
    them 404'd, because Vercel's Data Cache persists across deployments and the
    slug lookup was reading a list that predated them. Neither side was down,
    and no probe of either side alone could see it.

    Checked from the list rather than a hardcoded set, so a topic added later
    is covered without editing this file.

    Throttled, unlike every other probe here. This one fetches a full page per
    topic, and at the monitor's 2-minute cadence that is ~5,800 extra website
    requests a day to detect a condition that can only change on a deploy. Once
    an hour is well inside the window where a broken hub still gets caught the
    same day it ships.
    """
    started = time.monotonic()
    try:
        topics = _get_json("https://api.billcommons.org/api/v1/topics")["data"]
        if not topics:
            return False, "API advertises no topics", time.monotonic() - started
        broken = []
        for topic in topics:
            slug = topic["slug"]
            url = f"https://billcommons.org/topics/{slug}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:
                    if res.status != 200:
                        broken.append(f"{slug}:{res.status}")
            except urllib.error.HTTPError as exc:
                broken.append(f"{slug}:{exc.code}")
        elapsed = time.monotonic() - started
        if broken:
            return False, f"topic hubs unreachable: {', '.join(broken)}", elapsed
        return True, f"{len(topics)} hubs, {elapsed:.2f}s", elapsed
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:120]}", time.monotonic() - started


def main() -> int:
    results = [(label, *probe(label, url, needle)) for label, url, needle in PROBES]
    results.append(("mcp tool call", *probe_mcp()))
    results.append(("bills sponsor filter applied", *probe_filter_is_applied()))
    # Hourly, not every run -- see probe_topic_hubs_resolve's docstring. Keyed
    # off the wall clock rather than a counter in the state file so a monitor
    # restart cannot reset it into running every time.
    if datetime.now(timezone.utc).minute < 2:
        results.append(("topic hubs resolve", *probe_topic_hubs_resolve()))
    failures = [(label, detail) for label, ok, detail, _ in results if not ok]
    healthy = not failures

    now = datetime.now(timezone.utc)
    state = load_state()
    previous = state.get("last_status")
    consecutive = 0 if healthy else state.get("consecutive_failures", 0) + 1

    status = "healthy" if healthy else "failing"
    # Only a sustained failure counts as an outage worth waking someone for.
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

    if should_alert:
        if confirmed_outage:
            detail = "\n".join(f"  ✗ {label}: {why}" for label, why in failures)
            telegram(
                "🔴 Bill Commons: READ PATH DOWN\n\n"
                f"{len(failures)} of {len(results)} probes failing "
                f"({consecutive} consecutive runs)\n\n"
                f"{detail}\n\n"
                "Note: /health returns HTTP 200 even when the DB is unreachable "
                "-- this checks the body, not the status code. The site can also "
                "still serve 200s from Next's cache over a dead API."
            )
        else:
            telegram(
                "✅ Bill Commons: read path recovered\n\n"
                + "\n".join(f"  ✓ {label}: {detail}" for label, _, detail, _ in results)
            )

    effective = "outage" if confirmed_outage else status
    since = state.get("since") if previous == effective else now.isoformat()
    save_state({
        "last_status": effective,
        "consecutive_failures": consecutive,
        "since": since,
        "last_checked": now.isoformat(),
    })

    for label, ok, detail, _ in results:
        print(f"  [{'ok' if ok else 'FAIL'}] {label}: {detail}")
    print(f"[{effective}] {len(failures)}/{len(results)} failing")
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
