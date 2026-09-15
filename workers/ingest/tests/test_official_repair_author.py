"""Bind an authored proposal to retained evidence without running its code."""
import hashlib
import json

import pytest

from billcommons_ingest import official_repair_author as author
from billcommons_ingest import official_repair_author_provider as provider
from billcommons_ingest.official_repair_bundle import RepairBundleError, _manifest_sha256, build_repair_bundle
from billcommons_ingest.official_repair_proposal import validate_repair_proposal
from billcommons_ingest.official_repair_evaluation import evaluate_repair_proposal
from .test_official_repair_bundle import _archive, _failure_db
from .test_official_repair_sandbox import isolated_supervisor_state as isolated_supervisor_state


def _bundle(tmp_path):
    db, observation = _failure_db(raw=_archive())
    root = tmp_path / "bundle"
    manifest = build_repair_bundle(db, observation.id, root)
    return root, _manifest_sha256(manifest)


def _response(context, disposition="propose", candidate=None):
    return {"result": {"disposition": disposition, "rationale": "Synthetic adapter response for this test.",
            "candidate_source": candidate if candidate is not None else (
                context["baseline_source"] + "\n# authored test candidate\n" if disposition == "propose" else ""),
            "regression_source": "def run_regression(*args, **kwargs):\n    return None\n" if disposition == "propose" else ""},
            "evidence": {"request_sha256": "a" * 64, "response_sha256": "b" * 64,
                         "model": "synthetic-model", "response_id": "synthetic", "usage": {}}}


def test_authored_proposal_executes_only_in_explicit_evaluation(tmp_path, monkeypatch):
    root, digest = _bundle(tmp_path)
    monkeypatch.setattr(provider, "request_repair_author", lambda context, **kw: _response(context))
    output = tmp_path / "authored"
    report = author.author_repair_bundle(root, expected_manifest_sha256=digest, model="synthetic", output_dir=output)
    assert report["status"] == "proposal_prepared"
    assert report["execution_authorized"] is report["promotion_authorized"] is False
    proposal = validate_repair_proposal(output / "proposal", expected_proposal_sha256=report["proposal_sha256"])
    assert proposal["baseline_manifest_sha256"] == digest
    for name, field in (("context.json", "context_sha256"), ("author-output.json", "author_output_sha256")):
        assert _manifest_sha256(json.loads((output / name).read_bytes())) == report[field]
    evaluation = evaluate_repair_proposal(output / "proposal", expected_proposal_sha256=report["proposal_sha256"],
                                          run_regressions=True)
    assert evaluation["comparison"] == "same_as_current_baseline"
    assert evaluation["regression_tests"]["candidate"]["status"] == "returned_without_error"


def test_candidate_is_not_loaded_during_authoring(tmp_path, monkeypatch):
    root, digest = _bundle(tmp_path)
    marker = tmp_path / "must-not-exist"
    candidate = f"open({str(marker)!r}, 'w').write('executed')\nraise RuntimeError('inert')\n"
    monkeypatch.setattr(provider, "request_repair_author", lambda context, **kw: _response(context, candidate=candidate))
    report = author.author_repair_bundle(root, expected_manifest_sha256=digest, model="synthetic",
                                         output_dir=tmp_path / "authored")
    assert report["status"] == "proposal_prepared"
    assert not marker.exists()


@pytest.mark.parametrize("disposition", ["no_change", "needs_more_evidence"])
def test_nonproposal_dispositions_preserve_evidence_without_inventing_a_patch(tmp_path, monkeypatch, disposition):
    root, digest = _bundle(tmp_path)
    monkeypatch.setattr(provider, "request_repair_author", lambda context, **kw: _response(context, disposition))
    output = tmp_path / "authored"
    report = author.author_repair_bundle(root, expected_manifest_sha256=digest, model="synthetic", output_dir=output)
    assert report["status"] == disposition
    assert report["proposal_sha256"] is None
    assert {path.name for path in output.iterdir()} == {"authoring.json", "context.json", "author-output.json"}


def test_bad_bundle_digest_stops_before_provider_call(tmp_path, monkeypatch):
    root, _ = _bundle(tmp_path)
    monkeypatch.setattr(provider, "request_repair_author", lambda *a, **kw: pytest.fail("must not contact provider"))
    with pytest.raises(RepairBundleError):
        author.author_repair_bundle(root, expected_manifest_sha256="0" * 64, model="synthetic",
                                     output_dir=tmp_path / "authored")
    assert not (tmp_path / "authored").exists()


def test_existing_output_preserved_without_provider_call(tmp_path, monkeypatch):
    root, digest = _bundle(tmp_path)
    output = tmp_path / "authored"
    output.mkdir()
    (output / "keep").write_text("existing")
    monkeypatch.setattr(provider, "request_repair_author", lambda *a, **kw: pytest.fail("must not contact provider"))
    with pytest.raises(RepairBundleError):
        author.author_repair_bundle(root, expected_manifest_sha256=digest, model="synthetic", output_dir=output)
    assert (output / "keep").read_text() == "existing"


def test_provider_failure_publishes_no_partial_authoring(tmp_path, monkeypatch):
    root, digest = _bundle(tmp_path)
    def fail(*args, **kwargs):
        raise provider.AuthorProviderError("synthetic_failure")
    monkeypatch.setattr(provider, "request_repair_author", fail)
    with pytest.raises(provider.AuthorProviderError):
        author.author_repair_bundle(root, expected_manifest_sha256=digest, model="synthetic",
                                     output_dir=tmp_path / "authored")
    assert not (tmp_path / "authored").exists()
    assert not list(tmp_path.glob(".authored.repair-author-*"))


def test_context_preserves_fixture_prefix_hashes_and_marks_truncation(tmp_path, monkeypatch):
    root, digest = _bundle(tmp_path)
    monkeypatch.setattr(author, "TABLE_PREFIX_BYTES", 7)
    context = author.build_author_context(root, expected_manifest_sha256=digest)
    assert context["baseline_manifest_sha256"] == digest
    assert hashlib.sha256(context["baseline_source"].encode()).hexdigest() == context["bundle_manifest"]["candidate_parser"]["source_sha256"]
    assert context["source_excerpts"]["not_an_expected_fact_oracle"] is True
    for table in context["source_excerpts"]["tables"]:
        assert table["truncated"] is True
        assert table["prefix_byte_count"] == 7
        assert hashlib.sha256(table["prefix_text"].encode()).hexdigest() == table["prefix_sha256"]


def test_malformed_archive_does_not_become_fabricated_source_context():
    context = author._archive_context(b"not an archive")
    assert context["status"] == "unavailable_under_current_archive_policy"
    assert context["tables"] == []
