"""Exercise the real isolated interpreter using synthetic inputs and canaries."""
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from billcommons_ingest import official_repair_sandbox as sandbox

URL = "https://downloads.leginfo.legislature.ca.gov/pubinfo_Mon.zip"
WHEN = "2026-09-15T00:00:00+00:00"


def _run(code):
    return sandbox.run_ca_parser(code.encode(), b"synthetic fixture", source_url=URL, retrieved_at=WHEN)


def _emit(payload):
    return f"import os\nos.write(1, {json.dumps(payload).encode()!r})\nos._exit(0)\n"


def test_returned_json_is_only_untrusted_transport():
    payload = {"status": "parsed", "bills": []}
    result = _run(_emit(payload))
    assert result.status == "returned"
    assert result.payload == payload  # The evaluator must independently reject this answer.
    assert result.source_sha256 == hashlib.sha256(_emit(payload).encode()).hexdigest()


def test_outside_canary_network_and_process_capabilities_are_denied(tmp_path, monkeypatch):
    canary = tmp_path / "synthetic-canary"
    canary.write_bytes(b"outside synthetic value")
    monkeypatch.setenv("BC_SYNTHETIC_HOST_CANARY", "not-a-real-secret")
    prefix = f'''import os, socket, ctypes, errno
def denied(call):
    try:
        call()
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM):
            os._exit(41)
    else:
        os._exit(42)
denied(lambda: open({str(canary)!r}, 'rb'))
denied(lambda: open({str(canary)!r}, 'wb'))
denied(lambda: open('source.py', 'wb'))
denied(lambda: os.chmod('source.py', 0o600))
denied(lambda: os.symlink({str(canary)!r}, 'link'))
for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        denied(lambda: socket.socket(family, kind))
denied(os.fork)
denied(lambda: os.execve('/bin/true', ['true'], {{}}))
denied(lambda: os.kill(os.getpid(), 0))
if os.environ:
    os._exit(43)
for fd in range(3, 32):
    try:
        os.fstat(fd)
    except OSError:
        pass
    else:
        os._exit(44)
'''
    result = _run(prefix + _emit({"status": "parsed", "bills": []}))
    assert result.status == "returned"
    assert canary.read_bytes() == b"outside synthetic value"


def test_inheritable_host_descriptor_is_closed(tmp_path):
    canary = tmp_path / "fd-canary"
    canary.write_bytes(b"synthetic descriptor")
    with canary.open("rb") as stream:
        os.set_inheritable(stream.fileno(), True)
        result = _run(f'''import os
try:
    os.read({stream.fileno()}, 1)
except OSError:
    pass
else:
    os._exit(45)
''' + _emit({"status": "parsed", "bills": []}))
    assert result.status == "returned"


def test_dangerous_native_syscalls_are_denied_before_argument_processing():
    # x86_64 numbers; the child refuses other architectures. Invalid arguments
    # avoid performing a dangerous operation even if a filter regresses.
    source = '''import ctypes, errno, os
libc = ctypes.CDLL(None, use_errno=True)
for number in (101, 310, 311, 434, 425, 435, 165, 321):
    ctypes.set_errno(0)
    result = libc.syscall(number, -1, 0, 0, 0, 0, 0)
    if result != -1 or ctypes.get_errno() != errno.EPERM:
        os._exit(46)
'''
    assert _run(source + _emit({"status": "parsed", "bills": []})).status == "returned"


def test_alternate_x32_syscall_abi_cannot_bypass_filter():
    source = '''import ctypes, os
libc = ctypes.CDLL(None, use_errno=True)
result = libc.syscall(0x40000000 | 39)
if result >= 0:
    os.write(1, b'{"status":"parsed","bills":[{"bypass":true}]}')
    os._exit(0)
'''
    result = _run(source + _emit({"status": "parsed", "bills": []}))
    # libseccomp can kill a disallowed ABI or return an error for the syscall.
    assert result.status in {"returned", "child_failed"}
    if result.status == "returned":
        assert result.payload == {"status": "parsed", "bills": []}


@pytest.mark.parametrize("code", [
    "while True: pass",
    "bytearray(512 * 1024**2)",
    "import os\nwhile True: os.write(1, b'x' * 65536)",
    "import os\nwhile True: os.write(2, b'x' * 65536)",
])
def test_resource_overruns_are_bounded_and_sanitized(code):
    result = _run(code)
    assert result.status in {"child_failed", "output_limit", "wall_limit"}
    assert result.payload is None


