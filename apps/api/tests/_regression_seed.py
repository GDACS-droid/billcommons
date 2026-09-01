"""Small deterministic legislative corpus for guarded API integration tests."""
from __future__ import annotations

from sqlalchemy import text

_SOURCE = "api-regression-seed"


def seed_regression_corpus() -> None:
    """Replace only this harness's rows with the minimum contract corpus.

    It intentionally exceeds the search match cap: sampled-search assertions
    exercise the same Postgres CTE path as production without needing a live
    legislative export.  All deletion is scoped by the harness provenance.
    """
    from billcommons_shared.db import get_session

    db = get_session()
    try:
        for table in ("sponsorships", "bill_subjects", "bill_actions", "bill_events"):
            db.execute(text(
                f"DELETE FROM {table} WHERE bill_id IN "
                "(SELECT id FROM bills WHERE source_name = :source)"
            ), {"source": _SOURCE})
        db.execute(text("DELETE FROM bills WHERE source_name = :source"), {"source": _SOURCE})
        db.execute(text("DELETE FROM organizations WHERE source_name = :source"), {"source": _SOURCE})
        db.execute(text("DELETE FROM sessions WHERE source_name = :source"), {"source": _SOURCE})
        db.execute(text("DELETE FROM jurisdictions WHERE source_name = :source"), {"source": _SOURCE})

        # A stable set of state/session labels supports list, batch, sponsor,
        # mortality, and filter contracts.  TX deliberately has HB 1 in two
        # sessions, which is the public ambiguity contract.
        db.execute(text("""
            INSERT INTO jurisdictions (id, name, abbreviation, classification, source_name, created_at, updated_at)
            SELECT gen_random_uuid(), code || ' regression fixture', code, 'state', :source, now(), now()
            FROM unnest(ARRAY['TX', 'FL', 'NC', 'AK']) AS code
        """), {"source": _SOURCE})
        db.execute(text("""
            INSERT INTO sessions (id, jurisdiction_id, identifier, active, source_name, created_at, updated_at)
            SELECT gen_random_uuid(), j.id, s.identifier, s.active, :source, now(), now()
            FROM (VALUES
                ('TX', '2025', false), ('TX', '2026', true), ('FL', '2025-2026', true),
                ('NC', '2025-2026', true), ('AK', '2025-2026', true)
            ) AS s(code, identifier, active)
            JOIN jurisdictions j ON j.abbreviation = s.code AND j.source_name = :source
        """), {"source": _SOURCE})
        db.execute(text("""
            INSERT INTO bills (id, jurisdiction_id, session_id, identifier, identifier_norm, title,
                               chamber, status, introduced_date, latest_action_date, latest_action_text,
                               source_name, created_at, updated_at)
            SELECT gen_random_uuid(), j.id, s.id, 'HB 1', 'HB 1', 'An act concerning Texas fixture',
                   'lower', 'dead', DATE '2026-01-01', DATE '2026-02-01', 'Died in committee', :source, now(), now()
            FROM jurisdictions j JOIN sessions s ON s.jurisdiction_id = j.id
            WHERE j.abbreviation = 'TX' AND j.source_name = :source
        """), {"source": _SOURCE})
        # 5,001 common-word matches prove the capped/sampled branch.  Every
        # row has Smith sponsorship so the filter-before-cap regression remains
        # meaningful (>1,000 results), not merely a response-shape check.
        db.execute(text("""
            INSERT INTO bills (id, jurisdiction_id, session_id, identifier, identifier_norm, title,
                               chamber, status, introduced_date, latest_action_date, latest_action_text,
                               source_name, created_at, updated_at)
            SELECT gen_random_uuid(), j.id, s.id, 'HB ' || n, 'HB ' || n,
                   'An act concerning clean energy education funding ' || n,
                   'lower', CASE WHEN n = 1 THEN 'substituted' ELSE 'introduced' END,
                   DATE '2026-01-01', DATE '2026-02-01', 'Referred to Judiciary', :source, now(), now()
            FROM generate_series(1, 5001) AS n
            JOIN jurisdictions j ON j.abbreviation = 'FL' AND j.source_name = :source
            JOIN sessions s ON s.jurisdiction_id = j.id AND s.identifier = '2025-2026'
        """), {"source": _SOURCE})
        db.execute(text("""
            INSERT INTO bills (id, jurisdiction_id, session_id, identifier, identifier_norm, title,
                               chamber, status, introduced_date, latest_action_date, latest_action_text,
                               source_name, created_at, updated_at)
            SELECT gen_random_uuid(), j.id, s.id, 'HB 123', 'HB 123',
                   'An act concerning local education', 'lower', 'introduced',
                   DATE '2026-01-01', DATE '2026-02-01', 'Referred to Judiciary', :source, now(), now()
            FROM jurisdictions j JOIN sessions s ON s.jurisdiction_id = j.id
            WHERE j.abbreviation IN ('NC', 'AK') AND j.source_name = :source
        """), {"source": _SOURCE})
        db.execute(text("""
            INSERT INTO sponsorships (id, bill_id, name, classification, "primary", source_name, created_at, updated_at)
            SELECT gen_random_uuid(), id, 'Smith', 'primary', true, :source, now(), now()
            FROM bills WHERE source_name = :source AND title LIKE 'An act concerning clean energy%'
        """), {"source": _SOURCE})
        db.execute(text("""
            INSERT INTO bill_subjects (id, bill_id, subject, created_at, updated_at)
            SELECT gen_random_uuid(), id, 'education', now(), now()
            FROM bills WHERE source_name = :source AND identifier = 'HB 1'
        """), {"source": _SOURCE})
        db.execute(text("""
            INSERT INTO organizations (id, name, classification, source_name, created_at, updated_at)
            VALUES (gen_random_uuid(), 'Judiciary', 'committee', :source, now(), now())
        """), {"source": _SOURCE})
        db.execute(text("""
            INSERT INTO bill_actions (id, bill_id, organization_id, description, action_date, source_name, created_at, updated_at)
            SELECT gen_random_uuid(), b.id, o.id, 'Referred to Judiciary', DATE '2026-02-01', :source, now(), now()
            FROM bills b CROSS JOIN organizations o
            WHERE b.source_name = :source AND b.identifier = 'HB 1' AND o.source_name = :source
        """), {"source": _SOURCE})
        db.execute(text("""
            INSERT INTO bill_events (bill_id, kind, changed_at, detail)
            SELECT id, 'status', now() - interval '10 minutes', 'regression fixture'
            FROM bills WHERE source_name = :source LIMIT 10
        """), {"source": _SOURCE})
        db.commit()
    finally:
        db.close()
