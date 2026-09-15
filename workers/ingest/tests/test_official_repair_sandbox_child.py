"""Black-box checks for the native candidate child protocol."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap


CHILD = Path(__file__).parents[1] / "billcommons_ingest" / "official_repair_sandbox_child.py"
REQUEST = '{"source_url":"https://example.invalid/ca.zip","retrieved_at":"2026-01-02T00:00:00+00:00"}'


def _candidate(body: str = "") -> str:
    return """import datetime
class Event:
    occurrence_id = 'occurrence-1'
    official_bill_id = 'AB 1'
    history_id = 'history-1'
    action_date = datetime.date(2026, 1, 2)
    description = 'Referred'
    sequence = 4
    updated_at = '2026-01-02T00:00:00+00:00'
    source_url = 'https://example.invalid/ca.zip'
    raw_fields = {'ACTION': 'Referred'}
class Batch:
    scoped_bill_ids = ('AB 1',)
    events_by_official_bill_id = {'AB 1': (Event(),)}
def parse_ca_official_actions_zip(raw, *, source_url, retrieved_at):
    if raw != b'fixture' or source_url != 'https://example.invalid/ca.zip':
        raise ValueError('unexpected trusted input')
""" + body + "\n    return Batch()\n"


def _stage(tmp_path: Path, source: str) -> Path:
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copyfile(CHILD, stage / "bootstrap.py")
    (stage / "source.py").write_text(source)
    (stage / "fixture.bin").write_bytes(b"fixture")
    (stage / "request.json").write_text(REQUEST)
    for path in stage.iterdir():
        path.chmod(0o400)
    stage.chmod(0o500)
    return stage


def _run(stage: Path, *, parent_pid: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(stage / "bootstrap.py"), str(stage),
         str(os.getpid() if parent_pid is None else parent_pid)],
        env={},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )


def test_child_emits_only_ordered_parser_facts(tmp_path):
    completed = _run(_stage(tmp_path, _candidate()))
    assert completed.returncode == 0
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "status": "parsed",
        "bills": [{"bill_id": "AB 1", "events": [{
            "occurrence_id": "occurrence-1", "official_bill_id": "AB 1", "history_id": "history-1",
            "action_date": "2026-01-02", "description": "Referred", "sequence": 4,
            "updated_at": "2026-01-02T00:00:00+00:00", "source_url": "https://example.invalid/ca.zip",
            "raw_fields": {"ACTION": "Referred"},
        }]}],
    }


def test_child_candidate_failure_is_silent_and_fixed_code(tmp_path):
    forbidden = tmp_path / "forbidden-child-write"
    completed = _run(_stage(
        tmp_path, _candidate(f"    open({str(forbidden)!r}, 'wb').write(b'x')"),
    ))
    assert completed.returncode == 70
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert not forbidden.exists()


def test_invalid_stage_is_silent_bootstrap_failure(tmp_path):
    stage = _stage(tmp_path, _candidate())
    stage.chmod(0o700)
    completed = _run(stage)
    assert completed.returncode == 78
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_missing_native_library_stops_before_candidate_loading(tmp_path):
    marker = tmp_path / "candidate-must-not-run"
    stage = _stage(tmp_path, f"open({str(marker)!r}, 'w').write('unexpected')\n")
    bootstrap = stage / "bootstrap.py"
    source = bootstrap.read_text()
    assert '"libseccomp.so.2"' in source
    bootstrap.chmod(0o600)
    bootstrap.write_text(source.replace('"libseccomp.so.2"', '"libseccomp-bc-test-missing.so.2"'))
    bootstrap.chmod(0o400)
    completed = _run(stage)
    assert completed.returncode == 78
    assert completed.stdout == completed.stderr == ""
    assert not marker.exists()


def test_wrong_parent_identity_stops_before_candidate_loading(tmp_path):
    completed = _run(_stage(tmp_path, "import os\nos.write(1, b'candidate-loaded')\n"), parent_pid=0)
    assert completed.returncode == 78
    assert completed.stdout == completed.stderr == ""


def test_blocked_candidate_dies_when_supervisor_is_killed(tmp_path):
    # A real FUTEX_WAIT sleeps without using RLIMIT_CPU. Run a separate trusted
    # subreaper harness so pytest never acquires global child-reaping duties.
    stage = _stage(tmp_path, """import os, ctypes
os.write(1, b'candidate-started\\n')
word = ctypes.c_int(0)
ctypes.CDLL(None).syscall(202, ctypes.byref(word), 0, 0, 0, 0, 0)
""")
    supervisor = textwrap.dedent('''
        import os, signal, subprocess, sys, time
        signal.alarm(7)
        stage = sys.argv[1]
        child = subprocess.Popen([sys.executable, '-I', '-S', '-B', stage + '/bootstrap.py',
            stage, str(os.getpid())], env={}, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        print(child.pid, flush=True)
        assert child.stdout.readline() == b'candidate-started\\n'
        print('ready', flush=True)
        time.sleep(30)
    ''')
    harness = textwrap.dedent('''
        import ctypes, json, os, signal, subprocess, sys, time
        assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
        parent = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        child_pid = None
        child_reaped = False
        try:
            child_pid = int(parent.stdout.readline())
            assert parent.stdout.readline().strip() == 'ready'
            parent.kill()
            parent.wait(timeout=2)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                found, status = os.waitpid(child_pid, os.WNOHANG)
                if found:
                    child_reaped = True
                    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
                    print(json.dumps({'blocked_candidate_killed': True}))
                    break
                time.sleep(0.01)
            else:
                raise AssertionError('candidate survived supervisor death')
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=2)
            if child_pid is not None and not child_reaped:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(child_pid, 0)
    ''')
    completed = subprocess.run([sys.executable, "-c", harness, supervisor, str(stage)],
                               capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"blocked_candidate_killed": True}
