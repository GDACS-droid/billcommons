"""API settings (pydantic-settings). DATABASE_URL is resolved separately via
billcommons_shared.db (never surfaced here) -- this module only holds
API-level configuration such as CORS origins and the version string.
"""
from __future__ import annotations

from pydantic import Field
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
    # 2026-08-21 bleed-stop: a single scraper spread ~500 req/min across 4
    # AWS IPs (~125/min each) never tripped the 300/minute PER-IP ceiling
    # above. This second bucket keys on the containing subnet (IPv4 /24,
    # IPv6 /48) instead of the exact address, so addresses rotated within
    # one small block share a budget. A request must pass both buckets.
    # Same BILLCOMMONS_API_ prefix / naming convention as `rate_limit_default`
    # above -- env var is BILLCOMMONS_API_RATE_LIMIT_SUBNET.
    #
    # Verify round fd9997c, finding #7: 600/minute would NOT have caught the
    # actual incident on a light route -- ~500 req/min aggregate across 4
    # IPs, each individually under the 300/minute per-IP ceiling, still sits
    # under a 600/minute subnet ceiling. 450/minute sits below the observed
    # attack volume while comfortably clearing normal traffic (4 real
    # visitors behind one NAT/office /24 hitting light routes).
    rate_limit_subnet: str = "450/minute"
    # Heavy tier: the expensive per-bill/search routes the same scraper was
    # enumerating (bill detail's /full, /versions, /compare, plus the
    # /bills list and /search endpoints) get a tighter ceiling than the
    # general default, on top of it -- both must pass. Env vars:
    # BILLCOMMONS_API_RATE_LIMIT_HEAVY / BILLCOMMONS_API_RATE_LIMIT_HEAVY_SUBNET.
    rate_limit_heavy: str = "60/minute"
    # Verify round fd9997c, finding #7: raised from 120 to 180/minute in the
    # same direction as `rate_limit_subnet` above, for the same reason --
    # scaled down from the (also-raised) subnet default rather than left at
    # a number picked before that incident math was worked through.
    rate_limit_heavy_subnet: str = "180/minute"
    environment: str = "development"

    # 2026-08-21 monetization (round-2 amendment C10). No BILLCOMMONS_API_
    # prefix on either -- both are shared with non-API contexts (the reveal
    # key is a plain Fernet key read directly by `billcommons_api.api_keys`;
    # the allowed-origins list is also the CORS-with-credentials allowlist
    # `AccountCorsMiddleware` in app.py reads for /account and /billing).
    #
    # `reveal_key` has NO DEFAULT on purpose: `billcommons_api.api_keys`
    # raises loudly the first time a key is minted or revealed without one
    # set, rather than silently falling back to something insecure.
    reveal_key: str | None = Field(default=None, validation_alias="BILLCOMMONS_REVEAL_KEY")
    allowed_origins: str = Field(
        default="https://billcommons.org,https://www.billcommons.org",
        validation_alias="BILLCOMMONS_ALLOWED_ORIGINS",
    )


def get_settings() -> Settings:
    return Settings()
