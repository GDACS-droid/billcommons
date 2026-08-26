"""Every symbol cli.py reaches for on the status module must exist.

On 2026-08-02 a refactor of status.py deleted ActionRow, derive_status,
status_for_action and both text/classification helpers while cli.py went on
calling them. `recompute_status_for_bills` -- the function that derives every
bill's status -- therefore raised AttributeError on every invocation for about
seven hours.

It failed silently by construction. The adjournment sweep wraps its call in
`except Exception: traceback.print_exc()`, so the pipeline rolled back, printed
into Railway's log stream, and carried on looking healthy. No status was ever
written wrong; statuses simply stopped being computed, which is invisible from
outside.

The API and MCP suites were green throughout, because neither imports this
package. This test is the cheap structural check that closes that gap: it needs
no database, no fixtures, and it fails the moment the two files disagree.
"""
from __future__ import annotations

import ast
import pathlib

from billcommons_ingest import status as status_mod

CLI = pathlib.Path(__file__).resolve().parents[1] / "billcommons_ingest" / "cli.py"


def _referenced_status_attributes() -> set[str]:
    """Every `status_mod.NAME` in cli.py, read from the AST rather than by
    regex so a name in a comment or a string cannot produce a false failure."""
    tree = ast.parse(CLI.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "status_mod"
        ):
            names.add(node.attr)
    return names


def test_cli_only_uses_symbols_the_status_module_defines():
    referenced = _referenced_status_attributes()
    assert referenced, "found no status_mod usages -- the check is not looking at anything"
    missing = sorted(n for n in referenced if not hasattr(status_mod, n))
    assert not missing, (
        f"cli.py calls status_mod.{{{','.join(missing)}}} which status.py does not "
        "define. recompute_status_for_bills will raise AttributeError at runtime "
        "and the adjournment sweep will swallow it."
    )


def test_the_derivation_entry_points_still_exist():
    """Named explicitly so deleting one fails here rather than in production
    at 3am inside an except block."""
    for name in (
        "ActionRow",
        "derive_status",
        "status_for_action",
        "apply_session_outcome",
        "substitution_target",
        "substitution_lookup_candidates",
        "LIVE_STATUSES",
        "TERMINAL_STATUSES",
        "SUBSTITUTED",
    ):
        assert hasattr(status_mod, name), f"status.{name} is gone"


def test_substitution_lookup_candidates_tolerates_ny_print_version_suffix():
    """"SUBSTITUTED BY A10008C" normalizes to "A 10008C", but the corpus
    identifies the bill as "A 10008" -- the trailing letter is NY's print/
    amendment version, never part of bill identity. Exact match must still
    be tried first, and only when the caller opts in with print_suffix=True
    (NY only -- FL "HB 1A" / CA "AB 1X" use the same shape as identity)."""
    assert status_mod.substitution_lookup_candidates("A 10008C") == ["A 10008C"]
    assert status_mod.substitution_lookup_candidates(
        "A 10008C", print_suffix=True
    ) == ["A 10008C", "A 10008"]
    assert status_mod.substitution_lookup_candidates("A 10008") == ["A 10008"]
    assert status_mod.substitution_lookup_candidates("A 10008", print_suffix=True) == [
        "A 10008"
    ]
    assert status_mod.substitution_lookup_candidates("HB 12") == ["HB 12"]
    # print_suffix is a caller-supplied opt-in, not a jurisdiction lookup --
    # the FL/CA gate (only NY passes print_suffix=True) lives in the CLI's
    # pass-2 resolution, not in this function.
    assert status_mod.substitution_lookup_candidates("HB 1A") == ["HB 1A"]


def test_substituted_is_in_the_vocabulary_and_non_terminal():
    """Added for the substitution-propagation fix (R3): SUBSTITUTED is a LIVE
    status, never a terminal one -- a substituted print only concludes once
    its survivor does, and `recompute_status_for_bills` is what resolves
    that, not this module."""
    assert status_mod.SUBSTITUTED in status_mod.ALL_STATUSES
    assert status_mod.SUBSTITUTED in status_mod.LIVE_STATUSES
    assert status_mod.SUBSTITUTED not in status_mod.TERMINAL_STATUSES
