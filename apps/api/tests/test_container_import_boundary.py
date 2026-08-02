"""The API must not import from workers/ -- its container does not ship them.

infra/docker/Dockerfile.api copies only requirements.txt, packages/schema,
packages/shared and apps/api. A `from billcommons_ingest...` import therefore
passes every local test, where the whole repo is on sys.path, and then fails at
container start -- taking the entire API down on deploy rather than in CI.

This nearly shipped on 2026-08-02: the enrolled-window helper was written in
workers/ingest/billcommons_ingest/status.py and imported from the bills router.
It now lives in billcommons_shared.enrollment, which both containers get.
"""
import pathlib
import re

API_ROOT = pathlib.Path(__file__).resolve().parents[1] / "billcommons_api"
FORBIDDEN = re.compile(r"^\s*(?:from|import)\s+(billcommons_ingest|billcommons_alerts)\b", re.M)


def test_api_package_imports_nothing_from_workers():
    offenders = []
    for path in API_ROOT.rglob("*.py"):
        for match in FORBIDDEN.finditer(path.read_text()):
            offenders.append(f"{path.relative_to(API_ROOT.parent)}: {match.group(0).strip()}")
    assert not offenders, (
        "the API image does not contain workers/ -- these imports would fail at "
        "container start, not in CI:\n  " + "\n  ".join(offenders)
    )


def test_the_dockerfile_still_excludes_workers():
    """If the image ever DOES ship workers/, this guard should be revisited
    deliberately rather than silently becoming a lie."""
    dockerfile = (
        pathlib.Path(__file__).resolve().parents[3] / "infra/docker/Dockerfile.api"
    ).read_text()
    copied = re.findall(r"^COPY\s+(\S+)", dockerfile, re.M)
    assert not any(c.startswith("workers") for c in copied), (
        "Dockerfile.api now copies workers/ — update or delete this guard on purpose"
    )
