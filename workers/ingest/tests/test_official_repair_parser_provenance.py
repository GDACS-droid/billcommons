"""Pure provenance checks for the CA parser replay fingerprint."""
from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path

from billcommons_ingest import official_ca_actions as wrapper
from billcommons_ingest.official_repair_plan import _candidate_parser_sha256
from billcommons_shared import ca_official_actions as shared_parser


def _loaded_parser(monkeypatch, tmp_path: Path, name: str, source: str):
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    monkeypatch.setitem(sys.modules, name, module)
    return module.parse


def test_repair_fingerprint_follows_the_loaded_shared_parser_not_its_transport_wrapper():
    assert wrapper.parse_ca_official_actions_zip is shared_parser.parse_ca_official_actions_zip
    assert _candidate_parser_sha256(wrapper.parse_ca_official_actions_zip) == hashlib.sha256(
        Path(shared_parser.__file__).read_bytes()
    ).hexdigest()
    assert _candidate_parser_sha256(wrapper.parse_ca_official_actions_zip) != hashlib.sha256(
        Path(wrapper.__file__).read_bytes()
    ).hexdigest()


def test_repair_fingerprint_changes_with_the_loaded_parser_source(monkeypatch, tmp_path: Path):
    first = _loaded_parser(monkeypatch, tmp_path, "candidate_parser_one", "def parse():\n    return 1\n")
    second = _loaded_parser(monkeypatch, tmp_path, "candidate_parser_two", "def parse():\n    return 2\n")

    first_hash = _candidate_parser_sha256(first)
    second_hash = _candidate_parser_sha256(second)

    assert first_hash == hashlib.sha256((tmp_path / "candidate_parser_one.py").read_bytes()).hexdigest()
    assert second_hash == hashlib.sha256((tmp_path / "candidate_parser_two.py").read_bytes()).hexdigest()
    assert first_hash != second_hash
