"""Two things:

1. GET /bills/lookup -- a GET-query alias of POST /bills/lookup (see that
   handler's docstring), registered ahead of GET /bills/{bill_id} on purpose:
   FastAPI/Starlette match routes in registration order, so a route
   registered AFTER {bill_id} would have "lookup" swallowed by the
   {bill_id} path parameter and fail UUID parsing (422) instead of ever
   running. Guard against that regressing.

2. GET /bills/{bill_id}/documents/{document_id}/text -- one document's
   extracted full text as JSON, reading the same `extracted_text` column
   /compare already reads. bc_test starts empty, so the happy-path tests
   insert (and clean up) a minimal bill/version/document fixture directly.

Deliberately does NOT use the shared session-scoped `client` fixture from
conftest.py: the in-process rate limiter's buckets live on the app instance
for that fixture's whole lifetime, and it is shared across this entire test
file's suite-mates. A fresh `TestClient(create_app())` per test gets its own
buckets instead of spending down the shared budget the rest of the suite
depends on.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete
from starlette.testclient import TestClient

from billcommons_api.app import create_app
from billcommons_schema.models import (
    Bill,
    BillDocument,
    BillVersion,
    Jurisdiction,
    Session as SessionModel,
)
from billcommons_shared.db import get_session

NIL_UUID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture()
def local_client():
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def bill_with_documents():
    """Insert a jurisdiction/session/bill/version/two-documents fixture,
    yield their ids, then delete everything this test created."""
    db = get_session()
    suffix = uuid.uuid4().hex[:6]
    jurisdiction = Jurisdiction(
        name=f"Test Jurisdiction {suffix}",
        abbreviation=f"Z{suffix[:2].upper()}",
        classification="state",
    )
    db.add(jurisdiction)
    db.flush()
    session = SessionModel(jurisdiction_id=jurisdiction.id, identifier=f"session-{suffix}")
    db.add(session)
    db.flush()
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session.id,
        identifier=f"HB{suffix}",
        identifier_norm=f"HB{suffix}".upper(),
        title="A fixture bill for lookup/full-text route tests",
    )
    db.add(bill)
    db.flush()
    version = BillVersion(bill_id=bill.id, note="Introduced")
    db.add(version)
    db.flush()
    document = BillDocument(
        bill_version_id=version.id,
        media_type="application/pdf",
        url="https://example.test/bill.pdf",
        extracted_text="Section 1. This is the fixture bill text.",
        license_note="fulltext_status=ok_browser via=browser",
    )
    empty_document = BillDocument(
        bill_version_id=version.id,
        media_type="application/pdf",
        url="https://example.test/no-text.pdf",
        extracted_text=None,
    )
    db.add_all([document, empty_document])
    db.commit()
    db.refresh(document)
    db.refresh(empty_document)
    try:
        yield {
            "bill_id": str(bill.id),
            "document_id": str(document.id),
            "version_id": str(version.id),
            "empty_document_id": str(empty_document.id),
        }
    finally:
        db.execute(delete(BillDocument).where(BillDocument.bill_version_id == version.id))
        db.execute(delete(BillVersion).where(BillVersion.id == version.id))
        db.execute(delete(Bill).where(Bill.id == bill.id))
        db.execute(delete(SessionModel).where(SessionModel.id == session.id))
        db.execute(delete(Jurisdiction).where(Jurisdiction.id == jurisdiction.id))
        db.commit()
        db.close()


# --------------------------------------------------------------------------
# GET /bills/lookup
# --------------------------------------------------------------------------


def test_lookup_route_is_not_shadowed_by_bill_id_route(local_client, bill_with_documents):
    """The literal /lookup path must resolve to the lookup handler, not fail
    UUID parsing against {bill_id}."""
    resp = local_client.get(
        "/api/v1/bills/lookup", params={"ids": bill_with_documents["bill_id"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.status_code != 422
    body = resp.json()
    ids_returned = {row["id"] for row in body["data"]}
    assert bill_with_documents["bill_id"] in ids_returned
    assert body["not_found"] == []


def test_lookup_route_unresolvable_key_is_200_not_found_not_422(local_client):
    """A key that resolves to nothing is reported in `not_found`, not an
    error -- same contract as POST /bills/lookup and GET /bills/batch."""
    resp = local_client.get("/api/v1/bills/lookup", params={"ids": NIL_UUID})
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert NIL_UUID in body["not_found"]


def test_lookup_route_no_keys_is_400_not_422(local_client):
    resp = local_client.get("/api/v1/bills/lookup")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "no_keys"


def test_bill_id_route_still_works_for_a_real_uuid(local_client, bill_with_documents):
    """The route-order fix for /lookup must not break the {bill_id} route it
    sits ahead of."""
    resp = local_client.get(f"/api/v1/bills/{bill_with_documents['bill_id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == bill_with_documents["bill_id"]


def test_bill_id_route_unknown_uuid_is_404_not_422(local_client):
    resp = local_client.get(f"/api/v1/bills/{NIL_UUID}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "bill_not_found"


# --------------------------------------------------------------------------
# GET /bills/{bill_id}/documents/{document_id}/text
# --------------------------------------------------------------------------


def test_document_text_happy_path(local_client, bill_with_documents):
    bill_id = bill_with_documents["bill_id"]
    document_id = bill_with_documents["document_id"]
    resp = local_client.get(f"/api/v1/bills/{bill_id}/documents/{document_id}/text")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document_id"] == document_id
    assert body["bill_id"] == bill_id
    assert body["version_id"] == bill_with_documents["version_id"]
    assert body["extracted_text"] == "Section 1. This is the fixture bill text."
    assert body["char_count"] == len("Section 1. This is the fixture bill text.")
    # license_note = "fulltext_status=ok_browser via=browser" -> token before the space
    assert body["fulltext_status"] == "ok_browser"


def test_document_text_wrong_bill_is_404(local_client, bill_with_documents):
    """A real document id under the WRONG bill_id must 404, not leak text
    for a document that belongs to a different bill."""
    document_id = bill_with_documents["document_id"]
    resp = local_client.get(f"/api/v1/bills/{NIL_UUID}/documents/{document_id}/text")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "document_not_found"


def test_document_text_missing_document_is_404(local_client, bill_with_documents):
    bill_id = bill_with_documents["bill_id"]
    resp = local_client.get(f"/api/v1/bills/{bill_id}/documents/{NIL_UUID}/text")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "document_not_found"


def test_document_text_no_extracted_text_is_404(local_client, bill_with_documents):
    bill_id = bill_with_documents["bill_id"]
    empty_document_id = bill_with_documents["empty_document_id"]
    resp = local_client.get(f"/api/v1/bills/{bill_id}/documents/{empty_document_id}/text")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "no_extracted_text"


def test_document_text_fulltext_status_is_null_without_a_license_note(local_client, bill_with_documents):
    bill_id = bill_with_documents["bill_id"]
    empty_document_id = bill_with_documents["empty_document_id"]
    # empty_document has no extracted_text, so hit the fulltext_status parser
    # directly on the document WITH text but no license_note by re-fetching
    # via a second fixture-free assertion: the fixture document already has
    # a license_note, so assert the parser behavior at the unit level here.
    from billcommons_api.routers.bills import _fulltext_status

    assert _fulltext_status(None) is None
    assert _fulltext_status("") is None
    assert _fulltext_status("fulltext_status=ok") == "ok"
    assert _fulltext_status("fulltext_status=ok_browser via=browser") == "ok_browser"
    assert _fulltext_status("some_other_note") is None


def test_document_text_is_a_heavy_route_same_as_compare(bill_with_documents):
    """The new endpoint must be wired into the same heavy quota/rate-limit
    tier as /compare -- anonymous callers on both must see the same binding
    X-RateLimit-Limit (the heavy tier's, not the general default)."""
    app_client = TestClient(create_app())
    ip = {"X-Forwarded-For": "203.0.113.209"}
    bill_id = bill_with_documents["bill_id"]
    document_id = bill_with_documents["document_id"]

    text_resp = app_client.get(
        f"/api/v1/bills/{bill_id}/documents/{document_id}/text", headers=ip
    )
    compare_resp = app_client.get(
        f"/api/v1/bills/{bill_id}/compare",
        params={"from": NIL_UUID, "to": NIL_UUID},
        headers={"X-Forwarded-For": "203.0.113.210"},
    )
    assert text_resp.status_code == 200
    assert "X-RateLimit-Limit" in text_resp.headers
    assert text_resp.headers["X-RateLimit-Limit"] == compare_resp.headers["X-RateLimit-Limit"]

    from billcommons_api.rate_limit import _is_heavy_route

    assert _is_heavy_route(f"/api/v1/bills/{bill_id}/documents/{document_id}/text")
