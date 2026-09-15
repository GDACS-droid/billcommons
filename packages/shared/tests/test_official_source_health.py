"""Deterministic operational classification without source requests."""
from datetime import datetime, timedelta, timezone

import pytest

from billcommons_shared.official_source_health import target_health, collect_official_source_health


NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def _row(**overrides):
    return dict({
        "target_id": "target", "observation_id": "observation",
        "adapter_name": "adapter", "observed_adapter_name": "adapter",
        "source_url": "https://example.invalid/source", "observed_source_url": "https://example.invalid/source",
        "enabled": True, "status": "succeeded", "cadence_seconds": 3600,
        "retrieved_at": NOW - timedelta(minutes=10), "upstream_updated_at": NOW - timedelta(days=30),
        "next_check_at": NOW + timedelta(minutes=50),
    }, **overrides)


@pytest.mark.parametrize("changes,expected", [
    ({}, "observed"),
    ({"enabled": False, "status": "failed"}, "disabled"),
    ({"observation_id": None, "retrieved_at": None}, "not_observed"),
    ({"status": "failed"}, "failed"),
    ({"status": "invalid"}, "failed"),
    ({"retrieved_at": NOW - timedelta(hours=1)}, "observed"),
    ({"retrieved_at": NOW - timedelta(hours=1, seconds=1)}, "observation_overdue"),
    ({"retrieved_at": NOW + timedelta(minutes=5)}, "observed"),
    ({"retrieved_at": NOW + timedelta(minutes=5, seconds=1)}, "future_observation"),
    ({"observed_source_url": "https://example.invalid/old"}, "changed_target"),
    ({"observed_adapter_name": "old"}, "changed_target"),
    ({"observed_adapter_name": "old", "retrieved_at": NOW + timedelta(minutes=6)}, "future_observation"),
    ({"observed_source_url": "https://example.invalid/old", "retrieved_at": NOW + timedelta(minutes=6)}, "future_observation"),
    ({"status": "failed", "next_check_at": NOW + timedelta(days=1)}, "failed"),
])
def test_operational_states(changes, expected):
    result = target_health(_row(**changes), now=NOW)
    assert result.state == expected
    assert result.upstream_updated_at == NOW - timedelta(days=30)


def test_ambiguous_naive_timestamp_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        target_health(_row(retrieved_at=NOW.replace(tzinfo=None)), now=NOW)


def test_empty_scope_never_queries_database():
    assert collect_official_source_health(None, jurisdiction_ids=(), now=NOW) == {}
