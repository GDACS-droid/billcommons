"""Deterministic, fixture-only reconciliation of legislative event records.

This module is deliberately a *comparison* tool.  It never fetches a source,
opens a database, ranks bills, or suggests mutations.  An adapter can give it
one recorded official response and one recorded local export, then use the
machine-readable report to decide what a human should inspect.

The matching rule is intentionally conservative: an event is matched only by
an explicit ``occurrence_id`` or ``source_identity``.  Text, a date, or an
array position are evidence, never an occurrence identity.  That prevents two
repeated official actions from being collapsed into one local record.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


MAX_FIXTURE_BYTES = 2 * 1024 * 1024
MAX_EVENTS_PER_SIDE = 1_000
MAX_EVENT_BYTES = 16 * 1024
MAX_REPORT_BYTES = 8 * 1024 * 1024

_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR = re.compile(r"^(\d{4})$")


class ReconciliationInputError(ValueError):
    """A fixture cannot be compared safely or does not meet the contract."""


@dataclass(frozen=True)
class DateEvidence:
    value: str | None
    precision: str
    lower: date | None
    upper: date | None


@dataclass(frozen=True)
class NormalizedEvent:
    """A raw fixture record plus only the normalized comparison evidence."""

    raw: Mapping[str, Any]
    identity_kind: str | None
    identity_value: str | None
    evidence: Mapping[str, str | None]
    event_date: DateEvidence
    sort_key: str

    @property
    def identity_key(self) -> tuple[str, str] | None:
        if self.identity_kind is None or self.identity_value is None:
            return None
        return (self.identity_kind, self.identity_value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _required_string(record: Mapping[str, Any], field: str) -> str | None:
    value = record.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReconciliationInputError(f"event field {field!r} must be a string or null")
    normalized = " ".join(value.split())
    return normalized.casefold() if normalized else None


def _identity_value(record: Mapping[str, Any], field: str) -> str | None:
    """Normalize only presentation whitespace; source IDs may be case-sensitive."""

    value = record.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReconciliationInputError(f"event field {field!r} must be a string or null")
    return value.strip() or None


def _source_url(record: Mapping[str, Any]) -> str | None:
    """URLs are evidence, so retain path/query case instead of case-folding them."""

    value = record.get("source_url")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReconciliationInputError("event field 'source_url' must be a string or null")
    return value.strip() or None


def _date_evidence(record: Mapping[str, Any]) -> DateEvidence:
    value = record.get("date")
    declared_precision = record.get("date_precision")
    if declared_precision is not None and declared_precision not in {"day", "month", "year", "unknown"}:
        raise ReconciliationInputError("date_precision must be day, month, year, unknown, or null")
    if value is None or value == "":
        if declared_precision not in {None, "unknown"}:
            raise ReconciliationInputError("a date_precision requires a date value")
        return DateEvidence(value=None, precision="unknown", lower=None, upper=None)
    if not isinstance(value, str):
        raise ReconciliationInputError("event field 'date' must be an ISO string or null")

    match = _DAY.fullmatch(value)
    if match:
        try:
            parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError as exc:
            raise ReconciliationInputError("event date is not a real calendar day") from exc
        inferred = "day"
        result = DateEvidence(value=value, precision=inferred, lower=parsed, upper=parsed)
    else:
        match = _MONTH.fullmatch(value)
        if match:
            year, month = int(match.group(1)), int(match.group(2))
            try:
                lower = date(year, month, 1)
                upper = date(year, month, monthrange(year, month)[1])
            except ValueError as exc:
                raise ReconciliationInputError("event date is not a real calendar month") from exc
            inferred = "month"
            result = DateEvidence(value=value, precision=inferred, lower=lower, upper=upper)
        else:
            match = _YEAR.fullmatch(value)
            if not match:
                raise ReconciliationInputError("event date must be YYYY, YYYY-MM, or YYYY-MM-DD")
            year = int(match.group(1))
            inferred = "year"
            try:
                result = DateEvidence(
                    value=value,
                    precision=inferred,
                    lower=date(year, 1, 1),
                    upper=date(year, 12, 31),
                )
            except ValueError as exc:
                raise ReconciliationInputError("event date is not a real calendar year") from exc

    if declared_precision not in {None, inferred}:
        raise ReconciliationInputError("date_precision conflicts with the supplied date")
    return result


def _normalize_event(raw: Any) -> NormalizedEvent:
    if not isinstance(raw, dict):
        raise ReconciliationInputError("every event must be a JSON object")
    try:
        raw_size = len(_canonical_json(raw).encode("utf-8"))
    except (TypeError, ValueError) as exc:  # Defensive; parsed JSON normally cannot hit this.
        raise ReconciliationInputError("event must contain JSON-compatible evidence") from exc
    if raw_size > MAX_EVENT_BYTES:
        raise ReconciliationInputError(
            f"an event exceeds the {MAX_EVENT_BYTES}-byte evidence safety cap"
        )

    occurrence_id = _identity_value(raw, "occurrence_id")
    source_identity = _identity_value(raw, "source_identity")
    # `occurrence_id` is the stronger claim.  A duplicate explicit key is
    # reported as ambiguous later rather than silently paired by order.
    identity_kind, identity_value = (
        ("occurrence_id", occurrence_id)
        if occurrence_id is not None
        else ("source_identity", source_identity)
        if source_identity is not None
        else (None, None)
    )
    description = _required_string(raw, "description")
    text = _required_string(raw, "text")
    if description is not None and text is not None and description != text:
        raise ReconciliationInputError("description and text disagree in one event")

    evidence = {
        "event_type": _required_string(raw, "event_type"),
        "description": description if description is not None else text,
        "chamber": _required_string(raw, "chamber"),
        "stage": _required_string(raw, "stage"),
        "source_url": _source_url(raw),
        "source_identity": source_identity,
        # Scope does not create identity. It protects an otherwise matching
        # occurrence from being silently attributed to another bill or source.
        "jurisdiction": _required_string(raw, "jurisdiction"),
        "session": _required_string(raw, "session"),
        "bill_id": _required_string(raw, "bill_id"),
        "source_namespace": _required_string(raw, "source_namespace"),
    }
    return NormalizedEvent(
        raw=raw,
        identity_kind=identity_kind,
        identity_value=identity_value,
        evidence=evidence,
        event_date=_date_evidence(raw),
        sort_key=_canonical_json(raw),
    )


def _events_from_fixture(fixture: Any, side: str) -> list[NormalizedEvent]:
    if isinstance(fixture, dict):
        fixture = fixture.get("events")
    if not isinstance(fixture, list):
        raise ReconciliationInputError(f"{side} fixture must be an event array or an object with an 'events' array")
    if len(fixture) > MAX_EVENTS_PER_SIDE:
        raise ReconciliationInputError(
            f"{side} fixture exceeds the {MAX_EVENTS_PER_SIDE}-event safety cap"
        )
    return [_normalize_event(raw) for raw in fixture]


def _event_report(event: NormalizedEvent) -> dict[str, Any]:
    """Keep the unmodified fixture object alongside normalized comparison data."""

    return {
        "identity": (
            {"kind": event.identity_kind, "value": event.identity_value}
            if event.identity_key is not None
            else None
        ),
        "normalized_evidence": {
            **event.evidence,
            "date": event.event_date.value,
            "date_precision": event.event_date.precision,
        },
        "raw_evidence": event.raw,
    }


def _compare_pair(official: NormalizedEvent, local: NormalizedEvent) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    differences: list[dict[str, Any]] = []
    uncertainties: list[dict[str, Any]] = []
    for field in (
        "event_type",
        "description",
        "chamber",
        "stage",
        "source_url",
        "source_identity",
        "jurisdiction",
        "session",
        "bill_id",
        "source_namespace",
    ):
        official_value = official.evidence[field]
        local_value = local.evidence[field]
        if field == "description" and official_value is None and local_value is None:
            uncertainties.append(
                {"field": field, "official": None, "local": None, "reason": "missing_on_both_sides"}
            )
        elif official_value is None or local_value is None:
            if official_value != local_value:
                uncertainties.append({"field": field, "official": official_value, "local": local_value})
        elif official_value != local_value:
            differences.append({"field": field, "official": official_value, "local": local_value})

    official_date, local_date = official.event_date, local.event_date
    if official_date.lower is None or local_date.lower is None:
        if official_date.value != local_date.value:
            uncertainties.append(
                {
                    "field": "date",
                    "official": official_date.value,
                    "official_precision": official_date.precision,
                    "local": local_date.value,
                    "local_precision": local_date.precision,
                }
            )
        elif official_date.value is None:
            uncertainties.append(
                {
                    "field": "date",
                    "official": None,
                    "official_precision": official_date.precision,
                    "local": None,
                    "local_precision": local_date.precision,
                    "reason": "missing_on_both_sides",
                }
            )
    elif official_date.upper < local_date.lower or local_date.upper < official_date.lower:
        differences.append(
            {
                "field": "date",
                "official": official_date.value,
                "official_precision": official_date.precision,
                "local": local_date.value,
                "local_precision": local_date.precision,
            }
        )
    elif (
        official_date.value != local_date.value
        or official_date.precision != local_date.precision
    ):
        uncertainties.append(
            {
                "field": "date",
                "official": official_date.value,
                "official_precision": official_date.precision,
                "local": local_date.value,
                "local_precision": local_date.precision,
                "reason": "overlapping_imprecise_dates",
            }
        )
    return differences, uncertainties


def _group_by_identity(events: Iterable[NormalizedEvent]) -> tuple[dict[tuple[str, str], list[NormalizedEvent]], list[NormalizedEvent]]:
    grouped: dict[tuple[str, str], list[NormalizedEvent]] = {}
    unidentified: list[NormalizedEvent] = []
    for event in events:
        if event.identity_key is None:
            unidentified.append(event)
        else:
            grouped.setdefault(event.identity_key, []).append(event)
    return grouped, unidentified


def _sorted_reports(events: Sequence[NormalizedEvent]) -> list[dict[str, Any]]:
    return [_event_report(event) for event in sorted(events, key=lambda event: event.sort_key)]


def reconcile_events(official_fixture: Any, local_fixture: Any) -> dict[str, Any]:
    """Compare recorded official and local events without proposing a mutation.

    ``missing_from_local`` is evidence that the official fixture contains an
    identified occurrence not present locally.  ``local_only_not_deletion``
    is deliberately not a deletion instruction: source snapshots can be
    incomplete or revised.  Records without a unique explicit identity are
    kept in ``ambiguous_identities`` rather than being guessed together.
    """

    official = _events_from_fixture(official_fixture, "official")
    local = _events_from_fixture(local_fixture, "local")
    official_groups, official_unidentified = _group_by_identity(official)
    local_groups, local_unidentified = _group_by_identity(local)

    missing_from_local: list[dict[str, Any]] = []
    local_only_not_deletion: list[dict[str, Any]] = []
    mismatched_evidence: list[dict[str, Any]] = []
    uncertain_evidence: list[dict[str, Any]] = []
    ambiguous_identities: list[dict[str, Any]] = []
    matched: list[dict[str, Any]] = []

    for key in sorted(set(official_groups) | set(local_groups)):
        official_group = official_groups.get(key, [])
        local_group = local_groups.get(key, [])
        identity = {"kind": key[0], "value": key[1]}
        if not official_group:
            local_only_not_deletion.extend(_sorted_reports(local_group))
            continue
        if not local_group:
            missing_from_local.extend(_sorted_reports(official_group))
            continue
        if len(official_group) != 1 or len(local_group) != 1:
            ambiguous_identities.append(
                {
                    "identity": identity,
                    "reason": "duplicate_explicit_identity",
                    "official": _sorted_reports(official_group),
                    "local": _sorted_reports(local_group),
                }
            )
            continue

        official_event, local_event = official_group[0], local_group[0]
        differences, uncertainties = _compare_pair(official_event, local_event)
        paired = {"identity": identity, "official": _event_report(official_event), "local": _event_report(local_event)}
        if differences:
            mismatched_evidence.append({**paired, "differences": differences, "uncertain_fields": uncertainties})
        elif uncertainties:
            uncertain_evidence.append({**paired, "uncertain_fields": uncertainties})
        else:
            matched.append(paired)

    for side, events in (("official", official_unidentified), ("local", local_unidentified)):
        for event in sorted(events, key=lambda item: item.sort_key):
            ambiguous_identities.append(
                {
                    "identity": None,
                    "reason": "missing_explicit_identity",
                    side: _event_report(event),
                }
            )

    report = {
        "schema_version": 1,
        "summary": {
            "official_records": len(official),
            "local_records": len(local),
            "matched": len(matched),
            "missing_from_local": len(missing_from_local),
            "local_only_not_deletion": len(local_only_not_deletion),
            "mismatched_evidence": len(mismatched_evidence),
            "uncertain_evidence": len(uncertain_evidence),
            "ambiguous_identities": len(ambiguous_identities),
        },
        "matched": matched,
        "missing_from_local": missing_from_local,
        "local_only_not_deletion": local_only_not_deletion,
        "mismatched_evidence": mismatched_evidence,
        "uncertain_evidence": uncertain_evidence,
        "ambiguous_identities": ambiguous_identities,
    }
    if len(_canonical_json(report).encode("utf-8")) > MAX_REPORT_BYTES:
        raise ReconciliationInputError(f"reconciliation report exceeds the {MAX_REPORT_BYTES}-byte safety cap")
    return report


def load_fixture(path: Path) -> Any:
    """Read one explicitly named recorded JSON fixture, with no directory walk."""

    try:
        # A bounded read stays bounded even if a fixture changes after a
        # metadata check; there is no directory scan or implicit companion
        # file read here.
        with path.open("rb") as fixture:
            raw = fixture.read(MAX_FIXTURE_BYTES + 1)
        if len(raw) > MAX_FIXTURE_BYTES:
            raise ReconciliationInputError(f"fixture exceeds the {MAX_FIXTURE_BYTES}-byte safety cap")
        return json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconciliationInputError("fixture is not readable JSON") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare recorded official and local legislative event JSON fixtures.")
    parser.add_argument("--official", type=Path, required=True, help="Recorded official-events JSON fixture")
    parser.add_argument("--local", type=Path, required=True, help="Recorded local-events JSON fixture")
    args = parser.parse_args(argv)
    try:
        report = reconcile_events(load_fixture(args.official), load_fixture(args.local))
    except ReconciliationInputError as exc:
        json.dump({"error": {"code": "invalid_reconciliation_input", "message": str(exc)}}, sys.stdout)
        sys.stdout.write("\n")
        return 2
    json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main() and CLI smoke tests.
    raise SystemExit(main())
