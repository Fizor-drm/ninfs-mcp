"""Runner: ninfs CLI resident-process management and MountSession ownership.

The ninfs package is driven exclusively through
``[sys.executable, "-m", "ninfs", <kind>, ...]`` subprocesses; it is never
imported here. All mount CLIs run with ``-f`` so the Popen object itself is
the filesystem process. stdout always goes to DEVNULL (ninfs prints key
material there); stderr is drained from spawn time into a bounded buffer.

This module returns internal types only. Public dict shaping lives in
server.py (Task 5).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .policy import check_no_overlap, get_secrets, sanitize

READY_TIMEOUT_S = 10.0
READY_INTERVAL_S = 0.2
STDERR_TAIL_BYTES = 2000
STDERR_BUFFER_CHUNKS = 64  # x 1KB reads ~= 64KB retained
STOP_WAIT_S = 10.0

_LOCK = threading.RLock()
_SESSIONS: dict[str, MountSession] = {}
_RESIDUALS: list[InternalMount] = []


class RunnerError(Exception):
    """Internal error with sanitized message. Server maps it to public dicts."""

    def __init__(self, code: str, message_sanitized: str, incomplete: bool = False):
        super().__init__(message_sanitized)
        self.code = code
        self.message_sanitized = message_sanitized
        self.incomplete = incomplete


@dataclass
class ErrorContext:
    secrets: list[str]


@dataclass
class InternalMount:
    session_id: str
    kind: str  # "sd" | "sdtitle" | "ncch" | "exefs"
    mount_point: str
    proc: Any
    handle_id: str | None
    secrets: list[str]


@dataclass
class MountSession:
    mount_id: str
    staging: str
    children: list[InternalMount] = field(default_factory=list)
    title_handles: dict[str, TitleHandle] = field(default_factory=dict)
    sd_root: str = ""


@dataclass
class TitleHandle:
    handle_id: str
    mount_id: str
    requested_title_id: str
    resolved_title_id: str
    kind: str
    tmd_found: bool
    tmd_path: str


@dataclass
class ExtractResult:
    dest_path: str
    size: int
    sha256: str
    code_entry: str


@dataclass
class UnmountSummary:
    ok: bool
    incomplete: bool
    errors_sanitized: list[str]
    residuals: list[InternalMount]


@dataclass
class RetryReport:
    recovered: int
    remaining: int
    errors_sanitized: list[str]


def _fail(code: str, raw: str, secrets: list[str], incomplete: bool = False) -> RunnerError:
    tail = raw[-STDERR_TAIL_BYTES:]
    return RunnerError(code, sanitize(tail, secrets), incomplete)


def _drain_in_background(stream: Any) -> deque:
    buf: deque = deque(maxlen=STDERR_BUFFER_CHUNKS)

    def _pump() -> None:
        try:
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    break
                buf.append(chunk)
        except Exception:
            pass

    threading.Thread(target=_pump, daemon=True).start()
    return buf


def check_winfs() -> None:
    """Gate: isolated subprocess proves the FUSE import works. No state."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "ninfs", "sd", "--help"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    buf = _drain_in_background(proc.stderr)
    try:
        rc = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RunnerError("winfsp-unavailable", "help probe timed out")
    if rc != 0:
        raw = b"".join(buf).decode("utf-8", "replace")
        raise _fail("winfsp-unavailable", raw or f"exit={rc}", get_secrets())


def spawn_mount(argv: list[str]) -> Any:
    """Start a resident mount process. ``-f`` is mandatory."""
    if "-f" not in argv:
        raise ValueError("foreground (-f) is mandatory so Popen owns the mount")
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    proc._stderr_buf = _drain_in_background(proc.stderr)  # type: ignore[attr-defined]
    return proc


def _stderr_tail(proc: Any) -> str:
    buf = getattr(proc, "_stderr_buf", None)
    raw = b"".join(buf) if buf else b""
    return raw[-STDERR_TAIL_BYTES:].decode("utf-8", "replace")


