"""Contract test: every call site that UNPACKS
`recompute_status_for_bills`'s return value must unpack exactly 3 names.

The function returns `(changed, cleared, related_upserted)` -- a 3-tuple.
Before this test existed, `workers/ingest/rederive_active_session_bills.py`
unpacked it as a 2-tuple in two places and would have raised `ValueError:
too many values to unpack` the moment either code path ran (F1, verify
round 1). A functional test of that script would need a live DB fixture and
argparse plumbing just to exercise two lines; walking the AST for every
assignment whose right-hand side calls `recompute_status_for_bills` is the
simple version of the same guarantee, and it covers every caller in the repo
at once, present and future.

Call sites that do not unpack at all (a bare expression statement, or a
single-name assignment) are legal Python and are not this test's concern --
only a tuple/list unpack with the wrong arity ever raises.
"""
from __future__ import annotations

import ast
from pathlib import Path

# Repo root: workers/ingest/tests/ -> workers/ingest/ -> workers/ -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]

FUNC_NAME = "recompute_status_for_bills"

# Directories that are never source we care about, or that would make this
# test wander into an unrelated git worktree / vendored code.
_SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    ".claude",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
}


def _iter_repo_python_files():
    for path in REPO_ROOT.rglob("*.py"):
        rel_parts = path.relative_to(REPO_ROOT).parts
        if any(part in _SKIP_DIR_NAMES for part in rel_parts):
            continue
        yield path


def _call_targets_func(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == FUNC_NAME
    if isinstance(func, ast.Attribute):
        return func.attr == FUNC_NAME
    return False


def _find_bad_unpacks(path: Path) -> list[str]:
    """Return human-readable descriptions of any wrong-arity unpack in
    `path`. Skips the function's own definition."""
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:
        # Not our concern here -- a broken file fails collection elsewhere.
        return []

    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not isinstance(value, ast.Call) or not _call_targets_func(value):
            continue
        for target in node.targets:
            if isinstance(target, (ast.Tuple, ast.List)):
                n = len(target.elts)
                if n != 3:
                    bad.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                        f"unpacks {FUNC_NAME}(...) into {n} name(s), expected 3"
                    )
    return bad


def test_every_recompute_status_for_bills_call_site_unpacks_three_values():
    """Every `a, b, c = recompute_status_for_bills(...)`-shaped assignment in
    the repo must have exactly 3 names on the left. A 2-tuple (or 4-tuple)
    unpack raises ValueError the instant that code path runs -- this test
    fails at collection time instead of in production."""
    problems: list[str] = []
    for path in _iter_repo_python_files():
        problems.extend(_find_bad_unpacks(path))
    assert not problems, "wrong-arity recompute_status_for_bills unpack(s):\n" + "\n".join(problems)


def _has_call_site(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_targets_func(node):
            return True
    return False


def test_at_least_one_call_site_exists_so_this_test_is_not_vacuous():
    """Guards against the AST walk silently finding nothing (e.g. a path
    filter bug) and the test above passing for the wrong reason."""
    found_any = any(_has_call_site(path) for path in _iter_repo_python_files())
    assert found_any, "expected to find at least one caller of recompute_status_for_bills"
