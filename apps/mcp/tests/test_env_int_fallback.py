"""Regression test for Finding 4: a malformed MCP_* env int value must never
crash the whole MCP server at import time.

`billcommons_mcp.tools` used to parse `MCP_MAX_COMPARE_TEXT_BYTES` (and, now,
the evidence-packet caps too) with a bare `int(os.environ.get(name, default))`
at MODULE IMPORT TIME -- an operator typo (a trailing space, a unit suffix
like "1500000B", an empty string set instead of unset) would raise ValueError
straight out of the module body, before `server.py` could even register a
single tool, taking the entire public MCP server down on boot.

`_env_int` (already defined in rate_limit.py for the same reason) is a
defensive int parser that falls back to the default on any parse failure
instead of raising. This test proves `billcommons_mcp.tools`'s module-level
constants actually go through it (not a re-implemented, possibly-buggy copy)
by importing the module fresh, in a subprocess, with a deliberately-malformed
env value set -- a fresh subprocess is necessary because module-level
constants are computed once at import time; re-importing an already-imported
module in the same process wouldn't re-run that code.

Run directly (no pytest config wires up apps/mcp's testpaths -- see
docs in this directory):
    .venv/bin/python -m pytest apps/mcp/tests/test_env_int_fallback.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

MCP_APP_DIR = Path(__file__).resolve().parent.parent

_CHECK_SCRIPT = (
    "import billcommons_mcp.tools as t; "
    "print(t.MAX_COMPARE_TEXT_BYTES); "
    "print(t.MAX_EVIDENCE_VOTES); "
    "print(t.MAX_EVIDENCE_HEARINGS); "
    "print(t.MAX_EVIDENCE_HISTORY_ITEMS)"
)


def _run_import_with_env(extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", _CHECK_SCRIPT],
        cwd=MCP_APP_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_malformed_max_compare_text_bytes_falls_back_to_default_no_crash():
    result = _run_import_with_env({"MCP_MAX_COMPARE_TEXT_BYTES": "not-a-number"})
    assert result.returncode == 0, (
        f"import must not crash on a malformed env value; stderr:\n{result.stderr}"
    )
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "1500000", "malformed value must fall back to the documented default"


def test_malformed_evidence_caps_fall_back_to_default_no_crash():
    result = _run_import_with_env(
        {
            "MCP_MAX_EVIDENCE_VOTES": "abc",
            "MCP_MAX_EVIDENCE_HEARINGS": "12.5",
            "MCP_MAX_EVIDENCE_HISTORY_ITEMS": "",
        }
    )
    assert result.returncode == 0, (
        f"import must not crash on malformed evidence-cap env values; stderr:\n{result.stderr}"
    )
    lines = result.stdout.strip().splitlines()
    assert lines[1] == "100", "malformed MCP_MAX_EVIDENCE_VOTES must fall back to default 100"
    assert lines[2] == "100", "malformed MCP_MAX_EVIDENCE_HEARINGS must fall back to default 100"
    assert lines[3] == "500", "empty MCP_MAX_EVIDENCE_HISTORY_ITEMS must fall back to default 500"


def test_valid_env_value_is_actually_used_not_just_ignored():
    """Sanity check the other direction: a well-formed env value must still
    be honored, proving this isn't a fallback-always stub."""
    result = _run_import_with_env({"MCP_MAX_COMPARE_TEXT_BYTES": "42"})
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "42"