def wait_ready(
    proc: Any,
    mount_point: str,
    check: Callable[[], bool],
    timeout_s: float = READY_TIMEOUT_S,
) -> None:
    """Wait until the mount serves content. Raises RunnerError (no cleanup)."""
    deadline = time.monotonic() + timeout_s
    while True:
        if proc.poll() is not None:
            raise RunnerError("mount-exited", _stderr_tail(proc) or "process exited early")
        try:
            if check():
                return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise RunnerError("not-ready", _stderr_tail(proc) or "mount not ready in time")
        time.sleep(min(READY_INTERVAL_S, max(0.0, deadline - time.monotonic())))


def _staging_is_clean(mount_point: str) -> bool:
    try:
        return os.listdir(mount_point) == []
    except OSError:
        return False


def stop_proc(mount: InternalMount) -> bool:
    """Idempotent stop. Always verifies staging. True = fully cleaned."""
    proc = mount.proc
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            pass
        if proc.poll() is None:
            return False  # still alive: residual (no escalation on Windows)
    return _staging_is_clean(mount.mount_point)


def cleanup_core(mounts: list[InternalMount]) -> list[InternalMount]:
    """Reverse-order best-effort cleanup. Returns un-cleanable residuals."""
    residuals: list[InternalMount] = []
    for mount in reversed(mounts):
        try:
            cleaned = stop_proc(mount)
        except Exception:
            cleaned = False
        if not cleaned:
            residuals.append(mount)
    return residuals


