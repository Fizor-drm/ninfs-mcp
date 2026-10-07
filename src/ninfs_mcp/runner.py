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

import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .policy import check_no_overlap, get_secrets, resolve_dest, sanitize

_TITLE_ID_RE = re.compile(r"[0-9a-fA-F]{16}")

# sig-type: (sig-size, padding), mirrored from pyctr (reference only).
_SIG_SIZES = {
    0x00010000: (0x200, 0x3C),
    0x00010001: (0x100, 0x3C),
    0x00010002: (0x3C, 0x40),
    0x00010003: (0x200, 0x3C),
    0x00010004: (0x100, 0x3C),
    0x00010005: (0x3C, 0x40),
}

READY_TIMEOUT_S = 10.0
READY_INTERVAL_S = 0.2
STDERR_TAIL_BYTES = 2000
STDERR_BUFFER_CHUNKS = 64  # x 1KB reads ~= 64KB retained
STOP_WAIT_S = 10.0

_LOCK = threading.RLock()
_SESSIONS: dict[str, MountSession] = {}
_RESIDUALS: list[InternalMount] = []
CANCEL_CHECK: Callable[[], None] = lambda: None


class RunnerError(Exception):
    """Internal error with sanitized message. Server maps it to public dicts."""

    def __init__(self, code: str, message_sanitized: str, incomplete: bool = False):
        super().__init__(message_sanitized)
        self.code = code
        self.message_sanitized = message_sanitized
        self.incomplete = incomplete


class CancelRequested(Exception):
    """Cooperative cancellation signal for checkpoints."""


def _cancel_point() -> None:
    CANCEL_CHECK()


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
    timeout_s: float | None = None,
) -> None:
    """Wait until the mount serves content. Raises RunnerError (no cleanup)."""
    if timeout_s is None:
        timeout_s = READY_TIMEOUT_S
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


def _parse_tmd_exec(data: bytes) -> tuple[int, str] | None:
    """Parse TMD without pyctr. Returns (version, exec content id) or None."""
    if len(data) < 4:
        return None
    sig_type = int.from_bytes(data[0:4], "big")
    sizes = _SIG_SIZES.get(sig_type)
    if sizes is None:
        return None
    hs = 4 + sizes[0] + sizes[1]
    if len(data) < hs + 0xC4:
        return None
    header = data[hs:hs + 0xC4]
    version = int.from_bytes(header[0x9C:0x9E], "big")
    count = int.from_bytes(header[0x9E:0xA0], "big")
    recs_off = hs + 0xC4 + 0x900
    if len(data) < recs_off + count * 0x30:
        return None
    for i in range(count):
        rec = data[recs_off + i * 0x30:recs_off + (i + 1) * 0x30]
        if int.from_bytes(rec[4:6], "big") == 0:
            return (version, rec[0:4].hex())
    return None


def _complete_candidates(sd_mount_point: str, high: str, low: str) -> list[tuple[int, str]]:
    """Complete (parseable TMD + non-empty exec .app) candidates, unsorted."""
    found: list[tuple[int, str]] = []
    try:
        id1s = sorted(os.listdir(sd_mount_point))
    except OSError:
        return found
    for id1 in id1s:
        content = os.path.join(sd_mount_point, id1, "title", high, low, "content")
        if not os.path.isdir(content):
            continue
        try:
            names = sorted(os.listdir(content))
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(".tmd"):
                continue
            path = os.path.join(content, name)
            try:
                if os.path.getsize(path) > 1 << 20:
                    continue
                with open(path, "rb") as f:
                    parsed = _parse_tmd_exec(f.read())
            except OSError:
                continue
            if parsed is None:
                continue
            version, exec_id = parsed
            app = os.path.join(content, f"{exec_id}.app")
            try:
                if os.path.isfile(app) and os.path.getsize(app) > 0:
                    found.append((version, path))
            except OSError:
                continue
    return found


def _rank(candidates: list[tuple[int, str]]) -> str | None:
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-c[0], c[1]))
    return candidates[0][1]


