from billcommons_ingest import data_health


def test_connection_failure_is_controlled_and_redacted(monkeypatch, capsys):
    def fail():
        raise RuntimeError("private-connection-value")

    monkeypatch.setattr(data_health, "get_session", fail)
    assert data_health.main(["--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "[CHECK-FAILED] RuntimeError\n"
    assert "private-connection-value" not in captured.err


def test_quiet_failure_is_silent(monkeypatch, capsys):
    monkeypatch.setattr(data_health, "get_session", lambda: (_ for _ in ()).throw(RuntimeError("private")))
    assert data_health.main(["--quiet"]) == 2
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_query_failure_closes_read_only_session(monkeypatch, capsys):
    statements = []

    class Session:
        def execute(self, stmt):
            statements.append(str(stmt))

        def close(self):
            statements.append("close")

    def fail(db):
        raise RuntimeError("private-query-value")

    monkeypatch.setattr(data_health, "get_session", Session)
    monkeypatch.setattr(data_health, "collect_report", fail)
    assert data_health.main([]) == 2
    assert statements == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        "SET LOCAL statement_timeout = '5000ms'", "close",
    ]
    assert "private-query-value" not in capsys.readouterr().err
