"""Pure provenance checks for the CA parser replay fingerprint."""
from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path

import pytest

from billcommons_ingest import official_ca_actions as wrapper
from billcommons_ingest.official_parser_provenance import (
    MAX_PARSER_SOURCE_BYTES,
    ParserProvenanceError,
    parser_source_sha256,
)
from billcommons_ingest.official_repair_plan import _candidate_parser_sha256
from billcommons_shared import ca_official_actions as shared_parser


def _provenance_source(parser_source: str) -> str:
    return f"""import hashlib
{parser_source}
_source_limit = 2 * 1024 * 1024
with open(__file__, \"rb\") as _source_file:
    _source_bytes = _source_file.read(_source_limit + 1)
__billcommons_parser_source_sha256__ = hashlib.sha256(_source_bytes).hexdigest()
__billcommons_parser_source_callables__ = {{
    \"parse\": (parse, parse.__code__),
}}
"""


def _loaded_parser(monkeypatch, tmp_path: Path, name: str, source: str):
    path = tmp_path / f"{name}.py"
    source = _provenance_source(source)
    path.write_text(source)
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    monkeypatch.setitem(sys.modules, name, module)
    return module.parse, path


def test_repair_fingerprint_follows_the_loaded_shared_parser_not_its_transport_wrapper():
    assert wrapper.parse_ca_official_actions_zip is shared_parser.parse_ca_official_actions_zip
    assert _candidate_parser_sha256(wrapper.parse_ca_official_actions_zip) == hashlib.sha256(
        Path(shared_parser.__file__).read_bytes()
    ).hexdigest()
    assert _candidate_parser_sha256(wrapper.parse_ca_official_actions_zip) != hashlib.sha256(
        Path(wrapper.__file__).read_bytes()
    ).hexdigest()


def test_unchanged_loaded_module_returns_its_real_source_hash(monkeypatch, tmp_path: Path):
    parser, path = _loaded_parser(monkeypatch, tmp_path, "candidate_parser", "def parse():\n    return 1\n")

    assert parser_source_sha256(parser) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_source_edit_in_parser_body_after_load_fails_closed(monkeypatch, tmp_path: Path):
    parser, path = _loaded_parser(monkeypatch, tmp_path, "body_edit", "def parse():\n    return 1\n")

    path.write_text(_provenance_source("def parse():\n    return 2\n"))

    assert parser() == 1
    with pytest.raises(ParserProvenanceError, match="changed after the parser was loaded"):
        parser_source_sha256(parser)


def test_source_edit_in_parser_helper_after_load_fails_closed(monkeypatch, tmp_path: Path):
    parser, path = _loaded_parser(
        monkeypatch,
        tmp_path,
        "helper_edit",
        "def _helper():\n    return 1\n\ndef parse():\n    return _helper()\n",
    )

    path.write_text(_provenance_source("def _helper():\n    return 2\n\ndef parse():\n    return _helper()\n"))

    assert parser() == 1
    with pytest.raises(ParserProvenanceError, match="changed after the parser was loaded"):
        parser_source_sha256(parser)


def test_source_above_provenance_read_cap_is_unavailable(monkeypatch, tmp_path: Path):
    parser, path = _loaded_parser(monkeypatch, tmp_path, "oversized_source", "def parse():\n    return 1\n")

    path.write_bytes(b"x" * (MAX_PARSER_SOURCE_BYTES + 1))

    with pytest.raises(ParserProvenanceError, match="exceeds the provenance limit"):
        parser_source_sha256(parser)


def test_monkeypatched_dynamic_callable_cannot_attest_source(monkeypatch):
    dynamic_parser = lambda: None
    dynamic_parser.__name__ = "parse_ca_official_actions_zip"
    dynamic_parser.__module__ = shared_parser.__name__
    monkeypatch.setattr(wrapper, "parse_ca_official_actions_zip", dynamic_parser)

    with pytest.raises(ParserProvenanceError, match="not bound"):
        parser_source_sha256(wrapper.parse_ca_official_actions_zip)
