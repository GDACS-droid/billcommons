from __future__ import annotations

import sys
import csv
import io
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


scripts_directory = str(Path(__file__).resolve().parents[1])
if scripts_directory not in sys.path:
    sys.path.insert(0, scripts_directory)

import dedupe_ca_actions as dedupe


class Action:
    def __init__(self, identity, *, classification=None, source_name=None, retrieved_at=None, order=None):
        self.id = identity
        self.classification = classification
        self.source_name = source_name
        self.retrieved_at = retrieved_at
        self.order = order


def test_choose_excess_preserves_official_multiplicity_and_best_rows():
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    current = datetime(2026, 2, 1, tzinfo=timezone.utc)
    rows = [
        Action("unclassified", source_name="openstates_bulk_csv", retrieved_at=old),
        Action(
            "current-api",
            classification="passage",
            source_name="openstates_api_sync",
            retrieved_at=current,
            order=7,
        ),
        Action("classified-bulk", classification="passage", source_name="openstates_bulk_csv", retrieved_at=old),
    ]

    assert [row.id for row in dedupe.choose_excess(rows, 2)] == ["unclassified"]
    assert dedupe.choose_excess(rows, 3) == []


def test_choose_excess_never_deletes_a_fact_absent_from_official_snapshot():
    rows = [Action("a"), Action("b")]
    assert dedupe.choose_excess(rows, 0) == []


def test_dedupe_action_projection_excludes_large_parent_and_provenance_columns():
    names = {column.key for column in dedupe.ACTION_READ_COLUMNS}
    assert names == {
        "id", "bill_id", "description", "action_date", "classification",
        "source_name", "retrieved_at", "order",
    }
    assert "search_tsv" not in names
    assert "raw_ref" not in names


def test_planned_deletion_revalidation_rejects_changed_action_identity():
    planned = dedupe.PlannedDeletion(
        action_id="action-1",
        bill_id="bill-1",
        official_bill_id="202520260AB1",
        action_date="2026-08-31",
        description="Read first time.",
    )
    matching = SimpleNamespace(
        id="action-1", bill_id="bill-1", action_date=date(2026, 8, 31), description="Read  first time."
    )
    changed = SimpleNamespace(
        id="action-1", bill_id="bill-1", action_date=date(2026, 8, 31), description="Different action."
    )

    assert dedupe._matches_planned_deletion(matching, planned) is True
    assert dedupe._matches_planned_deletion(changed, planned) is False


def test_planned_deletion_loader_acquires_row_locks_before_validation():
    class Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class Db:
        statement = None

        def execute(self, statement):
            self.statement = statement
            return Result()

    db = Db()
    assert dedupe._load_planned_actions_for_update(db, ["action-1"]) == []
    assert db.statement._for_update_arg is not None  # noqa: SLF001 - SQLAlchemy contract inspection
    assert db.statement._order_by_clauses  # noqa: SLF001 - deterministic lock order


def test_affected_set_loader_locks_parent_bills_before_every_action_row():
    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    class Db:
        statements = None

        def __init__(self):
            self.statements = []

        def execute(self, statement):
            self.statements.append(statement)
            return Result(["bill-1"] if len(self.statements) == 1 else [])

    db = Db()
    assert dedupe._lock_affected_bills_and_actions(db, ["bill-1"]) == []
    assert len(db.statements) == 2
    assert db.statements[0]._for_update_arg is not None  # noqa: SLF001
    assert db.statements[1]._for_update_arg is not None  # noqa: SLF001
    assert db.statements[0]._order_by_clauses  # noqa: SLF001
    assert db.statements[1]._order_by_clauses  # noqa: SLF001
    assert "FROM bills" in str(db.statements[0])
    assert "FROM bill_actions" in str(db.statements[1])


def test_missing_official_multiplicity_detects_retained_row_race():
    action = ("202520260AB1", "2026-08-31", "Read first time.")
    assert dedupe._missing_official_multiplicity({action: 2}, {action: 1}) == 1
    assert dedupe._missing_official_multiplicity({action: 2}, {action: 2}) == 0


def test_replanned_deletion_outside_locked_bill_scope_refuses_and_rolls_back(monkeypatch, tmp_path: Path):
    initial = dedupe.PlannedDeletion("action-1", "bill-1", "202520260AB1", "2026-08-31", "Read first time.")
    expanded = dedupe.PlannedDeletion("action-2", "bill-2", "202520260AB2", "2026-08-31", "Read first time.")

    class Db:
        rolled_back = False
        committed = False
        closed = False

        def execute(self, *_args, **_kwargs):
            return None

        def rollback(self):
            self.rolled_back = True

        def commit(self):
            self.committed = True

        def close(self):
            self.closed = True

    db = Db()
    plans = iter(
        [
            ([initial], {"planned_deletions": 1}),
            ([expanded], {"planned_deletions": 1}),
        ]
    )
    monkeypatch.setattr(dedupe, "_sha256", lambda _path: "pinned")
    monkeypatch.setattr(dedupe, "get_session", lambda: db)
    monkeypatch.setattr(dedupe, "plan", lambda *_args: next(plans))
    monkeypatch.setattr(dedupe, "_lock_affected_bills_and_actions", lambda *_args: [])

    with pytest.raises(dedupe.DedupeError, match="scope expanded"):
        dedupe.run(zip_path=tmp_path / "unused.zip", expected_sha256="pinned", apply=True)

    assert db.rolled_back is True
    assert db.committed is False
    assert db.closed is True


def test_official_counts_ignores_history_orphan_absent_from_bill_table(tmp_path: Path):
    archive_path = tmp_path / "official.zip"
    bill_row = ["202520260AB1"] + [""] * 18
    history_row = ["202520260AB1", "100", "2026-08-31", "Read first time."] + [""] * 9
    orphan_row = ["retained-history-only", "101", "2026-08-31", "Must not be counted."] + [""] * 9
    with zipfile.ZipFile(archive_path, "w") as archive:
        bill_stream = io.StringIO()
        history_stream = io.StringIO()
        csv.writer(bill_stream, delimiter="\t", quotechar="`", lineterminator="\n").writerow(bill_row)
        writer = csv.writer(history_stream, delimiter="\t", quotechar="`", lineterminator="\n")
        writer.writerow(history_row)
        writer.writerow(orphan_row)
        archive.writestr("BILL_TBL.dat", bill_stream.getvalue())
        archive.writestr("BILL_HISTORY_TBL.dat", history_stream.getvalue())

    counts, bill_ids = dedupe._official_counts(archive_path)

    assert bill_ids == {"202520260AB1"}
    assert counts == {("202520260AB1", "2026-08-31", "Read first time."): 1}
