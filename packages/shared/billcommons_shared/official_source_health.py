"""Bounded operational health of retained official-source observations.

These states describe observation work, never statewide legislative freshness.
No source request, replay, or database mutation occurs during collection.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import bindparam, text


MAX_OFFICIAL_HEALTH_TARGETS = 1000
OFFICIAL_TARGET_STATES = (
    "observed", "failed", "observation_overdue", "not_observed", "disabled",
    "changed_target", "future_observation",
)


@dataclass(frozen=True)
class OfficialTargetHealth:
    target_id: str
    observation_id: str | None
    state: str
    retrieved_at: datetime | None
    upstream_updated_at: datetime | None
    next_check_at: datetime


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("official observation health requires timezone-aware timestamps")
    return value.astimezone(timezone.utc)


def target_health(row: Mapping[str, Any], *, now: datetime) -> OfficialTargetHealth:
    """Classify one target and its latest recorded observation."""
    now = _utc(now)
    retrieved_at = _utc(row["retrieved_at"]) if row["retrieved_at"] is not None else None
    upstream_updated_at = _utc(row["upstream_updated_at"]) if row["upstream_updated_at"] is not None else None
    if not row["enabled"]:
        state = "disabled"
    elif row["observation_id"] is None:
        state = "not_observed"
    elif retrieved_at is None:
        raise ValueError("retained observation is missing its retrieval timestamp")
    elif retrieved_at > now + timedelta(minutes=5):
        state = "future_observation"
    elif (row["adapter_name"], row["source_url"]) != (row["observed_adapter_name"], row["observed_source_url"]):
        state = "changed_target"
    elif row["status"] != "succeeded":
        state = "failed"
    elif (now - retrieved_at).total_seconds() > row["cadence_seconds"]:
        state = "observation_overdue"
    else:
        state = "observed"
    return OfficialTargetHealth(
        target_id=str(row["target_id"]),
        observation_id=str(row["observation_id"]) if row["observation_id"] is not None else None,
        state=state, retrieved_at=retrieved_at, upstream_updated_at=upstream_updated_at,
        next_check_at=_utc(row["next_check_at"]),
    )


def collect_official_source_health(
    db, *, jurisdiction_ids: tuple, now: datetime,
) -> dict[Any, tuple[OfficialTargetHealth, ...]]:
    """Load at most 1,000 targets and one latest observation per target.

    Exceeding the bound fails collection visibly; it never reports a truncated
    target inventory as complete. The caller owns read-only transaction policy.
    """
    if not jurisdiction_ids:
        return {}
    statement = text("""
        SELECT t.jurisdiction_id, t.id AS target_id, t.adapter_name,
               t.source_url, t.enabled, t.cadence_seconds, t.next_check_at,
               o.id AS observation_id, o.adapter_name AS observed_adapter_name,
               o.source_url AS observed_source_url, o.retrieved_at,
               o.upstream_updated_at, o.status
          FROM official_source_targets t
          LEFT JOIN LATERAL (
              SELECT id, adapter_name, source_url, retrieved_at,
                     upstream_updated_at, status
                FROM official_source_observations
               WHERE target_id = t.id
               ORDER BY retrieved_at DESC, created_at DESC, id DESC
               LIMIT 1
          ) o ON true
         WHERE t.jurisdiction_id IN :jurisdiction_ids
         ORDER BY t.jurisdiction_id, t.id
         LIMIT :row_limit
    """).bindparams(bindparam("jurisdiction_ids", expanding=True))
    rows = db.execute(statement, {
        "jurisdiction_ids": jurisdiction_ids,
        "row_limit": MAX_OFFICIAL_HEALTH_TARGETS + 1,
    }).mappings().all()
    if len(rows) > MAX_OFFICIAL_HEALTH_TARGETS:
        raise ValueError("official source health target inventory exceeds its bound")
    result = defaultdict(list)
    for row in rows:
        result[row["jurisdiction_id"]].append(target_health(row, now=now))
    return {key: tuple(value) for key, value in result.items()}
