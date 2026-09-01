#!/usr/bin/env python3
"""Run one guarded, private-production Scout canary through the public API.

This is an operator tool, not a customer login path.  It has deliberately
small authority: it will create or reuse *only* the named customer already in
the server-side Scout canary allowlist, then uses the account router's signed
session-cookie primitive to create and observe one Scout job.  It never emits
the cookie, account ID, job ID, API response body, source text, or any other
capability returned by the API.

It does not deploy, migrate, configure a remote service, or run unless the
operator supplies ``--ack-production-canary``.  The configured API base must
be HTTPS, and public-rollout configuration is rejected even if an allowlist is
also present.

Example (run only by an authorized operator after the production canary is
configured):

    BILLCOMMONS_SCOUT_CANARY_EMAIL=operator@example.org \\
    BILLCOMMONS_SCOUT_CANARY_QUERY='HB 625' \\
    BILLCOMMONS_SCOUT_CANARY_API_BASE=https://api.billcommons.org \\
    BILLCOMMONS_SCOUT_CANARY_EMAILS=operator@example.org \\
    DATABASE_URL=... ACCOUNT_SESSION_SECRET=... \\
    python scripts/private_scout_canary.py --ack-production-canary

Secrets above are intentionally only environment inputs; this program never
prints them.  It also never falls back to an HTTP endpoint.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from billcommons_api.routers.account import (
    _allowed_origins,
    _normalize_email,
    _session_secret,
    _sign_session,
    _upsert_customer_by_email,
)
from billcommons_schema.models import ApiCustomer
from billcommons_shared.db import get_session
from billcommons_shared.scout import ScoutPolicyError, ScoutSettings, normalize_jurisdiction, normalize_query


_TERMINAL_STATUSES = frozenset({"completed", "partial", "failed", "canceled"})
_KNOWN_STATUSES = _TERMINAL_STATUSES | frozenset({"queued", "running"})
_USAGE_FIELDS = ("external_requests", "browser_sessions", "browser_pages", "browser_actions", "browser_routed_requests")


class CanaryError(RuntimeError):
    """An operator-safe error.  Its text must never include remote bodies."""


class ApiClient(Protocol):
    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]: ...


@dataclass(frozen=True)
class CanaryConfig:
    email: str
    query: str
    jurisdiction: str
    api_base: str
    origin: str
    poll_interval_seconds: float
    poll_timeout_seconds: float


def _env_or_arg(value: str | None, env_name: str) -> str:
    candidate = value if value is not None else os.environ.get(env_name, "")
    return candidate.strip()


def _https_origin(value: str, *, name: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise CanaryError(f"{name} must be an HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise CanaryError(f"{name} must be an HTTPS origin")
    return urlunsplit(("https", parsed.netloc, "", "", ""))


def _positive_seconds(value: float, *, name: str, maximum: float) -> float:
    if not 0 < value <= maximum:
        raise CanaryError(f"{name} must be greater than zero and at most {maximum:g}")
    return value


def _config_from_args(args: argparse.Namespace) -> CanaryConfig:
    if not args.ack_production_canary:
        raise CanaryError("--ack-production-canary is required")

    email = _normalize_email(_env_or_arg(args.email, "BILLCOMMONS_SCOUT_CANARY_EMAIL"))
    if email is None:
        raise CanaryError("a valid named canary email is required")
    query = _env_or_arg(args.query, "BILLCOMMONS_SCOUT_CANARY_QUERY")
    try:
        normalized_query = normalize_query(query)
        jurisdiction = normalize_jurisdiction(args.jurisdiction)
    except ScoutPolicyError as exc:
        raise CanaryError("query or jurisdiction is not permitted by Scout policy") from exc
    # Keep the original spelling for customer-visible research while rejecting
    # whitespace-only input through the shared Scout normalization primitive.
    del normalized_query

    settings = ScoutSettings.from_env()
    if not settings.enabled:
        raise CanaryError("BILLCOMMONS_SCOUT_ENABLED must be enabled for the private canary")
    if settings.allow_public_rollout:
        raise CanaryError("public Scout rollout must be disabled for a private canary")
    if not settings.canary_emails:
        raise CanaryError("BILLCOMMONS_SCOUT_CANARY_EMAILS must name a private cohort")
    if email not in settings.canary_emails:
        raise CanaryError("named identity is not in BILLCOMMONS_SCOUT_CANARY_EMAILS")

    api_base = _https_origin(_env_or_arg(args.api_base, "BILLCOMMONS_SCOUT_CANARY_API_BASE"), name="API base")
    origin = _https_origin(_env_or_arg(args.origin, "BILLCOMMONS_SCOUT_CANARY_ORIGIN") or "https://billcommons.org", name="Origin")
    if origin not in _allowed_origins():
        raise CanaryError("Origin is not in BILLCOMMONS_ALLOWED_ORIGINS")
    return CanaryConfig(
        email=email,
        query=query.strip(),
        jurisdiction=jurisdiction,
        api_base=api_base,
        origin=origin,
        poll_interval_seconds=_positive_seconds(args.poll_interval_seconds, name="poll interval", maximum=60),
        poll_timeout_seconds=_positive_seconds(args.poll_timeout_seconds, name="poll timeout", maximum=900),
    )


class UrllibApiClient:
    """HTTPS-only JSON client that intentionally discards error response bodies."""

    def __init__(self, config: CanaryConfig, session_cookie: str):
        self._api_base = config.api_base
        self._origin = config.origin
        self._session_cookie = session_cookie

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        if not path.startswith("/api/v1/scout/"):
            raise CanaryError("refusing a non-Scout API path")
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            f"{self._api_base}{path}",
            data=payload,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": self._origin,
                "Cookie": f"bc_session={self._session_cookie}",
                "User-Agent": "billcommons-private-scout-canary/1.0",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                status = response.status
                raw_body = response.read()
        except HTTPError as exc:
            # Do not read ``exc``: API error bodies can gain fields over time.
            raise CanaryError(f"Scout API returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise CanaryError("Scout API request failed") from exc
        try:
            decoded = json.loads(raw_body)
        except (TypeError, ValueError) as exc:
            raise CanaryError("Scout API returned a non-JSON response") from exc
        if not isinstance(decoded, dict):
            raise CanaryError("Scout API returned an unexpected JSON response")
        return status, decoded


def _ensure_customer(email: str) -> uuid.UUID:
    """Atomically create/reuse the exact allowlisted account, without key minting."""
    db = get_session()
    try:
        customer: ApiCustomer = _upsert_customer_by_email(db, email)
        db.commit()
        return customer.id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _job_from_response(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    if status not in (200, 201):
        raise CanaryError(f"Scout API returned unexpected HTTP {status}")
    job = payload.get("job")
    if not isinstance(job, dict):
        raise CanaryError("Scout API response omitted its job descriptor")
    try:
        uuid.UUID(str(job["id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise CanaryError("Scout API response omitted a valid job reference") from exc
    return job


def _safe_metrics(job: dict[str, Any]) -> dict[str, Any]:
    usage = job.get("usage")
    safe_usage: dict[str, int] = {}
    if isinstance(usage, dict):
        for name in _USAGE_FIELDS:
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                safe_usage[name] = value
    findings = job.get("finding_count")
    return {
        "observed_status": job.get("status") if job.get("status") in _KNOWN_STATUSES else "unknown",
        "partial_success": bool(job.get("partial_success")),
        "finding_count": findings if isinstance(findings, int) and not isinstance(findings, bool) and findings >= 0 else 0,
        "usage": safe_usage,
    }


def _emit(out: TextIO, outcome: str, job: dict[str, Any], *, cache_reused: bool | None = None) -> None:
    report: dict[str, Any] = {"canary": {"outcome": outcome}, "result": _safe_metrics(job)}
    if cache_reused is not None:
        report["canary"]["cache_reused"] = cache_reused
    print(json.dumps(report, sort_keys=True), file=out)


def run_canary(
    config: CanaryConfig,
    *,
    ensure_customer: Callable[[str], uuid.UUID],
    sign_session: Callable[[uuid.UUID], str],
    client_factory: Callable[[str], ApiClient],
    out: TextIO,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Run the two-submit canary; return false for a truthful non-success result."""
    customer_id = ensure_customer(config.email)
    session_cookie = sign_session(customer_id)
    client = client_factory(session_cookie)
    request_body = {"query": config.query, "jurisdiction": config.jurisdiction}
    _status, first_payload = client.request("POST", "/api/v1/scout/jobs", body=request_body)
    first_job = _job_from_response(_status, first_payload)
    job_id = str(first_job["id"])

    deadline = clock() + config.poll_timeout_seconds
    job = first_job
    while job.get("status") not in _TERMINAL_STATUSES:
        if clock() >= deadline:
            _emit(out, "poll_timed_out", job)
            return False
        sleep(config.poll_interval_seconds)
        status, payload = client.request("GET", f"/api/v1/scout/jobs/{job_id}")
        job = _job_from_response(status, {"job": payload})

    if job["status"] not in {"completed", "partial"}:
        # Failed/canceled jobs are deliberately not resubmitted: failures are
        # not cacheable and a second submit would create avoidable new work.
        _emit(out, "terminal_without_cache_reuse", job, cache_reused=False)
        return False

    duplicate_status, duplicate_payload = client.request("POST", "/api/v1/scout/jobs", body=request_body)
    duplicate_job = _job_from_response(duplicate_status, duplicate_payload)
    cache_reused = (
        duplicate_status == 200
        and duplicate_payload.get("coalesced") is True
        and duplicate_payload.get("cached") is True
        and str(duplicate_job["id"]) == job_id
    )
    _emit(out, "cache_reuse_verified" if cache_reused else "cache_reuse_not_verified", job, cache_reused=cache_reused)
    return cache_reused


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one guarded private Scout production canary.")
    parser.add_argument("--email", help="canary email; defaults to BILLCOMMONS_SCOUT_CANARY_EMAIL")
    parser.add_argument("--query", help="Scout query; defaults to BILLCOMMONS_SCOUT_CANARY_QUERY")
    parser.add_argument("--api-base", help="HTTPS API origin; defaults to BILLCOMMONS_SCOUT_CANARY_API_BASE")
    parser.add_argument("--origin", help="HTTPS Origin header; defaults to BILLCOMMONS_SCOUT_CANARY_ORIGIN or billcommons.org")
    parser.add_argument("--jurisdiction", default="FL")
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--poll-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--ack-production-canary", action="store_true", help="explicitly acknowledge the production canary")
    return parser


def main(argv: list[str] | None = None, *, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    try:
        config = _config_from_args(_parser().parse_args(argv))
        # Resolve the signing configuration before the customer upsert.  A
        # missing secret must never cause an account write followed by failure.
        _session_secret()
        succeeded = run_canary(
            config,
            ensure_customer=_ensure_customer,
            sign_session=_sign_session,
            client_factory=lambda cookie: UrllibApiClient(config, cookie),
            out=out,
        )
        return 0 if succeeded else 1
    except CanaryError as exc:
        print(f"private Scout canary failed: {exc}", file=err)
        return 2
    except Exception:
        # Exception messages from database/network libraries may contain a URL
        # or other sensitive context.  Keep this terminal diagnostic fixed.
        print("private Scout canary failed before a sanitized result was available", file=err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
