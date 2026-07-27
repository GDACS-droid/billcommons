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
    # 60/minute was too tight to use the API for what it is for. A consumer
    # monitoring a modest watchlist of ~160 bills needed three minutes of
    # serial polling to check it once, and this project's own contract test
    # suite could not complete a run without nine tests failing on 429.
    #
    # Reads here are indexed lookups and the website sits behind its own cache,
    # so the ceiling was protecting against very little. 300/minute (5 req/s
    # per IP) keeps a floor under abuse while letting the API be used for
    # monitoring, which is the point of publishing it.
    rate_limit_default: str = "300/minute"
    environment: str = "development"


def get_settings() -> Settings:
    return Settings()
