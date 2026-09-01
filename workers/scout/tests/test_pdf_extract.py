from __future__ import annotations

import time
import zlib
from io import BytesIO

import pytest
from pypdf import PdfWriter

from billcommons_scout.pdf_extract import PDFExtractionError, _run_isolated, extract_pdf_text


def _pdf_with_text(value: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({value}) Tj ET".encode()
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    )
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value_bytes in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(value_bytes)
        output.extend(b"\nendobj\n")
    startxref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n".encode())
    return bytes(output)


def _compressed_text_bomb(characters: int) -> bytes:
    content = b"BT /F1 12 Tf 72 720 Td (" + (b"x" * characters) + b") Tj ET"
    stream = zlib.compress(content, level=9)
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\nstream\n" + stream + b"\nendstream",
    )
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value_bytes in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(value_bytes)
        output.extend(b"\nendobj\n")
    startxref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n".encode())
    return bytes(output)


def _slow_worker(connection, document, max_pages, max_text_chars, memory_limit_bytes, cpu_limit_seconds):
    del document, max_pages, max_text_chars, memory_limit_bytes, cpu_limit_seconds
    time.sleep(2)
    connection.send(("ok", "too late"))


def _oversized_result_worker(connection, document, max_pages, max_text_chars, memory_limit_bytes, cpu_limit_seconds):
    del document, max_pages, max_text_chars, memory_limit_bytes, cpu_limit_seconds
    connection.send(("ok", "x" * 10_000))


def test_extracts_normal_pdf_in_a_bounded_child():
    assert extract_pdf_text(_pdf_with_text("Florida committee analysis"), max_pages=1, max_text_chars=128) == "Florida committee analysis"


def test_malformed_pdf_returns_sanitized_fixed_error():
    with pytest.raises(PDFExtractionError, match="^pdf_invalid$"):
        extract_pdf_text(b"%PDF-not-really-a-pdf", max_pages=1, max_text_chars=128)


def test_page_limit_is_enforced_before_text_extraction():
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    document = BytesIO()
    writer.write(document)

    with pytest.raises(PDFExtractionError, match="^pdf_page_limit$"):
        extract_pdf_text(document.getvalue(), max_pages=1, max_text_chars=128)


def test_oversized_extraction_is_returned_within_text_cap():
    result = extract_pdf_text(_pdf_with_text("x" * 10_000), max_pages=1, max_text_chars=37)
    assert result == "x" * 37


def test_wall_timeout_terminates_child_without_in_process_fallback():
    with pytest.raises(PDFExtractionError, match="^pdf_extract_timeout$"):
        _run_isolated(
            _slow_worker,
            b"irrelevant",
            max_pages=1,
            max_text_chars=10,
            timeout_seconds=0.05,
            memory_limit_bytes=32 * 1024 * 1024,
            cpu_limit_seconds=1,
        )


def test_parent_rejects_oversized_child_result():
    with pytest.raises(PDFExtractionError, match="^pdf_extract_failed$"):
        _run_isolated(
            _oversized_result_worker,
            b"irrelevant",
            max_pages=1,
            max_text_chars=10,
            timeout_seconds=1,
            memory_limit_bytes=32 * 1024 * 1024,
            cpu_limit_seconds=1,
        )


def test_high_ratio_compressed_pdf_cannot_exhaust_parent_worker():
    document = _compressed_text_bomb(20 * 1024 * 1024)
    assert len(document) < 32 * 1024
    started = time.monotonic()
    with pytest.raises(PDFExtractionError) as raised:
        extract_pdf_text(
            document,
            max_pages=1,
            max_text_chars=20_000,
            timeout_seconds=1,
            memory_limit_bytes=128 * 1024 * 1024,
            cpu_limit_seconds=1,
        )
    assert str(raised.value) in {
        "pdf_invalid",
        "pdf_extract_timeout",
        "pdf_extract_failed",
    }
    assert time.monotonic() - started < 3
