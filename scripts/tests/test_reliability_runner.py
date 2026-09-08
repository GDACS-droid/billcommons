"""Reject libpq routing overrides before any disposable-runner subprocess."""
import importlib.util
from pathlib import Path

import pytest


def _runner():
    path = Path(__file__).resolve().parents[1] / 'test_data_reliability.py'
    spec = importlib.util.spec_from_file_location('reliability_runner_guard_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('variable,value', [
    ('PGHOSTADDR', '192.0.2.1'), ('PGHOSTADDR', '127.0.0.1'),
    ('PGSERVICE', 'remote'), ('PGSERVICEFILE', '/tmp/untrusted-service.conf'),
])
def test_libpq_override_is_rejected_before_createdb_or_alembic(monkeypatch, variable, value):
    runner = _runner()
    for key in ('PGHOSTADDR', 'PGSERVICE', 'PGSERVICEFILE'):
        monkeypatch.delenv(key, raising=False)
    for key, setting in {'PGHOST': 'localhost', 'PGPORT': '6543',
                         'PGPASSWORD': 'fixture-placeholder', 'PGUSER': 'fixture'}.items():
        monkeypatch.setenv(key, setting)
    monkeypatch.setenv(variable, value)
    monkeypatch.setattr(runner.subprocess, 'run', lambda *a, **kw: pytest.fail('must reject before any subprocess'))
    assert runner.main() == 2
