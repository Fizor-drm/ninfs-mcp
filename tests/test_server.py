"""Task 5 tests: server wiring (runner mocked except where noted)."""
import anyio
import pytest

from ninfs_mcp import runner, server
from ninfs_mcp.runner import (
    ExtractResult,
    InternalMount,
    MountSession,
    RunnerError,
    TitleHandle,
    UnmountSummary,
)

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("NINFS_WORKSPACE", str(ws))
    monkeypatch.setenv("NINFS_BOOT9_PATH", "C:\\k\\boot9.bin")
    monkeypatch.setenv("NINFS_MOVABLE_PATH", "C:\\k\\movable.sed")
    monkeypatch.delenv("NINFS_SD_ROOT", raising=False)
    runner._SESSIONS.clear()
    runner._RESIDUALS.clear()


def _session():
    return MountSession(mount_id="M", staging="C:\\ws\\.mounts\\M",
                        children=[], sd_root="D:\\Nintendo 3DS")


async def test_exposes_exactly_five_tools():
    names = {t.name async for t in _aiter(server.mcp.list_tools())}
    assert names == {"detect_sd", "mount_sd", "find_title", "extract_code", "unmount"}


async def _aiter(coro):
    for t in await coro:
        yield t


async def test_public_key_sets(monkeypatch):
    session = _session()
    session.title_handles["H"] = runner.TitleHandle(
        "H", "M", "000400000016c700", "000400000016c700", "base", True, "C:\\t.tmd")
    runner._SESSIONS["M"] = session
    monkeypatch.setattr(runner, "detect_sd",
                        lambda: {"sd_root": "D:\\N3DS", "has_n3ds_dir": True,
                                 "has_boot9": True, "has_movable": True})
    monkeypatch.setattr(runner, "mount_sd", lambda: _session())
    monkeypatch.setattr(runner, "find_title",
                        lambda s, t: TitleHandle("H", "M", t, t, "base", True, "C:\\t.tmd"))
    monkeypatch.setattr(runner, "extract_code",
                        lambda h, d: ExtractResult("C:\\ws\\c.bin", 6, "ab" * 32, "code-decompressed.bin"))
    monkeypatch.setattr(runner, "unmount",
                        lambda m: UnmountSummary(True, False, [], []))
    assert set((await server.detect_sd()).keys()) == {
        "sd_root", "has_n3ds_dir", "has_boot9", "has_movable"}
    assert set((await server.mount_sd()).keys()) == {"mount_id", "mount_point"}
    assert set((await server.find_title("M", "000400000016c700")).keys()) == {
        "requested_title_id", "resolved_title_id", "kind", "tmd_found", "title_handle"}
    assert set((await server.extract_code("H", "c.bin")).keys()) == {
        "dest_path", "size", "sha256", "code_entry"}
    assert set((await server.unmount("M")).keys()) == {"ok", "incomplete", "errors_sanitized"}


async def test_maps_internal_without_leak(monkeypatch):
    session = _session()
    session.children.append(InternalMount("M", "sd", "D:\\mp", object(), None, ["S3CR3T"]))
    monkeypatch.setattr(runner, "mount_sd", lambda: session)
    out = await server.mount_sd()
    blob = str(out)
    assert "S3CR3T" not in blob
    assert "proc" not in blob
    assert "tmd_path" not in blob


async def test_rejects_absolute_dest_before_runner(monkeypatch):
    def _boom(handle, dest):
        raise AssertionError("runner must not be reached")

    monkeypatch.setattr(runner, "extract_code", _boom)
    out = await server.extract_code("H", "C:\\Windows\\x.bin")
    assert "error" in out


async def test_error_mapping_is_sanitized(monkeypatch):
    def _leak(session, title_id):
        raise RunnerError("title-not-found", "missing C:\\k\\boot9.bin")

    monkeypatch.setattr(runner, "find_title", _leak)
    runner._SESSIONS["M"] = _session()
    out = await server.find_title("M", "000400000016c700")
    assert out["error"] == "title-not-found"
    assert "C:\\k\\boot9.bin" not in out["message_sanitized"]


async def test_workspace_init_and_finally_cleanup(monkeypatch, tmp_path):
    target = tmp_path / "new-ws"
    monkeypatch.setenv("NINFS_WORKSPACE", str(target))
    monkeypatch.setattr(runner, "check_winfs", lambda: None)
    monkeypatch.setattr(runner, "retry_residuals",
                        lambda: runner.RetryReport(0, 0, []))
    out = server.startup()
    assert target.is_dir()
    assert out["workspace"] == str(target)
    monkeypatch.setenv("NINFS_WORKSPACE", "relative/path")
    with pytest.raises(RunnerError):
        server.startup()
    cleaned = []
    monkeypatch.setattr(runner, "retry_residuals",
                        lambda: (cleaned.append(1), runner.RetryReport(0, 0, []))[1])
    server.cleanup_all()
    assert cleaned == [1]


async def test_cancel_propagates_without_result(monkeypatch):
    ran = []

    def _slow():
        ran.append(1)
        return {"sd_root": "x", "has_n3ds_dir": True, "has_boot9": True, "has_movable": True}

    monkeypatch.setattr(runner, "detect_sd", _slow)
    with anyio.CancelScope() as scope:
        scope.cancel()
        await server.detect_sd()
        assert False, "cancel must surface, not a result"
    assert scope.cancel_called
    assert ran == []


async def test_deleted_id_retry_failure_masked(monkeypatch):
    def _gone(mount_id):
        raise RunnerError("unknown-id", "gone C:\\k\\boot9.bin")

    monkeypatch.setattr(runner, "unmount", _gone)
    out = await server.unmount("gone")
    assert out["error"] == "unknown-id"
    assert "C:\\k\\boot9.bin" not in out["message_sanitized"]


async def test_worker_resolves_ids(monkeypatch):
    session = _session()
    runner._SESSIONS["M"] = session
    seen = []
    monkeypatch.setattr(runner, "find_title", lambda s, t: seen.append(s) or TitleHandle(
        "H", "M", t, t, "base", True, "C:\\t.tmd"))
    out = await server.find_title("M", "000400000016c700")
    assert seen == [session]
    assert out["title_handle"] == "H"
