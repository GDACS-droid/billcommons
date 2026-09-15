from __future__ import annotations

import hashlib
import csv
import io
import sys
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

scripts_directory = str(Path(__file__).resolve().parents[1])
if scripts_directory not in sys.path:
    sys.path.insert(0, scripts_directory)

import sync_ca_official_actions as sweep
from billcommons_shared.ca_official_actions import BILL_COLUMNS, HISTORY_COLUMNS


def _archive_bytes(description: str) -> bytes:
    fields = {
        "bill_id": "202520260AB1", "session_year": "20252026", "session_num": "0",
        "measure_type": "AB", "measure_num": "1", "bill_history_id": "10",
        "action_date": "2026-08-31", "action": description,
        "trans_update_dt": "2026-09-02", "action_sequence": "1",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, columns in (("BILL_TBL.dat", BILL_COLUMNS), ("BILL_HISTORY_TBL.dat", HISTORY_COLUMNS)):
            table = io.StringIO()
            csv.writer(table, delimiter="\t", quotechar="`", lineterminator="\n").writerow(
                [fields.get(column, "") for column in columns])
            archive.writestr(name, table.getvalue())
    return output.getvalue()


@pytest.mark.parametrize("replacement_mode", ["replace", "overwrite"])
def test_repair_parses_the_pinned_snapshot_after_source_path_changes(tmp_path, monkeypatch, replacement_mode):
    original = _archive_bytes("Introduced.")
    replacement = _archive_bytes("Passed.")
    path = tmp_path / "source.zip"
    path.write_bytes(original)
    loader = sweep.load_official_actions
    handles = []

    def replace_source_then_parse(snapshot):
        handles.append(snapshot)
        if replacement_mode == "replace":
            new_path = tmp_path / "replacement.zip"
            new_path.write_bytes(replacement)
            new_path.replace(path)
        else:
            path.write_bytes(replacement)
        return loader(snapshot)

    monkeypatch.setattr(sweep, "load_official_actions", replace_source_then_parse)
    parsed, digest = sweep._load_pinned_official_actions(path, hashlib.sha256(original).hexdigest())
    assert parsed["202520260AB1"][0].description == "Introduced."
    assert digest == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == replacement
    assert handles[0].closed


def test_invalid_pinned_archive_closes_snapshot_without_opening_database(tmp_path, monkeypatch):
    path = tmp_path / "invalid.zip"
    raw = b"not a ZIP"
    path.write_bytes(raw)
    loader = sweep.load_official_actions
    handles = []

    def capture(snapshot):
        handles.append(snapshot)
        return loader(snapshot)

    monkeypatch.setattr(sweep, "load_official_actions", capture)
    monkeypatch.setattr(sweep, "get_session", lambda: pytest.fail("database opened before archive validation"))
    with pytest.raises(sweep.OfficialActionSweepError):
        sweep.run(zip_path=path, expected_sha256=hashlib.sha256(raw).hexdigest(), apply=True)
    assert handles[0].closed


def _official(
    history_id: str,
    sequence: int,
    description: str,
    *,
    bill: str = "202520260AB1",
    action_date: date = date(2026, 8, 31),
) -> sweep.OfficialAction:
    return sweep.OfficialAction(bill, history_id, action_date, description, sequence, "2026-09-02")


def _local(
    identity: str,
    description: str,
    *,
    bill: str = "202520260AB1",
    order: int | None = None,
    action_date: date = date(2026, 8, 31),
) -> sweep.LocalAction:
    return sweep.LocalAction(identity, "bill-1", bill, action_date, description, None, order)


def test_plan_adds_missing_official_action_without_touching_existing_fact():
    first = _official("10", 1, "Introduced.")
    second = _official("11", 2, "Ordered to third reading.")
    plan = sweep.build_plan({first.official_bill_id: (first, second)}, [_local("local-1", first.description, order=1)])

    assert [item.history_id for item in plan.additions] == ["11"]
    assert plan.deletions == ()
    assert plan.missing_count == 1


def test_plan_preserves_official_duplicate_multiplicity():
    # The ledger can contain the same human text twice.  Two local copies are
    # therefore not a duplicate to delete; one local copy needs one addition.
    first = _official("10", 1, "Read first time.")
    second = _official("11", 2, "Read   first time.")
    official = {first.official_bill_id: (first, second)}

    retained = sweep.build_plan(official, [_local("a", first.description), _local("b", first.description)])
    assert retained.additions == ()
    assert retained.deletions == ()

    missing = sweep.build_plan(official, [_local("a", first.description)])
    assert [item.history_id for item in missing.additions] == ["11"]


def test_duplicate_official_actions_reconstruct_each_sequence_and_provenance():
    """AB 1546 shape: repeated normalized text is not an ambiguous order."""
    first = _official("1546-1", 41, "Read first time.")
    second = _official("1546-2", 42, "Read  first time.")

    def local(identity: str, order: int):
        return sweep.LocalAction(
            identity,
            "bill-1",
            first.official_bill_id,
            first.action_date,
            "Read first time.",
            None,
            order,
            SimpleNamespace(
                classification=None,
                order=order,
                source_name=None,
                source_url=None,
                upstream_id=None,
                retrieved_at=None,
                raw_ref=None,
                checksum=None,
                parser_version=None,
            ),
        )

    plan = sweep.build_plan({first.official_bill_id: (first, second)}, [local("a", 900), local("b", 901)])
    assert {(item.action_id, item.official.sequence) for item in plan.order_updates} == {("a", 42), ("b", 41)}

    mappings = sweep._action_update_mappings(
        plan, zip_sha256="a" * 64, now=datetime(2026, 9, 2, tzinfo=timezone.utc)
    )
    assert {(item["id"], item["order"], item["upstream_id"]) for item in mappings} == {
        ("a", 42, "ca-history:1546-2"),
        ("b", 41, "ca-history:1546-1"),
    }


def test_reloaded_duplicate_provenance_keeps_exact_official_sequence_pairing():
    """A second sweep must not swap AB 1546-style duplicate text rows."""
    first = _official("1546-1", 41, "Read first time.")
    second = _official("1546-2", 42, "Read first time.")

    def local(identity: str, official: sweep.OfficialAction, retrieved_at: datetime):
        provenance = sweep._action_provenance(official, "a" * 64, retrieved_at)
        row = SimpleNamespace(
            classification=None,
            order=official.sequence,
            **provenance,
        )
        return sweep.LocalAction(
            identity,
            "bill-1",
            first.official_bill_id,
            first.action_date,
            first.description,
            None,
            official.sequence,
            row,
        )

    # Deliberately reverse the retrieval ordering that chose the initial
    # first-pass survivors. Existing official history IDs, not that mutable
    # ordering, must determine the second-pass pairing.
    locals_after_first_apply = [
        local("a", first, datetime(2026, 9, 1, tzinfo=timezone.utc)),
        local("b", second, datetime(2026, 9, 2, tzinfo=timezone.utc)),
    ]
    plan = sweep.build_plan(
        {first.official_bill_id: (first, second)}, locals_after_first_apply
    )

    assert plan.order_updates == ()
    assert sweep._action_update_mappings(
        plan, zip_sha256="a" * 64, now=datetime(2026, 9, 3, tzinfo=timezone.utc)
    ) == []


def test_duplicate_addition_uses_unpaired_history_then_second_plan_is_zero():
    first = _official("1546-1", 41, "Read first time.")
    second = _official("1546-2", 42, "Read first time.")

    def local(identity: str, official: sweep.OfficialAction):
        provenance = sweep._action_provenance(
            official, "a" * 64, datetime(2026, 9, 2, tzinfo=timezone.utc)
        )
        return sweep.LocalAction(
            identity,
            "bill-1",
            first.official_bill_id,
            first.action_date,
            first.description,
            None,
            official.sequence,
            SimpleNamespace(classification=None, order=official.sequence, **provenance),
        )

    # The one existing copy was already proven to be history row 42. The
    # missing row is 41, not the positional tail 42.
    plan = sweep.build_plan({first.official_bill_id: (first, second)}, [local("second", second)])
    assert [item.history_id for item in plan.additions] == ["1546-1"]

    second_plan = sweep.build_plan(
        {first.official_bill_id: (first, second)},
        [local("second", second), local("first", first)],
    )
    assert second_plan.additions == ()
    assert second_plan.deletions == ()
    assert second_plan.order_updates == ()
    assert sweep._action_update_mappings(
        second_plan, zip_sha256="a" * 64, now=datetime(2026, 9, 3, tzinfo=timezone.utc)
    ) == []


def test_apply_plan_keeps_duplicate_history_classification_on_its_provenance_pair(monkeypatch):
    """A status repair must not FIFO-swap AB 1546-style duplicate rows.

    The pre-existing local copy is explicitly CA history 42 and carries a
    failure classification.  Applying the missing history 41 row must retain
    that classification on sequence 42; assigning it to the new row instead
    would falsely derive the wrong official chronology.
    """
    first = _official("1546-1", 41, "Read first time.")
    second = _official("1546-2", 42, "Read first time.")
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    second_provenance = sweep._action_provenance(second, "a" * 64, now)
    # Source URL deliberately differs so reconciliation invokes the bulk
    # update. The fake row then raises on any action attribute access after
    # that update, modeling SQLAlchemy expiration/lazy-load behavior.
    second_provenance["source_url"] = None

    class ExpiringRow:
        _EXPIRED_ATTRIBUTES = frozenset({
            "classification", "organization_id", "order", "source_name",
            "source_url", "upstream_id", "retrieved_at", "raw_ref",
            "checksum", "parser_version",
        })

        def __init__(self, **values):
            self.expired = False
            for name, value in values.items():
                setattr(self, name, value)

        def __getattribute__(self, name):
            if (
                name in object.__getattribute__(self, "_EXPIRED_ATTRIBUTES")
                and object.__getattribute__(self, "expired")
            ):
                raise AssertionError(f"unexpected post-update ORM access: {name}")
            return object.__getattribute__(self, name)

    existing_row = ExpiringRow(
        classification="failure",
        organization_id="committee-2",
        order=42,
        **second_provenance,
    )
    existing = sweep.LocalAction(
        "local-second",
        "bill-1",
        first.official_bill_id,
        first.action_date,
        first.description,
        "failure",
        42,
        existing_row,
    )
    plan = sweep.build_plan({first.official_bill_id: (first, second)}, [existing])
    assert [action.history_id for action in plan.additions] == ["1546-1"]

    class Db:
        def __init__(self):
            self.added: list[object] = []
            self.flushed = False

        def add(self, row):
            self.added.append(row)

        def flush(self):
            self.flushed = True

    db = Db()
    bill = SimpleNamespace(
        id="bill-1",
        identifier="AB 1",
        latest_action_date=None,
        latest_action_text=None,
        status=None,
        status_date=None,
        updated_at=None,
    )
    session = SimpleNamespace(
        identifier=sweep.REGULAR_SESSION_IDENTIFIER,
        classification="regular",
        end_date=date(2026, 12, 31),
        active=True,
    )
    captured: list[sweep.status.ActionRow] = []
    derive_status = sweep.status.derive_status

    def capture_then_derive(rows):
        captured.extend(rows)
        return derive_status(rows)

    monkeypatch.setattr(sweep.status, "derive_status", capture_then_derive)
    monkeypatch.setattr(sweep.events, "record_event", lambda *_args, **_kwargs: None)

    def expire_after_bulk_update(_db, mappings):
        assert len(mappings) == 1
        existing_row.expired = True
        return len(mappings)

    monkeypatch.setattr(sweep, "_execute_action_updates", expire_after_bulk_update)

    updates, touched = sweep._apply_plan(
        db,
        plan=plan,
        bill_by_id={bill.id: bill},
        session_by_bill_id={bill.id: session},
        zip_sha256="a" * 64,
        now=now,
    )

    assert updates == 0
    assert touched == 1
    assert db.flushed is True
    assert [(row.order, row.classification, row.organization_id) for row in captured] == [
        (41, None, None),
        (42, "failure", "committee-2"),
    ]
    assert bill.status == sweep.status.DEAD
    assert existing_row.expired is True

    added = next(row for row in db.added if getattr(row, "upstream_id", None) == "ca-history:1546-1")
    first_local = sweep.LocalAction(
        "local-first",
        bill.id,
        first.official_bill_id,
        first.action_date,
        first.description,
        added.classification,
        added.order,
        added,
    )
    existing_row.expired = False
    second_plan = sweep.build_plan(
        {first.official_bill_id: (first, second)}, [first_local, existing]
    )
    assert second_plan.additions == ()
    assert second_plan.deletions == ()
    assert second_plan.order_updates == ()


def test_apply_plan_snapshots_retained_metadata_and_mappings_before_delete_expires_rows(monkeypatch):
    """An excess delete cannot force a retained sibling's lazy ORM reload."""
    official = _official("h1", 1, "Read first time.")
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    retained_provenance = sweep._action_provenance(official, "a" * 64, now)
    retained_provenance["source_url"] = None  # force one provenance mapping

    class ExpiringRow:
        _EXPIRED_ATTRIBUTES = frozenset({
            "classification", "organization_id", "order", "source_name",
            "source_url", "upstream_id", "retrieved_at", "raw_ref",
            "checksum", "parser_version",
        })

        def __init__(self, **values):
            self.expired = False
            for name, value in values.items():
                setattr(self, name, value)

        def __getattribute__(self, name):
            if (
                name in object.__getattribute__(self, "_EXPIRED_ATTRIBUTES")
                and object.__getattribute__(self, "expired")
            ):
                raise AssertionError(f"unexpected post-delete ORM access: {name}")
            return object.__getattribute__(self, name)

    retained_row = ExpiringRow(
        classification="failure",
        organization_id="committee-1",
        order=1,
        **retained_provenance,
    )
    retained = sweep.LocalAction(
        "retained",
        "bill-1",
        official.official_bill_id,
        official.action_date,
        official.description,
        "failure",
        1,
        retained_row,
    )
    excess = sweep.LocalAction(
        "excess",
        "bill-1",
        official.official_bill_id,
        official.action_date,
        official.description,
        None,
        None,
        SimpleNamespace(classification=None, organization_id=None),
    )
    plan = sweep.build_plan({official.official_bill_id: (official,)}, [retained, excess])
    assert [local.id for local in plan.deletions] == ["excess"]

    class Db:
        def __init__(self):
            self.flushed = False

        def execute(self, _statement):
            retained_row.expired = True
            return SimpleNamespace(rowcount=1)

        def add(self, _row):
            raise AssertionError("no official action should be added")

        def flush(self):
            self.flushed = True

    db = Db()
    bill = SimpleNamespace(
        id="bill-1",
        identifier="AB 1",
        latest_action_date=None,
        latest_action_text=None,
        status=None,
        status_date=None,
        updated_at=None,
    )
    session = SimpleNamespace(
        identifier=sweep.REGULAR_SESSION_IDENTIFIER,
        classification="regular",
        end_date=date(2026, 12, 31),
        active=True,
    )
    captured: list[sweep.status.ActionRow] = []
    derive_status = sweep.status.derive_status

    def capture_then_derive(rows):
        captured.extend(rows)
        return derive_status(rows)

    mapped_ids: list[object] = []

    def apply_snapshot_mappings(_db, mappings):
        mapped_ids.extend(mapping["id"] for mapping in mappings)
        return len(mappings)

    monkeypatch.setattr(sweep.status, "derive_status", capture_then_derive)
    monkeypatch.setattr(sweep.events, "record_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sweep, "_execute_action_updates", apply_snapshot_mappings)

    updates, touched = sweep._apply_plan(
        db,
        plan=plan,
        bill_by_id={bill.id: bill},
        session_by_bill_id={bill.id: session},
        zip_sha256="a" * 64,
        now=now,
    )

    assert updates == 0
    assert touched == 1
    assert db.flushed is True
    assert retained_row.expired is True
    assert mapped_ids == ["retained"]
    assert [(row.order, row.classification, row.organization_id) for row in captured] == [
        (1, "failure", "committee-1"),
    ]
    assert bill.status == sweep.status.DEAD


def test_plan_deletes_only_excess_copy_of_officially_proven_fact_and_preserves_unsupported():
    official = _official("10", 1, "Introduced.")
    plan = sweep.build_plan(
        {official.official_bill_id: (official,)},
        [_local("a", official.description), _local("b", official.description), _local("unsupported", "Local clerk note")],
    )

    assert len(plan.deletions) == 1
    assert plan.deletions[0].id in {"a", "b"}
    assert plan.unsupported_local_singletons == 1
    assert all(item.description != "Local clerk note" for item in plan.deletions)


def test_plan_reconciles_only_unambiguous_order_and_official_latest_is_sequence_ordered():
    early = _official("10", 1, "Introduced.", action_date=date(2026, 1, 1))
    latest = _official("11", 7, "Concurred in amendments.", action_date=date(2026, 8, 31))
    plan = sweep.build_plan(
        {early.official_bill_id: (latest, early)},
        [_local("a", early.description, order=99, action_date=early.action_date), _local("b", latest.description, order=1, action_date=latest.action_date)],
    )

    assert {(item.action_id, item.official.sequence) for item in plan.order_updates} == {("a", 1), ("b", 7)}
    assert sweep._latest(plan.official_by_bill[early.official_bill_id]) == latest


def test_bulk_action_mappings_preserve_pairing_and_only_change_needed_fields():
    official = _official("10", 7, "Introduced.")
    local = sweep.LocalAction(
        "local-1",
        "bill-1",
        official.official_bill_id,
        official.action_date,
        official.description,
        None,
        1,
        SimpleNamespace(
            classification=None,
            source_name=None,
            source_url=None,
            upstream_id=None,
            retrieved_at=None,
            raw_ref=None,
            checksum=None,
            parser_version=None,
        ),
    )
    plan = sweep.build_plan({official.official_bill_id: (official,)}, [local])

    mappings = sweep._action_update_mappings(
        plan,
        zip_sha256="a" * 64,
        now=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )

    assert len(mappings) == 1
    assert mappings[0]["id"] == "local-1"
    assert mappings[0]["order"] == 7
    assert mappings[0]["source_name"] == sweep.SOURCE_NAME
    assert mappings[0]["upstream_id"] == "ca-history:10"
    assert "classification" not in mappings[0]
    assert "organization_id" not in mappings[0]


def test_action_update_chunks_are_parameter_bounded_and_keep_the_full_update_contract():
    assert sweep.ACTION_UPDATE_CHUNK_SIZE == 5_000
    assert sweep._ACTION_UPDATE_FIELDS == (
        "order", "source_name", "source_url", "upstream_id", "retrieved_at",
        "raw_ref", "checksum", "parser_version",
    )
    assert sweep._ACTION_UPDATE_BIND_PARAMETERS_PER_ROW == 1 + len(sweep._ACTION_UPDATE_FIELDS)
    assert (
        sweep.ACTION_UPDATE_CHUNK_SIZE * sweep._ACTION_UPDATE_BIND_PARAMETERS_PER_ROW
        < sweep.POSTGRES_BIND_PARAMETER_LIMIT
    )


def test_sixty_thousand_and_one_updates_dispatch_once_each_in_parameter_safe_chunks(monkeypatch):
    """A first CA provenance pass must not make 120 proxy round trips."""
    mappings = [{"id": index} for index in range(60_001)]
    dispatched: list[list[int]] = []

    def execute_chunk(_db, chunk):
        dispatched.append([mapping["id"] for mapping in chunk])
        return len(chunk)

    monkeypatch.setattr(sweep, "_execute_action_update_chunk", execute_chunk)

    assert sweep._execute_action_updates(object(), mappings) == 60_001
    assert [len(chunk) for chunk in dispatched] == [5_000] * 12 + [1]
    assert [mapping_id for chunk in dispatched for mapping_id in chunk] == list(range(60_001))


def test_exact_five_thousand_row_values_update_compiles_below_postgres_bind_limit():
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    mappings = [
        {
            "id": uuid4(),
            "order": index,
            "source_name": sweep.SOURCE_NAME,
            "source_url": sweep.OFFICIAL_SOURCE_URL,
            "upstream_id": f"ca-history:{index}",
            "retrieved_at": now,
            "raw_ref": f"raw:{index}",
            "checksum": f"{index:064x}",
            "parser_version": sweep.PARSER_VERSION,
        }
        for index in range(sweep.ACTION_UPDATE_CHUNK_SIZE)
    ]

    class Db:
        statement = None

        def execute(self, statement):
            self.statement = statement
            return SimpleNamespace(rowcount=len(mappings))

    db = Db()
    assert sweep._execute_action_update_chunk(db, mappings) == len(mappings)
    compiled = db.statement.compile(dialect=postgresql.dialect())
    assert "UPDATE bill_actions SET" in str(compiled)
    assert "FROM (VALUES" in str(compiled)
    assert len(compiled.params) == 45_000
    assert len(compiled.params) < sweep.POSTGRES_BIND_PARAMETER_LIMIT


def test_action_update_chunk_rejects_a_partial_database_rowcount():
    mapping = {
        "id": uuid4(),
        "order": 1,
        "source_name": sweep.SOURCE_NAME,
        "source_url": sweep.OFFICIAL_SOURCE_URL,
        "upstream_id": "ca-history:1",
        "retrieved_at": datetime(2026, 9, 2, tzinfo=timezone.utc),
        "raw_ref": "raw:1",
        "checksum": "a" * 64,
        "parser_version": sweep.PARSER_VERSION,
    }

    class Db:
        def execute(self, _statement):
            return SimpleNamespace(rowcount=0)

    with pytest.raises(sweep.OfficialActionSweepError, match="expected 1"):
        sweep._execute_action_update_chunk(Db(), [mapping])


def test_bulk_action_update_requires_every_target_row_to_match():
    sweep._require_exact_rowcount(500, 500, operation="bulk action update")

    try:
        sweep._require_exact_rowcount(499, 500, operation="bulk action update")
    except sweep.OfficialActionSweepError as exc:
        assert "499" in str(exc)
        assert "500" in str(exc)
    else:  # pragma: no cover - assertion reads better than pytest.raises here.
        raise AssertionError("partial bulk update was accepted")


def test_same_pinned_provenance_keeps_original_retrieval_timestamp():
    official = _official("10", 7, "Introduced.")
    original = datetime(2026, 9, 2, tzinfo=timezone.utc)
    provenance = sweep._action_provenance(official, "a" * 64, original)
    row = SimpleNamespace(classification=None, order=7, **provenance)
    local = sweep.LocalAction(
        "local-1", "bill-1", official.official_bill_id, official.action_date,
        official.description, None, 7, row,
    )
    plan = sweep.build_plan({official.official_bill_id: (official,)}, [local])

    assert sweep._action_update_mappings(
        plan, zip_sha256="a" * 64, now=datetime(2026, 9, 3, tzinfo=timezone.utc)
    ) == []


def test_run_is_dry_by_default_and_rolls_back_without_mutating(monkeypatch, tmp_path: Path):
    zip_path = tmp_path / "pinned.zip"
    zip_path.write_bytes(b"not opened because the official loader is isolated here")
    official = _official("10", 1, "Introduced.")
    plan_local = [_local("a", official.description, order=1)]

    class Db:
        rolled_back = False
        committed = False
        closed = False
        writes: list[object] = []

        def execute(self, *_args, **_kwargs):
            return None

        def rollback(self):
            self.rolled_back = True

        def commit(self):
            self.committed = True

        def close(self):
            self.closed = True

        def add(self, value):
            self.writes.append(value)

        def delete(self, value):
            self.writes.append(value)

    db = Db()
    monkeypatch.setattr(sweep, "get_session", lambda: db)
    monkeypatch.setattr(sweep, "load_official_actions", lambda _path: {official.official_bill_id: (official,)})
    monkeypatch.setattr(sweep, "_load_local_actions", lambda *_args: ({}, plan_local, {}))

    result = sweep.run(
        zip_path=zip_path,
        expected_sha256=hashlib.sha256(zip_path.read_bytes()).hexdigest(),
        apply=False,
    )

    assert result["applied"] is False
    assert set(result["timing_seconds"]) == {
        "lock_acquisition",
        "load_local_actions",
        "build_plan",
        "total_transaction",
    }
    assert all(value >= 0 for value in result["timing_seconds"].values())
    assert db.rolled_back is True
    assert db.committed is False
    assert db.writes == []
    assert db.closed is True


def test_run_rejects_a_nonmatching_zip_hash_before_opening_database(monkeypatch, tmp_path: Path):
    zip_path = tmp_path / "wrong.zip"
    zip_path.write_bytes(b"pinned bytes")
    monkeypatch.setattr(
        sweep,
        "get_session",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )

    try:
        sweep.run(zip_path=zip_path, expected_sha256="0" * 64, apply=False)
    except sweep.OfficialActionSweepError as exc:
        assert "SHA-256" in str(exc)
    else:  # pragma: no cover - assertion reads better than pytest.raises here.
        raise AssertionError("mismatched pinned archive was accepted")


def test_run_apply_failure_rolls_back_the_outer_transaction(monkeypatch, tmp_path: Path):
    zip_path = tmp_path / "pinned.zip"
    zip_path.write_bytes(b"pinned bytes")
    official = _official("10", 1, "Introduced.")
    plan_local = [_local("a", official.description, order=1)]

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
    monkeypatch.setattr(sweep, "get_session", lambda: db)
    monkeypatch.setattr(sweep, "load_official_actions", lambda _path: {official.official_bill_id: (official,)})
    monkeypatch.setattr(sweep, "_load_local_actions", lambda *_args: ({}, plan_local, {}))

    def fail_apply(*_args, **_kwargs):
        raise sweep.OfficialActionSweepError("simulated bulk update failure")

    monkeypatch.setattr(sweep, "_apply_plan", fail_apply)

    with pytest.raises(sweep.OfficialActionSweepError, match="simulated bulk update failure"):
        sweep.run(
            zip_path=zip_path,
            expected_sha256=hashlib.sha256(zip_path.read_bytes()).hexdigest(),
            apply=True,
        )

    assert db.rolled_back is True
    assert db.committed is False
    assert db.closed is True
