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


def test_large_pdf_with_failed_spawn_bootstrap_does_not_stall_parent(tmp_path):
    """The deadline must be reachable when the child exits before unpickling."""
    import os
    from pathlib import Path
    import signal
    import subprocess
    import sys

    if os.name != "posix":
        pytest.skip("process-group cleanup for the historical hang requires POSIX")
    module_root = str(Path(__file__).resolve().parents[1])
    program = (
        "import sys, __main__\n"
        f"sys.path.insert(0, {module_root!r})\n"
        f"__main__.__file__ = {str(tmp_path / 'missing-bootstrap.py')!r}\n"
        "from billcommons_scout.pdf_extract import PDFExtractionError, extract_pdf_text\n"
        "try:\n"
        "    extract_pdf_text(b'%PDF-' + b'x' * (2 * 1024 * 1024), "
        "max_pages=1, max_text_chars=128, timeout_seconds=1)\n"
        "except PDFExtractionError as exc:\n"
        "    print(str(exc), flush=True)\n"
        "else:\n"
        "    raise AssertionError('unexpected extraction success')\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", program], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=2)
        pytest.fail("large PDF blocked process startup before the extraction deadline")
    finally:
        # Also remove a surviving resource tracker after a failed interpreter
        # bootstrap. The session contains only this test's owned processes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert process.returncode == 0
    assert output.strip() in {"pdf_extract_failed", "pdf_extract_timeout", "pdf_isolation_unavailable"}


@pytest.mark.parametrize("mode", ["success", "invalid", "timeout", "startup_failure"])
def test_private_pdf_snapshot_is_removed_on_every_outcome(tmp_path, monkeypatch, mode):
    import multiprocessing
    import os
    from pathlib import Path
    import tempfile
    import billcommons_scout.pdf_extract as module

    original_directory = tempfile.TemporaryDirectory
    original_file = tempfile.NamedTemporaryFile
    seen_directories = []
    seen_files = []

    def private_directory(**kwargs):
        context = original_directory(dir=tmp_path, **kwargs)
        seen_directories.append(Path(context.name))
        assert os.stat(context.name).st_mode & 0o777 == 0o700
        return context

    def private_file(**kwargs):
        stream = original_file(**kwargs)
        seen_files.append(Path(stream.name))
        assert os.stat(stream.name).st_mode & 0o777 == 0o600
        return stream

    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", private_directory)
    monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", private_file)
    if mode == "startup_failure":
        def fail_start(_process):
            raise OSError("private path must not appear in the public error")
        monkeypatch.setattr(multiprocessing.process.BaseProcess, "start", fail_start)
    if mode == "success":
        assert extract_pdf_text(_pdf_with_text("retained evidence"), max_pages=1, max_text_chars=128) == "retained evidence"
    else:
        expected = {"invalid": "pdf_invalid", "timeout": "pdf_extract_timeout", "startup_failure": "pdf_isolation_unavailable"}[mode]
        with pytest.raises(PDFExtractionError, match=f"^{expected}$"):
            if mode == "timeout":
                _run_isolated(_slow_worker, b"input", max_pages=1, max_text_chars=128,
                              timeout_seconds=0.05, memory_limit_bytes=256 * 1024 * 1024, cpu_limit_seconds=1)
            else:
                extract_pdf_text(b"%PDF-invalid", max_pages=1, max_text_chars=128)
    assert len(seen_directories) == len(seen_files) == 1
    assert all(not path.exists() for path in seen_directories + seen_files)


def test_pdf_snapshot_staging_time_consumes_deadline_before_start(tmp_path, monkeypatch):
    import billcommons_scout.pdf_extract as module

    ticks = iter((0.0, 2.0))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    def forbidden_start(*_args, **_kwargs):
        raise AssertionError("must not start a parser after staging exhausts the budget")
    monkeypatch.setattr(module.multiprocessing, "get_context", forbidden_start)
    with pytest.raises(PDFExtractionError, match="^pdf_extract_timeout$"):
        extract_pdf_text(b"%PDF-input", max_pages=1, max_text_chars=128, timeout_seconds=1)


def test_pdf_snapshot_staging_failure_is_sanitized(monkeypatch):
    import billcommons_scout.pdf_extract as module

    def fail_directory(**_kwargs):
        raise OSError("private filesystem details")
    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", fail_directory)
    with pytest.raises(PDFExtractionError, match="^pdf_isolation_unavailable$"):
        extract_pdf_text(b"%PDF-input", max_pages=1, max_text_chars=128)


@pytest.mark.parametrize("payload", [b"ab", b"abcd"])
def test_child_rejects_changed_snapshot_before_invoking_parser(monkeypatch, payload):
    import billcommons_scout.pdf_extract as module

    limits_applied = []
    reads = []
    parser_calls = []
    messages = []
    class Input(BytesIO):
        def read(self, count):
            reads.append(count)
            return super().read(count)
    class Reply:
        closed = False
        def send(self, value):
            messages.append(value)
        def close(self):
            self.closed = True
    def open_snapshot(path, mode):
        assert limits_applied == [(1000, 1)]
        assert (path, mode) == ("private-input.pdf", "rb")
        return Input(payload)
    monkeypatch.setattr(module, "_apply_resource_limits", lambda memory, cpu: limits_applied.append((memory, cpu)))
    monkeypatch.setattr(module, "open", open_snapshot, raising=False)
    reply = Reply()
    module._dispatch_pdf_worker(reply, "private-input.pdf", 3, lambda *args: parser_calls.append(args), 1, 128, 1000, 1)
    assert reads == [4]
    assert parser_calls == []
    assert messages == [("error", "pdf_extract_failed")]
    assert reply.closed


def test_pipe_endpoints_close_when_process_construction_fails(monkeypatch):
    import multiprocessing
    import billcommons_scout.pdf_extract as module

    context = multiprocessing.get_context("spawn")
    endpoints = []
    class FailingContext:
        def Pipe(self, **kwargs):
            pair = context.Pipe(**kwargs)
            endpoints.extend(pair)
            return pair
        def Process(self, **_kwargs):
            raise OSError("private setup diagnostics")
    monkeypatch.setattr(module.multiprocessing, "get_context", lambda _method: FailingContext())
    with pytest.raises(PDFExtractionError, match="^pdf_isolation_unavailable$"):
        extract_pdf_text(b"%PDF-input", max_pages=1, max_text_chars=128)
    assert len(endpoints) == 2
    assert all(endpoint.closed for endpoint in endpoints)
