"""Integration test for the Bill Commons MCP server.

Connects to a running server over Streamable HTTP (env MCP_TEST_URL,
default http://localhost:8400/mcp), lists tools, and calls a few tools with
real arguments, asserting structured response shapes. This is meant to run
against a booted `server.py` process (see docs in apps/mcp), not to boot the
server itself.

Run: MCP_TEST_URL=http://localhost:8400/mcp .venv/bin/python apps/mcp/tests/mcp_integration.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

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


async def main() -> None:
    url = os.environ.get("MCP_TEST_URL", DEFAULT_URL)
    print(f"connecting to {url} ...")

    async with streamablehttp_client(url) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # --- list_tools: assert all 10 required tools are present ---
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

    print("\nALL MCP INTEGRATION CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
