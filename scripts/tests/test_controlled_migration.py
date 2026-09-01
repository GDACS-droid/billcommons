from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

scripts_directory = str(Path(__file__).resolve().parents[1])
if scripts_directory not in sys.path:
    sys.path.insert(0, scripts_directory)

import controlled_migration as migration


PROJECT = "92e10559-88b7-49ec-ae77-b0dc72b12752"
ENVIRONMENT = "78036c32-1cac-4fae-9a22-ef81c6f99772"
SERVICE = "2211baee-950a-4adc-8d82-87c09b1ce3d6"
PROVENANCE = "railway-production-postgres-public-proxy"


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "BILLCOMMONS_MIGRATION_DATABASE_URL": "postgresql://migration_user:secret@db.example.test:5432/railway",
        "BILLCOMMONS_MIGRATION_BINDING_PROJECT_ID": PROJECT,
        "BILLCOMMONS_MIGRATION_BINDING_ENVIRONMENT_ID": ENVIRONMENT,
        "BILLCOMMONS_MIGRATION_BINDING_SERVICE_ID": SERVICE,
        "BILLCOMMONS_MIGRATION_TARGET_PROVENANCE": PROVENANCE,
    }
    values.update(overrides)
    return values


def _target(environment: dict[str, str] | None = None) -> migration.Target:
    return migration.select_target(
        environment or _environment(),
        expected_project_id=PROJECT,
        expected_environment_id=ENVIRONMENT,
        expected_service_id=SERVICE,
        expected_provenance=PROVENANCE,
    )


class _Cursor:
    def __init__(self, revision: str):
        self.revision = revision
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query: str):
        self.query = query

    def fetchone(self):
        return ("railway", "180006", "10.0.0.9", "5432")

    def fetchall(self):
        return [(self.revision,)]


class _Connection:
    def __init__(self, revisions: list[str]):
        self.revisions = revisions

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _Cursor(self.revisions.pop(0))


def _connector(revisions: list[str]):
    def connect(url: str, *, connect_timeout: int):
        assert "secret" in url
        assert connect_timeout == 20
        return _Connection(revisions)

    return connect


def test_database_url_is_never_a_target_input_and_empty_expansion_fails_closed():
    with pytest.raises(migration.ControlledMigrationError, match="DATABASE_URL is never accepted"):
        _target(
            _environment(
                BILLCOMMONS_MIGRATION_DATABASE_URL="",
                DATABASE_URL="postgresql://fallback:secret@localhost/fallback",
            )
        )


def test_ambient_database_url_must_equal_the_explicitly_selected_target():
    with pytest.raises(migration.ControlledMigrationError, match="conflicts"):
        _target(_environment(DATABASE_URL="postgresql://other:secret@localhost/other"))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("BILLCOMMONS_MIGRATION_BINDING_PROJECT_ID", "wrong"),
        ("BILLCOMMONS_MIGRATION_BINDING_ENVIRONMENT_ID", "wrong"),
        ("BILLCOMMONS_MIGRATION_BINDING_SERVICE_ID", "wrong"),
        ("BILLCOMMONS_MIGRATION_TARGET_PROVENANCE", "wrong"),
    ],
)
def test_binding_provenance_must_match_expected_values(key: str, value: str):
    with pytest.raises(migration.ControlledMigrationError, match="does not match"):
        _target(_environment(**{key: value}))


def test_proxy_binding_is_supported_only_when_complete_and_never_printed():
    environment = _environment(BILLCOMMONS_MIGRATION_DATABASE_URL="")
    environment.update(
        {
            "PGHOST": "proxy.example.test",
            "PGPORT": "5432",
            "PGUSER": "operator",
            "PGPASSWORD": "proxy-secret",
            "PGDATABASE": "railway",
        }
    )
    target = _target(environment)
    assert target.source == "proxy_pg_binding"
    assert "proxy-secret" in target.url
    with pytest.raises(migration.ControlledMigrationError, match="incomplete proxy PG binding"):
        _target(_environment(BILLCOMMONS_MIGRATION_DATABASE_URL="", PGHOST="proxy.example.test"))
    with pytest.raises(migration.ControlledMigrationError, match="plain hostname"):
        _target(
            _environment(
                BILLCOMMONS_MIGRATION_DATABASE_URL="",
                PGHOST="proxy.example.test@unexpected.example.test",
                PGPORT="5432",
                PGUSER="operator",
                PGPASSWORD="proxy-secret",
                PGDATABASE="railway",
            )
        )


