"""Constants shared between the MCP writer and the API reader of
`tool_invocations`.

Why a shared module rather than the obvious import: `Dockerfile.api` copies
only `packages/` and `apps/api`. An API module importing `billcommons_mcp`
passes every local test -- the whole repo is on the path -- and then fails at
container start with ModuleNotFoundError. That exact mistake nearly shipped a
production outage on 2026-08-02 (an API import of a `workers/` helper), and is
covered by apps/api/tests/test_container_import_boundary.py.

A duplicated string literal in two packages would work until someone changed
one of them, at which point the usage figures would silently stop excluding the
monitor -- a failure with no error and no symptom except a flattering number.
"""
from __future__ import annotations

# client_family value marking a call from our own uptime monitor rather than a
# user. Written by billcommons_mcp.telemetry, subtracted by
# GET /api/v1/stats/usage.
PROBE_FAMILY = "self-probe"

# HTTP header the monitor sets to claim that identity. Deliberately not a
# User-Agent match: a UA is trivially spoofable, and anything a stranger can
# set to erase their own calls from a public usage figure is a bad idea.
PROBE_HEADER = "x-billcommons-probe"
