"""Bill Commons canonical schema package.

Single source of truth for the data model: SQLAlchemy 2.0 declarative models
(see billcommons_schema.models) plus Alembic migrations (packages/schema/alembic).
"""
from billcommons_schema.base import Base

__all__ = ["Base"]