def test_wall_timeout_kills_child_that_closed_both_output_pipes(monkeypatch):
    monkeypatch.setattr(sandbox, "WALL_SECONDS", 0.5)
    result = _run("import os\nos.close(1)\nos.close(2)\nwhile True: pass")
    assert result.status == "wall_limit"
    assert result.payload is None


@pytest.mark.parametrize("output", [
    b'{"status":"parsed","status":"parsed","bills":[]}',
    b'{"status":"parsed","bills":[],"pass":true}',
    b'{"status":"parsed","bills":NaN}',
    b'\xff', b'[]', b'[' * 2000 + b']' * 2000,
])
def test_host_refuses_malformed_child_protocol(output):
    result = _run(f"import os\nos.write(1, {output!r})\nos._exit(0)")
    assert result.status == "invalid_output"
    assert result.payload is None


def test_object_amplification_is_rejected_before_json_decoding(monkeypatch):
    def no_decode(*args, **kwargs):
        pytest.fail("object budget must be checked before allocating a JSON graph")
    monkeypatch.setattr(sandbox.json, "loads", no_decode)
    source = f'''import os
os.write(1, b'{{"status":"parsed","bills":[')
for _ in range({sandbox.MAX_JSON_CONTAINERS}):
    os.write(1, b'{{}},')
os.write(1, b'{{}}]}}')
os._exit(0)
'''
    result = _run(source)
    assert result.status == "invalid_output"
    assert result.payload is None


def test_scalar_amplification_is_rejected_before_decoding():
    data = b"[" + b"0," * sandbox.MAX_JSON_SCALARS + b"0]"
    with pytest.raises(ValueError, match="scalar count"):
        sandbox._check_json_budget(data)


def test_json_budget_ignores_escaped_structural_text():
    payload = {"status": "parsed", "bills": [], "text": '[]{}\\\"' * 1000}
    sandbox._check_json_budget(json.dumps(payload).encode())


def test_candidate_exception_content_is_never_reported():
    result = _run("raise RuntimeError('synthetic-private-error-marker')")
    assert result.status == "child_failed"
    assert "synthetic-private-error-marker" not in repr(result)


def test_launch_failure_removes_private_stage(tmp_path, monkeypatch):
    stages = []
    original = sandbox._private_file
    def record(path, contents):
        stages.append(path.parent)
        original(path, contents)
    def fail(*args, **kwargs):
        raise OSError("synthetic spawn failure")
    monkeypatch.setattr(sandbox, "_private_file", record)
    monkeypatch.setattr(sandbox.subprocess, "Popen", fail)
    assert _run("pass").status == "supervisor_failed"
    assert stages and all(not stage.exists() for stage in stages)


def test_staging_deadline_prevents_launch(monkeypatch):
    monkeypatch.setattr(sandbox, "WALL_SECONDS", 0)
    def no_launch(*args, **kwargs):
        pytest.fail("expired staging budget must not launch a child")
    monkeypatch.setattr(sandbox.subprocess, "Popen", no_launch)
    assert _run("pass").status == "wall_limit"


