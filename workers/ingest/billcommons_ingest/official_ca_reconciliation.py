"""Pure, bounded content comparison for California official action evidence.

CA's ``bill_history_id`` and ``action_sequence`` can change across weekday
archives.  They are retained in event evidence, but are never used here as
cross-snapshot occurrence identities.  This comparator reports agreement of
content *multisets* only; it does not create a pairing claim or a mutation.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from billcommons_shared.reconciliation import (
    MAX_EVENT_BYTES,
    MAX_EVENTS_PER_SIDE,
    MAX_REPORT_BYTES,
    ReconciliationInputError,
)

COMPARATOR_VERSION = "ca-action-content-multiset/1"
CA_JURISDICTION = "ca"


@dataclass(frozen=True)
class _Event:
    raw: Mapping[str, Any]
    scope: tuple[str, str, str]
    date: str | None
    description: str | None
    sort_key: str

    @property
    def content_key(self) -> tuple[str, str, str] | None:
        if self.date is None or self.description is None:
            return None
        return (self.scope[2], self.date, self.description)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _normalized_string(raw: Mapping[str, Any], field: str, *, required: bool = False, fold_case: bool = True) -> str | None:
    value = raw.get(field)
    if value is None:
        if required:
            raise ReconciliationInputError(f"CA event field {field!r} is required for scope")
        return None
    if not isinstance(value, str):
        raise ReconciliationInputError(f"CA event field {field!r} must be a string or null")
    normalized = " ".join(value.split())
    if fold_case:
        normalized = normalized.casefold()
    if not normalized and required:
        raise ReconciliationInputError(f"CA event field {field!r} is required for scope")
    return normalized or None


def _exact_day(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("date")
    precision = raw.get("date_precision")
    if precision is not None and (not isinstance(precision, str) or precision not in {"day", "month", "year", "unknown"}):
        raise ReconciliationInputError("date_precision must be day, month, year, unknown, or null")
    if value is None or value == "":
        if precision not in {None, "unknown"}:
            raise ReconciliationInputError("a date_precision requires a date value")
        return None
    if not isinstance(value, str):
        raise ReconciliationInputError("CA event field 'date' must be an ISO string or null")
    if precision not in {None, "day"} or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if value == parsed.isoformat() else None


def _event(raw: Any) -> _Event:
    if not isinstance(raw, dict):
        raise ReconciliationInputError("every CA event must be a JSON object")
    try:
        size = len(_canonical_json(raw).encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ReconciliationInputError("CA event must contain JSON-compatible evidence") from exc
    if size > MAX_EVENT_BYTES:
        raise ReconciliationInputError(f"a CA event exceeds the {MAX_EVENT_BYTES}-byte evidence safety cap")

    jurisdiction = _normalized_string(raw, "jurisdiction", required=True)
    if jurisdiction != CA_JURISDICTION:
        raise ReconciliationInputError("CA content comparison requires jurisdiction 'CA'")
    scope = (jurisdiction, _normalized_string(raw, "session", required=True, fold_case=False), _normalized_string(raw, "bill_id", required=True, fold_case=False))
    description = _normalized_string(raw, "description")
    text = _normalized_string(raw, "text")
    if description is not None and text is not None and description != text:
        raise ReconciliationInputError("description and text disagree in one CA event")
    return _Event(raw=raw, scope=scope, date=_exact_day(raw), description=description or text, sort_key=_canonical_json(raw))


def _events(fixture: Any, side: str) -> list[_Event]:
    source = fixture.get("events") if isinstance(fixture, dict) else fixture
    if not isinstance(source, list):
        raise ReconciliationInputError(f"{side} fixture must be an event array or an object with an 'events' array")
    if len(source) > MAX_EVENTS_PER_SIDE:
        raise ReconciliationInputError(f"{side} fixture exceeds the {MAX_EVENTS_PER_SIDE}-event safety cap")
    return [_event(raw) for raw in source]


def _evidence(event: _Event) -> dict[str, Any]:
    return {
        "content_evidence": {
            "jurisdiction": event.scope[0].upper(), "session": event.scope[1], "bill_id": event.scope[2],
            "date": event.date, "description": event.description,
        },
        "original_evidence": event.raw,
    }


def _content(key: tuple[str, str, str]) -> dict[str, str]:
    return {"bill_id": key[0], "date": key[1], "description": key[2]}


def _ambiguous_reason(event: _Event) -> str:
    if event.date is None and event.description is None:
        return "missing_exact_day_and_description"
    return "missing_exact_day" if event.date is None else "missing_description"


def reconcile_ca_action_content(official_fixture: Any, local_fixture: Any, *, scope: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Compare one CA bill/session's action-content multisets without matching occurrences.

    A record joins a content multiset only with an exact ISO day and a nonblank
    whitespace/case-folded description.  Source IDs and sequences remain only
    in ``original_evidence``.  A matching multiset proves content agreement,
    never that two records are the same historical occurrence.
    """
    official, local = _events(official_fixture, "official"), _events(local_fixture, "local")
    scopes = {event.scope for event in official + local}
    if scope is not None:
        scopes.add(_event(dict(scope)).scope)
    if not scopes:
        raise ReconciliationInputError("CA content comparison requires scope evidence")
    if len(scopes) != 1:
        raise ReconciliationInputError("CA content comparison rejects mixed jurisdiction, session, or bill scope evidence")
    scope = next(iter(scopes))
    official_groups: dict[tuple[str, str, str], list[_Event]] = defaultdict(list)
    local_groups: dict[tuple[str, str, str], list[_Event]] = defaultdict(list)
    ambiguous: list[dict[str, Any]] = []
    for side, events, groups in (("official", official, official_groups), ("local", local, local_groups)):
        for event in events:
            if event.content_key is None:
                ambiguous.append({"side": side, "reason": _ambiguous_reason(event), "evidence": _evidence(event)})
            else:
                groups[event.content_key].append(event)

    agreement, official_only, local_only = [], [], []
    for key in sorted(set(official_groups) | set(local_groups)):
        official_events = sorted(official_groups.get(key, ()), key=lambda event: event.sort_key)
        local_events = sorted(local_groups.get(key, ()), key=lambda event: event.sort_key)
        if len(official_events) == len(local_events) and official_events:
            agreement.append({
                "content": _content(key),
                "agreement": "unique_content_agreement" if len(official_events) == 1 else "repeated_content_multiset_agreement",
                "official_count": len(official_events), "local_count": len(local_events),
                "occurrence_proof": False,
                "official_evidence": [_evidence(event) for event in official_events],
                "local_evidence": [_evidence(event) for event in local_events],
            })
        elif len(official_events) > len(local_events):
            official_only.append({"content": _content(key), "official_count": len(official_events), "local_count": len(local_events),
                                  "unmatched_count": len(official_events) - len(local_events),
                                  "official_evidence": [_evidence(event) for event in official_events],
                                  "local_evidence": [_evidence(event) for event in local_events]})
        else:
            local_only.append({"content": _content(key), "official_count": len(official_events), "local_count": len(local_events),
                               "unmatched_count": len(local_events) - len(official_events),
                               "official_evidence": [_evidence(event) for event in official_events],
                               "local_evidence": [_evidence(event) for event in local_events]})

    report = {
        "schema_version": 1,
        "comparator_version": COMPARATOR_VERSION,
        "interpretation": "Counts compare retained records, not identified occurrences. Content agreement is not occurrence proof; local-only content does not imply deletion, and official-only content does not authorize insertion.",
        "scope": {"jurisdiction": scope[0].upper(), "session": scope[1], "bill_id": scope[2]},
        "summary": {"official_records": len(official), "local_records": len(local), "content_agreement": len(agreement),
                    "official_only_content": len(official_only), "local_only_content": len(local_only),
                    "ambiguous_insufficient_evidence": len(ambiguous)},
        "content_agreement": agreement,
        "official_only_content": official_only,
        "local_only_content": local_only,
        "ambiguous_insufficient_evidence": sorted(ambiguous, key=_canonical_json),
    }
    if len(_canonical_json(report).encode("utf-8")) > MAX_REPORT_BYTES:
        raise ReconciliationInputError(f"CA content report exceeds the {MAX_REPORT_BYTES}-byte safety cap")
    return report
