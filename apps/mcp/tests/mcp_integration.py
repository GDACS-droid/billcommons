"""Integration test for the Bill Commons MCP server.

Connects to a running server over Streamable HTTP (env MCP_TEST_URL,
default http://localhost:8400/mcp), lists tools, and calls a few tools with
real arguments, asserting structured response shapes. This is meant to run
against a booted `server.py` process (see docs in apps/mcp), not to boot the
server itself.

Also covers Finding 2 hardening: a request burst past the per-IP rate limit
must get structured 429 rejections (not hang/500), and an oversized
compare_bill_versions call must get a structured `text_too_large` tool
error rather than attempting an unbounded diff.

Run: MCP_TEST_URL=http://localhost:8400/mcp .venv/bin/python apps/mcp/tests/mcp_integration.py

For the rate-limit test to actually trip within a short burst, boot the
server with a small limit, e.g.:
    PORT=8410 MCP_RATE_LIMIT_PER_MINUTE=10 MCP_RATE_LIMIT_BURST=5 \\
        .venv/bin/python apps/mcp/server.py &
    MCP_TEST_URL=http://localhost:8410/mcp .venv/bin/python apps/mcp/tests/mcp_integration.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

DEFAULT_URL = "http://localhost:8400/mcp"

REQUIRED_TOOLS = {
    "search_legislation",
    "get_bill_record",
    "compare_bill_versions",
    "find_similar_bills",
    "get_vote_details",
    "get_upcoming_hearings",
    "trace_legislative_history",
    "build_legislative_evidence_packet",
    "get_jurisdiction_coverage",
    "get_active_sessions",
    "list_topics",
}


def _tool_result_json(result) -> dict:
    """Extract the JSON dict a FastMCP tool returned (as structured content
    or as a JSON text block, depending on SDK version)."""
    if getattr(result, "structuredContent", None):
        # FastMCP wraps non-object-with-"result" dict returns directly.
        return result.structuredContent
    assert result.content, "tool call returned no content"
    block = result.content[0]
    text = getattr(block, "text", None)
    assert text is not None, f"unexpected content block type: {block!r}"
    return json.loads(text)


async def check_core_tools(url: str) -> None:
    print(f"connecting to {url} ...")

    async with streamablehttp_client(url) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # --- list_tools: assert all required tools are present ---
            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            missing = REQUIRED_TOOLS - tool_names
            assert not missing, f"missing required tools: {missing}"
            print(f"OK: all {len(REQUIRED_TOOLS)} required tools present ({sorted(tool_names)})")

            # --- get_active_sessions() ---
            result = await session.call_tool("get_active_sessions", {})
            assert not result.isError, f"get_active_sessions errored: {result}"
            payload = _tool_result_json(result)
            assert "active_sessions" in payload, payload
            assert "meta" in payload and "retrieved_at" in payload["meta"], payload
            assert isinstance(payload["active_sessions"], list), payload
            print(f"OK: get_active_sessions -> {len(payload['active_sessions'])} sessions, "
                  f"result_count={payload.get('result_count')}, "
                  f"note={payload.get('note')!r}")

            # --- search_legislation(q='test') ---
            result = await session.call_tool("search_legislation", {"q": "test"})
            assert not result.isError, f"search_legislation errored: {result}"
            payload = _tool_result_json(result)
            assert "results" in payload, payload
            assert "match_type" in payload, payload
            assert "meta" in payload and "retrieved_at" in payload["meta"], payload
            assert isinstance(payload["results"], list), payload
            print(f"OK: search_legislation(q='test') -> match_type={payload['match_type']!r}, "
                  f"{len(payload['results'])} results")

            # --- get_jurisdiction_coverage(state='NC') ---
            result = await session.call_tool("get_jurisdiction_coverage", {"state": "NC"})
            assert not result.isError, f"get_jurisdiction_coverage(state='NC') transport-errored: {result}"
            payload = _tool_result_json(result)
            if "error" in payload:
                # Acceptable only if NC hasn't been seeded as a jurisdiction yet;
                # must still be a structured error, not a crash/empty body.
                assert payload["error"].get("code") == "unknown_jurisdiction", payload
                print(f"OK (structured error, NC not yet seeded): {payload['error']}")
            else:
                assert "jurisdiction" in payload, payload
                assert "coverage" in payload, payload
                assert "status" in payload, payload
                assert "meta" in payload and "retrieved_at" in payload["meta"], payload
                print(
                    f"OK: get_jurisdiction_coverage(state='NC') -> "
                    f"jurisdiction={payload['jurisdiction']!r}, status={payload['status']!r}, "
                    f"{len(payload['coverage'])} coverage row(s)"
                )

            # --- get_jurisdiction_coverage() full matrix, sanity check ---
            result = await session.call_tool("get_jurisdiction_coverage", {})
            assert not result.isError, f"get_jurisdiction_coverage() errored: {result}"
            payload = _tool_result_json(result)
            assert "coverage_matrix" in payload, payload
            assert "jurisdiction_count" in payload, payload
            print(
                f"OK: get_jurisdiction_coverage() full matrix -> "
                f"{payload['jurisdiction_count']} jurisdiction(s) "
                f"(note={payload.get('note')!r})"
            )

            # --- list_topics() ---
            result = await session.call_tool("list_topics", {})
            assert not result.isError, f"list_topics errored: {result}"
            payload = _tool_result_json(result)
            assert "topics" in payload and isinstance(payload["topics"], list), payload
            assert payload["topics"], "expected at least one curated topic"
            assert "meta" in payload and "retrieved_at" in payload["meta"], payload
            first = payload["topics"][0]
            for key in ("slug", "name", "description", "bill_count", "how_to_fetch_bills"):
                assert key in first, f"list_topics topic missing {key!r}: {first}"
            print(f"OK: list_topics -> {len(payload['topics'])} topic(s)")


async def check_rate_limit_burst(url: str) -> None:
    """Finding 2a: fire a burst of raw HTTP requests at the MCP endpoint and
    assert that once the per-IP token bucket is exhausted, the server starts
    rejecting with structured 429s (JSON body with error.code=='rate_limited')
    rather than accepting unbounded traffic. Uses a plain `ping` JSON-RPC body
    so this doesn't depend on any DB-backed tool."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    statuses: list[int] = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Fire enough requests to exceed any reasonable configured
        # capacity (steady rate + burst) within one token-bucket window.
        for _ in range(80):
            resp = await client.post(url, json=body, headers=headers)
            statuses.append(resp.status_code)
            if resp.status_code == 429:
                rejected_body = resp.json()
                assert rejected_body.get("error", {}).get("code") == "rate_limited", rejected_body
                break

    assert 429 in statuses, (
        "expected at least one 429 rate_limited rejection in a burst of "
        f"{len(statuses)} requests -- got statuses: {statuses}. Either the "
        "rate limiter isn't wired in, or the server was booted with a "
        "capacity too high for this burst size (see MCP_RATE_LIMIT_PER_MINUTE"
        "/MCP_RATE_LIMIT_BURST env vars in the module docstring)."
    )
    print(
        f"OK: rate limiter rejected burst traffic with 429 after "
        f"{statuses.index(429) + 1} request(s) (statuses so far: {statuses})"
    )


