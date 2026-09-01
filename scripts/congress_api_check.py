#!/usr/bin/env python3
"""Verify Congress.gov API authentication without exposing the API key."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _local_value(name: str) -> str | None:
    direct = os.environ.get(name)
    if direct:
        return direct
    path = Path.home() / ".config" / "billcommons" / ".env"
    if not path.exists():
        return None
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'") or None
    return None


def main() -> int:
    api_key = _local_value("CONGRESS_API_KEY")
    if not api_key:
        print("Congress.gov authentication not configured.", file=sys.stderr)
        return 2
    request = Request(
        "https://api.congress.gov/v3/congress/current?format=json",
        headers={"X-Api-Key": api_key, "User-Agent": "BillCommons-Congress-Check/1.0"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
            rate_limit = response.headers.get("X-RateLimit-Limit", "unknown")
    except HTTPError as exc:
        print(f"Congress.gov authentication failed (HTTP {exc.code}).", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, ValueError):
        print("Congress.gov check failed before a valid response was received.", file=sys.stderr)
        return 1
    finally:
        api_key = ""
    congress = payload.get("congress", {}).get("number", "unknown")
    print(f"Congress.gov authentication: ok; current_congress={congress}; hourly_limit={rate_limit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
