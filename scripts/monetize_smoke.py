#!/usr/bin/env python
"""Phase 1 monetization smoke test against a REAL Postgres instance.

Mints a customer + API key via the service layer (`billcommons_api.api_keys`
directly, not an HTTP round trip), fires three keyed requests through a
`TestClient`-wrapped app against that same database (so the full
`QuotaMiddleware` stack -- burst bucket, daily quota pre-check, post-response
`FOR UPDATE`/`ON CONFLICT` accounting -- runs for real, not against the
SQLite test harness), and prints the `X-Quota-*` response headers plus the
resulting `api_customer_usage` row after each call.

Requires `DATABASE_URL` to point at a Postgres instance with migration 0019
already applied (`alembic upgrade head` from `packages/schema`). Does NOT
apply migrations itself and does NOT touch a live/production database on its
own -- point it at a throwaway instance.

Usage:
    DATABASE_URL=postgresql://bc@127.0.0.1:54329/bc_staging \\
        ../../.venv/bin/python scripts/monetize_smoke.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("ACCOUNT_SESSION_SECRET", "smoke-test-session-secret")
os.environ.setdefault("BILLCOMMONS_ADMIN_TOKEN", "smoke-test-admin-token")

if not os.environ.get("DATABASE_URL"):
    print("DATABASE_URL is not set -- point it at a throwaway Postgres instance "
          "with migration 0019 already applied.", file=sys.stderr)
    sys.exit(1)

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("BILLCOMMONS_REVEAL_KEY", Fernet.generate_key().decode())

from sqlalchemy import select, text  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import billcommons_api.api_keys as api_keys  # noqa: E402
from billcommons_api.app import create_app  # noqa: E402
from billcommons_shared.db import get_session  # noqa: E402
from billcommons_schema.models import ApiCustomer  # noqa: E402


def _print_quota_headers(label: str, res) -> None:
    quota_headers = {k: v for k, v in res.headers.items() if k.lower().startswith(("x-quota", "x-ratelimit", "x-plan"))}
    print(f"[{label}] status={res.status_code}")
    for k, v in sorted(quota_headers.items()):
        print(f"    {k}: {v}")


def _print_usage_row(db, customer_id) -> None:
    row = db.execute(
        text(
            "SELECT customer_id, usage_date, requests, heavy_requests "
            "FROM api_customer_usage WHERE customer_id = :customer_id"
        ),
        {"customer_id": str(customer_id)},
    ).first()
    print(f"    api_customer_usage row: {dict(row._mapping) if row else None}")


def main() -> None:
    db = get_session()
    email = "monetize-smoke@example.com"
    try:
        existing = db.execute(
            select(ApiCustomer).where(ApiCustomer.email == email)
        ).scalar_one_or_none()
        if existing is not None:
            # Idempotent re-runs: this script is meant to be safe to run
            # more than once against the same throwaway DB.
            db.execute(text("DELETE FROM api_customers WHERE id = :id"), {"id": str(existing.id)})
            db.commit()

        customer = ApiCustomer(email=email)
        db.add(customer)
        db.flush()
        db.commit()

        row, full_key = api_keys.mint_key(db, customer.id, environment="live", plan="developer")
        db.commit()
        print(f"minted key {row.key_prefix}... for customer {customer.id} (plan={row.plan})")
    finally:
        db.close()

    app = create_app()

    @app.get("/api/v1/_smoke_ok")
    def _ok():
        return {"ok": True}

    with TestClient(app) as client:
        for i in range(1, 4):
            res = client.get(
                "/api/v1/_smoke_ok",
                headers={"Authorization": f"Bearer {full_key}", "X-Forwarded-For": "203.0.113.199"},
            )
            _print_quota_headers(f"request {i}", res)
            db2 = get_session()
            try:
                _print_usage_row(db2, customer.id)
            finally:
                db2.close()

    print("smoke test complete.")


if __name__ == "__main__":
    main()
