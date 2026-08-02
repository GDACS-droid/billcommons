"""Citation primitives for evidence packets: snapshot ids, permalinks, citations.

An evidence packet is only worth citing if a reader can do three things a
normal API response does not allow:

1. **Go back to it.** A permalink that resolves for a human, not a UUID buried
   in an agent transcript.
2. **Tell whether it changed.** The corpus is re-crawled continuously. A
   citation to "the record" is worthless if the record silently moved.
3. **Quote it in a footnote** without hand-assembling the jurisdiction, session
   and retrieval date from six JSON fields.

What a snapshot id promises, stated narrowly because the honest version is
narrower than the impressive version:

    Same snapshot_id  => every fact in the digest below is unchanged.
    Different id      => at least one of them changed.
    We do NOT archive packets. The id cannot retrieve yesterday's packet.

That last line matters. A "snapshot id" that implies retrievable history when
none is stored would be exactly the kind of claim this project exists to
refuse. It is a change-detector, not an archive.

The digest deliberately covers identity and evidence-bearing structure, not
presentation: two packets that differ only in how many votes were included
under a display cap describe the same record and must hash the same. Both the
MCP tool and the REST endpoint build the digest from this module so the two
surfaces cannot drift into disagreeing about whether a bill changed.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

# Bumped when the digest's FIELD SET changes. Without it, a schema change would
# silently invalidate every previously issued id and look to a citing reader
# like the underlying bills had all changed on the same day.
DIGEST_VERSION = "bcs1"

SITE_URL = "https://billcommons.org"
API_URL = "https://api.billcommons.org"


def evidence_digest(
    *,
    bill_id: str,
    jurisdiction_code: str | None,
    session_name: str | None,
    identifier: str | None,
    title: str | None,
    status: str | None,
    action_ids: Iterable[str],
    version_ids: Iterable[str],
    vote_event_ids: Iterable[str],
) -> dict[str, Any]:
    """The canonical set of facts a citation depends on.

    Ids are sorted, so row ordering from the database cannot change the hash.
    Counts are implied by the lists and deliberately not stored separately --
    two representations of the same fact can disagree, and then the digest is
    lying about something.
    """
    return {
        "v": DIGEST_VERSION,
        "bill_id": bill_id,
        "jurisdiction": jurisdiction_code,
        "session": session_name,
        "identifier": identifier,
        "title": title,
        # Derived, and included ON PURPOSE: a change in our own conclusion is
        # exactly the kind of change a citing reader needs to be told about,
        # and it is the field most likely to move without the legislature
        # doing anything.
        "status": status,
        "actions": sorted(str(a) for a in action_ids),
        "versions": sorted(str(v) for v in version_ids),
        "votes": sorted(str(v) for v in vote_event_ids),
    }


def snapshot_id(digest: dict[str, Any]) -> str:
    """Stable short id for a digest. `sort_keys` + no whitespace so the same
    facts hash identically regardless of dict insertion order or serializer."""
    canonical = json.dumps(digest, sort_keys=True, separators=(",", ":"), default=str)
    return f"{DIGEST_VERSION}_{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"


def permalink(bill_id: str) -> str:
    return f"{SITE_URL}/evidence/{bill_id}"


def download_url(bill_id: str) -> str:
    return f"{API_URL}/api/v1/bills/{bill_id}/evidence?download=1"


def citation_text(
    *,
    identifier: str | None,
    title: str | None,
    jurisdiction_name: str | None,
    session_name: str | None,
    status: str | None,
    retrieved_at: str,
    snapshot: str,
    bill_id: str,
) -> str:
    """One quotable line.

    Status is rendered with an explicit attribution to Bill Commons rather than
    presented as a fact of record, because it is derived --
    `died_on_adjournment` in particular has no filed action behind it. A
    citation is precisely where that distinction gets lost, so it is carried in
    the sentence itself rather than in a nearby field the quoter will drop.
    """
    head = " ".join(p for p in [identifier, f"({title})" if title else None] if p)
    where = ", ".join(p for p in [jurisdiction_name, session_name] if p)
    parts = [p for p in [head or bill_id, where] if p]
    sentence = ". ".join(parts)
    if status:
        sentence += f". Status: {status.replace('_', ' ')} (derived by Bill Commons, not an official designation)"
    sentence += (
        f". Bill Commons, retrieved {retrieved_at[:10]}, snapshot {snapshot}. "
        f"{permalink(bill_id)}"
    )
    return sentence


def reproducibility_note() -> str:
    return (
        "snapshot_id identifies the FACTS in this packet, not a stored copy of "
        "it. Bill Commons does not archive packets: re-requesting this "
        "permalink returns today's record, and a different snapshot_id means "
        "at least one cited fact changed. Keep your own copy if you need the "
        "version you cited."
    )
