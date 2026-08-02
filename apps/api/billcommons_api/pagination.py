"""Pagination envelope shared by every list endpoint.

Envelope shape (locked in BRIEF-wave2.md):
    {data, pagination: {page, per_page, total, total_pages}, meta: {...}}
"""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

MAX_PER_PAGE = 50

# How deep offset pagination may go, in rows.
#
# `page` was bounded below (ge=1) and not above, so `?page=1000000` became
# OFFSET 50,000,000 -- and Postgres walks every one of those rows before
# discarding them. On a public, anonymous, unauthenticated API over a 209k-bill
# corpus that is a one-line denial of service, and it costs the caller nothing.
#
# 50,000 is set above the deepest legitimate browse (the largest state's bill
# list at the 50-per-page maximum is well inside it) and far below the point
# where a single request can pin a connection. Callers who genuinely need to
# walk the whole corpus should filter by jurisdiction or session, which is both
# cheaper for them and indexed for us.
MAX_RESULT_WINDOW = 50_000

# Declared as a bound on `page` rather than checked inside each handler, so it
# appears in OpenAPI, rejects before any query runs, and cannot be forgotten
# when someone adds the tenth list endpoint.
MAX_PAGE = MAX_RESULT_WINDOW // MAX_PER_PAGE

T = TypeVar("T")


class PaginationMeta(BaseModel):
    page: int
    per_page: int
    total: int
    total_pages: int


class ResponseMeta(BaseModel):
    source_freshness: str | None = None
    api_version: str
    request_id: str


class Page(BaseModel, Generic[T]):
    data: list[T]
    pagination: PaginationMeta
    meta: ResponseMeta


def clamp_per_page(per_page: int, maximum: int = MAX_PER_PAGE) -> int:
    return max(1, min(per_page, maximum))


def total_pages(total: int, per_page: int) -> int:
    if total <= 0:
        return 0
    return (total + per_page - 1) // per_page


def paginate(
    items: list[T],
    *,
    page: int,
    per_page: int,
    total: int,
    api_version: str,
    request_id: str,
    source_freshness: str | None = None,
) -> Page[T]:
    return Page[T](
        data=items,
        pagination=PaginationMeta(
            page=page,
            per_page=per_page,
            total=total,
            total_pages=total_pages(total, per_page),
        ),
        meta=ResponseMeta(
            source_freshness=source_freshness,
            api_version=api_version,
            request_id=request_id,
        ),
    )


# Common query params, wired up per-router via Query() with these defaults:
DEFAULT_PAGE = 1
DEFAULT_PER_PAGE = 25


def enforce_result_window(page: int, per_page: int) -> None:
    """Reject a page whose offset would walk past MAX_RESULT_WINDOW rows.

    Raises a structured 400 rather than silently clamping to the last allowed
    page: a caller paging through results needs to know it hit a boundary, and
    quietly serving page 1,000 in response to a request for page 1,000,000
    would look like the corpus simply ended there.
    """
    from billcommons_api.errors import bad_request

    offset = (page - 1) * per_page
    if offset > MAX_RESULT_WINDOW:
        raise bad_request(
            "result_window_exceeded",
            f"Offset pagination is bounded at {MAX_RESULT_WINDOW:,} rows "
            f"(requested offset {offset:,}). This is a limit on how DEEP a "
            "single result set can be paged, not on how much data you can "
            "reach: narrow the query with a jurisdiction, session, or status "
            "filter and page within that. Bulk consumers should use the "
            "sitemaps or POST /api/v1/bills/lookup.",
        )
