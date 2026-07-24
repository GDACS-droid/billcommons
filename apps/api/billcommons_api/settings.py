"""API settings (pydantic-settings). DATABASE_URL is resolved separately via
billcommons_shared.db (never surfaced here) -- this module only holds
API-level configuration such as CORS origins and the version string.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BILLCOMMONS_API_", extra="ignore")

    api_version: str = "v1"
    title: str = "Bill Commons API"
    description: str = (
        "Public, read-only REST API for Bill Commons: nonpartisan legislative "
        "search and data across all 50 states + DC."
    )
    cors_allow_origins: list[str] = ["*"]
    rate_limit_default: str = "60/minute"
    environment: str = "development"


def get_settings() -> Settings:
    return Settings()
