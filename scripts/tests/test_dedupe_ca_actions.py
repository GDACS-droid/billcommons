from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path


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
