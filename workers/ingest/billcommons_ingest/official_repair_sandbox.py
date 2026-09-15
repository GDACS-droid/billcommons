"""Bounded parent supervisor for the offline CA repair interpreter.

The child, its serializer, and its output are untrusted. A successful transport
does not establish correct parsing or approve a repair. No application settings,
environment, expected answers, or database access are passed to the child.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time

MAX_SOURCE_BYTES = 256 * 1024
MAX_FIXTURE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
WALL_SECONDS = 12.0


@dataclass(frozen=True)
class SandboxResult:
    status: str
    source_sha256: str
    fixture_sha256: str
    bootstrap_sha256: str
    payload: dict | None = None


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite JSON")


def _private_file(path: Path, contents: bytes) -> None:
    with path.open("xb") as output:
        output.write(contents)
    path.chmod(0o400)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o400
            or path.read_bytes() != contents):
        raise OSError("invalid private evaluation input")


def _stop(process: subprocess.Popen) -> None:
    # _collect observes with WNOWAIT and never reaps the leader. Its PID stays
    # reserved until after group teardown, even if it exited before a pipe-
    # holding descendant. Never poll()/wait() before this group kill.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _collect(process: subprocess.Popen, *, deadline: float) -> tuple[str, bytes]:
    output = bytearray()
    seen = 0
    with selectors.DefaultSelector() as selector:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "wall_limit", b""
            for key, _ in selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                seen += len(chunk)
                if seen > MAX_OUTPUT_BYTES:
                    return "output_limit", b""
                if key.fileobj is process.stdout:
                    output.extend(chunk)
                # stderr counts toward the bound but is never retained or
                # surfaced. Candidate exception text is also untrusted output.
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "wall_limit", b""
            state = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if state is not None:
                returncode = state.si_status if state.si_code == os.CLD_EXITED else -state.si_status
                break
            time.sleep(min(remaining, 0.01))
    if returncode == 78:
        return "isolation_unavailable", b""
    if returncode != 0:
        return "child_failed", b""
    return "returned", bytes(output)


def run_ca_parser(source: bytes, fixture: bytes, *, source_url: str,
                  retrieved_at: str) -> SandboxResult:
    """Run exact copied input bytes; report transport results, never acceptance.

    This Linux x86_64 path refuses missing confinement. Resource and output caps
    are fixed policy. The caller must independently compare returned facts.
    """
    if (not isinstance(source, bytes) or not 0 < len(source) <= MAX_SOURCE_BYTES
            or not isinstance(fixture, bytes) or not 0 < len(fixture) <= MAX_FIXTURE_BYTES
            or not isinstance(source_url, str) or len(source_url) > 2048
            or not isinstance(retrieved_at, str) or len(retrieved_at) > 64):
        raise ValueError("invalid bounded evaluator input")
    source.decode("utf-8")
    request = json.dumps({"source_url": source_url, "retrieved_at": retrieved_at},
                         ensure_ascii=True).encode("utf-8")
    bootstrap_path = Path(__file__).with_name("official_repair_sandbox_child.py")
    # Only this installed, trusted bootstrap is read here. Candidate code is
    # never compiled or imported in the supervisor.
    bootstrap = bootstrap_path.read_bytes()
    evidence = {
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "fixture_sha256": hashlib.sha256(fixture).hexdigest(),
        "bootstrap_sha256": hashlib.sha256(bootstrap).hexdigest(),
    }
    deadline = time.monotonic() + WALL_SECONDS
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="bc-repair-eval-", dir="/tmp") as temporary:
            stage = Path(temporary)
            stage.chmod(0o700)
            try:
                for name, contents in {"source.py": source, "fixture.bin": fixture,
                                       "request.json": request,
                                       "bootstrap.py": bootstrap}.items():
                    _private_file(stage / name, contents)
                stage.chmod(0o500)
                if time.monotonic() >= deadline:
                    return SandboxResult("wall_limit", **evidence)
                process = subprocess.Popen(
                    [sys.executable, "-I", "-S", "-B", str(stage / "bootstrap.py"), str(stage)],
                    env={}, cwd=stage, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    close_fds=True, start_new_session=True,
                )
                try:
                    status, output = _collect(process, deadline=deadline)
                finally:
                    _stop(process)
                    process.stdout.close()
                    process.stderr.close()
                if status != "returned":
                    return SandboxResult(status, **evidence)
                try:
                    payload = json.loads(output, object_pairs_hook=_unique_object,
                                         parse_constant=_reject_constant)
                    if (not isinstance(payload, dict) or set(payload) != {"status", "bills"}
                            or payload["status"] != "parsed" or not isinstance(payload["bills"], list)):
                        raise ValueError("invalid child result")
                except (ValueError, TypeError, RecursionError):
                    return SandboxResult("invalid_output", **evidence)
                return SandboxResult("returned", payload=payload, **evidence)
            finally:
                # The restricted child cannot chmod this directory. Restore
                # parent write permission only after the child has been reaped.
                stage.chmod(0o700)
    except (OSError, subprocess.SubprocessError):
        return SandboxResult("supervisor_failed", **evidence)
