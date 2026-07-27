"""Sitemap feed contract.

Search engines cannot discover a bill page unless it appears in a sitemap, so
this endpoint is the whole indexing path for ~200k pages. Two properties matter
more than the response shape:

  * chunks are STABLE -- chunk N returns the same slice every time, or the
    sitemap's contents shuffle between crawls and lastmod stops meaning anything;
  * chunks are DISJOINT and cover the corpus -- an overlap wastes crawl budget,
    a gap makes pages permanently undiscoverable.
"""
from __future__ import annotations

import pytest


def test_sitemap_stats_reports_chunk_count_covering_every_bill(client):
    resp = client.get("/api/v1/sitemap/stats")
    assert resp.status_code == 200
    bills = resp.json()["bills"]
    assert bills["chunk_size"] > 0
    # Ceiling division: a corpus one row longer than a whole number of chunks
    # still needs the extra chunk, or that row is never crawled.
    assert bills["chunks"] * bills["chunk_size"] >= bills["total"]
    assert (bills["chunks"] - 1) * bills["chunk_size"] < max(bills["total"], 1)


def test_sitemap_chunks_are_stable_and_disjoint(client):
    stats = client.get("/api/v1/sitemap/stats").json()["bills"]
    if stats["total"] == 0:
        pytest.skip("no bills loaded in this DB")

    first = client.get("/api/v1/sitemap/bills", params={"chunk": 0})
    assert first.status_code == 200
    rows = first.json()["data"]
    assert rows, "chunk 0 must contain bills when the corpus is non-empty"

    again = client.get("/api/v1/sitemap/bills", params={"chunk": 0}).json()["data"]
    assert [r["id"] for r in again] == [r["id"] for r in rows], (
        "chunk 0 returned a different slice on a second fetch -- unstable chunks "
        "make every sitemap's lastmod a lie"
    )

    if stats["chunks"] > 1:
        second = client.get("/api/v1/sitemap/bills", params={"chunk": 1}).json()["data"]
        assert second, "chunk 1 must contain bills when the corpus spans >1 chunk"
        overlap = {r["id"] for r in rows} & {r["id"] for r in second}
        assert not overlap, f"chunks 0 and 1 share {len(overlap)} bills"


def test_sitemap_rows_carry_the_fields_a_bill_url_is_built_from(client):
    """A row missing jurisdiction or session cannot be turned into a URL, so it
    would silently drop out of the sitemap rather than fail loudly."""
    stats = client.get("/api/v1/sitemap/stats").json()["bills"]
    if stats["total"] == 0:
        pytest.skip("no bills loaded in this DB")
    rows = client.get("/api/v1/sitemap/bills", params={"chunk": 0}).json()["data"]
    for row in rows[:50]:
        assert row["jurisdiction"], f"bill {row['id']} has no jurisdiction abbreviation"
        assert row["session"], f"bill {row['id']} has no session identifier"
        assert row["identifier_norm"], f"bill {row['id']} has no bill number"


def test_sitemap_chunk_past_the_end_is_empty_not_an_error(client):
    """generateSitemaps asks for exactly `chunks` files; an off-by-one at the
    tail must degrade to an empty sitemap, never a 500 that fails the crawl."""
    stats = client.get("/api/v1/sitemap/stats").json()["bills"]
    resp = client.get("/api/v1/sitemap/bills", params={"chunk": stats["chunks"] + 5})
    assert resp.status_code == 200
    assert resp.json()["data"] == []
