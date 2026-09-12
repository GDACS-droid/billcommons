"""Bounded source provenance for loaded official parser callables.

The digest deliberately covers the source module that supplied a parser
callable.  It is evidence about the parser reviewed by an operator; it does
not imply that a historical observation can be replayed under that parser.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import inspect
from pathlib import Path


# Parser source should be small and code-reviewable.  Do not turn provenance
# collection into an unbounded file read when an unusual module path is used.
MAX_PARSER_SOURCE_BYTES = 2 * 1024 * 1024
_SOURCE_WITNESS_ATTR = "__billcommons_parser_source_sha256__"
_CALLABLE_BINDINGS_ATTR = "__billcommons_parser_source_callables__"


class ParserProvenanceError(ValueError):
    """The current process cannot attest the source file of a parser."""


def _source_sha256(source_file: str) -> str:
    """Hash one source file without accepting an unbounded read."""

    try:
        with Path(source_file).open("rb") as source:
            source_bytes = source.read(MAX_PARSER_SOURCE_BYTES + 1)
    except OSError as exc:
        raise ParserProvenanceError("parser source is unavailable") from exc
    if len(source_bytes) > MAX_PARSER_SOURCE_BYTES:
        raise ParserProvenanceError("parser source exceeds the provenance limit")
    return hashlib.sha256(source_bytes).hexdigest()


def parser_source_sha256(parser: object) -> str:
    """Return the SHA-256 of the loaded source file for ``parser``.

    Parser modules opt in at import time by retaining a bounded source witness
    and the exact callable/code identities it covers.  Re-reading the source
    then detects a file replaced after that callable was loaded, including a
    change confined to one of its helpers.  Resolving the callable's module
    also avoids attesting a transport wrapper that only re-exports a shared
    pure parser.

    This is intentionally narrower than general Python runtime attestation:
    dynamically created callables, modules without the import-time witness,
    and in-memory code mutation are unavailable rather than guessed.
    """

    if not inspect.isfunction(parser):
        raise ParserProvenanceError("parser source is unavailable")
    module = inspect.getmodule(parser)
    source_file = getattr(module, "__file__", None) if module is not None else None
    if not isinstance(source_file, str) or not source_file:
        raise ParserProvenanceError("parser source is unavailable")
    witness = getattr(module, _SOURCE_WITNESS_ATTR, None)
    bindings = getattr(module, _CALLABLE_BINDINGS_ATTR, None)
    if not is_sha256(witness) or not isinstance(bindings, Mapping):
        raise ParserProvenanceError("parser source is unavailable")
    parser_name = parser.__name__
    binding = bindings.get(parser_name)
    if (
        not isinstance(binding, tuple)
        or len(binding) != 2
        or binding[0] is not parser
        or binding[1] is not parser.__code__
    ):
        raise ParserProvenanceError("parser callable is not bound to its source witness")
    if _source_sha256(source_file) != witness:
        raise ParserProvenanceError("parser source changed after the parser was loaded")
    return witness


def is_sha256(value: object) -> bool:
    """Accept only the canonical digest representation retained in scope."""

    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)
