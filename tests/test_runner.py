"""Task 2 tests: runner core (resident procs, detect/mount/unmount, residuals).

All subprocess and filesystem-mount interactions are mocked. No live mounts.
"""
import io
import os
import subprocess
import sys
import unittest.mock as mock

import pytest

from ninfs_mcp import runner
from ninfs_mcp.runner import (
    InternalMount,
    MountSession,
    RunnerError,
)

_REAL_POPEN = subprocess.Popen


def _mklink(link, target):
    with mock.patch.object(subprocess, "Popen", _REAL_POPEN):
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")


class FakePopen:
    """Minimal Popen double. alive until terminate()/kill(), then exit 0."""

    instances = []

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self._alive = True
        self.terminated = False
        self.killed = False
        self.stderr = io.BytesIO(kwargs.pop("stderr_bytes", b""))
        FakePopen.instances.append(self)

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        if self._alive:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return 0


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    runner._SESSIONS.clear()
    runner._RESIDUALS.clear()
    FakePopen.instances.clear()
    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)
    monkeypatch.setenv("NINFS_WORKSPACE", "C:\\ws")
    monkeypatch.setenv("NINFS_BOOT9_PATH", "C:\\k\\boot9.bin")
    monkeypatch.setenv("NINFS_MOVABLE_PATH", "C:\\k\\movable.sed")
    monkeypatch.delenv("NINFS_SD_ROOT", raising=False)


def _mount(kind="sd", alive=True, session_id="s1", handle_id=None, tmp=None):
    proc = FakePopen(["x"])
    if not alive:
        proc._alive = False
    mp = os.path.join(str(tmp) if tmp else "C:\\m", kind) if tmp else f"C:\\m\\{kind}"
    return InternalMount(
        session_id=session_id,
        kind=kind,
        mount_point=mp,
        proc=proc,
        handle_id=handle_id,
        secrets=["C:\\k\\boot9.bin"],
    )


# --- check_winfs -----------------------------------------------------------


def test_check_winfs_uses_isolated_subprocess(monkeypatch):
    calls = []
    orig_popen = FakePopen

    class RecordingPopen(orig_popen):
        def __init__(self, argv, **kwargs):
            calls.append(argv)
            super().__init__(argv, **kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", RecordingPopen)
    # --help succeeding means WinFsp/FUSE import worked
    monkeypatch.setattr(RecordingPopen, "wait", lambda self, timeout=None: 0)
    monkeypatch.setattr(RecordingPopen, "poll", lambda self: 0)
    runner.check_winfs()
    assert calls
    argv = calls[0]
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "ninfs"]
    assert "import ninfs" not in open(runner.__file__).read()


# --- detect_sd --------------------------------------------------------------


def test_detect_sd_reports_all_four_keys(monkeypatch, tmp_path):
    n3ds = tmp_path / "Nintendo 3DS"
    n3ds.mkdir()
    monkeypatch.setattr(runner, "_drives", lambda: [str(tmp_path)])
    out = runner.detect_sd()
    assert set(out.keys()) == {"sd_root", "has_n3ds_dir", "has_boot9", "has_movable"}
    assert out["sd_root"] == str(n3ds)
    assert out["has_n3ds_dir"] is True


def test_detect_sd_zero_candidates_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_drives", lambda: [str(tmp_path)])
    out = runner.detect_sd()
    assert out["error"] == "not-found"
    assert out["incomplete"] is False


def test_detect_sd_multiple_candidates_ambiguous(monkeypatch, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "Nintendo 3DS").mkdir(parents=True)
    (b / "Nintendo 3DS").mkdir(parents=True)
    monkeypatch.setattr(runner, "_drives", lambda: [str(a), str(b)])
    out = runner.detect_sd()
    assert out["error"] == "ambiguous"
    assert len(out["candidates"]) == 2


def test_detect_sd_env_override_validates_single_dir(monkeypatch, tmp_path):
    n3ds = tmp_path / "Nintendo 3DS"
    n3ds.mkdir()
    out = runner.detect_sd(sd_root_override=str(n3ds))
    assert out["sd_root"] == str(n3ds)
    missing = runner.detect_sd(sd_root_override=str(tmp_path / "missing"))
    assert missing["error"] == "not-found"


