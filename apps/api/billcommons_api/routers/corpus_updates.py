"""Public, bounded before/after evidence for forward corpus mutations."""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from billcommons_api.routers.official_evidence import _session, _close, _iso, _public_failure

router = APIRouter(prefix='/corpus-updates', tags=['corpus update evidence'])


@router.get('/derived')
def derived_updates(
    bill_id: uuid.UUID = Query(...),
    limit: int = Query(20, ge=1, le=100),
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> dict:
    db = None
    try:
        db = _session()
        rows = db.execute(text('''
            SELECT id, bill_id, causal_corpus_update_evidence_id,
                   derivation_input_sha256, before_snapshot_sha256,
                   after_snapshot_sha256, processing_version,
                   changed_components, derived_at, created_at
              FROM derived_status_evidence
             WHERE bill_id = :bill_id
             ORDER BY derived_at DESC, id DESC
             LIMIT :limit OFFSET :offset
        '''), {'bill_id': bill_id, 'limit': limit + 1, 'offset': offset}).mappings().all()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            for key in ('id', 'bill_id', 'causal_corpus_update_evidence_id'):
                item[key] = str(item[key]) if item[key] is not None else None
            for key in ('derived_at', 'created_at'):
                item[key] = _iso(item[key])
            item['evidence_urls'] = {
                label: '/api/v1/official-evidence/blobs/' + row[column]
                for label, column in (
                    ('local_derivation_inputs', 'derivation_input_sha256'),
                    ('before', 'before_snapshot_sha256'),
                    ('after', 'after_snapshot_sha256'),
                )
            }
            items.append(item)
        return {
            'bill_id': str(bill_id), 'items': items, 'has_more': len(rows) > limit,
            'next_offset': offset + limit if len(rows) > limit and offset + limit <= 10000 else None,
            'interpretation': 'Forward evidence for local status and relationship derivations. These are computed from retained local inputs, not newly fetched official assertions. Absence of records does not prove a bill has never changed.',
        }
    except HTTPException:
        raise
    except Exception:
        raise _public_failure() from None
    finally:
        if db is not None:
            _close(db)


@router.get('')
def corpus_updates(
    bill_id: uuid.UUID = Query(...),
    limit: int = Query(20, ge=1, le=100),
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> dict:
    db = None
    try:
        db = _session()
        rows = db.execute(text('''
            SELECT id, bill_id, original_bill_upstream_id, source_name,
                   source_url, request_scope, response_sha256,
                   before_snapshot_sha256, after_snapshot_sha256,
                   processing_version, mutation_kind, changed_components,
                   retrieved_at, created_at
              FROM corpus_update_evidence
             WHERE bill_id = :bill_id
             ORDER BY created_at DESC, id DESC
             LIMIT :limit OFFSET :offset
        '''), {'bill_id': bill_id, 'limit': limit + 1, 'offset': offset}).mappings().all()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            for key in ('id', 'bill_id'):
                item[key] = str(item[key])
            for key in ('retrieved_at', 'created_at'):
                item[key] = _iso(item[key])
            item['evidence_urls'] = {
                label: '/api/v1/official-evidence/blobs/' + row[column] if row[column] else None
                for label, column in (
                    ('source_response', 'response_sha256'),
                    ('before', 'before_snapshot_sha256'),
                    ('after', 'after_snapshot_sha256'),
                )
            }
            items.append(item)
        return {
            'bill_id': str(bill_id), 'items': items,
            'has_more': len(rows) > limit,
            'next_offset': offset + limit if len(rows) > limit and offset + limit <= 10000 else None,
            'interpretation': 'Forward mutation evidence only. Each source is labeled; aggregator responses are not direct official-source observations. Absence of records does not prove a bill has never changed.',
        }
    except HTTPException:
        raise
    except Exception:
        raise _public_failure() from None
    finally:
        if db is not None:
            _close(db)
