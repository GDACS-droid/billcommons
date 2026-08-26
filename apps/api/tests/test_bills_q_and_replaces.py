"""Gap A + Gap B from the NY researcher product test (2026-08-25):

Gap A: GET /api/v1/bills?q=<bill-number-shaped value> must ALSO match on
identifier_norm, ranked first, instead of only running a title/full-text
search a caller has to know to bypass via `identifier=`.

Gap B: a bill detail must expose the substitution relationship
(`related_bills.relation_type` matching "substituted-by") in both directions
-- `replaces` on the survivor, `replaced_by` on the substituted print.

Both fixtures insert isolated, uniquely-tokened rows (see test_search.py's
capped_boundary_bills for the same pattern) and clean up afterward -- these
tests run against a real DB (see conftest.py), not a mock.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sqltext

from billcommons_shared.db import get_session


@pytest.fixture()
def q_identifier_bills():
    """One bill whose identifier IS the bill-number-shaped `q` under test,
    and one unrelated bill in the same jurisdiction whose TITLE contains a
    unique keyword -- so the identifier-shaped-q test and the
    non-identifier-q-unchanged test can share one fixture without either one
    accidentally matching the other's row."""
    token = uuid.uuid4().hex[:8].upper()
    digits = str(uuid.uuid4().int)[:5]
    other_digits = str(uuid.uuid4().int)[:5]
    jurisdiction_abbr = f"Z{token[:7]}"
    identifier = f"ZQ {digits}"  # well-formed: alpha prefix + digits only
    identifier_norm = f"ZQ {digits}"
    keyword_title = f"WidgetKeyword{token} Manufacturing Safety Act"

    db = get_session()
    jurisdiction_id = None
    try:
        jurisdiction_id = db.execute(
            sqltext(
                "INSERT INTO jurisdictions (id, name, abbreviation, classification, "
                "created_at, updated_at) "
                "VALUES (gen_random_uuid(), :name, :abbr, 'state', now(), now()) "
                "RETURNING id"
            ),
            {"name": f"QIdentifierJurisdiction{token}", "abbr": jurisdiction_abbr},
        ).scalar_one()
        session_id = db.execute(
            sqltext(
                "INSERT INTO sessions (id, jurisdiction_id, identifier, active, "
                "created_at, updated_at) "
                "VALUES (gen_random_uuid(), :jid, :ident, false, now(), now()) "
                "RETURNING id"
            ),
            {"jid": jurisdiction_id, "ident": f"session-{token}"},
        ).scalar_one()
        identifier_bill_id = db.execute(
            sqltext(
                "INSERT INTO bills (id, jurisdiction_id, session_id, identifier, "
                "identifier_norm, title, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :jid, :sid, :ident, :ident_norm, :title, "
                "now(), now()) RETURNING id"
            ),
            {
                "jid": jurisdiction_id,
                "sid": session_id,
                "ident": identifier,
                "ident_norm": identifier_norm,
                "title": f"An unrelated title {token}",
            },
        ).scalar_one()
        keyword_bill_id = db.execute(
            sqltext(
                "INSERT INTO bills (id, jurisdiction_id, session_id, identifier, "
                "identifier_norm, title, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :jid, :sid, :ident, :ident_norm, :title, "
                "now(), now()) RETURNING id"
            ),
            {
                "jid": jurisdiction_id,
                "sid": session_id,
                "ident": f"HB {other_digits}",
                "ident_norm": f"HB {other_digits}",
                "title": keyword_title,
            },
        ).scalar_one()
        db.commit()
        yield {
            "jurisdiction_abbr": jurisdiction_abbr,
            "identifier": identifier,
            "identifier_norm": identifier_norm,
            "identifier_bill_id": str(identifier_bill_id),
            "keyword_bill_id": str(keyword_bill_id),
            "keyword": f"WidgetKeyword{token}",
        }
    finally:
        if jurisdiction_id is not None:
            db.execute(sqltext("DELETE FROM bills WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM sessions WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM jurisdictions WHERE id = :jid"), {"jid": jurisdiction_id})
            db.commit()
        db.close()


