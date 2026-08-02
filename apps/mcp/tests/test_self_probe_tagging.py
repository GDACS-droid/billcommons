"""The uptime monitor's calls must be distinguishable from a user's.

The read-path monitor calls a real MCP tool every two minutes on purpose -- an
`initialize` handshake succeeds against a dead database, so nothing cheaper
detects the outage it exists to catch. The cost is ~720 synthetic tool calls a
day landing in the same table that answers "is anyone using this?". Hours after
telemetry shipped, 61 of 63 recorded calls were the monitor.

Scope of this file, stated honestly: it tests the middleware contract -- tag
set, tag not set, tag reset between requests. It does NOT prove the ContextVar
survives FastMCP's StreamableHTTPSessionManager task group into the tool call,
because driving that stack in-process deadlocks on the streaming response.
That leg is verified against production after deploy by reading back
client_family on a row the monitor itself wrote. If the tag were lost there,
the symptom would be a flattering usage number and no error at all, so the
check is written down in docs/operations/incident-runbook.md rather than left
to memory.
"""
import asyncio

from billcommons_mcp import telemetry
from billcommons_shared.telemetry_constants import PROBE_FAMILY, PROBE_HEADER

PROBE_HEADER_BYTES = PROBE_HEADER.encode("ascii")


def _scope(headers: list[tuple[bytes, bytes]]) -> dict:
    return {"type": "http", "headers": headers}


def _drive(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Run the middleware over one request; return what a tool would have seen."""
    seen: list[str | None] = []

    async def inner_app(scope, receive, send):
        # Stands in for a tool call: the only thing that matters is what
        # current_client_family() reports at the moment telemetry records.
        seen.append(telemetry.current_client_family())

    async def noop():
        return {}

    app = telemetry.ClientFamilyMiddleware(inner_app)
    asyncio.run(app(_scope(headers), noop, noop))
    return seen[0]


def test_probe_header_tags_the_call_as_self_probe():
    assert _drive([(PROBE_HEADER_BYTES, b"read-path-monitor")]) == PROBE_FAMILY


def test_a_call_without_the_header_is_not_tagged():
    """An untagged caller counted as our own monitor would be real usage
    silently subtracted from the published figure."""
    assert _drive([]) != PROBE_FAMILY
    assert _drive([(b"user-agent", b"billcommons-read-path-monitor/1.0")]) != PROBE_FAMILY


def test_the_tag_does_not_leak_between_requests():
    """A ContextVar set but never reset would mark every later caller in the
    process as a probe, permanently zeroing the usage figure."""
    assert _drive([(PROBE_HEADER_BYTES, b"read-path-monitor")]) == PROBE_FAMILY
    assert _drive([]) != PROBE_FAMILY


def test_non_http_scopes_pass_through_untouched():
    """Lifespan startup must not be swallowed -- FastMCP's session manager
    raises "Task group is not initialized" if it never sees it, which takes the
    whole MCP server down."""
    delivered: list[str] = []

    async def inner_app(scope, receive, send):
        delivered.append(scope["type"])

    async def noop():
        return {}

    app = telemetry.ClientFamilyMiddleware(inner_app)
    asyncio.run(app({"type": "lifespan"}, noop, noop))
    assert delivered == ["lifespan"]


def test_the_monitor_actually_sends_the_header():
    """The tag is worthless if the one caller meant to set it does not. Reads
    the monitor source rather than trusting that the two files agree."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[3]
        / "infra"
        / "monitoring"
        / "read_path_monitor.py"
    ).read_text()
    assert PROBE_HEADER in src, "read_path_monitor.py no longer sends the probe header"


def test_composed_tools_do_not_double_count():
    """One user request must record ONE invocation.

    build_legislative_evidence_packet composes the legislative-history query.
    While it called the PUBLIC tool to do that, every packet recorded two
    invocations -- and since the packet is the flagship, usage was inflated for
    exactly the tool we most wanted an honest count of. Verified in production
    on 2026-08-02: nine recorded calls, four of which were phantom traces
    emitted by four real packet requests.

    Same failure as the uptime monitor, one layer in: the metric measuring
    itself. Any future tool that composes another must call the _body form.
    """
    import inspect

    from billcommons_mcp import tools

    src = inspect.getsource(tools.build_legislative_evidence_packet)
    assert "_trace_legislative_history_body" in src
    # The bare public name would re-enter _run_tool and record a second row.
    assert "= trace_legislative_history(" not in src