def find_title(session: MountSession, title_id: str) -> TitleHandle:
    """Resolve update-first, then mount sdtitle -> ncch -> exefs. Internal type."""
    tid = (title_id or "").lower()
    if not _TITLE_ID_RE.fullmatch(tid):
        raise RunnerError("bad-title-id", sanitize(f"bad title id: {title_id}", get_secrets()))
    high, low = tid[:8], tid[8:]
    if high not in ("00040000", "0004000e"):
        raise RunnerError("unsupported-category", sanitize(f"unsupported category: {high}", get_secrets()))
    with _LOCK:
        sd_mp = None
        for child in session.children:
            if child.kind == "sd":
                sd_mp = child.mount_point
                break
        if sd_mp is None:
            raise RunnerError("no-sd-mount", "session has no SD mount", False)
        secrets = get_secrets([session.sd_root])
        if high == "0004000e":
            tmd_path = _rank(_complete_candidates(sd_mp, "0004000e", low))
            if tmd_path is None:
                raise RunnerError("title-not-found",
                                  sanitize(f"no complete update title for {tid}", secrets))
            resolved, kind = tid, "update"
        else:
            tmd_path = _rank(_complete_candidates(sd_mp, "0004000e", low))
            if tmd_path is not None:
                resolved, kind = "0004000e" + low, "update"
            else:
                tmd_path = _rank(_complete_candidates(sd_mp, "00040000", low))
                if tmd_path is None:
                    raise RunnerError("title-not-found",
                                      sanitize(f"no complete title for {tid}", secrets))
                resolved, kind = tid, "base"
        handle_id = uuid.uuid4().hex
        base = os.path.join(session.staging, handle_id)
        created: list[InternalMount] = []
        try:
            boot9 = os.environ.get("NINFS_BOOT9_PATH", "")
            sdtitle_mp = os.path.join(base, "sdtitle")
            os.makedirs(sdtitle_mp, exist_ok=True)
            proc = spawn_mount(_mount_args("sdtitle", [tmd_path], sdtitle_mp, ["--boot9", boot9]))
            created.append(InternalMount(session.mount_id, "sdtitle", sdtitle_mp, proc,
                                         handle_id, secrets))
            session.children.append(created[-1])
            wait_ready(proc, sdtitle_mp, lambda: os.path.exists(os.path.join(sdtitle_mp, "tmd.bin")))
            try:
                entries = os.listdir(sdtitle_mp)
            except OSError as e:
                raise RunnerError("title-listing-failed", sanitize(str(e), secrets))
            ncchs = [e for e in entries if e.lower().endswith(".ncch")]
            if not ncchs:
                raise RunnerError("no-ncch", sanitize("no NCCH content found", secrets))
            zero = sorted(n for n in ncchs if n.startswith("0000."))
            if zero:
                chosen = zero[0]
            else:
                def _size(name: str) -> int:
                    try:
                        return os.path.getsize(os.path.join(sdtitle_mp, name))
                    except OSError:
                        return -1
                chosen = sorted(ncchs, key=lambda n: (-_size(n), n))[0]
            ncch_mp = os.path.join(base, "ncch")
            os.makedirs(ncch_mp, exist_ok=True)
            proc = spawn_mount(_mount_args("ncch", [os.path.join(sdtitle_mp, chosen)],
                                           ncch_mp, ["--boot9", boot9]))
            created.append(InternalMount(session.mount_id, "ncch", ncch_mp, proc,
                                         handle_id, secrets))
            session.children.append(created[-1])
            wait_ready(proc, ncch_mp, lambda: os.path.exists(os.path.join(ncch_mp, "exefs.bin")))
            exefs_mp = os.path.join(base, "exefs")
            os.makedirs(exefs_mp, exist_ok=True)
            proc = spawn_mount(_mount_args("exefs", [os.path.join(ncch_mp, "exefs.bin")],
                                           exefs_mp, ["--decompress-code"]))
            created.append(InternalMount(session.mount_id, "exefs", exefs_mp, proc,
                                         handle_id, secrets))
            session.children.append(created[-1])
            wait_ready(proc, exefs_mp, lambda: any(
                os.path.exists(os.path.join(exefs_mp, n))
                for n in ("code-decompressed.bin", "code.bin")))
        except RunnerError as e:
            leftovers = cleanup_core(created)
            for m in created:
                if m in session.children:
                    session.children.remove(m)
            for m in leftovers:
                if m not in _RESIDUALS:
                    _RESIDUALS.append(m)
            raise RunnerError(e.code, sanitize(e.message_sanitized, secrets), bool(leftovers))
        handle = TitleHandle(handle_id=handle_id, mount_id=session.mount_id,
                             requested_title_id=tid, resolved_title_id=resolved, kind=kind,
                             tmd_found=True, tmd_path=tmd_path)
        session.title_handles[handle_id] = handle
        return handle


def extract_code(handle: TitleHandle, dest_rel: str) -> ExtractResult:
    """Copy code-decompressed.bin to the workspace atomically. Internal type."""
    with _LOCK:
        session = _SESSIONS.get(handle.mount_id)
        if session is None or handle.handle_id not in session.title_handles:
            raise RunnerError("unknown-handle", "unknown or expired title handle")
        secrets = get_secrets([session.sd_root])
        exefs_mp = None
        for child in session.children:
            if child.handle_id == handle.handle_id and child.kind == "exefs":
                exefs_mp = child.mount_point
                break
        if exefs_mp is None:
            session.title_handles.pop(handle.handle_id, None)
            raise RunnerError("mounts-gone", sanitize("exefs mount is gone", secrets))

        def _burned(error: RunnerError) -> RunnerError:
            session.title_handles.pop(handle.handle_id, None)
            return error

        tmp_path = None
        try:
            _cancel_point()
            dest = resolve_dest(_workspace(), dest_rel, secrets)
            _cancel_point()
            src = os.path.join(exefs_mp, "code-decompressed.bin")
            if not os.path.isfile(src):
                raise RunnerError("code-missing",
                                  sanitize("code-decompressed.bin is absent", secrets))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp_path = os.path.join(
                os.path.dirname(str(dest)),
                os.path.basename(str(dest)) + f".tmp-{uuid.uuid4().hex[:8]}")
            _cancel_point()  # pre-replace boundary: cancel keeps the old file
            shutil.copyfile(src, tmp_path)
            digest = hashlib.sha256()
            with open(tmp_path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
            os.replace(tmp_path, dest)
            tmp_path = None
        except CancelRequested as e:
            raise _burned(RunnerError("cancelled", sanitize(f"cancelled: {e}", secrets)))
        except RunnerError as e:
            raise _burned(RunnerError(e.code, sanitize(e.message_sanitized, secrets),
                                      e.incomplete))
        except Exception as e:  # noqa: BLE001 - convert to sanitized contract
            raise _burned(RunnerError("extract-failed", sanitize(f"{type(e).__name__}", secrets)))
        except BaseException:
            session.title_handles.pop(handle.handle_id, None)
            raise
        finally:
            if tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        size = os.path.getsize(str(dest))
        return ExtractResult(dest_path=str(dest), size=size,
                             sha256=digest.hexdigest(), code_entry="code-decompressed.bin")


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
