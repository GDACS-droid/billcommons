"""Bounded parent supervisor for the offline CA repair interpreter.

The child, its serializer, and its output are untrusted. A successful transport
does not establish correct parsing or approve a repair. No application settings,
environment, expected answers, or database access are passed to the child.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
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
_STATE_RECORD_MAX_BYTES = 16 * 1024
SUPERVISOR_STATE_DIR = Path(os.environ.get(
    "BC_REPAIR_SUPERVISOR_STATE_DIR", f"/tmp/bc-repair-supervisor-{os.getuid()}"))


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
_PENDING: list[tuple[subprocess.Popen | None, Path, dict]] = []
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


def _validate_child_json_bytes(raw: bytes) -> None:
    """Accept only the byte encoding scanned by _check_json_budget.

    Passing bytes to json.loads permits its encoding detection, including UTF-16.
    That would make the byte-oriented scanner observe a different language than
    the decoder. The child protocol is deliberately ASCII-only.
    """
    if b"\0" in raw or not raw.isascii():
        raise ValueError("child JSON must be ASCII without NUL")


def _owned_regular(info: os.stat_result, mode: int) -> bool:
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == mode)


def _supervisor_state() -> Path:
    """Return the private durable state root after validating its identity."""
    try:
        SUPERVISOR_STATE_DIR.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = SUPERVISOR_STATE_DIR.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise OSError("invalid supervisor state directory")
    return SUPERVISOR_STATE_DIR


def _state_path(state: Path) -> Path:
    return state / "recovery.json"


def _validate_state_file(path: Path, mode: int = 0o600) -> os.stat_result:
    info = path.lstat()
    if not _owned_regular(info, mode):
        raise OSError("invalid supervisor state file")
    return info


@contextmanager
def _process_reservation():
    """Reserve the durable state across independent supervisor processes."""
    state = _supervisor_state()
    lock = state / "lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not _owned_regular(info, 0o600):
            raise OSError("invalid supervisor lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        try:
            yield state
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _record_for_result(record: dict, state: Path) -> dict:
    return {**record, "state_path": str(_state_path(state))}


def _read_record(state: Path) -> dict | None:
    path = _state_path(state)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not _owned_regular(info, 0o600) or info.st_size > _STATE_RECORD_MAX_BYTES:
            raise OSError("invalid supervisor recovery record")
        data = bytearray()
        while len(data) <= _STATE_RECORD_MAX_BYTES:
            chunk = os.read(fd, _STATE_RECORD_MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > _STATE_RECORD_MAX_BYTES:
            raise OSError("oversized supervisor recovery record")
    finally:
        os.close(fd)
    try:
        record = json.loads(bytes(data).decode("ascii"), object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise OSError("invalid supervisor recovery record") from None
    if (not isinstance(record, dict) or record.get("version") != 1
            or not isinstance(record.get("status"), str)
            or not isinstance(record.get("stage"), str)):
        raise OSError("invalid supervisor recovery record")
    return record


def _write_record(state: Path, record: dict) -> None:
    """Atomically replace the owner-only record while the flock is held."""
    encoded = json.dumps(record, sort_keys=True, ensure_ascii=True,
                         separators=(",", ":")).encode("ascii")
    if len(encoded) > _STATE_RECORD_MAX_BYTES:
        raise OSError("supervisor recovery record too large")
    path = _state_path(state)
    if path.exists():
        _validate_state_file(path)
    temporary = state / f".recovery-{os.getpid()}-{threading.get_ident()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        written = 0
        while written < len(encoded):
            written += os.write(fd, encoded[written:])
        os.fsync(fd)
        if not _owned_regular(os.fstat(fd), 0o600):
            raise OSError("invalid temporary supervisor recovery record")
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(fd)
    try:
        os.replace(temporary, path)
        _validate_state_file(path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _clear_record(state: Path) -> None:
    path = _state_path(state)
    _validate_state_file(path)
    path.unlink()


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
    except OSError:
        # A permission or kernel error does not establish that this process
        # group is gone. Keep the stage and durable recovery record intact.
        return False
    try:
        process.wait(timeout=REAP_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
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


def _mark_cleanup_failure(state: Path, record: dict) -> dict:
    record["status"] = "stage_cleanup_failed"
    try:
        _write_record(state, record)
    except OSError:
        # The already-existing record remains the host stop even when its
        # status cannot be updated.
        pass
    return record


def _reap_pending(state: Path) -> dict | None:
    """Only this process may resolve entries for Popen objects it owns."""
    for process, stage, record in list(_PENDING):
        if process is not None and (process.poll() is None or not _group_gone(process.pid)):
            return record
        try:
            stage.chmod(0o700)
            shutil.rmtree(stage)
        except FileNotFoundError:
            if stage.exists():
                return _mark_cleanup_failure(state, record)
        except OSError:
            return _mark_cleanup_failure(state, record)
        try:
            _clear_record(state)
        except OSError:
            return _mark_cleanup_failure(state, record)
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
        try:
            with _process_reservation() as state:
                if state is None:
                    return SandboxResult("runner_busy", **evidence)
                return _run_prepared(source, fixture, request, bootstrap, evidence, state)
        except OSError:
            return SandboxResult("supervisor_failed", **evidence)
    finally:
        _RUN_LOCK.release()


def _run_prepared(source: bytes, fixture: bytes, request: bytes, bootstrap: bytes,
                  evidence: dict, state: Path) -> SandboxResult:
    pending = _reap_pending(state)
    if pending is not None:
        return SandboxResult("cleanup_pending", cleanup=_record_for_result(pending, state),
                             **evidence)
    # A record left by another (possibly SIGKILLed) supervisor is deliberately
    # not interpreted as proof that its child or stage is safe to remove.
    # Recovery needs an operator who can establish host state out of band.
    record = _read_record(state)
    if record is not None:
        return SandboxResult("cleanup_pending", cleanup=_record_for_result(record, state),
                             **evidence)
    deadline = time.monotonic() + WALL_SECONDS
    pending_entry: tuple[subprocess.Popen | None, Path, dict] | None = None
    try:
        stage = Path(tempfile.mkdtemp(prefix="stage-", dir=state))
        try:
            stage.chmod(0o700)
            # Create the durable stop before the first possible child exists.
            # It remains outside the candidate-readable immutable stage.
            record = {"version": 1, "status": "active", "stage": str(stage), "pid": None}
            pending_entry = (None, stage, record)
            _write_record(state, record)
            try:
                for name, contents in {"source.py": source, "fixture.bin": fixture,
                                       "request.json": request,
                                       "bootstrap.py": bootstrap}.items():
                    _private_file(stage / name, contents)
                stage.chmod(0o500)
                if time.monotonic() >= deadline:
                    return SandboxResult("wall_limit", **evidence)
                process = subprocess.Popen(
                    [sys.executable, "-I", "-S", "-B", str(stage / "bootstrap.py"), str(stage),
                     str(os.getpid())],
                    env={}, cwd=stage, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    close_fds=True, start_new_session=True,
                )
                pending_entry = (process, stage, record)
                _PENDING.append(pending_entry)
                record.update({"pid": process.pid, "pid_start_ticks": _start_ticks(process.pid),
                               "status": "active"})
                try:
                    _write_record(state, record)
                    status, output = _collect(process, deadline=deadline)
                finally:
                    try:
                        reaped = _stop(process)
                    finally:
                        process.stdout.close()
                        process.stderr.close()
                    if not reaped:
                        record["status"] = "sigkill_sent_reap_pending"
                        try:
                            _write_record(state, record)
                        except OSError:
                            pass
                if not reaped:
                    return SandboxResult("cleanup_pending",
                                         cleanup=_record_for_result(record, state), **evidence)
                pending = _reap_pending(state)
                if pending is not None:
                    return SandboxResult("cleanup_pending",
                                         cleanup=_record_for_result(pending, state), **evidence)
                pending_entry = None
                if status != "returned":
                    return SandboxResult(status, **evidence)
                try:
                    _validate_child_json_bytes(output)
                    _check_json_budget(output)
                    payload = json.loads(output.decode("ascii"), object_pairs_hook=_unique_object,
                                         parse_constant=_reject_constant)
                    if (not isinstance(payload, dict) or set(payload) != {"status", "bills"}
                            or payload["status"] != "parsed" or not isinstance(payload["bills"], list)):
                        raise ValueError("invalid child result")
                except (ValueError, TypeError, RecursionError, MemoryError):
                    return SandboxResult("invalid_output", **evidence)
                return SandboxResult("returned", payload=payload, **evidence)
            finally:
                # Any pre-spawn failure has no child; this local supervisor may
                # safely retry its stage cleanup. A child path only reaches
                # here after _stop and _reap_pending above.
                if pending_entry is not None and pending_entry not in _PENDING:
                    _PENDING.append(pending_entry)
                    pending = _reap_pending(state)
                    if pending is not None:
                        return SandboxResult("cleanup_pending",
                                             cleanup=_record_for_result(pending, state), **evidence)
                    pending_entry = None
        finally:
            pass
    except (OSError, subprocess.SubprocessError):
        if pending_entry is not None:
            if pending_entry not in _PENDING:
                _PENDING.append(pending_entry)
            pending = _reap_pending(state)
            if pending is not None:
                return SandboxResult("cleanup_pending", cleanup=_record_for_result(pending, state),
                                     **evidence)
        return SandboxResult("supervisor_failed", **evidence)