def test_check_only_reads_revision_and_fingerprint_without_alembic():
    target = _target()
    report = migration.run(
        target=target,
        repo_root=Path.cwd(),
        check_only=True,
        acknowledged=False,
        expected_current=None,
        environ=_environment(),
        connector=_connector(["0021"]),
        runner=lambda **kwargs: pytest.fail("check-only must not invoke Alembic"),
    )
    rendered = report.safe_json()
    assert report.pre_revision == report.post_revision == "0021"
    assert report.alembic_invocations == 0
    assert "migration_user" not in rendered
    assert "db.example.test" not in rendered
    assert "railway\"" not in rendered
    assert len(json.loads(rendered)["database_fingerprint"]) == 20


def test_upgrade_requires_acknowledgement_and_expected_pre_revision_before_subprocess():
    target = _target()
    with pytest.raises(migration.ControlledMigrationError, match="acknowledge-upgrade-0025"):
        migration.run(
            target=target,
            repo_root=Path.cwd(),
            check_only=False,
            acknowledged=False,
            expected_current="0021",
            environ=_environment(),
            connector=_connector(["0021"]),
        )
    with pytest.raises(migration.ControlledMigrationError, match="does not match"):
        migration.run(
            target=target,
            repo_root=Path.cwd(),
            check_only=False,
            acknowledged=True,
            expected_current="0020",
            environ=_environment(),
            connector=_connector(["0021"]),
        )


def test_upgrade_invokes_exactly_one_pinned_alembic_command_in_an_isolated_environment():
    calls: list[dict] = []

    def runner(*args, **kwargs):
        kwargs["args"] = args[0]
        calls.append(kwargs)
        return subprocess.CompletedProcess(kwargs["args"], 0)

    report = migration.run(
        target=_target(),
        repo_root=Path("/repo"),
        check_only=False,
        acknowledged=True,
        expected_current="0021",
        environ=_environment(
            DATABASE_URL="postgresql://migration_user:secret@db.example.test:5432/railway",
            PGHOST="",
        ),
        connector=_connector(["0021", "0025"]),
        runner=runner,
    )
    assert report.pre_revision == "0021"
    assert report.post_revision == "0025"
    assert report.alembic_invocations == 1
    assert len(calls) == 1
    call = calls[0]
    assert call["args"][-2:] == ["upgrade", "0025"]
    assert call["env"]["DATABASE_URL"] == "postgresql://migration_user:secret@db.example.test:5432/railway"
    assert call["env"]["HOME"] != str(Path.home())
    assert migration._MIGRATION_URL_ENV not in call["env"]
    assert "PGHOST" not in call["env"]
    assert call["stdout"] is subprocess.PIPE
    assert call["stderr"] is subprocess.PIPE


def test_alembic_failure_suppresses_subprocess_output_and_refuses_post_check():
    leaked = "postgresql://leak:secret@example.test/database"

    def runner(*args, **kwargs):
        kwargs["args"] = args[0]
        return subprocess.CompletedProcess(kwargs["args"], 1, stdout=leaked, stderr=leaked)

    with pytest.raises(migration.ControlledMigrationError) as raised:
        migration.run(
            target=_target(),
            repo_root=Path.cwd(),
            check_only=False,
            acknowledged=True,
            expected_current="0021",
            environ=_environment(),
            connector=_connector(["0021"]),
            runner=runner,
        )
    assert leaked not in str(raised.value)


def test_main_check_only_outputs_only_sanitized_report(monkeypatch, capsys):
    monkeypatch.setattr(
        migration,
        "_revision_and_fingerprint",
        lambda url, **kwargs: ("0021", migration.DatabaseFingerprint("0123456789abcdef0123", "180006")),
    )
    exit_code = migration.main(
        [
            "--check-only",
            "--expected-project-id", PROJECT,
            "--expected-environment-id", ENVIRONMENT,
            "--expected-service-id", SERVICE,
            "--expected-provenance", PROVENANCE,
        ],
        environ=_environment(),
    )
    assert exit_code == 0
    rendered = capsys.readouterr().out
    assert "migration_user" not in rendered
    assert json.loads(rendered)["pre_revision"] == "0021"