def _drives() -> list[str]:
    return [f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")]


def detect_sd(sd_root_override: str | None = None) -> dict:
    """Stateless query. Returns a public-shaped dict (documented exception)."""
    if sd_root_override is not None:
        if not os.path.isdir(sd_root_override):
            return {"error": "not-found", "message_sanitized": "override is not a directory",
                    "incomplete": False}
        root = os.path.abspath(sd_root_override)
        return {
            "sd_root": root,
            "has_n3ds_dir": os.path.basename(os.path.normpath(root)) == "Nintendo 3DS",
            "has_boot9": os.path.isfile(os.environ.get("NINFS_BOOT9_PATH", "")),
            "has_movable": os.path.isfile(os.environ.get("NINFS_MOVABLE_PATH", "")),
        }
    if "NINFS_SD_ROOT" in os.environ and os.environ["NINFS_SD_ROOT"]:
        return detect_sd(os.environ["NINFS_SD_ROOT"])
    candidates = [
        os.path.join(drive, "Nintendo 3DS")
        for drive in _drives()
        if os.path.isdir(os.path.join(drive, "Nintendo 3DS"))
    ]
    if not candidates:
        return {"error": "not-found", "message_sanitized": "no SD candidate found",
                "incomplete": False}
    if len(candidates) > 1:
        return {"error": "ambiguous", "candidates": sorted(candidates)}
    return {
        "sd_root": candidates[0],
        "has_n3ds_dir": True,
        "has_boot9": os.path.isfile(os.environ.get("NINFS_BOOT9_PATH", "")),
        "has_movable": os.path.isfile(os.environ.get("NINFS_MOVABLE_PATH", "")),
    }


def _workspace() -> str:
    ws = os.environ.get("NINFS_WORKSPACE", "")
    if not ws or not os.path.isabs(ws):
        raise RunnerError("workspace-misconfigured", "NINFS_WORKSPACE must be an absolute path")
    return ws


def _check_staging_area(workspace: str, staging: str, secrets: list[str]) -> None:
    """Refuse when existing staging ancestors escape the managed area."""
    ws_real = os.path.realpath(workspace)
    node = os.path.abspath(staging)
    while True:
        if os.path.lexists(node):
            try:
                inside = os.path.commonpath([ws_real, os.path.realpath(node)]) == ws_real
            except ValueError:
                inside = False
            if not inside:
                raise RunnerError(
                    "staging-refused",
                    sanitize(f"staging escapes workspace: {staging}", secrets),
                )
        parent = os.path.dirname(node)
        if parent == node or os.path.realpath(node) == ws_real or parent == ws_real:
            break
        node = parent


def find_title(session: MountSession, title_id: str) -> TitleHandle:  # noqa: ARG001
    raise NotImplementedError


def extract_code(handle: TitleHandle, dest_rel: str) -> ExtractResult:  # noqa: ARG001
    raise NotImplementedError


def _mount_args(kind: str, inputs: list[str], mount_point: str, extra: list[str]) -> list[str]:
    return [sys.executable, "-m", "ninfs", kind, *extra, *inputs, mount_point, "-f"]


def mount_sd() -> MountSession:
    """Select, overlap-check, stage, spawn, verify. Immediate cleanup on failure."""
    with _LOCK:
        found = detect_sd()
        if "error" in found:
            raise RunnerError(
                found["error"],
                sanitize(str(found.get("message_sanitized", found["error"])), get_secrets()),
            )
        sd_root = found["sd_root"]
        ctx = ErrorContext(secrets=get_secrets([sd_root]))
        ws = _workspace()
        try:
            check_no_overlap(ws, sd_root)
        except ValueError as e:
            raise RunnerError("overlap", sanitize(str(e), ctx.secrets))
        staging = os.path.join(ws, ".mounts", uuid.uuid4().hex, "sd", "sd")
        _check_staging_area(ws, staging, ctx.secrets)
        os.makedirs(staging, exist_ok=True)
        boot9 = os.environ.get("NINFS_BOOT9_PATH", "")
        movable = os.environ.get("NINFS_MOVABLE_PATH", "")
        proc = spawn_mount(
            _mount_args("sd", [sd_root], staging,
                        ["--movable", movable, "--boot9", boot9, "--ro"])
        )
        mount = InternalMount(
            session_id="", kind="sd", mount_point=staging, proc=proc,
            handle_id=None, secrets=ctx.secrets,
        )
        try:
            wait_ready(proc, staging, lambda: len(os.listdir(staging)) > 0)
        except RunnerError as e:
            if stop_proc(mount):
                raise RunnerError(e.code, sanitize(e.message_sanitized, ctx.secrets))
            mount.session_id = "pending"
            _RESIDUALS.append(mount)
            raise RunnerError(e.code, sanitize(e.message_sanitized, ctx.secrets), True)
        session = MountSession(
            mount_id=uuid.uuid4().hex, staging=os.path.dirname(os.path.dirname(staging)),
            children=[mount], sd_root=sd_root,
        )
        mount.session_id = session.mount_id
        _SESSIONS[session.mount_id] = session
        return session


def retry_residuals() -> RetryReport:
    """Re-attempt every tracked residual. Structured report."""
    with _LOCK:
        recovered = 0
        errors: list[str] = []
        for mount in list(_RESIDUALS):
            leftovers = cleanup_core([mount])
            if not leftovers:
                _RESIDUALS.remove(mount)
                recovered += 1
            else:
                errors.extend(
                    sanitize(f"still held: {mount.kind} for {mount.session_id}", mount.secrets)
                    for _ in [0]
                )
        remaining = len(_RESIDUALS)
        return RetryReport(recovered=recovered, remaining=remaining, errors_sanitized=errors)


def unmount(mount_id: str) -> UnmountSummary:
    """Invalidate IDs, reverse-clean the session, sweep all residuals, merge."""
    with _LOCK:
        secrets = get_secrets()
        session = _SESSIONS.pop(mount_id, None)
        owned = [m for m in _RESIDUALS if m.session_id == mount_id] if session is None else []
        for m in owned:
            _RESIDUALS.remove(m)
        targets = (session.children if session else []) + owned
        primary_left = cleanup_core(targets)
        for m in primary_left:
            if m not in _RESIDUALS:
                _RESIDUALS.append(m)
        report = retry_residuals()
        errors = list(report.errors_sanitized)
        if session is None and not owned and report.recovered == 0 and not errors:
            # global sweep already ran; nothing ever known under this id
            raise RunnerError("unknown-id", sanitize(f"unknown mount id", secrets))
        if session is None and not owned and report.recovered > 0:
            pass  # recovered strangers: still report below
        ok = not errors and not _RESIDUALS
        return UnmountSummary(
            ok=ok,
            incomplete=bool(_RESIDUALS),
            errors_sanitized=errors,
            residuals=list(_RESIDUALS),
        )
