"""Database engine/session factory.

Resolves DATABASE_URL from the environment, falling back to
~/.config/billcommons/.env for local development. The connection string is
never printed or logged — callers should not either.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

FALLBACK_ENV_PATH = Path.home() / ".config" / "billcommons" / ".env"


def _load_fallback_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Never logs contents."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _use_psycopg3(url: str) -> str:
    """Force the psycopg3 driver (our pinned dependency) for plain
    postgresql:// / postgres:// URLs that would otherwise default to
    psycopg2 in SQLAlchemy."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    return url


def get_database_url() -> str:
    """Resolve DATABASE_URL from the environment, falling back to
    ~/.config/billcommons/.env. Never print/log the returned value.
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return _use_psycopg3(url)
    fallback = _load_fallback_env_file(FALLBACK_ENV_PATH)
    url = fallback.get("DATABASE_URL")
    if url:
        return _use_psycopg3(url)
    raise RuntimeError(
        "DATABASE_URL is not set in the environment and was not found in "
        f"{FALLBACK_ENV_PATH}"
    )


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return a process-wide singleton SQLAlchemy engine.

    Sets conservative server-side `statement_timeout` /
    `idle_in_transaction_session_timeout` options so a worker process that
    dies mid-transaction (SIGKILL/OOM/deploy restart -- anything that skips
    Python's `finally: db.close()`) doesn't leave an orphaned "idle in
    transaction" connection holding row locks indefinitely; Postgres itself
    now terminates it after 5 minutes idle-in-transaction.

    Also sets TCP keepalives (30s idle / 10s interval / 5 probes) so a
    long-running ingest job's connection doesn't go silently dead behind a
    NAT/load balancer without either side noticing.
    """
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_database_url(),
            pool_pre_ping=True,
            connect_args={
                "options": (
                    "-c statement_timeout=300000"
                    " -c idle_in_transaction_session_timeout=300000"
                    # Parallel query is disabled for every session, everywhere.
                    #
                    # Parallel workers coordinate through dynamic shared memory
                    # allocated from the container's /dev/shm, which on managed
                    # Postgres is small. Once the corpus grew past ~730k
                    # documents the planner began choosing parallel plans and
                    # they failed outright:
                    #
                    #   psycopg.errors.DiskFull: could not resize shared memory
                    #   segment ... No space left on device
                    #
                    # Despite the name the disk is fine -- it is /dev/shm that
                    # is exhausted. On 2026-07-26 this killed the crawl's queue
                    # top-up for 2h, and by the end of the day even a bare
                    # `select max(updated_at)` failed on a 192 KB allocation,
                    # taking the healthcheck down with it. A per-query opt-out
                    # only protected the one query we had already lost.
                    #
                    # Nothing here needs intra-query parallelism: the workers
                    # are I/O-bound on external fetches and the API's search
                    # path is GIN index lookups. Serial plans are strictly
                    # better than plans that raise.
                    " -c max_parallel_workers_per_gather=0"
                ),
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5,
            },
        )
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    """Return a process-wide singleton sessionmaker bound to get_engine()."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)
    return _SessionLocal


def get_session() -> Session:
    """Return a new Session from the shared sessionmaker.

    Callers are responsible for closing it (use as a context manager or in a
    try/finally); this mirrors FastAPI dependency-style usage:

        def get_db():
            db = get_session()
            try:
                yield db
            finally:
                db.close()
    """
    return get_sessionmaker()()
