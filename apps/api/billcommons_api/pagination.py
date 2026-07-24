"""Pagination envelope shared by every list endpoint.

Envelope shape (locked in BRIEF-wave2.md):
    {data, pagination: {page, per_page, total, total_pages}, meta: {...}}
"""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

MAX_PER_PAGE = 50

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
