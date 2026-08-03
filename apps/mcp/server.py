"""Bill Commons MCP server entry point.

Runs the 10 Bill Commons tools (see docs/SPEC.md "MCP" section) over
Streamable HTTP, mounted at /mcp, stateless (no session affinity — safe
behind a load balancer). Listens on env PORT (default 8400).

Security: this server is public and anonymous-callable, so it is fronted by
an ASGI-level per-IP token-bucket rate limiter (billcommons_mcp.rate_limit;
env-tunable via MCP_RATE_LIMIT_PER_MINUTE / MCP_RATE_LIMIT_BURST) in addition
to the per-tool work caps enforced inside billcommons_mcp.tools.

Run directly: .venv/bin/python apps/mcp/server.py
"""
from __future__ import annotations

import os
import sys

# Allow running this file directly (python apps/mcp/server.py) as well as as
# a module, by ensuring the apps/mcp dir is on sys.path for the local package.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from billcommons_mcp import tools
from billcommons_mcp.rate_limit import RateLimitMiddleware
from billcommons_mcp.telemetry import ClientFamilyMiddleware

mcp = FastMCP(
    name="billcommons",
    instructions=(
        "Bill Commons: public, open-source legislative search for all 50 US "
        "states + DC. Tools return structured JSON with canonical ids, "
        "official source_url fields, retrieved_at freshness timestamps, and "
        "explicit official-vs-derived labeling. Coverage is uneven while "
        "ingestion is in progress -- call get_jurisdiction_coverage before "
        "relying on an empty result to mean 'no such legislation exists'.\n\n"
        "SCOPE -- what is NOT here. Do not answer these from Bill Commons, and "
        "do not report an empty result as evidence that the legislation does "
        "not exist:\n"
        "- US CONGRESS. State legislatures only. No federal bills, no H.R./S. "
        "numbers, no Congressional committees.\n"
        "- CITY AND COUNTY ORDINANCES. Nothing below the state legislature. A "
        "question about a municipal code, city commission, or county board is "
        "out of scope even for a state we cover well. (State bills that "
        "REGULATE cities -- preemption, home rule, municipal finance -- are in "
        "scope and are searchable.)\n"
        "- CURRENT SESSION ONLY. Not an archive; no prior-year sessions.\n"
        "- HEARINGS AND COMMITTEE CALENDARS. Not collected for any state.\n"
        "- LEGISLATOR AND COMMITTEE RECORDS. Not collected. Bill sponsors are "
        "available per bill, as bare names without party or district.\n\n"
        "Bill status is DERIVED by Bill Commons from the action record, not "
        "reported by the state. Attribute it accordingly."
    ),
    host=os.environ.get("HOST", "0.0.0.0"),
    port=int(os.environ.get("PORT", "8400")),
    stateless_http=True,
)


@mcp.tool()
def search_legislation(
    q: str,
    jurisdiction: str | None = None,
    chamber: str | None = None,
    status: str | None = None,
    limit: int | None = None,
) -> dict:
    """Search bills by keyword/phrase (full-text) or normalized bill number
    (e.g. "HB 123", "H.B. 123", "hb123" all match). Optionally filter by
    jurisdiction (two-letter state code or name), chamber, and status."""
    return tools.search_legislation(q, jurisdiction, chamber, status, limit)


@mcp.tool()
def get_bill_record(
    bill_id: str | None = None,
    jurisdiction: str | None = None,
    session: str | None = None,
    identifier: str | None = None,
    include_full_text: bool = False,
) -> dict:
    """Fetch the full record for one bill: metadata, sponsors, subjects,
    actions, versions, and vote-event summaries. Look up by canonical
    bill_id (UUID), or by jurisdiction + identifier (+ optional session to
    disambiguate)."""
    return tools.get_bill_record(bill_id, jurisdiction, session, identifier, include_full_text)


@mcp.tool()
def compare_bill_versions(
    bill_id: str,
    version_id_a: str | None = None,
    version_id_b: str | None = None,
) -> dict:
    """Deterministic diff (unified + structured) between two versions of a
    bill's extracted text. Defaults to earliest vs. latest texted version if
    version ids are omitted. Errors if fewer than 2 versions have extracted
    text. Result is labeled 'derived' (computed by Bill Commons, not an
    official document)."""
    return tools.compare_bill_versions(bill_id, version_id_a, version_id_b)


@mcp.tool()
def find_similar_bills(bill_id: str, limit: int | None = None) -> dict:
    """Find bills with similar titles via deterministic trigram similarity,
    across jurisdictions. Labeled 'derived' -- not an official
    cross-reference or companion-bill designation."""
    return tools.find_similar_bills(bill_id, limit)


@mcp.tool()
def get_vote_details(vote_event_id: str | None = None, bill_id: str | None = None) -> dict:
    """Fetch vote event(s) with member-level (yes/no/other/absent) vote
    records. Provide vote_event_id for one vote, or bill_id for all recorded
    votes on that bill."""
    return tools.get_vote_details(vote_event_id, bill_id)


@mcp.tool()
def get_upcoming_hearings(
    jurisdiction: str | None = None, bill_id: str | None = None, limit: int | None = None
) -> dict:
    """List upcoming (future-dated) legislative hearings/events, optionally
    filtered by jurisdiction and/or bill."""
    return tools.get_upcoming_hearings(jurisdiction, bill_id, limit)


@mcp.tool()
def trace_legislative_history(bill_id: str) -> dict:
    """Full chronological legislative history for a bill: merged timeline of
    actions, version publications, and votes, plus related-bill links."""
    return tools.trace_legislative_history(bill_id)


@mcp.tool()
def build_legislative_evidence_packet(
    bill_id: str,
    include_full_text: bool = False,
    question: str | None = None,
) -> dict:
    """Compile a citation-ready evidence packet for a bill: full official
    record, legislative history timeline, votes with member-level detail, and
    hearings -- each explicitly labeled official vs. derived with source URLs.

    Returns a `how_to_cite` block with a one-line `cite_as` sentence, a human
    `permalink`, a JSON `download_url`, and a `snapshot_id` that changes if and
    only if a cited fact about the bill changes -- so a reader can check later
    whether the citation still holds. Snapshots are NOT archived; the id is a
    change detector, not a way to retrieve an older packet.

    Pass `question` to record what the packet was assembled to answer; it is
    echoed into the artifact so a filed packet carries its own scope."""
    return tools.build_legislative_evidence_packet(bill_id, include_full_text, question)


@mcp.tool()
def get_jurisdiction_coverage(state: str | None = None) -> dict:
    """Return the jurisdiction_coverage state-machine status for one state
    (abbreviation or name), or the full 51-jurisdiction coverage matrix if
    state is omitted."""
    return tools.get_jurisdiction_coverage(state)


@mcp.tool()
def get_active_sessions(jurisdiction: str | None = None) -> dict:
    """List legislative sessions currently flagged active, optionally
    filtered by jurisdiction (two-letter state code or name)."""
    return tools.get_active_sessions(jurisdiction)


def build_app():
    """Return the ASGI app: FastMCP's Streamable HTTP Starlette app wrapped
    in the client tagger and the per-IP rate limiter. Used both by `__main__`
    (uvicorn.run below) and by tests that want to boot the app in-process.

    Order matters: the rate limiter is outermost so a rejected request never
    reaches the tagger, and the tagger sits directly outside FastMCP so the
    ContextVar it sets is still current when a tool records its invocation."""
    app = ClientFamilyMiddleware(mcp.streamable_http_app())
    return RateLimitMiddleware(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        build_app(),
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )
