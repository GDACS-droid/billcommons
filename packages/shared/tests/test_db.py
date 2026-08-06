"""Tests for billcommons_shared.db.

Verify round-3 fix #16: two independent verifiers (agy, opus) both flagged
`expire_on_commit` as a risk on this shared sessionmaker -- REFUTED at the
time (db.py:169 already sets `expire_on_commit=False`), but two independent
false positives on the same line means it is worth pinning with an actual
test rather than trusting the next reader to re-derive the same conclusion.
"""
from __future__ import annotations

from billcommons_shared.db import get_sessionmaker


def test_shared_sessionmaker_pins_expire_on_commit_false():
    """`expire_on_commit=False` is the invariant workers/webhooks/
    dispatch_webhooks.py's whole "transactions never span HTTP" design
    depends on: a payload/headers built from `sub`'s attributes AFTER
    `db.commit()` (e.g. between the write that records a delivery outcome
    and the next loop iteration reading `sub.last_seq`) must never silently
    trigger a NEW transaction/query just to re-fetch an expired attribute --
    that would reopen exactly the kind of "transaction spans an unrelated
    operation" hazard this module's whole architecture is built to avoid.
    `expire_on_commit=True` (the SQLAlchemy default) would do precisely
    that on every single attribute access after a commit.
    """
    sessionmaker_ = get_sessionmaker()
    assert sessionmaker_.kw["expire_on_commit"] is False
