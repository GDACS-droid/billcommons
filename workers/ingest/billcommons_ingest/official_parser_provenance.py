"""Bounded source provenance for loaded official parser callables.

The digest deliberately covers the loaded module file which supplied a parser
callable. It is evidence about the parser reviewed by an operator; it does
not imply that a historical observation can be replayed under that parser.
"""
from __future__ import annotations

import hashlib
import inspect
from pathlib import Path


class ParserProvenanceError(ValueError):
    """The current process cannot attest the source file of a parser."""


def parser_source_sha256(parser: object) -> str:
    """Return the SHA-256 of the loaded source file for ``parser``.

    Resolving the callable's module avoids attesting a transport wrapper that
    only re-exports a shared pure parser. This fails closed for dynamically
    created callables and unavailable source files.
    """

    module = inspect.getmodule(parser)
    source_file = getattr(module, "__file__", None) if module is not None else None
    if not isinstance(source_file, str) or not source_file:
        raise ParserProvenanceError("parser source is unavailable")
    try:
        return hashlib.sha256(Path(source_file).read_bytes()).hexdigest()
    except OSError as exc:
        raise ParserProvenanceError("parser source is unavailable") from exc


def is_sha256(value: object) -> bool:
    """Accept only the canonical digest representation retained in scope."""

    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)
