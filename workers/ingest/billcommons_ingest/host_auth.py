"""Per-host credentials for official full-text sources.

Configuration is deliberately opt-in. A configured credential may authorize
access that the public crawler policy would not, so a robots exemption is only
available for the exact host carrying that configuration.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)

_ENV_TEMPLATE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DEFAULT_CONFIG_PATH = Path("~/.config/billcommons/host-auth.json")


@dataclass(frozen=True)
class _HostCredentials:
    headers: dict[str, str]
    robots_exempt: bool


class HostAuth:
    """Resolved authorization configuration, keyed by lowercased hostname."""

    def __init__(self, entries: dict[str, _HostCredentials]) -> None:
        self._entries = entries

    @classmethod
    def from_environment(cls) -> "HostAuth":
        """Load environment JSON first, then the optional configuration file."""
        raw = os.environ.get("BILLCOMMONS_HOST_AUTH_JSON")
        if raw is None or not raw.strip():
            config_path = Path(
                os.environ.get("BILLCOMMONS_HOST_AUTH_FILE", str(_DEFAULT_CONFIG_PATH))
            ).expanduser()
            try:
                raw = config_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return cls({})

        try:
            config = json.loads(raw)
        except (TypeError, ValueError):
            return cls({})
        if not isinstance(config, dict):
            return cls({})

        entries: dict[str, _HostCredentials] = {}
        for host, settings in config.items():
            if not isinstance(host, str) or not isinstance(settings, dict):
                continue
            headers = cls._resolve_headers(host, settings)
            if not headers:
                continue
            entries[host.lower()] = _HostCredentials(
                headers=headers,
                # A robots exemption has no meaning without usable,
                # host-specific request headers.
                robots_exempt=bool(settings.get("robots_exempt")),
            )

        if entries:
            logger.info("host auth configured for: %s", ", ".join(sorted(entries)))
        return cls(entries)

    @staticmethod
    def _resolve_headers(host: str, settings: dict) -> dict[str, str]:
        configured_headers = settings.get("headers")
        if not isinstance(configured_headers, dict) or not configured_headers:
            return {}

        token: str | None = None
        token_file = settings.get("token_file")
        token_key = settings.get("token_key")
        if token_file is not None or token_key is not None:
            if not isinstance(token_file, str) or not isinstance(token_key, str):
                HostAuth._log_unresolved(host)
                return {}
            token = HostAuth._token_from_file(token_file, token_key)
            if token is None:
                HostAuth._log_unresolved(host)
                return {}

        resolved: dict[str, str] = {}
        for name, template in configured_headers.items():
            if not isinstance(name, str) or not isinstance(template, str) or not name:
                return {}
            value = HostAuth._expand_environment(template)
            if value is None:
                HostAuth._log_unresolved(host)
                return {}
            if "{token}" in value:
                if token is None:
                    HostAuth._log_unresolved(host)
                    return {}
                value = value.replace("{token}", token)
            if not value:
                HostAuth._log_unresolved(host)
                return {}
            resolved[name] = value
        return resolved

    @staticmethod
    def _log_unresolved(host: str) -> None:
        logger.info("host auth for %s skipped: unresolved token", host)

    @staticmethod
    def _expand_environment(template: str) -> str | None:
        missing = False

        def replace(match: re.Match[str]) -> str:
            nonlocal missing
            value = os.environ.get(match.group(1))
            if not value:
                missing = True
                return ""
            return value

        value = _ENV_TEMPLATE.sub(replace, template)
        return None if missing else value

    @staticmethod
    def _token_from_file(token_file: str, token_key: str) -> str | None:
        """Read one token from a JSON file directly inside the config dir."""
        config_dir = (Path.home() / ".config" / "billcommons").resolve()
        path = Path(token_file).expanduser()
        try:
            path = path.resolve()
        except OSError:
            return None
        if path.parent != config_dir or path.suffix != ".json":
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        value = payload.get(token_key) if isinstance(payload, dict) else None
        return value if isinstance(value, str) and value else None

    def headers_for(self, url: str) -> dict[str, str]:
        entry = self._entries.get(_hostname(url))
        return dict(entry.headers) if entry else {}

    def robots_exempt(self, url: str) -> bool:
        if urlsplit(url).scheme != "https":
            return False
        entry = self._entries.get(_hostname(url))
        return bool(entry and entry.robots_exempt)

    def robots_exempt_hosts(self) -> frozenset[str]:
        return frozenset(host for host, entry in self._entries.items() if entry.robots_exempt)


def _hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


_default_auth: HostAuth | None = None


def _configured_auth() -> HostAuth:
    global _default_auth
    if _default_auth is None:
        _default_auth = HostAuth.from_environment()
    return _default_auth


def headers_for(url: str) -> dict[str, str]:
    """Return configured request headers for this URL's exact hostname."""
    return _configured_auth().headers_for(url)


def robots_exempt(url: str) -> bool:
    """Whether this URL's exact hostname has an authorized robots exemption."""
    return _configured_auth().robots_exempt(url)


def robots_exempt_hosts() -> frozenset[str]:
    """Configured hosts whose past robots verdicts may safely be requeued."""
    return _configured_auth().robots_exempt_hosts()
