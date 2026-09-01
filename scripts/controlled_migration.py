#!/usr/bin/env python3
"""Apply one explicitly acknowledged Bill Commons production migration safely.

This is an operator-only guard around Alembic.  It deliberately does *not*
read ``DATABASE_URL`` as its target input: Alembic's normal environment file
fallback is useful for local development, but is unsafe for a release command
whose Railway variable expansion might have produced an empty value.

The selected database must instead arrive through exactly one of:

* ``BILLCOMMONS_MIGRATION_DATABASE_URL``; or
* the complete ``PGHOST``, ``PGPORT``, ``PGUSER``, ``PGPASSWORD`` and
  ``PGDATABASE`` proxy binding.

The caller also supplies the expected Railway project/environment/service IDs
and provenance label on the command line.  The matching injected binding
metadata is required in the environment.  This cannot cryptographically prove
where a credential originated, but it makes a missing expansion, accidental
wrong service context, and fallback-to-a-developer-dotenv fail closed before
Alembic is allowed to run.

The program never prints a DSN, password, database name, host, Alembic output,
or environment values.  Its fingerprint is a one-way, truncated SHA-256 of
server and database identity material.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import quote, urlencode, urlunsplit

import psycopg


RELEASE_REVISION = "0025"
_BINDING_FIELDS = ("PROJECT_ID", "ENVIRONMENT_ID", "SERVICE_ID")
_PROXY_FIELDS = ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE")
_MIGRATION_URL_ENV = "BILLCOMMONS_MIGRATION_DATABASE_URL"
_PROVENANCE_ENV = "BILLCOMMONS_MIGRATION_TARGET_PROVENANCE"


class ControlledMigrationError(RuntimeError):
    """Failure safe to show an operator; it never includes a connection URL."""


@dataclass(frozen=True)
class Binding:
    project_id: str
    environment_id: str
    service_id: str
    provenance: str


@dataclass(frozen=True)
class Target:
    url: str
    source: str
    binding: Binding


@dataclass(frozen=True)
class DatabaseFingerprint:
    digest: str
    server_version_num: str


@dataclass(frozen=True)
class MigrationReport:
    target_source: str
    fingerprint: DatabaseFingerprint
    pre_revision: str
    post_revision: str
    alembic_invocations: int
    mode: str

    def safe_json(self) -> str:
        return json.dumps(
            {
                "alembic_invocations": self.alembic_invocations,
                "database_fingerprint": self.fingerprint.digest,
                "mode": self.mode,
                "post_revision": self.post_revision,
                "pre_revision": self.pre_revision,
                "server_version_num": self.fingerprint.server_version_num,
                "target_source": self.target_source,
            },
            sort_keys=True,
        )


def _required(value: str | None, *, name: str) -> str:
    result = (value or "").strip()
    if not result:
        raise ControlledMigrationError(f"{name} is required and must be nonempty")
    return result


def _value_without_whitespace(value: str | None, *, name: str) -> str:
    result = _required(value, name=name)
    if any(character.isspace() for character in result):
        raise ControlledMigrationError(f"{name} must not contain whitespace")
    return result


def _binding_from_environment(
    environ: Mapping[str, str],
    *,
    expected_project_id: str,
    expected_environment_id: str,
    expected_service_id: str,
    expected_provenance: str,
) -> Binding:
    expected = {
        "PROJECT_ID": _value_without_whitespace(expected_project_id, name="--expected-project-id"),
        "ENVIRONMENT_ID": _value_without_whitespace(expected_environment_id, name="--expected-environment-id"),
        "SERVICE_ID": _value_without_whitespace(expected_service_id, name="--expected-service-id"),
    }
    actual: dict[str, str] = {}
    for field in _BINDING_FIELDS:
        key = f"BILLCOMMONS_MIGRATION_BINDING_{field}"
        actual[field] = _value_without_whitespace(environ.get(key), name=key)
        if actual[field] != expected[field]:
            raise ControlledMigrationError(f"{key} does not match the expected Railway binding")
    provenance = _value_without_whitespace(environ.get(_PROVENANCE_ENV), name=_PROVENANCE_ENV)
    if provenance != _value_without_whitespace(expected_provenance, name="--expected-provenance"):
        raise ControlledMigrationError(f"{_PROVENANCE_ENV} does not match --expected-provenance")
    return Binding(
        project_id=actual["PROJECT_ID"],
        environment_id=actual["ENVIRONMENT_ID"],
        service_id=actual["SERVICE_ID"],
        provenance=provenance,
    )


def _proxy_url(environ: Mapping[str, str]) -> str:
    values = {field: environ.get(field, "") for field in _PROXY_FIELDS}
    missing = [field for field, value in values.items() if not value.strip()]
    if missing:
        raise ControlledMigrationError(
            "incomplete proxy PG binding; all PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE are required"
        )
    try:
        port = int(values["PGPORT"])
    except ValueError as exc:
        raise ControlledMigrationError("PGPORT must be a numeric TCP port") from exc
    if not 1 <= port <= 65535:
        raise ControlledMigrationError("PGPORT must be a valid TCP port")

    host = values["PGHOST"].strip()
    if any(character.isspace() for character in host) or any(character in host for character in "@/?#"):
        raise ControlledMigrationError("PGHOST must be a plain hostname or IP address")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    userinfo = f"{quote(values['PGUSER'], safe='')}:{quote(values['PGPASSWORD'], safe='')}"
    netloc = f"{userinfo}@{host}:{port}"
    return urlunsplit(
        (
            "postgresql",
            netloc,
            f"/{quote(values['PGDATABASE'], safe='')}",
            urlencode({"sslmode": environ.get("PGSSLMODE", "require").strip() or "require"}),
            "",
        )
    )


def _selected_url(environ: Mapping[str, str]) -> tuple[str, str]:
    explicit = environ.get(_MIGRATION_URL_ENV, "").strip()
    proxy_present = any(environ.get(field, "").strip() for field in _PROXY_FIELDS)
    if explicit and proxy_present:
        raise ControlledMigrationError("provide exactly one selected target: migration URL or proxy PG binding")
    if explicit:
        if not explicit.startswith(("postgresql://", "postgres://")):
            raise ControlledMigrationError(f"{_MIGRATION_URL_ENV} must be a PostgreSQL URL")
        return explicit, "explicit_migration_url"
    if proxy_present:
        return _proxy_url(environ), "proxy_pg_binding"
    raise ControlledMigrationError(
        f"{_MIGRATION_URL_ENV} is required, or provide the complete proxy PG binding; "
        "DATABASE_URL is never accepted as input"
    )


def select_target(
    environ: Mapping[str, str],
    *,
    expected_project_id: str,
    expected_environment_id: str,
    expected_service_id: str,
    expected_provenance: str,
) -> Target:
    """Resolve only an explicit release target and reject ambiguous ambient DSNs."""
    url, source = _selected_url(environ)
    ambient_database_url = environ.get("DATABASE_URL", "").strip()
    if ambient_database_url and ambient_database_url != url:
        raise ControlledMigrationError(
            "ambient DATABASE_URL conflicts with the explicitly selected migration target"
        )
    return Target(
        url=url,
        source=source,
        binding=_binding_from_environment(
            environ,
            expected_project_id=expected_project_id,
            expected_environment_id=expected_environment_id,
            expected_service_id=expected_service_id,
            expected_provenance=expected_provenance,
        ),
    )


def _revision_and_fingerprint(
    url: str, *, connector: Callable[..., object] = psycopg.connect
) -> tuple[str, DatabaseFingerprint]:
    """Read the target identity and one Alembic revision without logging the DSN."""
    try:
        with connector(url, connect_timeout=20) as connection:  # type: ignore[union-attr]
            with connection.cursor() as cursor:  # type: ignore[union-attr]
                cursor.execute(
                    "SELECT current_database(), current_setting('server_version_num'), "
                    "COALESCE(inet_server_addr()::text, 'local'), "
                    "COALESCE(inet_server_port()::text, '0')"
                )
                identity = cursor.fetchone()
                cursor.execute("SELECT version_num FROM alembic_version")
                revisions = cursor.fetchall()
    except Exception as exc:
        raise ControlledMigrationError(
            "could not connect to the explicitly selected migration target"
        ) from exc
    if not identity or len(identity) != 4:
        raise ControlledMigrationError("target did not return a complete server fingerprint")
    if len(revisions) != 1 or not revisions[0] or not isinstance(revisions[0][0], str):
        raise ControlledMigrationError("target has an invalid Alembic revision state")
    digest_input = "\x1f".join(str(value) for value in identity).encode("utf-8")
    return revisions[0][0], DatabaseFingerprint(
        digest=hashlib.sha256(digest_input).hexdigest()[:20],
        server_version_num=str(identity[1]),
    )


def _alembic_environment(environ: Mapping[str, str], *, url: str, isolated_home: str) -> dict[str, str]:
    child = dict(environ)
    # Strip every alternate input path.  Alembic receives one nonempty target,
    # and its developer fallback path resolves under a fresh empty HOME.
    child.pop(_MIGRATION_URL_ENV, None)
    for field in _PROXY_FIELDS:
        child.pop(field, None)
    child["DATABASE_URL"] = url
    child["HOME"] = isolated_home
    child["BILLCOMMONS_CONTROLLED_MIGRATION"] = "1"
    return child


def _invoke_alembic_once(
    *,
    repo_root: Path,
    environ: Mapping[str, str],
    url: str,
    runner: Callable[..., object] = subprocess.run,
) -> None:
    with tempfile.TemporaryDirectory(prefix="billcommons-controlled-migration-") as isolated_home:
        completed = runner(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                "packages/schema/alembic.ini",
                "upgrade",
                RELEASE_REVISION,
            ],
            cwd=repo_root,
            env=_alembic_environment(environ, url=url, isolated_home=isolated_home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    if getattr(completed, "returncode", 1) != 0:
        # Alembic output can contain remote server messages.  Suppress it
        # rather than risk exposing a URL, host, credentials, or untrusted SQL.
        raise ControlledMigrationError("Alembic upgrade failed; subprocess output was suppressed")


def run(
    *,
    target: Target,
    repo_root: Path,
    check_only: bool,
    acknowledged: bool,
    expected_current: str | None,
    environ: Mapping[str, str],
    connector: Callable[..., object] = psycopg.connect,
    runner: Callable[..., object] = subprocess.run,
) -> MigrationReport:
    pre_revision, fingerprint = _revision_and_fingerprint(target.url, connector=connector)
    if check_only:
        return MigrationReport(target.source, fingerprint, pre_revision, pre_revision, 0, "check")
    if not acknowledged:
        raise ControlledMigrationError("--acknowledge-upgrade-0025 is required before any upgrade")
    if expected_current is None:
        raise ControlledMigrationError("--expected-current is required before any upgrade")
    if pre_revision != expected_current:
        raise ControlledMigrationError("target pre-revision does not match --expected-current")
    _invoke_alembic_once(repo_root=repo_root, environ=environ, url=target.url, runner=runner)
    post_revision, post_fingerprint = _revision_and_fingerprint(target.url, connector=connector)
    if post_fingerprint != fingerprint:
        raise ControlledMigrationError("database server fingerprint changed during migration")
    if post_revision != RELEASE_REVISION:
        raise ControlledMigrationError("target post-revision is not 0025")
    return MigrationReport(target.source, fingerprint, pre_revision, post_revision, 1, "upgrade")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed controlled Bill Commons migration runner")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check-only",
        action="store_true",
        help="verify selected target and revision without invoking Alembic",
    )
    mode.add_argument(
        "--upgrade",
        action="store_true",
        help="apply revision 0025 exactly once after acknowledgement",
    )
    parser.add_argument("--acknowledge-upgrade-0025", action="store_true")
    parser.add_argument(
        "--expected-current",
        help="required pre-revision for --upgrade (for Scout production: 0021)",
    )
    parser.add_argument("--expected-project-id", required=True)
    parser.add_argument("--expected-environment-id", required=True)
    parser.add_argument("--expected-service-id", required=True)
    parser.add_argument("--expected-provenance", required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    environment = os.environ if environ is None else environ
    try:
        target = select_target(
            environment,
            expected_project_id=args.expected_project_id,
            expected_environment_id=args.expected_environment_id,
            expected_service_id=args.expected_service_id,
            expected_provenance=args.expected_provenance,
        )
        report = run(
            target=target,
            repo_root=Path(__file__).resolve().parents[1],
            check_only=args.check_only,
            acknowledged=args.acknowledge_upgrade_0025,
            expected_current=args.expected_current,
            environ=environment,
        )
    except ControlledMigrationError as exc:
        print(f"controlled migration refused: {exc}", file=sys.stderr)
        return 2
    print(report.safe_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