# --- spawn contract ----------------------------------------------------------


def test_spawn_always_foreground_and_devnull(monkeypatch, tmp_path):
    (tmp_path / "id1").mkdir()
    monkeypatch.setattr(os, "listdir", lambda p: ["id1"])
    proc = runner.spawn_mount(
        [sys.executable, "-m", "ninfs", "sd", "--ro", "a", "b", "-f"],
    )
    assert isinstance(proc, FakePopen)
    assert "-f" in proc.argv
    assert proc.kwargs["stdout"] == subprocess.DEVNULL
    assert proc.kwargs["stdin"] == subprocess.DEVNULL
    assert proc.kwargs["stderr"] == subprocess.PIPE


def test_spawn_refuses_without_foreground():
    with pytest.raises(ValueError):
        runner.spawn_mount([sys.executable, "-m", "ninfs", "sd", "a", "b"])


def test_mount_sd_returns_while_proc_alive(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    n3ds = tmp_path / "Nintendo 3DS"
    n3ds.mkdir()
    monkeypatch.setenv("NINFS_WORKSPACE", str(ws))
    monkeypatch.setattr(runner, "_drives", lambda: [str(tmp_path)])
    monkeypatch.setattr(os, "listdir", lambda p: ["id1abc"])
    session = runner.mount_sd()
    assert isinstance(session, MountSession)
    assert session.children[0].proc.poll() is None  # still resident
    assert "--ro" in session.children[0].proc.argv


def test_mount_failure_drains_sanitized_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "READY_TIMEOUT_S", 0.05)
    ws = tmp_path / "ws"
    ws.mkdir()
    n3ds = tmp_path / "Nintendo 3DS"
    n3ds.mkdir()
    monkeypatch.setenv("NINFS_WORKSPACE", str(ws))
    monkeypatch.setattr(runner, "_drives", lambda: [str(tmp_path)])
    monkeypatch.setattr(os, "listdir", lambda p: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(RunnerError) as exc:
        runner.mount_sd()
    assert "C:\\k\\boot9.bin" not in exc.value.message_sanitized


def test_order_check_before_creation(monkeypatch, tmp_path):
    """Overlap failure performs zero mkdir and zero spawn."""
    ws = tmp_path / "SD" / "Nintendo 3DS" / "ws"
    ws.mkdir(parents=True)
    monkeypatch.setenv("NINFS_WORKSPACE", str(ws))
    monkeypatch.setattr(runner, "_drives", lambda: [str(tmp_path)])
    # SD selected inside workspace's parent: overlap must trigger first
    monkeypatch.setenv("NINFS_SD_ROOT", str(tmp_path / "SD" / "Nintendo 3DS"))
    (tmp_path / "SD" / "Nintendo 3DS").mkdir(parents=True, exist_ok=True)
    created = []
    real_mkdir = os.mkdir
    monkeypatch.setattr(os, "mkdir", lambda p, *a, **k: (created.append(p), real_mkdir(p, *a, **k)))
    with pytest.raises(RunnerError):
        runner.mount_sd()
    assert FakePopen.instances == []
    assert not [p for p in created if ".mounts" in str(p)]


def test_staging_ancestor_junction_refused(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _mklink(ws / ".mounts", outside)
    n3ds = tmp_path / "Nintendo 3DS"
    n3ds.mkdir()
    monkeypatch.setenv("NINFS_WORKSPACE", str(ws))
    monkeypatch.setattr(runner, "_drives", lambda: [str(tmp_path)])
    with pytest.raises(RunnerError) as exc:
        runner.mount_sd()
    assert "staging" in exc.value.code or "overlap" in exc.value.code
    assert FakePopen.instances == []


# --- readiness ----------------------------------------------------------------


def test_readiness_rejects_empty_dir_and_dead_proc(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "listdir", lambda p: [])
    proc = FakePopen(["x"])
    with pytest.raises(RunnerError):
        runner.wait_ready(proc, str(tmp_path), lambda: len(os.listdir(str(tmp_path))) > 0, timeout_s=0.05)
    dead = FakePopen(["x"])
    dead.terminate()
    monkeypatch.setattr(os, "listdir", lambda p: ["id1"])
    with pytest.raises(RunnerError):
        runner.wait_ready(dead, str(tmp_path), lambda: True, timeout_s=0.05)


# --- stop / cleanup ------------------------------------------------------------


def test_stop_idempotent_and_single_terminate(tmp_path):
    m = _mount(alive=False, tmp=tmp_path)
    os.makedirs(m.mount_point, exist_ok=True)
    assert runner.stop_proc(m) is True
    assert m.proc.terminated is False  # nothing to do on exited proc
    m2 = _mount(alive=True, tmp=tmp_path)
    os.makedirs(m2.mount_point, exist_ok=True)
    assert runner.stop_proc(m2) is True
    assert m2.proc.terminated is True
    assert m2.proc.killed is False  # no escalation on Windows


def test_stop_nonempty_staging_is_residual(tmp_path):
    m = _mount(alive=False, tmp=tmp_path)
    os.makedirs(m.mount_point, exist_ok=True)
    with open(os.path.join(m.mount_point, "leftover.bin"), "wb") as f:
        f.write(b"x")
    assert runner.stop_proc(m) is False


def test_cleanup_core_reverses_and_continues(tmp_path):
    order = []
    a = _mount(kind="sdtitle", tmp=tmp_path)
    b = _mount(kind="ncch", tmp=tmp_path)
    for m in (a, b):
        os.makedirs(m.mount_point, exist_ok=True)
    orig_stop = runner.stop_proc
    monkeypatch_fail = False

    def flaky(m):
        order.append(m.kind)
        if m.kind == "ncch" and not monkeypatch_fail:
            return False
        return orig_stop(m)

    import unittest.mock as mock

    with mock.patch.object(runner, "stop_proc", side_effect=flaky):
        residuals = runner.cleanup_core([a, b])
    assert order[0] == "ncch"  # reversed
    assert [r.kind for r in residuals] == ["ncch"]


def test_unmount_order_ends_with_sd_and_reports(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    n3ds = tmp_path / "Nintendo 3DS"
    n3ds.mkdir()
    monkeypatch.setenv("NINFS_WORKSPACE", str(ws))
    monkeypatch.setattr(runner, "_drives", lambda: [str(tmp_path)])
    monkeypatch.setattr(os, "listdir", lambda p: ["id1"])
    session = runner.mount_sd()
    stopped = []
    orig_stop = runner.stop_proc
    import unittest.mock as mock

    with mock.patch.object(runner, "stop_proc", side_effect=lambda m: (stopped.append(m.kind), True)[1]):
        summary = runner.unmount(session.mount_id)
    assert stopped[-1] == "sd"
    assert summary.ok is True
    assert summary.incomplete is False
    assert session.mount_id not in runner._SESSIONS


def test_unmount_deleted_id_routes_to_residuals(tmp_path):
    m = _mount(session_id="gone", tmp=tmp_path)
    os.makedirs(m.mount_point, exist_ok=True)
    runner._RESIDUALS.append(m)
    summary = runner.unmount("gone")
    assert summary.ok is True
    assert runner._RESIDUALS == []
    with pytest.raises(RunnerError) as exc:
        runner.unmount("never-existed")
    assert exc.value.code == "unknown-id"


def test_retry_residuals_recovers(tmp_path):
    m = _mount(session_id="s9", tmp=tmp_path)
    os.makedirs(m.mount_point, exist_ok=True)
    runner._RESIDUALS.append(m)
    report = runner.retry_residuals()
    assert report.recovered == 1
    assert report.remaining == 0
    assert runner._RESIDUALS == []


def test_lock_serializes_ops():
    assert runner._LOCK is not None
    acquired = runner._LOCK.acquire(blocking=False)
    assert acquired is True
    runner._LOCK.release()
