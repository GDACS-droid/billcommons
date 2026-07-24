from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.deps import get_db
from billcommons_api.errors import bad_request
from billcommons_api.pagination import Page, clamp_per_page, paginate
from billcommons_api.schemas import SearchResult
from billcommons_api.search import VALID_SORTS, SearchFilters, run_search

router = APIRouter(tags=["search"])


@router.get("/search", response_model=Page[SearchResult])
def search(
    request: Request,
    q: str | None = Query(None, description="Search text; bill-number, keyword, or phrase"),
    jurisdiction: str | None = Query(None, description="Jurisdiction abbreviation, e.g. NC"),
    session: str | None = Query(None, description="Session identifier"),
    chamber: str | None = Query(None),
    status: str | None = Query(None),
    sponsor: str | None = Query(None),
    subject: str | None = Query(None),
    committee: str | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    sort: str = Query("relevance"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1),
    db: OrmSession = Depends(get_db),
) -> Page[SearchResult]:
    if sort not in VALID_SORTS:
        raise bad_request(
            "invalid_sort", f"sort must be one of {sorted(VALID_SORTS)}, got {sort!r}"
        )

    per_page = clamp_per_page(per_page)
    filters = SearchFilters(
        q=q,
        jurisdiction=jurisdiction,
        session=session,
        chamber=chamber,
        status=status,
        sponsor=sponsor,
        subject=subject,
        committee=committee,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        sort=sort,
        page=page,
        per_page=per_page,
    )
    rows, total = run_search(db, filters)
    items = [SearchResult.model_validate(r) for r in rows]
    return paginate(
        items,
        page=page,
        per_page=per_page,
        total=total,
        api_version="v1",
        request_id=request.state.request_id,
    )
