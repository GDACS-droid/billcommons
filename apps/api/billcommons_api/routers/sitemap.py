"""Bulk feed powering the website's XML sitemaps.

The public list endpoints cap per_page at 50 (pagination.MAX_PER_PAGE), which is
right for humans and API clients but useless for enumerating a 200k-bill corpus
-- it would take ~4,200 round trips to build one sitemap. Search engines cannot
discover a single bill page without that enumeration, so this endpoint exists to
hand the web app whole chunks at once, carrying only the columns a <url> entry
needs (id, bill number, jurisdiction, session, lastmod).

Chunks are ordered by primary key so the same `chunk` index always returns the
same slice: a sitemap whose contents shuffle between fetches teaches a crawler
nothing about what changed.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.deps import get_db
from billcommons_schema.models import Bill, Jurisdiction, Session

router = APIRouter(prefix="/sitemap", tags=["sitemap"])

# One sitemap file per chunk. The sitemaps protocol permits 50,000 URLs per
# file, but the generated XML is served by a Vercel function whose response body
# is capped well below what 50,000 entries would weigh, so we chunk smaller.
CHUNK_SIZE = 10_000


@router.get("/stats")
def sitemap_stats(db: OrmSession = Depends(get_db)) -> dict[str, object]:
    """Chunk count for the sitemap index.

    Split from the data route so the index can be built with one cheap count
    instead of fetching a chunk just to read its metadata.
    """
    total = db.execute(select(func.count()).select_from(Bill)).scalar_one()
    return {
        "bills": {
            "total": total,
            "chunk_size": CHUNK_SIZE,
            "chunks": (total + CHUNK_SIZE - 1) // CHUNK_SIZE,
        }
    }


@router.get("/bills")
def sitemap_bills(
    chunk: int = Query(0, ge=0, description="Zero-based chunk index"),
    db: OrmSession = Depends(get_db),
) -> dict[str, object]:
    stmt = (
        select(
            Bill.id,
            Bill.identifier_norm,
            Bill.updated_at,
            Jurisdiction.abbreviation,
            Session.identifier,
        )
        .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
        .join(Session, Session.id == Bill.session_id)
        .order_by(Bill.id)
        .offset(chunk * CHUNK_SIZE)
        .limit(CHUNK_SIZE)
    )
    rows = db.execute(stmt).all()
    return {
        "chunk": chunk,
        "chunk_size": CHUNK_SIZE,
        "data": [
            {
                "id": str(row.id),
                "identifier_norm": row.identifier_norm,
                "jurisdiction": row.abbreviation,
                "session": row.identifier,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ],
    }
