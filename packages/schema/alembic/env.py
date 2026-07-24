import os
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

from billcommons_schema.base import Base
from billcommons_schema import models  # noqa: F401  (registers all tables on Base.metadata)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

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


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