async def check_oversized_compare_is_structured_error(url: str) -> None:
    """Finding 2b: compare_bill_versions must refuse (structured
    `text_too_large` error) rather than attempt an unbounded diff when
    combined version text exceeds the configured cap. Requires the server to
    be booted with MCP_MAX_COMPARE_TEXT_BYTES set low enough that a real
    seeded bill's version text pair exceeds it (see module docstring)."""
    bill_id = os.environ.get("MCP_TEST_COMPARE_BILL_ID")
    if not bill_id:
        print(
            "SKIP: oversized-compare check needs MCP_TEST_COMPARE_BILL_ID "
            "(a bill_id with >=2 texted versions) -- set alongside a low "
            "MCP_MAX_COMPARE_TEXT_BYTES on the server process."
        )
        return

    async with streamablehttp_client(url) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("compare_bill_versions", {"bill_id": bill_id})
            payload = _tool_result_json(result)
            assert "error" in payload, f"expected a structured error, got: {payload}"
            assert payload["error"]["code"] in {"text_too_large", "insufficient_versions"}, payload
            if payload["error"]["code"] == "text_too_large":
                print(f"OK: oversized compare_bill_versions refused -> {payload['error']}")
            else:
                print(
                    "SKIP (bill_id has <2 texted versions in this DB, not a "
                    f"cap failure): {payload['error']}"
                )


async def main() -> None:
    url = os.environ.get("MCP_TEST_URL", DEFAULT_URL)

    await check_core_tools(url)
    # Oversized-compare check runs before the rate-limit burst test: both
    # share one per-IP token bucket (this script talks to the server from a
    # single client IP), and the burst test deliberately exhausts it.
    await check_oversized_compare_is_structured_error(url)
    await check_rate_limit_burst(url)

    print("\nALL MCP INTEGRATION CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
