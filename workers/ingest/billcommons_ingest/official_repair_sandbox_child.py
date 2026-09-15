"""Fail-closed native bootstrap for one untrusted CA parser candidate.

This file is copied as ``bootstrap.py`` into a parent-created private stage and
is executed as ``python -I -S -B bootstrap.py STAGE PARENT_PID``. It deliberately trusts
only the parent to create the immutable stage.  The candidate interpreter,
candidate source, and every byte written to stdout are untrusted; separate
Python globals are not a security boundary.  This is also a same-user,
kernel-feature-dependent containment layer, not a general VM or a defence
against kernel vulnerabilities.

The parent must independently validate the returned facts and bind them to its
own fixture and comparison result.  This child never reports a pass, digest, or
comparison result.
"""
from __future__ import annotations

import os

# The parent MUST pass an empty environment at exec. Clear any interpreter-
# added locale defaults too; clearing here cannot erase inherited secrets or
# undo loader configuration from an incorrectly launched process.
os.environ.clear()

# Keep every module the known-good CA parser imports resident before Landlock
# limits reads to the stage and seccomp applies its default-deny filter.
import __future__
import collections
import ctypes
import datetime
import encodings.cp437
import encodings.cp1252
import encodings.latin_1
import encodings.utf_8
import errno
import hashlib
import importlib.util
import io
import json
import lzma
import platform
import re
import resource
import socket
import stat
import sys
import time
import types
import typing
import urllib.parse
import zipfile
import zlib
import csv
import _strptime
from dataclasses import dataclass