def test_list_bills_identifier_shaped_q_matches_identifier_norm(client, q_identifier_bills):
    """The reported gap: q="ZQ AB12"-shaped values must resolve the bill by
    identifier_norm, not just fall through to a title search that finds
    nothing."""
    fixture = q_identifier_bills
    resp = client.get(
        "/api/v1/bills",
        params={
            "jurisdiction": fixture["jurisdiction_abbr"],
            "q": fixture["identifier"].replace(" ", "").lower(),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = [row["id"] for row in body["data"]]
    assert fixture["identifier_bill_id"] in ids, (
        "identifier-shaped q must match on identifier_norm"
    )
    # Identifier matches rank first.
    assert body["data"][0]["id"] == fixture["identifier_bill_id"]


def test_list_bills_non_identifier_q_only_matches_text(client, q_identifier_bills):
    """A non-identifier-shaped q (a real keyword) must behave exactly as
    plain full-text/title search always has -- it must NOT pull in the
    identifier-shaped bill from the same fixture, and must find the bill
    whose title actually contains the keyword."""
    fixture = q_identifier_bills
    resp = client.get(
        "/api/v1/bills",
        params={"jurisdiction": fixture["jurisdiction_abbr"], "q": fixture["keyword"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = [row["id"] for row in body["data"]]
    assert fixture["keyword_bill_id"] in ids
    assert fixture["identifier_bill_id"] not in ids, (
        "a keyword query must not also surface the unrelated identifier-shaped bill"
    )


def test_list_bills_q_respects_jurisdiction_filter(client, q_identifier_bills):
    """Structural filters must still apply on top of the new q handling."""
    fixture = q_identifier_bills
    resp = client.get(
        "/api/v1/bills",
        params={"jurisdiction": "ZZZZ_NO_SUCH_JURISDICTION", "q": fixture["identifier"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []


def test_list_bills_whitespace_only_q_is_treated_as_no_filter(client, q_identifier_bills):
    """A whitespace-only q (e.g. "   ") must NOT be handed to
    websearch_to_tsquery('english', '') -- that matches nothing and would
    silently empty out an otherwise-unfiltered listing. It must behave
    exactly like q being absent."""
    fixture = q_identifier_bills
    resp = client.get(
        "/api/v1/bills",
        params={"jurisdiction": fixture["jurisdiction_abbr"], "q": "   "},
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = [row["id"] for row in body["data"]]
    assert fixture["identifier_bill_id"] in ids
    assert fixture["keyword_bill_id"] in ids


@pytest.fixture()
def substitution_bills():
    """A substituted print and its survivor, linked by the one relation_type
    this corpus actually uses for substitutions (relation_type=
    "substituted-by"; see cli.py's `.ilike("%substitut%")` check and its own
    test fixture) -- bill_id=substituted, related_bill_id=survivor."""
    token = uuid.uuid4().hex[:8].upper()
    db = get_session()
    jurisdiction_id = None
    try:
        jurisdiction_id = db.execute(
            sqltext(
                "INSERT INTO jurisdictions (id, name, abbreviation, classification, "
                "created_at, updated_at) "
                "VALUES (gen_random_uuid(), :name, :abbr, 'state', now(), now()) "
                "RETURNING id"
            ),
            {"name": f"SubstitutionJurisdiction{token}", "abbr": f"Y{token[:7]}"},
        ).scalar_one()
        session_id = db.execute(
            sqltext(
                "INSERT INTO sessions (id, jurisdiction_id, identifier, active, "
                "created_at, updated_at) "
                "VALUES (gen_random_uuid(), :jid, :ident, false, now(), now()) "
                "RETURNING id"
            ),
            {"jid": jurisdiction_id, "ident": f"session-{token}"},
        ).scalar_one()
        survivor_id = db.execute(
            sqltext(
                "INSERT INTO bills (id, jurisdiction_id, session_id, identifier, "
                "identifier_norm, title, status, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :jid, :sid, :ident, :ident_norm, :title, "
                "'enacted', now(), now()) RETURNING id"
            ),
            {
                "jid": jurisdiction_id,
                "sid": session_id,
                "ident": f"A {token}C",
                "ident_norm": f"A {token}C",
                "title": f"Survivor act {token}",
            },
        ).scalar_one()
        substituted_id = db.execute(
            sqltext(
                "INSERT INTO bills (id, jurisdiction_id, session_id, identifier, "
                "identifier_norm, title, status, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :jid, :sid, :ident, :ident_norm, :title, "
                "'substituted', now(), now()) RETURNING id"
            ),
            {
                "jid": jurisdiction_id,
                "sid": session_id,
                "ident": f"S {token}",
                "ident_norm": f"S {token}",
                "title": f"Substituted print {token}",
            },
        ).scalar_one()
        db.execute(
            sqltext(
                "INSERT INTO related_bills (id, bill_id, related_bill_id, relation_type, "
                "created_at, updated_at) "
                "VALUES (gen_random_uuid(), :bid, :rbid, 'substituted-by', now(), now())"
            ),
            {"bid": substituted_id, "rbid": survivor_id},
        )
        db.commit()
        yield {"survivor_id": str(survivor_id), "substituted_id": str(substituted_id)}
    finally:
        if jurisdiction_id is not None:
            db.execute(sqltext("DELETE FROM related_bills WHERE bill_id IN (SELECT id FROM bills WHERE jurisdiction_id = :jid)"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM bills WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM sessions WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM jurisdictions WHERE id = :jid"), {"jid": jurisdiction_id})
            db.commit()
        db.close()


def test_bill_detail_survivor_exposes_replaces(client, substitution_bills):
    resp = client.get(f"/api/v1/bills/{substitution_bills['survivor_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["replaces"], "survivor bill detail must list the bill it replaced"
    assert body["replaces"][0]["id"] == substitution_bills["substituted_id"]
    assert body["replaced_by"] == []


def test_bill_detail_substituted_bill_exposes_replaced_by(client, substitution_bills):
    resp = client.get(f"/api/v1/bills/{substitution_bills['substituted_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["replaced_by"], "substituted bill detail must list its survivor"
    assert body["replaced_by"][0]["id"] == substitution_bills["survivor_id"]
    assert body["replaces"] == []


@pytest.fixture()
def unresolved_substitution_bills():
    """Same relationship as `substitution_bills`, but the `related_bills` row
    only has `related_identifier` set (related_bill_id NULL) -- the case
    where ingest recorded the substitution before/without resolving the FK.
    Exercises the identifier-normalization fallback on BOTH directions:
    the survivor's `replaces` (F1 fix) and the substituted bill's
    `replaced_by` (pre-existing fallback)."""
    token = uuid.uuid4().hex[:8].upper()
    digits = str(uuid.uuid4().int)[:5]  # well-formed: normalize_bill_number
    # requires letters-then-digits-then-optional-letter, not a hex token
    # (which mixes letters and digits and would raise ValueError).
    db = get_session()
    jurisdiction_id = None
    try:
        jurisdiction_id = db.execute(
            sqltext(
                "INSERT INTO jurisdictions (id, name, abbreviation, classification, "
                "created_at, updated_at) "
                "VALUES (gen_random_uuid(), :name, :abbr, 'state', now(), now()) "
                "RETURNING id"
            ),
            {"name": f"UnresolvedSubJurisdiction{token}", "abbr": f"X{token[:7]}"},
        ).scalar_one()
        session_id = db.execute(
            sqltext(
                "INSERT INTO sessions (id, jurisdiction_id, identifier, active, "
                "created_at, updated_at) "
                "VALUES (gen_random_uuid(), :jid, :ident, false, now(), now()) "
                "RETURNING id"
            ),
            {"jid": jurisdiction_id, "ident": f"session-{token}"},
        ).scalar_one()
        survivor_identifier = f"A {digits}C"
        survivor_id = db.execute(
            sqltext(
                "INSERT INTO bills (id, jurisdiction_id, session_id, identifier, "
                "identifier_norm, title, status, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :jid, :sid, :ident, :ident_norm, :title, "
                "'enacted', now(), now()) RETURNING id"
            ),
            {
                "jid": jurisdiction_id,
                "sid": session_id,
                "ident": survivor_identifier,
                "ident_norm": survivor_identifier,
                "title": f"Survivor act {token}",
            },
        ).scalar_one()
        substituted_identifier = f"S {digits}"
        substituted_id = db.execute(
            sqltext(
                "INSERT INTO bills (id, jurisdiction_id, session_id, identifier, "
                "identifier_norm, title, status, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :jid, :sid, :ident, :ident_norm, :title, "
                "'substituted', now(), now()) RETURNING id"
            ),
            {
                "jid": jurisdiction_id,
                "sid": session_id,
                "ident": substituted_identifier,
                "ident_norm": substituted_identifier,
                "title": f"Substituted print {token}",
            },
        ).scalar_one()
        # related_bill_id is deliberately NULL -- only related_identifier
        # names the survivor.
        db.execute(
            sqltext(
                "INSERT INTO related_bills (id, bill_id, related_bill_id, "
                "related_identifier, relation_type, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :bid, NULL, :related_ident, "
                "'substituted-by', now(), now())"
            ),
            {"bid": substituted_id, "related_ident": survivor_identifier},
        )
        db.commit()
        yield {"survivor_id": str(survivor_id), "substituted_id": str(substituted_id)}
    finally:
        if jurisdiction_id is not None:
            db.execute(
                sqltext(
                    "DELETE FROM related_bills WHERE bill_id IN "
                    "(SELECT id FROM bills WHERE jurisdiction_id = :jid)"
                ),
                {"jid": jurisdiction_id},
            )
            db.execute(sqltext("DELETE FROM bills WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM sessions WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM jurisdictions WHERE id = :jid"), {"jid": jurisdiction_id})
            db.commit()
        db.close()


def test_bill_detail_survivor_exposes_replaces_via_unresolved_identifier(
    client, unresolved_substitution_bills
):
    """F1: the survivor side must fall back to related_identifier
    normalization, exactly like the substituted-bill side already did --
    otherwise a row with related_bill_id NULL only shows up on one end."""
    resp = client.get(f"/api/v1/bills/{unresolved_substitution_bills['survivor_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["replaces"], "survivor must expose replaces via the unresolved-FK fallback"
    assert body["replaces"][0]["id"] == unresolved_substitution_bills["substituted_id"]
    assert body["replaced_by"] == []


def test_bill_detail_substituted_bill_exposes_replaced_by_via_unresolved_identifier(
    client, unresolved_substitution_bills
):
    """Same fixture, reverse direction -- confirms the pre-existing
    related_identifier fallback for `replaced_by` still works unchanged."""
    resp = client.get(f"/api/v1/bills/{unresolved_substitution_bills['substituted_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["replaced_by"], "substituted bill must expose replaced_by via the unresolved-FK fallback"
    assert body["replaced_by"][0]["id"] == unresolved_substitution_bills["survivor_id"]
    assert body["replaces"] == []


def test_bill_detail_replaces_defaults_empty_for_ordinary_bill(client, q_identifier_bills):
    """The overwhelming majority of bills are never substituted -- both
    fields must be present and empty, not absent."""
    resp = client.get(f"/api/v1/bills/{q_identifier_bills['identifier_bill_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["replaces"] == []
    assert body["replaced_by"] == []


def _make_print_suffix_bills(jurisdiction_abbr: str):
    """Shared builder for the NY-print-suffix fixtures below: a survivor
    whose identifier_norm has NO trailing print letter ("A 10008"-shape) and
    a substituted print whose only link to it is an UNRESOLVED
    related_identifier that DOES carry the trailing print letter
    ("A10008C"-shape, as parsed straight out of "SUBSTITUTED BY A10008C").
    Only actually resolves when the survivor's own jurisdiction is NY."""
    token = uuid.uuid4().hex[:8].upper()
    digits = str(uuid.uuid4().int)[:5]
    db = get_session()
    jurisdiction_id = None
    try:
        jurisdiction_id = db.execute(
            sqltext(
                "INSERT INTO jurisdictions (id, name, abbreviation, classification, "
                "created_at, updated_at) "
                "VALUES (gen_random_uuid(), :name, :abbr, 'state', now(), now()) "
                "RETURNING id"
            ),
            {"name": f"PrintSuffixJurisdiction{token}", "abbr": jurisdiction_abbr},
        ).scalar_one()
        session_id = db.execute(
            sqltext(
                "INSERT INTO sessions (id, jurisdiction_id, identifier, active, "
                "created_at, updated_at) "
                "VALUES (gen_random_uuid(), :jid, :ident, false, now(), now()) "
                "RETURNING id"
            ),
            {"jid": jurisdiction_id, "ident": f"session-{token}"},
        ).scalar_one()
        survivor_identifier_norm = f"A {digits}"  # no trailing print letter
        survivor_id = db.execute(
            sqltext(
                "INSERT INTO bills (id, jurisdiction_id, session_id, identifier, "
                "identifier_norm, title, status, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :jid, :sid, :ident, :ident_norm, :title, "
                "'enacted', now(), now()) RETURNING id"
            ),
            {
                "jid": jurisdiction_id,
                "sid": session_id,
                "ident": survivor_identifier_norm,
                "ident_norm": survivor_identifier_norm,
                "title": f"Survivor act {token}",
            },
        ).scalar_one()
        substituted_identifier_norm = f"S {digits}"
        substituted_id = db.execute(
            sqltext(
                "INSERT INTO bills (id, jurisdiction_id, session_id, identifier, "
                "identifier_norm, title, status, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :jid, :sid, :ident, :ident_norm, :title, "
                "'substituted', now(), now()) RETURNING id"
            ),
            {
                "jid": jurisdiction_id,
                "sid": session_id,
                "ident": substituted_identifier_norm,
                "ident_norm": substituted_identifier_norm,
                "title": f"Substituted print {token}",
            },
        ).scalar_one()
        # related_identifier carries the trailing print letter -- exactly
        # what "SUBSTITUTED BY A10008C" free text yields once normalized.
        related_identifier_with_print_letter = f"A {digits}C"
        db.execute(
            sqltext(
                "INSERT INTO related_bills (id, bill_id, related_bill_id, "
                "related_identifier, relation_type, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :bid, NULL, :related_ident, "
                "'substituted-by', now(), now())"
            ),
            {"bid": substituted_id, "related_ident": related_identifier_with_print_letter},
        )
        db.commit()
        yield {"survivor_id": str(survivor_id), "substituted_id": str(substituted_id)}
    finally:
        if jurisdiction_id is not None:
            db.execute(
                sqltext(
                    "DELETE FROM related_bills WHERE bill_id IN "
                    "(SELECT id FROM bills WHERE jurisdiction_id = :jid)"
                ),
                {"jid": jurisdiction_id},
            )
            db.execute(sqltext("DELETE FROM bills WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM sessions WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM jurisdictions WHERE id = :jid"), {"jid": jurisdiction_id})
            db.commit()
        db.close()


@pytest.fixture()
def ny_print_suffix_bills():
    yield from _make_print_suffix_bills("NY")


@pytest.fixture()
def ne_print_suffix_bills():
    """Same shape as ny_print_suffix_bills, but NOT jurisdiction NY -- the
    trailing letter must NOT be stripped (NE/FL/CA legitimately use it as
    part of identity), so this must stay unlinked."""
    yield from _make_print_suffix_bills("NE")


def test_bill_detail_ny_survivor_matches_print_suffix_related_identifier(
    client, ny_print_suffix_bills
):
    """Defect 1: 'SUBSTITUTED BY A10008C' normalizes to 'A 10008C', but the
    NY survivor's own identifier_norm is 'A 10008' (print letter stripped
    at ingest) -- the NY-gated print-suffix candidate must bridge that gap
    in both directions."""
    resp = client.get(f"/api/v1/bills/{ny_print_suffix_bills['survivor_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["replaces"], "NY survivor must resolve the print-suffix related_identifier"
    assert body["replaces"][0]["id"] == ny_print_suffix_bills["substituted_id"]

    resp = client.get(f"/api/v1/bills/{ny_print_suffix_bills['substituted_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["replaced_by"], "NY substituted bill must resolve the print-suffix related_identifier"
    assert body["replaced_by"][0]["id"] == ny_print_suffix_bills["survivor_id"]


def test_bill_detail_non_ny_print_suffix_shape_stays_unlinked(client, ne_print_suffix_bills):
    """Same identifier shapes, but outside NY -- FL/CA/NE legitimately use a
    trailing letter as part of a bill's real identity, so stripping it would
    resolve to the wrong bill. Must NOT link (exact match only)."""
    resp = client.get(f"/api/v1/bills/{ne_print_suffix_bills['survivor_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["replaces"] == []

    resp = client.get(f"/api/v1/bills/{ne_print_suffix_bills['substituted_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["replaced_by"] == []


@pytest.mark.parametrize("q_value", ["HJRES12", "SJRES 8", "HCONRES 10"])
def test_list_bills_congress_style_q_hits_identifier_branch(client, q_value):
    """Defect 2: Congress-style prefixes (HJRES/SJRES/HCONRES, up to 8
    letters) must still be recognized as identifier-shaped q, not silently
    degrade to full-text-only the way a >4-letter prefix used to."""
    resp = client.get("/api/v1/bills", params={"q": q_value})
    assert resp.status_code == 200
    from billcommons_api.routers.bills import _IDENTIFIER_LIKE_Q

    assert _IDENTIFIER_LIKE_Q.match(q_value.strip()), q_value


def test_list_bills_ny_print_suffix_q_matches_stripped_identifier(client, ny_print_suffix_bills):
    """Round-3 F1: q=A10008C on NY must find the survivor whose identifier_norm
    is 'A 10008' (NY strips the print letter at ingest), and rank it first."""
    survivor_id = ny_print_suffix_bills["survivor_id"]
    detail = client.get(f"/api/v1/bills/{survivor_id}").json()
    q_value = detail["identifier"].replace(" ", "") + "C"
    resp = client.get("/api/v1/bills", params={"q": q_value, "jurisdiction": "NY"})
    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()["data"]]
    assert ids and ids[0] == survivor_id, (q_value, ids)
    # Without the jurisdiction filter the NY gate still applies via the bill's own jurisdiction.
    resp = client.get("/api/v1/bills", params={"q": q_value})
    assert resp.status_code == 200
    assert survivor_id in [row["id"] for row in resp.json()["data"]]


def test_list_bills_non_ny_print_suffix_q_stays_exact(client, ne_print_suffix_bills):
    """Outside NY the trailing letter is identity: q=A12345C must NOT match 'A 12345'."""
    survivor_id = ne_print_suffix_bills["survivor_id"]
    detail = client.get(f"/api/v1/bills/{survivor_id}").json()
    q_value = detail["identifier"].replace(" ", "") + "C"
    resp = client.get("/api/v1/bills", params={"q": q_value, "jurisdiction": "NE"})
    assert resp.status_code == 200
    assert survivor_id not in [row["id"] for row in resp.json()["data"]]
