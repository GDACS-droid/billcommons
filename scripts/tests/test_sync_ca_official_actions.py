from __future__ import annotations

import hashlib
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace


scripts_directory = str(Path(__file__).resolve().parents[1])
if scripts_directory not in sys.path:
    sys.path.insert(0, scripts_directory)

import sync_ca_official_actions as sweep


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
    assert sweep.ACTION_UPDATE_CHUNK_SIZE == 500
    assert sweep._ACTION_UPDATE_FIELDS == (
        "order", "source_name", "source_url", "upstream_id", "retrieved_at",
        "raw_ref", "checksum", "parser_version",
    )


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