BOOTSTRAP_EXIT = 78
CANDIDATE_EXIT = 70
MAX_SOURCE_BYTES = 256 * 1024
MAX_FIXTURE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 8 * 1024
LANDLOCK_CREATE_RULESET = 444
LANDLOCK_ADD_RULE = 445
LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_ACCESS_FS_ALL = (1 << 15) - 1
LANDLOCK_ACCESS_NET_TCP = 3
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
PR_SET_DUMPABLE = 4
PR_SET_NO_NEW_PRIVS = 38
PR_SET_PDEATHSIG = 1
SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_ERRNO = 0x00050000


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = (("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64))


class _LandlockPathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


def _fail_bootstrap() -> None:
    raise RuntimeError("bootstrap failure")


def _check(result: bool) -> None:
    if not result:
        _fail_bootstrap()


def _checked_call(result: int) -> int:
    if result < 0:
        _fail_bootstrap()
    return result


def _single_thread_and_fds() -> None:
    _check(len(os.listdir("/proc/self/task")) == 1)
    try:
        entries = os.listdir("/proc/self/fd")
    except OSError:
        _fail_bootstrap()
    for entry in entries:
        try:
            descriptor = int(entry)
        except ValueError:
            _fail_bootstrap()
        if descriptor >= 3:
            try:
                os.close(descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    _fail_bootstrap()


def _verify_standard_fds() -> None:
    """Require the parent process contract before accepting a candidate."""
    _check(stat.S_ISCHR(os.fstat(0).st_mode))
    _check(os.fstat(0).st_rdev == os.stat("/dev/null").st_rdev)
    _check(stat.S_ISFIFO(os.fstat(1).st_mode))
    _check(stat.S_ISFIFO(os.fstat(2).st_mode))


def _lstat_regular(path: str, maximum: int) -> os.stat_result:
    info = os.lstat(path)
    _check(stat.S_ISREG(info.st_mode))
    _check(not stat.S_ISLNK(info.st_mode))
    _check(info.st_uid == os.getuid() and info.st_gid == os.getgid())
    _check(stat.S_IMODE(info.st_mode) == 0o400 and info.st_nlink == 1)
    _check(0 < info.st_size <= maximum)
    return info


def _validate_stage(stage: str) -> None:
    _check(os.path.isabs(stage))
    info = os.lstat(stage)
    _check(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode))
    _check(info.st_uid == os.getuid() and info.st_gid == os.getgid())
    _check(stat.S_IMODE(info.st_mode) == 0o500 and info.st_nlink >= 2)
    _check(set(os.listdir(stage)) == {"source.py", "fixture.bin", "request.json", "bootstrap.py"})
    _lstat_regular(os.path.join(stage, "source.py"), MAX_SOURCE_BYTES)
    _lstat_regular(os.path.join(stage, "fixture.bin"), MAX_FIXTURE_BYTES)
    _lstat_regular(os.path.join(stage, "request.json"), MAX_REQUEST_BYTES)
    bootstrap = _lstat_regular(os.path.join(stage, "bootstrap.py"), MAX_SOURCE_BYTES)
    own = os.lstat(os.path.abspath(__file__))
    _check((bootstrap.st_dev, bootstrap.st_ino) == (own.st_dev, own.st_ino))


def _read_stage_file(name: str, maximum: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags)
    try:
        parts: list[bytes] = []
        left = maximum + 1
        while left:
            chunk = os.read(descriptor, min(left, 65536))
            if not chunk:
                break
            parts.append(chunk)
            left -= len(chunk)
        value = b"".join(parts)
    finally:
        os.close(descriptor)
    _check(0 < len(value) <= maximum)
    return value


def _strict_request(raw: bytes) -> tuple[str, datetime.datetime]:
    def pairs(items: list[tuple[object, object]]) -> dict[object, object]:
        result: dict[object, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    request = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    _check(isinstance(request, dict) and set(request) == {"source_url", "retrieved_at"})
    source_url = request["source_url"]
    retrieved_at = request["retrieved_at"]
    _check(isinstance(source_url, str) and bool(source_url) and isinstance(retrieved_at, str))
    parsed_at = datetime.datetime.fromisoformat(retrieved_at)
    _check(parsed_at.tzinfo is not None and parsed_at.utcoffset() is not None)
    return source_url, parsed_at


def _configure_native(stage: str, expected_parent: int) -> None:
    _check(sys.platform == "linux" and platform.machine() == "x86_64")
    # Load libraries inside the bootstrap error boundary and before Landlock
    # removes system-library access. Missing libseccomp must fail closed.
    libc = ctypes.CDLL(None, use_errno=True)
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    # A sleeping candidate does not consume its CPU limit. Tie its lifetime to
    # the creating supervisor thread before loading any candidate bytes. The
    # second identity check closes the race where that parent died just before
    # prctl: the kernel does not send this signal retroactively. Seccomp later
    # denies prctl, credential changes and fork, so the candidate cannot undo it.
    _check(expected_parent > 0 and os.getppid() == expected_parent)
    _checked_call(libc.prctl(PR_SET_PDEATHSIG, 9, 0, 0, 0))
    _check(os.getppid() == expected_parent)
    abi = libc.syscall(LANDLOCK_CREATE_RULESET, 0, 0, LANDLOCK_CREATE_RULESET_VERSION)
    _check(abi >= 4)
    rules = _LandlockRulesetAttr(LANDLOCK_ACCESS_FS_ALL, LANDLOCK_ACCESS_NET_TCP)
    rules_fd = _checked_call(libc.syscall(
        LANDLOCK_CREATE_RULESET, ctypes.byref(rules), ctypes.sizeof(rules), 0,
    ))
    try:
        stage_fd = os.open(stage, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            path = _LandlockPathBeneathAttr(
                LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR, stage_fd,
            )
            _checked_call(libc.syscall(
                LANDLOCK_ADD_RULE, rules_fd, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(path), 0,
            ))
        finally:
            os.close(stage_fd)
        _checked_call(libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0))
        _checked_call(libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        os.chdir(stage)
        _checked_call(libc.syscall(LANDLOCK_RESTRICT_SELF, rules_fd, 0))
    finally:
        os.close(rules_fd)
    _install_seccomp(seccomp)


def _install_seccomp(seccomp) -> None:
    seccomp.seccomp_init.argtypes = (ctypes.c_uint32,)
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint)
    seccomp.seccomp_rule_add.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = (ctypes.c_void_p,)
    seccomp.seccomp_load.restype = ctypes.c_int
    seccomp.seccomp_release.argtypes = (ctypes.c_void_p,)
    context = seccomp.seccomp_init(SCMP_ACT_ERRNO | errno.EPERM)
    _check(bool(context))
    try:
        names = (
            "read write close fstat newfstatat stat lstat lseek mmap munmap mprotect brk "
            "rt_sigaction rt_sigprocmask rt_sigreturn sigaltstack exit exit_group getrandom "
            "clock_gettime futex getpid gettid getrusage madvise readlink openat open getdents64"
        ).split()
        for name in names:
            number = seccomp.seccomp_syscall_resolve_name(name.encode("ascii"))
            _check(number >= 0)
            _check(seccomp.seccomp_rule_add(context, SCMP_ACT_ALLOW, number, 0) == 0)
        _check(seccomp.seccomp_load(context) == 0)
    finally:
        seccomp.seccomp_release(context)


def _facts(batch: object) -> dict[str, object]:
    bills: list[dict[str, object]] = []
    for bill_id in batch.scoped_bill_ids:
        events: list[dict[str, object]] = []
        for event in batch.events_by_official_bill_id[bill_id]:
            action_date = event.action_date
            events.append({
                "occurrence_id": event.occurrence_id,
                "official_bill_id": event.official_bill_id,
                "history_id": event.history_id,
                "action_date": action_date.isoformat() if action_date is not None else None,
                "description": event.description,
                "sequence": event.sequence,
                "updated_at": event.updated_at,
                "source_url": event.source_url,
                "raw_fields": dict(event.raw_fields),
            })
        bills.append({"bill_id": bill_id, "events": events})
    return {"status": "parsed", "bills": bills}


def _run_candidate() -> None:
    source_url, retrieved_at = _strict_request(_read_stage_file("request.json", MAX_REQUEST_BYTES))
    fixture = _read_stage_file("fixture.bin", MAX_FIXTURE_BYTES)
    specification = importlib.util.spec_from_file_location("_untrusted_candidate", "source.py")
    _check(specification is not None and specification.loader is not None)
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    batch = module.parse_ca_official_actions_zip(fixture, source_url=source_url, retrieved_at=retrieved_at)
    payload = json.dumps(_facts(batch), ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    sys.stdout.write(payload + "\n")
    sys.stdout.flush()


def main() -> int:
    os.environ.clear()
    try:
        _check(len(sys.argv) == 3 and len(sys.argv[2]) <= 16)
        stage = sys.argv[1]
        expected_parent = int(sys.argv[2])
        _single_thread_and_fds()
        _verify_standard_fds()
        _validate_stage(stage)
        _configure_native(stage, expected_parent)
    except BaseException:
        return BOOTSTRAP_EXIT
    try:
        _run_candidate()
    except BaseException:
        return CANDIDATE_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
