"""Serializers turning ORM rows into canonical, official-vs-derived-labeled
JSON dicts for MCP tool responses.
"""
from __future__ import annotations

from typing import Any

from billcommons_schema.models import (
    Bill,
    BillAction,
    BillDocument,
    BillVersion,
    LegislativeEvent,
    Session as SessionModel,
    Sponsorship,
    VoteEvent,
    VoteRecord,
)

from billcommons_mcp.common import iso, to_str_id


def bill_official_url(bill: Bill) -> str | None:
    return bill.source_url


def serialize_session(session: SessionModel) -> dict[str, Any]:
    return {
        "id": to_str_id(session.id),
        "jurisdiction_id": to_str_id(session.jurisdiction_id),
        "identifier": session.identifier,
        "name": session.name,
        "classification": session.classification,
        "start_date": session.start_date.isoformat() if session.start_date else None,
        "end_date": session.end_date.isoformat() if session.end_date else None,
        "active": session.active,
        "source": {
            "source_name": session.source_name,
            "source_url": session.source_url,
            "retrieved_at": iso(session.retrieved_at),
        },
    }


def serialize_sponsorship(s: Sponsorship) -> dict[str, Any]:
    return {
        "id": to_str_id(s.id),
        "person_id": to_str_id(s.person_id),
        "organization_id": to_str_id(s.organization_id),
        "name": s.name or (s.person.name if s.person else None),
        "classification": s.classification,
        "primary": s.primary,
    }


def serialize_action(a: BillAction) -> dict[str, Any]:
    return {
        "id": to_str_id(a.id),
        "description": a.description,
        "action_date": a.action_date.isoformat() if a.action_date else None,
        "classification": a.classification,
        "order": a.order,
        "organization_id": to_str_id(a.organization_id),
        "source": {
            "source_name": a.source_name,
            "source_url": a.source_url,
            "retrieved_at": iso(a.retrieved_at),
        },
    }


def serialize_document(doc: BillDocument, include_text: bool = False) -> dict[str, Any]:
    out = {
        "id": to_str_id(doc.id),
        "media_type": doc.media_type,
        "url": doc.url,
        "has_extracted_text": bool(doc.extracted_text),
        "source": {
            "source_name": doc.source_name,
            "source_url": doc.source_url,
            "retrieved_at": iso(doc.retrieved_at),
        },
    }
    if include_text:
        out["extracted_text"] = doc.extracted_text
    return out


def serialize_version(v: BillVersion, include_text: bool = False) -> dict[str, Any]:
    return {
        "id": to_str_id(v.id),
        "note": v.note,
        "date": v.date.isoformat() if v.date else None,
        "documents": [serialize_document(d, include_text=include_text) for d in v.documents],
        "source": {
            "source_name": v.source_name,
            "source_url": v.source_url,
            "retrieved_at": iso(v.retrieved_at),
        },
    }


def serialize_vote_record(r: VoteRecord) -> dict[str, Any]:
    return {
        "id": to_str_id(r.id),
        "person_id": to_str_id(r.person_id),
        "voter_name": r.voter_name or (r.person.name if r.person else None),
        "option": r.option,
    }


def serialize_vote_event(v: VoteEvent, include_member_votes: bool = True) -> dict[str, Any]:
    out = {
        "id": to_str_id(v.id),
        "bill_id": to_str_id(v.bill_id),
        "organization_id": to_str_id(v.organization_id),
        "motion_text": v.motion_text,
        "motion_classification": v.motion_classification,
        "start_date": v.start_date.isoformat() if v.start_date else None,
        "result": v.result,
        "yes_count": v.yes_count,
        "no_count": v.no_count,
        "other_count": v.other_count,
        "source": {
            "source_name": v.source_name,
            "source_url": v.source_url,
            "retrieved_at": iso(v.retrieved_at),
        },
    }
    if include_member_votes:
        out["member_votes"] = [serialize_vote_record(r) for r in v.vote_records]
    return out


def serialize_event(e: LegislativeEvent) -> dict[str, Any]:
    return {
        "id": to_str_id(e.id),
        "jurisdiction_id": to_str_id(e.jurisdiction_id),
        "bill_id": to_str_id(e.bill_id),
        "committee_id": to_str_id(e.committee_id),
        "name": e.name,
        "description": e.description,
        "start_date": iso(e.start_date),
        "location": e.location,
        "source": {
            "source_name": e.source_name,
            "source_url": e.source_url,
            "retrieved_at": iso(e.retrieved_at),
        },
    }


def serialize_bill_summary(bill: Bill) -> dict[str, Any]:
    """Compact bill representation for search-result lists."""
    return {
        "id": to_str_id(bill.id),
        "jurisdiction_id": to_str_id(bill.jurisdiction_id),
        "session_id": to_str_id(bill.session_id),
        "identifier": bill.identifier,
        "identifier_norm": bill.identifier_norm,
        "chamber": bill.chamber,
        "title": bill.title,
        "short_title": bill.short_title,
        "bill_type": bill.bill_type,
        "status": bill.status,
        "status_date": bill.status_date.isoformat() if bill.status_date else None,
        "latest_action_text": bill.latest_action_text,
        "latest_action_date": (
            bill.latest_action_date.isoformat() if bill.latest_action_date else None
        ),
        "official_source_url": bill_official_url(bill),
    }


def serialize_bill_full(bill: Bill, include_text: bool = False) -> dict[str, Any]:
    """Full bill record: everything captured, per SPEC "Bill data" section."""
    out = serialize_bill_summary(bill)
    out.update(
        {
            "description": bill.description,
            "introduced_date": bill.introduced_date.isoformat() if bill.introduced_date else None,
            "subjects": [s.subject for s in bill.subjects],
            "sponsors": [serialize_sponsorship(s) for s in bill.sponsorships],
            "actions": [
                serialize_action(a)
                for a in sorted(
                    bill.actions,
                    key=lambda a: (a.order if a.order is not None else 0, a.action_date or ""),
                )
            ],
            "versions": [serialize_version(v, include_text=include_text) for v in bill.versions],
            "vote_events": [
                serialize_vote_event(v, include_member_votes=False) for v in bill.vote_events
            ],
            "provenance": {
                "source_name": bill.source_name,
                "source_url": bill.source_url,
                "upstream_id": bill.upstream_id,
                "retrieved_at": iso(bill.retrieved_at),
                "upstream_updated_at": iso(bill.upstream_updated_at),
                "license_note": bill.license_note,
            },
        }
    )
    return out
