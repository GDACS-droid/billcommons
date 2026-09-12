"""CLI completion status must preserve committed partial-progress evidence."""
from argparse import Namespace

import pytest

from billcommons_ingest import cli
from billcommons_ingest.api_sync import ApiSyncResult


@pytest.mark.parametrize("blocked", [False, True])
def test_api_sync_command_commits_progress_but_reports_snapshot_blockers(monkeypatch, capsys, blocked):
    calls = []

    class Database:
        def commit(self):
            calls.append("commit")

        def rollback(self):
            calls.append("rollback")

        def close(self):
            calls.append("close")

    db = Database()
    result = ApiSyncResult(state="AK", bills_updated=1, snapshot_blockers_remaining=blocked)
    monkeypatch.setattr(cli, "get_session", lambda: db)

    def run(received_db, state):
        assert received_db is db and state == "AK"
        calls.append("run")
        return result

    monkeypatch.setattr(cli.api_sync_mod, "run_api_sync_job", run)

    assert cli.cmd_api_sync(Namespace(state="AK")) == int(blocked)
    assert calls == ["run", "commit", "close"]
    output = capsys.readouterr().out
    assert "updated=1" in output
    assert ("INCOMPLETE" in output) is blocked
