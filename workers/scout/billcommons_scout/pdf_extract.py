"""Fail-closed, bounded extraction for untrusted PDF attachments.

PDFs from government sites are still untrusted input.  ``pypdf`` performs
decompression and page-content interpretation, so it must never run in the
Scout worker process.  This module deliberately has no network access and
does not fall back to in-process parsing when process isolation is unavailable.
"""
from __future__ import annotations

import multiprocessing
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Final


DEFAULT_TIMEOUT_SECONDS: Final[float] = 3.0
DEFAULT_MEMORY_LIMIT_BYTES: Final[int] = 256 * 1024 * 1024
DEFAULT_CPU_LIMIT_SECONDS: Final[int] = 2


class PDFExtractionError(RuntimeError):
    """A stable, safe reason for rejecting an untrusted PDF."""


def _apply_resource_limits(memory_limit_bytes: int, cpu_limit_seconds: int) -> None:
    """Apply best-effort OS limits inside the parser child.

    ``resource`` is unavailable on a few platforms.  The parent wall-clock
    timeout remains mandatory there; production Linux workers additionally get
    address-space and CPU ceilings.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows does not provide resource.
        return

    def lower_limit(kind: int, requested: int) -> None:
        try:
            _soft, hard = resource.getrlimit(kind)
            # RLIM_INFINITY is represented by a large integer.  Never raise a
            # hard limit and avoid an invalid soft > hard combination.
            soft = min(requested, hard) if hard != resource.RLIM_INFINITY else requested
            resource.setrlimit(kind, (soft, hard))
        except (OSError, ValueError):
            # Containers may reject a particular rlimit.  Keep parsing
            # isolated and retain the parent-enforced timeout instead.
            return

    lower_limit(resource.RLIMIT_AS, memory_limit_bytes)
    lower_limit(resource.RLIMIT_CPU, cpu_limit_seconds)


def _extract_pdf_worker(
    connection: Connection,
    document: bytes,
    max_pages: int,
    max_text_chars: int,
    memory_limit_bytes: int,
    cpu_limit_seconds: int,
) -> None:
    """Parse in the child and send only bounded, non-diagnostic output."""
    try:
        _apply_resource_limits(memory_limit_bytes, cpu_limit_seconds)
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(document))
        if len(reader.pages) > max_pages:
            connection.send(("error", "pdf_page_limit"))
            return

        remaining = max_text_chars
        parts: list[str] = []
        for page in reader.pages:
            # ``extract_text`` itself can be expensive; it is intentionally in
            # this constrained child.  Slice before crossing the process
            # boundary so a PDF cannot make the parent allocate unbounded text.
            part = (page.extract_text() or "")[:remaining]
            if part:
                parts.append(part)
                remaining -= len(part)
            if remaining <= 0:
                break
        connection.send(("ok", " ".join(parts).strip()[:max_text_chars]))
    except Exception:
        # Parser exceptions are untrusted document details.  Never persist or
        # expose their messages to a user, log, or job event.
        try:
            connection.send(("error", "pdf_invalid"))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        try:
            connection.close()
        except OSError:
            pass


WorkerTarget = Callable[[Connection, bytes, int, int, int, int], None]


def _run_isolated(
    worker: WorkerTarget,
    document: bytes,
    *,
    max_pages: int,
    max_text_chars: int,
    timeout_seconds: float,
    memory_limit_bytes: int,
    cpu_limit_seconds: int,
) -> str:
    """Run a picklable parser worker under a strict wall-clock deadline.

    The injectable worker is intentionally private and exists only to make the
    timeout and oversized-result boundary deterministic in tests.
    """
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=worker,
        args=(send, document, max_pages, max_text_chars, memory_limit_bytes, cpu_limit_seconds),
        daemon=True,
    )
    started = time.monotonic()
    try:
        process.start()
    except Exception as exc:
        receive.close()
        send.close()
        raise PDFExtractionError("pdf_isolation_unavailable") from exc
    finally:
        # The parent must not retain the write end; otherwise EOF cannot prove
        # that a crashed child produced no result.
        try:
            send.close()
        except OSError:
            pass

    try:
        remaining = max(0.0, timeout_seconds - (time.monotonic() - started))
        if not receive.poll(remaining):
            raise PDFExtractionError("pdf_extract_timeout")
        try:
            result = receive.recv()
        except EOFError as exc:
            raise PDFExtractionError("pdf_extract_failed") from exc
        if not isinstance(result, tuple) or len(result) != 2:
            raise PDFExtractionError("pdf_extract_failed")
        outcome, payload = result
        if outcome == "ok" and isinstance(payload, str) and len(payload) <= max_text_chars:
            return payload
        if outcome == "error" and payload in {"pdf_invalid", "pdf_page_limit"}:
            raise PDFExtractionError(payload)
        # Never trust a child response that exceeds the parent-side output cap.
        raise PDFExtractionError("pdf_extract_failed")
    finally:
        receive.close()
        process.join(timeout=0.1)
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.5)
        if process.is_alive():  # pragma: no cover - defensive platform fallback.
            process.kill()
            process.join(timeout=0.5)


def extract_pdf_text(
    document: bytes,
    *,
    max_pages: int,
    max_text_chars: int,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    memory_limit_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES,
    cpu_limit_seconds: int = DEFAULT_CPU_LIMIT_SECONDS,
) -> str:
    """Return bounded extracted text or a sanitized :class:`PDFExtractionError`.

    Callers should treat every error as a failed source and must not retry the
    document through an in-process parser.
    """
    if not isinstance(document, bytes):
        raise TypeError("document must be bytes")
    if max_pages < 1 or max_text_chars < 1:
        raise ValueError("PDF extraction limits must be positive")
    if timeout_seconds <= 0 or memory_limit_bytes < 1 or cpu_limit_seconds < 1:
        raise ValueError("PDF extraction resource limits must be positive")
    return _run_isolated(
        _extract_pdf_worker,
        document,
        max_pages=max_pages,
        max_text_chars=max_text_chars,
        timeout_seconds=timeout_seconds,
        memory_limit_bytes=memory_limit_bytes,
        cpu_limit_seconds=cpu_limit_seconds,
    )
