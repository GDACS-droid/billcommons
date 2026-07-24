"""Shared FastAPI dependencies: DB session lifecycle."""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy.orm import Session

from billcommons_shared.db import get_session


def get_db() -> Generator[Session, None, None]:
    """Yield a request-scoped SQLAlchemy session, closed after the request."""
    db = get_session()
    try:
        yield db
    finally:
        db.close()