def test_pipe_read_failure_kills_reaps_and_cleans_stage(monkeypatch):
    observed = []
    real_popen = sandbox.subprocess.Popen
    def launch(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        observed.append((process, kwargs["cwd"]))
        return process
    def failed_read(*args, **kwargs):
        raise OSError("synthetic pipe failure")
    monkeypatch.setattr(sandbox.subprocess, "Popen", launch)
    monkeypatch.setattr(sandbox, "_collect", failed_read)
    assert _run("while True: pass").status == "supervisor_failed"
    process, stage = observed[0]
    assert process.poll() is not None
    assert process.stdout.closed and process.stderr.closed
    assert not stage.exists()


def test_cleanup_kills_descendant_after_leader_exits(tmp_path):
    # Use a separate trusted subreaper harness so the test reaps its orphan too.
    # This deliberately exercises supervisor defense if child confinement ever
    # regresses; the real restricted parser cannot fork or change its group.
    harness = textwrap.dedent('''
        import ctypes, json, os, subprocess, sys, time
        from billcommons_ingest.official_repair_sandbox import _collect, _stop
        libc = ctypes.CDLL(None)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise RuntimeError('cannot enable test subreaper')
        code = "import os,time; child=os.fork(); os._exit(0) if child else time.sleep(60)"
        process = subprocess.Popen([sys.executable, '-I', '-S', '-c', code],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, start_new_session=True, env={})
        try:
            status, output = _collect(process, deadline=time.monotonic() + 0.3)
        finally:
            _stop(process)
            process.stdout.close()
            process.stderr.close()
        pid, wait_status = os.waitpid(-1, 0)
        print(json.dumps({'status': status, 'descendant_killed': os.WIFSIGNALED(wait_status)
            and os.WTERMSIG(wait_status) == 9, 'leader_reaped': process.returncode == 0}))
    ''')
    run = subprocess.run([sys.executable, "-c", harness],
                         capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {"status": "wall_limit", "descendant_killed": True,
                                    "leader_reaped": True}


def test_slow_staging_exhausts_budget_without_launching(monkeypatch):
    elapsed = [0.0]
    write = sandbox._private_file
    def slow_write(path, contents):
        write(path, contents)
        elapsed[0] += sandbox.WALL_SECONDS
    def no_launch(*args, **kwargs):
        pytest.fail("slow staging must not launch an evaluator")
    monkeypatch.setattr(sandbox.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(sandbox, "_private_file", slow_write)
    monkeypatch.setattr(sandbox.subprocess, "Popen", no_launch)
    assert _run("pass").status == "wall_limit"


def test_delayed_reaping_preserves_recovery_record_and_stops_new_work(monkeypatch):
    real_wait = subprocess.Popen.wait
    def delayed_wait(process, timeout=None):
        if timeout == sandbox.REAP_SECONDS:
            raise subprocess.TimeoutExpired("synthetic delayed kernel exit", timeout)
        return real_wait(process, timeout=timeout)
    monkeypatch.setattr(subprocess.Popen, "wait", delayed_wait)
    result = _run(_emit({"status": "parsed", "bills": []}))
    assert result.status == "cleanup_pending"
    process, stage, record = sandbox._PENDING[0]
    try:
        assert result.cleanup == record
        assert json.loads((stage / "cleanup.json").read_bytes()) == record
        assert record["pid_start_ticks"].isdigit()
        with monkeypatch.context() as patch:
            patch.setattr(process, "poll", lambda: None)
            def no_launch(*args, **kwargs):
                pytest.fail("pending cleanup must stop another candidate")
            patch.setattr(sandbox.subprocess, "Popen", no_launch)
            assert _run("pass").status == "cleanup_pending"
    finally:
        real_wait(process, timeout=5)
        assert sandbox._reap_pending() is None
    assert not Path(record["stage"]).exists()


def test_reaped_leader_does_not_clear_live_group(tmp_path, monkeypatch):
    class ReapedLeader:
        pid = 42
        def poll(self):
            return 0
    stage = tmp_path / "stage"
    stage.mkdir()
    record = {"status": "sigkill_sent_reap_pending"}
    monkeypatch.setattr(sandbox, "_PENDING", [(ReapedLeader(), stage, record)])
    monkeypatch.setattr(sandbox, "_group_gone", lambda group: False)
    assert sandbox._reap_pending() is record
    assert stage.is_dir() and sandbox._PENDING
    monkeypatch.setattr(sandbox, "_group_gone", lambda group: True)
    assert sandbox._reap_pending() is None
    assert not stage.exists() and not sandbox._PENDING


@pytest.mark.parametrize("missing", [False, True])
def test_pending_stage_cleanup_failure_and_external_removal(tmp_path, monkeypatch, missing):
    class ReapedLeader:
        pid = 42
        def poll(self):
            return 0
    stage = tmp_path / "stage"
    if not missing:
        stage.mkdir()
    record = {"status": "sigkill_sent_reap_pending"}
    monkeypatch.setattr(sandbox, "_PENDING", [(ReapedLeader(), stage, record)])
    monkeypatch.setattr(sandbox, "_group_gone", lambda group: True)
    if not missing:
        with monkeypatch.context() as patch:
            def unavailable(*args, **kwargs):
                raise PermissionError("synthetic cleanup failure")
            patch.setattr(sandbox.shutil, "rmtree", unavailable)
            assert sandbox._reap_pending() is record
            assert record["status"] == "stage_cleanup_failed"
            assert stage.exists() and sandbox._PENDING
    assert sandbox._reap_pending() is None
    assert not stage.exists() and not sandbox._PENDING


def test_concurrent_invocation_cannot_pass_supervisor_reservation(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    def controlled(source, fixture, request, bootstrap, evidence):
        entered.set()
        assert release.wait(3)
        return sandbox.SandboxResult("returned", payload={"status": "parsed", "bills": []}, **evidence)
    monkeypatch.setattr(sandbox, "_run_prepared", controlled)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(_run, "pass")
        try:
            assert entered.wait(3)
            assert _run("pass").status == "runner_busy"
        finally:
            release.set()
        assert first.result().status == "returned"
