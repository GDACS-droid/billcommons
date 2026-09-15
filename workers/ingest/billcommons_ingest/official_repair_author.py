"""Ask a text-only model to author an inert, evidence-bound CA repair proposal."""
from __future__ import annotations

import argparse
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import time
import uuid
import zipfile

from billcommons_shared import ca_official_actions as ca
from billcommons_ingest.official_repair_bundle import (
    FIXTURE_NAME, RepairBundleError, _canonical_json_bytes, _manifest_sha256,
    _output_path, validate_repair_bundle,
)
from billcommons_ingest.official_repair_evaluation import _verified_bytes
from billcommons_ingest.official_repair_proposal import prepare_repair_proposal
from billcommons_ingest.official_repair_sandbox import MAX_FIXTURE_BYTES, MAX_SOURCE_BYTES

AUTHOR_VERSION = "official-repair-author/1"
TABLE_PREFIX_BYTES = 24 * 1024


def _archive_context(raw: bytes) -> dict:
    """Return explicitly partial source text, never an inferred expected answer."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            # Reuse the installed parser's archive admission and CRC checks;
            # samples never extract paths or expand its accepted ZIP envelope.
            ca._validate_zip(archive, deadline=time.monotonic() + 5.0)
            tables = []
            for name in ("BILL_TBL.dat", "BILL_HISTORY_TBL.dat"):
                info = archive.getinfo(name)
                with archive.open(name) as stream:
                    prefix = stream.read(TABLE_PREFIX_BYTES)
                text = prefix.decode("utf-8", errors="replace")
                tables.append({"name": name, "declared_byte_count": info.file_size,
                               "prefix_byte_count": len(prefix),
                               "prefix_sha256": hashlib.sha256(prefix).hexdigest(),
                               "truncated": len(prefix) < info.file_size,
                               "utf8_replacement_present": "\ufffd" in text,
                               "prefix_text": text})
            return {"status": "bounded_table_prefixes", "tables": tables,
                    "not_an_expected_fact_oracle": True}
    except (ca.OfficialCaActionsError, zipfile.BadZipFile, OSError, ValueError,
            RuntimeError, NotImplementedError, EOFError):
        # Never copy an exception containing archive-controlled strings into
        # the report or imply that unavailable samples establish a repair.
        return {"status": "unavailable_under_current_archive_policy", "tables": [],
                "not_an_expected_fact_oracle": True}


def build_author_context(bundle_dir: str | Path, *, expected_manifest_sha256: str) -> dict:
    root = Path(bundle_dir).absolute()
    manifest = validate_repair_bundle(root, expected_manifest_sha256=expected_manifest_sha256)
    source_path = inspect.getsourcefile(ca.parse_ca_official_actions_zip)
    if source_path is None:
        raise RepairBundleError("baseline parser source is unavailable")
    source = _verified_bytes(Path(source_path), digest=manifest["candidate_parser"]["source_sha256"],
                             maximum=MAX_SOURCE_BYTES)
    fixture = _verified_bytes(root / FIXTURE_NAME, digest=manifest["fixture"]["sha256"],
                              maximum=MAX_FIXTURE_BYTES)
    return {"author_version": AUTHOR_VERSION, "baseline_manifest_sha256": expected_manifest_sha256,
            "bundle_manifest": manifest, "baseline_source": source.decode("utf-8"),
            "source_excerpts": _archive_context(fixture)}


def author_repair_bundle(bundle_dir: str | Path, *, expected_manifest_sha256: str,
                         model: str, output_dir: str | Path) -> dict:
    """Publish model-authored data, without executing, installing or promoting it."""
    from billcommons_ingest.official_repair_author_provider import request_repair_author

    destination = _output_path(output_dir)
    context = build_author_context(bundle_dir, expected_manifest_sha256=expected_manifest_sha256)
    stage = destination.parent / f".{destination.name}.repair-author-{uuid.uuid4().hex}"
    stage.mkdir(mode=0o700)
    try:
        context_bytes = _canonical_json_bytes(context)
        (stage / "context.json").write_bytes(context_bytes + b"\n")
        authored = request_repair_author(context, model=model, api_key=os.environ.get("OPENAI_API_KEY", ""))
        result = authored["result"]
        result_bytes = _canonical_json_bytes(result)
        (stage / "author-output.json").write_bytes(result_bytes + b"\n")
        report = {"author_version": AUTHOR_VERSION,
                  "baseline_manifest_sha256": expected_manifest_sha256,
                  "context_sha256": hashlib.sha256(context_bytes).hexdigest(),
                  "author_output_sha256": hashlib.sha256(result_bytes).hexdigest(),
                  "provider": authored["evidence"], "disposition": result["disposition"],
                  "status": result["disposition"], "proposal_sha256": None,
                  "execution_authorized": False, "promotion_authorized": False}
        if result["disposition"] == "propose":
            candidate = stage / "candidate.py.txt"
            regression = stage / "regression.py.txt"
            candidate.write_text(result["candidate_source"], encoding="utf-8")
            regression.write_text(result["regression_source"], encoding="utf-8")
            proposal = prepare_repair_proposal(bundle_dir,
                expected_manifest_sha256=expected_manifest_sha256,
                candidate_source=candidate, regression_source=regression,
                output_dir=stage / "proposal")
            report["proposal_sha256"] = _manifest_sha256(proposal)
            report["status"] = "proposal_prepared"
            candidate.unlink()
            regression.unlink()
        (stage / "authoring.json").write_bytes(_canonical_json_bytes(report) + b"\n")
        _output_path(destination)
        os.replace(stage, destination)
        return report
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main() -> int:
    from billcommons_ingest.official_repair_author_provider import AuthorProviderError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_dir", type=Path)
    parser.add_argument("--baseline-sha256", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = author_repair_bundle(args.bundle_dir, expected_manifest_sha256=args.baseline_sha256,
                                      model=args.model, output_dir=args.output_dir)
    except (AuthorProviderError, RepairBundleError, OSError, ValueError):
        print(json.dumps({"status": "authoring_failed", "execution_authorized": False}))
        return 2
    print(json.dumps({"status": report["status"], "authoring_sha256": _manifest_sha256(report),
                      "proposal_sha256": report["proposal_sha256"], "execution_authorized": False}))
    return 0 if report["status"] == "proposal_prepared" else 1


if __name__ == "__main__":
    raise SystemExit(main())
