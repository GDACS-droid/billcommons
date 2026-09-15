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
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

MAX_SOURCE_BYTES = 256 * 1024
MAX_FIXTURE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_JSON_CONTAINERS = 32_768
MAX_JSON_SCALARS = 524_288
MAX_JSON_DEPTH = 8
MAX_JSON_STRING_BYTES = 1024 * 1024
WALL_SECONDS = 12.0
REAP_SECONDS = 1.0


@dataclass(frozen=True)
class SandboxResult:
    status: str
    source_sha256: str
    fixture_sha256: str
    bootstrap_sha256: str
    payload: dict | None = None
    cleanup: dict | None = None


# Keep ownership of an unreaped, SIGKILL-pending child and refuse another run
# in this supervisor until it exits. A CLI exit leaves a private recovery
# record; automated callers must treat cleanup_pending as a host-health stop.
_PENDING: list[tuple[subprocess.Popen, Path, dict]] = []
_RUN_LOCK = threading.Lock()


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite JSON")


def _check_json_budget(raw: bytes) -> None:
    """Bound object-graph amplification BEFORE allocating decoded objects.

    This byte scanner is only a budget gate; json.loads remains the syntax
    validator. It tracks quoted/escaped text so punctuation in action text
    cannot consume the structural budget. No input-dependent object graph is
    allocated by this scan.
    """
    containers = scalars = depth = string_bytes = atom_bytes = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            string_bytes += 1
            if string_bytes > MAX_JSON_STRING_BYTES:
                raise ValueError("JSON string budget exceeded")
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
            continue
        if byte == 34:
            scalars += 1
            if scalars > MAX_JSON_SCALARS:
                raise ValueError("JSON scalar count exceeded")
            quoted = True
            string_bytes = atom_bytes = 0
        elif byte in (123, 91):
            containers += 1
            depth += 1
            atom_bytes = 0
            if containers > MAX_JSON_CONTAINERS or depth > MAX_JSON_DEPTH:
                raise ValueError("JSON container budget exceeded")
        elif byte in (125, 93):
            depth -= 1
            atom_bytes = 0
            if depth < 0:
                raise ValueError("invalid JSON depth")
        elif byte in (32, 9, 10, 13, 44, 58):
            atom_bytes = 0
        else:
            if atom_bytes == 0:
                scalars += 1
                if scalars > MAX_JSON_SCALARS:
                    raise ValueError("JSON scalar count exceeded")
            atom_bytes += 1
            if atom_bytes > 64:
                raise ValueError("JSON primitive budget exceeded")
    if quoted or depth:
        raise ValueError("incomplete JSON structure")


def _private_file(path: Path, contents: bytes) -> None:
    with path.open("xb") as output:
        output.write(contents)
    path.chmod(0o400)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o400
            or path.read_bytes() != contents):
        raise OSError("invalid private evaluation input")


def _stop(process: subprocess.Popen) -> bool:
    # _collect observes with WNOWAIT and never reaps the leader. Its PID stays
    # reserved until after group teardown, even if it exited before a pipe-
    # holding descendant. Never poll()/wait() before this group kill.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=REAP_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return _group_gone(process.pid)


def _group_gone(group: int) -> bool:
    # This is an existence probe only, never a destructive signal. After leader
    # reaping, an ambiguous/reused group ID conservatively keeps the host stop.
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _start_ticks(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", "r") as identity:
            return identity.read(4096).rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _reap_pending() -> dict | None:
    for process, stage, record in list(_PENDING):
        if process.poll() is None or not _group_gone(process.pid):
            return record
        try:
            stage.chmod(0o700)
            shutil.rmtree(stage)
        except FileNotFoundError:
            if stage.exists():
                record["status"] = "stage_cleanup_failed"
                return record
        except OSError:
            record["status"] = "stage_cleanup_failed"
            return record
        _PENDING.remove((process, stage, record))
    return None


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
    if not _RUN_LOCK.acquire(blocking=False):
        return SandboxResult("runner_busy", **evidence)
    try:
        return _run_prepared(source, fixture, request, bootstrap, evidence)
    finally:
        _RUN_LOCK.release()


def _run_prepared(source: bytes, fixture: bytes, request: bytes, bootstrap: bytes,
                  evidence: dict) -> SandboxResult:
    pending = _reap_pending()
    if pending is not None:
        return SandboxResult("cleanup_pending", cleanup=pending, **evidence)
    deadline = time.monotonic() + WALL_SECONDS
    process = None
    stage = None
    preserve_stage = False
    record = None
    try:
        stage = Path(tempfile.mkdtemp(prefix="bc-repair-eval-", dir="/tmp"))
        try:
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
                    start_ticks = _start_ticks(process.pid)
                    try:
                        reaped = _stop(process)
                    finally:
                        process.stdout.close()
                        process.stderr.close()
                    if not reaped:
                        preserve_stage = True
                        # Record only public process/stage identity. No new
                        # workload may be scheduled until this is resolved.
                        record = {"pid": process.pid, "stage": str(stage),
                                  "pid_start_ticks": start_ticks,
                                  "status": "sigkill_sent_reap_pending"}
                        _PENDING.append((process, stage, record))
                        stage.chmod(0o700)
                        _private_file(stage / "cleanup.json", json.dumps(record).encode())
                        stage.chmod(0o500)
                if not reaped:
                    return SandboxResult("cleanup_pending", cleanup=record, **evidence)
                if status != "returned":
                    return SandboxResult(status, **evidence)
                try:
                    _check_json_budget(output)
                    payload = json.loads(output, object_pairs_hook=_unique_object,
                                         parse_constant=_reject_constant)
                    if (not isinstance(payload, dict) or set(payload) != {"status", "bills"}
                            or payload["status"] != "parsed" or not isinstance(payload["bills"], list)):
                        raise ValueError("invalid child result")
                except (ValueError, TypeError, RecursionError, MemoryError):
                    return SandboxResult("invalid_output", **evidence)
                return SandboxResult("returned", payload=payload, **evidence)
            finally:
                # The restricted child cannot chmod this directory. Restore
                # parent write permission only after the child has been reaped.
                if not preserve_stage:
                    stage.chmod(0o700)
        finally:
            if not preserve_stage:
                shutil.rmtree(stage)
    except (OSError, subprocess.SubprocessError):
        if preserve_stage:
            return SandboxResult("cleanup_pending", cleanup=record, **evidence)
        return SandboxResult("supervisor_failed", **evidence)
