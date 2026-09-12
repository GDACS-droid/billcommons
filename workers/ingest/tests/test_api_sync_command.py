"""CLI completion status must preserve committed partial-progress evidence."""
from argparse import Namespace
from datetime import datetime, timedelta, timezone

import pytest

from billcommons_ingest import cli
from billcommons_ingest.api_sync import ApiSyncResult


@pytest.mark.parametrize("blocked,next_page", [(False, None), (True, None), (False, 11), (True, 11)])
def test_api_sync_command_commits_progress_but_reports_incompleteness(monkeypatch, capsys, blocked, next_page):
    calls = []

    class Database:
        def commit(self):
            calls.append("commit")

        def rollback(self):
            calls.append("rollback")

        def close(self):
            calls.append("close")

    db = Database()
    result = ApiSyncResult(state="AK", bills_updated=1, snapshot_blockers_remaining=blocked, next_page=next_page)
    monkeypatch.setattr(cli, "get_session", lambda: db)

    def run(received_db, state):
        assert received_db is db and state == "AK"
        calls.append("run")
        return result

    monkeypatch.setattr(cli.api_sync_mod, "run_api_sync_job", run)

    incomplete = blocked or next_page is not None
    assert cli.cmd_api_sync(Namespace(state="AK")) == int(incomplete)
    assert calls == ["run", "commit", "close"]
    output = capsys.readouterr().out
    assert "updated=1" in output
    assert ("INCOMPLETE" in output) is incomplete
    assert ("source pages remain" in output) is (next_page is not None)
    assert ("unresolved evidence snapshot blockers remain" in output) is blocked


@pytest.mark.parametrize("reason", ["ownership", "budget"])
@pytest.mark.parametrize("deferred", [True, False])
def test_sync_worker_bounds_deferrals_without_spending_attempts(monkeypatch, capsys, reason, deferred):
    calls = []

    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

        def __iter__(self):
            return iter(())

    class Database:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

        def commit(self):
            pass

        def rollback(self):
            calls.append("rollback")

        def close(self):
            pass

    job = Namespace(id="job", attempts=3, payload={"state": "AK"})

    def claim(*_args, **_kwargs):
        calls.append("claim")
        assert calls.count("claim") == 1, "max_jobs must bound deferred claims too"
        return job

    def run(*_args, **_kwargs):
        if reason == "ownership":
            raise cli.api_sync_mod.ApiSyncConcurrencyBusy("busy")
        raise cli.OpenStatesDailyBudgetExceeded("budget")

    started = datetime.now(timezone.utc)

    def defer(job_id, job_cls, *, claimed_attempts, run_after):
        assert calls[-1] == "rollback"
        assert job_id == job.id and claimed_attempts == 3
        if reason == "ownership":
            assert started + timedelta(seconds=30) <= run_after
            assert run_after <= datetime.now(timezone.utc) + timedelta(seconds=90)
        calls.append("defer")
        return deferred

    def forbidden(*_args, **_kwargs):
        pytest.fail("a deferred claim must not complete or spend a failure attempt")

    monkeypatch.setattr(cli, "get_session", Database)
    monkeypatch.setattr(cli.scheduler_mod, "run_schedule_pass", lambda _db: [])
    monkeypatch.setattr(cli.queue_mod, "claim_job", claim)
    monkeypatch.setattr(cli.queue_mod, "complete_job", forbidden)
    monkeypatch.setattr(cli, "record_job_failure", forbidden)
    monkeypatch.setattr(cli.api_sync_mod, "run_api_sync_job", run)
    monkeypatch.setattr(cli, "_defer_unclaimed_job", defer)
    monkeypatch.setattr(cli, "defer_job_for_budget", defer)
    assert cli.cmd_sync_worker(Namespace(worker_id="test", interval=0, max_jobs=1, once=True)) == 0
    assert calls == ["claim", "rollback", "defer"]
    output = capsys.readouterr().out
    assert "0 synced, 0 failed, 1 deferred" in output
    assert ("defer skipped" in output) is (not deferred)


def test_manual_sync_reports_busy_and_rolls_back(monkeypatch, capsys):
    calls = []

    class Database:
        def commit(self):
            pytest.fail("a busy sync must not commit")

        def rollback(self):
            calls.append("rollback")

        def close(self):
            calls.append("close")

    def busy(*_args, **_kwargs):
        raise cli.api_sync_mod.ApiSyncConcurrencyBusy("busy")

    monkeypatch.setattr(cli, "get_session", Database)
    monkeypatch.setattr(cli.api_sync_mod, "run_api_sync_job", busy)
    assert cli.cmd_api_sync(Namespace(state="AK")) == 1
    assert calls == ["rollback", "close"]
    output = capsys.readouterr()
    assert "another sync owns this jurisdiction; retry later" in output.out
    assert output.err == ""


@pytest.mark.parametrize("status,attempts,locked_by,expected", [
    ("queued", 2, None, True),
    ("running", 3, "other-worker", False),
    ("done", 3, None, False),
    ("queued", 3, None, False),
])
def test_ownership_deferral_preserves_concurrent_claim(status, attempts, locked_by, expected):
    original = datetime(2026, 9, 12, tzinfo=timezone.utc)
    delayed = original + timedelta(seconds=60)
    job = Namespace(status=status, attempts=attempts, locked_by=locked_by, run_after=original)
    calls = []

    class Database:
        def get(self, job_cls, job_id, *, with_for_update):
            assert with_for_update is True
            return job

        def commit(self):
            calls.append("commit")

        def rollback(self):
            calls.append("rollback")

        def close(self):
            calls.append("close")

    assert cli._defer_unclaimed_job(
        "job", object, claimed_attempts=3, run_after=delayed, session_factory=Database,
    ) is expected
    assert job.run_after == (delayed if expected else original)
    assert (job.status, job.attempts, job.locked_by) == (status, attempts, locked_by)
    assert calls == ["commit" if expected else "rollback", "close"]
