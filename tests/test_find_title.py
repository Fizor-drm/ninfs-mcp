"""Task 3 tests: find_title resolution (appended to Task 2 file content).

Staging mounts are faked at the listdir/exists/getsize layer (kind-keyed),
while SD content (TMD/.app) uses real small fixture files.
"""
import os
import struct

import pytest

from ninfs_mcp import runner
from ninfs_mcp.runner import (
    CancelRequested,
    InternalMount,
    MountSession,
    RunnerError,
)
from test_runner import FakePopen

STAGE = {"sdtitle": set(), "ncch": set(), "exefs": set()}
FAKE_SIZES = {}
_KINDS = ("sdtitle", "ncch", "exefs")


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    runner._SESSIONS.clear()
    runner._RESIDUALS.clear()
    FakePopen.instances.clear()
    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(runner, "READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(runner, "_GONE_GRACE_S", 0)
    monkeypatch.setenv("NINFS_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("NINFS_BOOT9_PATH", "C:\\k\\boot9.bin")
    monkeypatch.setenv("NINFS_MOVABLE_PATH", "C:\\k\\movable.sed")
    monkeypatch.delenv("NINFS_SD_ROOT", raising=False)

STAGE = {"sdtitle": set(), "ncch": set(), "exefs": set()}
FAKE_SIZES = {}
_KINDS = ("sdtitle", "ncch", "exefs")


def _kind_of(path):
    parts = str(path).replace("\\", "/").split("/")
    for kind in _KINDS:
        if kind in parts:
            return kind
    return None


@pytest.fixture(autouse=True)
def fake_mount_fs(monkeypatch):
    real_listdir = os.listdir
    real_exists = os.path.exists
    real_getsize = os.path.getsize
    STAGE["sdtitle"] = {"tmd.bin", "0000.aaa.ncch", "0001.bbb.ncch"}
    STAGE["ncch"] = {"exefs.bin"}
    STAGE["exefs"] = {"code-decompressed.bin", "code.bin"}
    FAKE_SIZES.clear()
    FAKE_SIZES.update({"0000.aaa.ncch": 100, "0001.bbb.ncch": 50})

    def _live(path):
        spath = str(path)
        for session in runner._SESSIONS.values():
            for m in session.children:
                if spath == m.mount_point or spath.startswith(m.mount_point + os.sep):
                    return m.proc.poll() is None
        for m in runner._RESIDUALS:
            if spath == m.mount_point or spath.startswith(m.mount_point + os.sep):
                return m.proc.poll() is None
        return False

    def fake_listdir(path):
        kind = _kind_of(path)
        if kind is not None and _live(path):
            return sorted(STAGE[kind])
        return real_listdir(path)

    def fake_exists(path):
        kind = _kind_of(path)
        if kind is not None:
            if _live(path) and os.path.basename(path) in STAGE[kind]:
                return True
            return real_exists(path)
        return real_exists(path)

    def fake_getsize(path):
        base = os.path.basename(path)
        if base in FAKE_SIZES:
            return FAKE_SIZES[base]
        return real_getsize(path)

    real_isfile = os.path.isfile

    def fake_isfile(path):
        kind = _kind_of(path)
        if kind is not None and _live(path):
            return os.path.basename(path) in STAGE[kind]
        return real_isfile(path)

    monkeypatch.setattr(os, "listdir", fake_listdir)
    monkeypatch.setattr(os.path, "exists", fake_exists)
    monkeypatch.setattr(os.path, "isfile", fake_isfile)
    monkeypatch.setattr(os.path, "getsize", fake_getsize)
    monkeypatch.setattr(runner, "READY_TIMEOUT_S", 0.05)


def _tmd_bytes(version, apps):
    """Minimal structurally-parseable TMD. apps: [(content_id_hex8, size)]."""
    sig = struct.pack(">I", 0x00010004) + bytes(0x100) + bytes(0x3C)
    header = bytearray(0xC4)
    header[0x9C:0x9E] = struct.pack(">H", version)
    header[0x9E:0xA0] = struct.pack(">H", len(apps))
    info = bytes(0x900)
    chunks = b"".join(
        bytes.fromhex(cid)
        + struct.pack(">H", idx)
        + struct.pack(">H", 0)
        + struct.pack(">Q", size)
        + bytes(32)
        for idx, (cid, size) in enumerate(apps)
    )
    return sig + bytes(header) + info + chunks


def _title_content(root, id1, high, low, tmds):
    """tmds: [(filename, version, [(cid, size, create_file)])]. Returns content dir."""
    content = os.path.join(root, id1, "title", high, low, "content")
    os.makedirs(content, exist_ok=True)
    for name, version, apps in tmds:
        with open(os.path.join(content, name), "wb") as f:
            f.write(_tmd_bytes(version, [(cid, size) for cid, size, _ in apps]))
        for cid, size, create in apps:
            if create:
                with open(os.path.join(content, f"{cid}.app"), "wb") as f:
                    f.write(b"\0" * size)
    return content


def _session(tmp_path, sdroot):
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    proc = FakePopen(["sd"])
    sd = InternalMount(session_id="S", kind="sd", mount_point=str(sdroot),
                       proc=proc, handle_id=None, secrets=[])
    session = MountSession(mount_id="S", staging=str(staging),
                           children=[sd], sd_root=str(sdroot))
    runner._SESSIONS["S"] = session
    FakePopen.instances.clear()  # the sd double is not a spawn under test
    return session


BASE = "000400000016c700"
UPDATE = "0004000e0016c700"


def test_find_title_rejects_bad_id_and_category(tmp_path):
    session = _session(tmp_path, str(tmp_path))
    for bad in ["xyz", "1234", "000400010016c700", ""]:
        with pytest.raises(RunnerError):
            runner.find_title(session, bad)
    assert FakePopen.instances == []  # no spawn before validation


def test_find_title_normalizes_uppercase(tmp_path):
    sdroot = tmp_path / "sd"
    _title_content(str(sdroot), "id1", "00040000", "0016c700",
                   [("a.tmd", 5, [("abcdef01", 10, True)])])
    session = _session(tmp_path, str(sdroot))
    handle = runner.find_title(session, BASE.upper())
    assert handle.requested_title_id == BASE
    assert handle.resolved_title_id == BASE
    assert handle.kind == "base"


def test_find_title_prefers_complete_update(tmp_path):
    sdroot = tmp_path / "sd"
    _title_content(str(sdroot), "id1", "00040000", "0016c700",
                   [("b.tmd", 3, [("abcdef01", 10, True)])])
    _title_content(str(sdroot), "id1", "0004000e", "0016c700",
                   [("u.tmd", 7, [("abcdef02", 10, True)])])
    session = _session(tmp_path, str(sdroot))
    handle = runner.find_title(session, BASE)
    assert handle.resolved_title_id == UPDATE
    assert handle.kind == "update"
    assert handle.tmd_found is True


def test_find_title_ignores_broken_update(tmp_path):
    sdroot = tmp_path / "sd"
    _title_content(str(sdroot), "id1", "00040000", "0016c700",
                   [("b.tmd", 3, [("abcdef01", 10, True)])])
    broken = _title_content(str(sdroot), "id1", "0004000e", "0016c700", [])
    with open(os.path.join(broken, "u.tmd"), "wb") as f:
        f.write(b"truncated")
    session = _session(tmp_path, str(sdroot))
    handle = runner.find_title(session, BASE)
    assert handle.resolved_title_id == BASE


def test_find_title_version_sig_aware(tmp_path):
    sdroot = tmp_path / "sd"
    _title_content(str(sdroot), "id1", "00040000", "0016c700",
                   [("zzz.tmd", 9, [("abcdef01", 10, True)]),
                    ("aaa.tmd", 4, [("abcdef01", 10, True)])])
    session = _session(tmp_path, str(sdroot))
    handle = runner.find_title(session, BASE)
    assert handle.tmd_path.endswith("zzz.tmd")  # version wins over name


def test_find_title_passes_explicit_tmd_file(tmp_path):
    sdroot = tmp_path / "sd"
    _title_content(str(sdroot), "id1", "00040000", "0016c700",
                   [("a.tmd", 5, [("abcdef01", 10, True)]),
                    ("b.tmd", 6, [("abcdef01", 10, True)])])
    session = _session(tmp_path, str(sdroot))
    handle = runner.find_title(session, BASE)
    sdtitle_argv = [p.argv for p in FakePopen.instances if "sdtitle" in p.argv]
    assert len(sdtitle_argv) == 1
    assert handle.tmd_path in sdtitle_argv[0]
    assert handle.tmd_path.endswith(".tmd")


def test_find_title_ncch_0000_first_and_fallback(tmp_path):
    sdroot = tmp_path / "sd"
    _title_content(str(sdroot), "id1", "00040000", "0016c700",
                   [("a.tmd", 5, [("abcdef01", 10, True)])])
    session = _session(tmp_path, str(sdroot))
    runner.find_title(session, BASE)
    ncch_argv = [p.argv for p in FakePopen.instances if "ncch" in p.argv and "sdtitle" not in p.argv]
    assert any("0000.aaa.ncch" in a for argv in ncch_argv for a in argv)
    # without 0000: largest wins
    STAGE["sdtitle"] = {"tmd.bin", "0002.ccc.ncch", "0003.ddd.ncch"}
    FAKE_SIZES.update({"0002.ccc.ncch": 10, "0003.ddd.ncch": 90})
    session2 = _session(tmp_path, str(sdroot))
    session2.mount_id = "S2"
    session2.staging = str(tmp_path / "staging2")
    os.makedirs(session2.staging, exist_ok=True)
    runner._SESSIONS["S2"] = session2
    n_before = len([p for p in FakePopen.instances if "ncch" in p.argv and "sdtitle" not in p.argv])
    runner.find_title(session2, BASE)
    ncch_argv2 = [p.argv for p in FakePopen.instances if "ncch" in p.argv and "sdtitle" not in p.argv]
    assert len(ncch_argv2) == n_before + 1
    assert any("0003.ddd.ncch" in a for a in ncch_argv2[-1])


def test_find_title_absent_starts_no_proc(tmp_path):
    sdroot = tmp_path / "sd"
    os.makedirs(os.path.join(str(sdroot), "id1", "title"), exist_ok=True)
    session = _session(tmp_path, str(sdroot))
    with pytest.raises(RunnerError):
        runner.find_title(session, BASE)
    assert FakePopen.instances == []


def test_find_title_mid_failure_discards_without_handle(tmp_path):
    sdroot = tmp_path / "sd"
    _title_content(str(sdroot), "id1", "00040000", "0016c700",
                   [("a.tmd", 5, [("abcdef01", 10, True)])])
    session = _session(tmp_path, str(sdroot))
    STAGE["ncch"] = set()  # ncch readiness fails
    with pytest.raises(RunnerError):
        runner.find_title(session, BASE)
    assert session.title_handles == {}
    assert [m.kind for m in session.children] == ["sd"]  # partial mounts removed
    assert "S" in runner._SESSIONS  # session reusable
    STAGE["ncch"] = {"exefs.bin"}
    handle = runner.find_title(session, BASE)  # retry works
    assert handle.handle_id in session.title_handles


def test_find_title_explicit_update_states(tmp_path):
    sdroot = tmp_path / "sd"
    _title_content(str(sdroot), "id1", "00040000", "0016c700",
                   [("b.tmd", 3, [("abcdef01", 10, True)])])
    session = _session(tmp_path, str(sdroot))
    with pytest.raises(RunnerError):  # update absent entirely
        runner.find_title(session, UPDATE)
    broken = _title_content(str(sdroot), "id1", "0004000e", "0016c700", [])
    with open(os.path.join(broken, "u.tmd"), "wb") as f:
        f.write(b"truncated")
    with pytest.raises(RunnerError):  # update present but broken: no base fallback
        runner.find_title(session, UPDATE)
    assert FakePopen.instances == []


def _sd_session_with_handle(tmp_path, monkeypatch):
    """Full find_title success; returns (session, handle)."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    monkeypatch.setenv("NINFS_WORKSPACE", str(ws))
    sdroot = tmp_path / "sd"
    _title_content(str(sdroot), "id1", "00040000", "0016c700",
                   [("a.tmd", 5, [("abcdef01", 10, True)])])
    session = _session(tmp_path, str(sdroot))
    return session, runner.find_title(session, BASE)


def _serve_code(session, handle, data=b"decompressed-code-bytes"):
    """Materialize the served code-decompressed.bin the fake mount advertises."""
    for m in session.children:
        if m.handle_id == handle.handle_id and m.kind == "exefs":
            os.makedirs(m.mount_point, exist_ok=True)  # WinFsp creates this live
            path = os.path.join(m.mount_point, "code-decompressed.bin")
            with open(path, "wb") as f:
                f.write(data)
            return path
    raise AssertionError("no exefs mount for handle")


def _teardown_served(session):
    """Emulate WinFsp teardown: served mount leaves vanish entirely."""
    import shutil

    for m in session.children:
        if m.kind != "sd" and os.path.lexists(m.mount_point):
            shutil.rmtree(m.mount_point, ignore_errors=True)


def test_extract_code_requires_decompressed_entry(tmp_path, monkeypatch):
    session, handle = _sd_session_with_handle(tmp_path, monkeypatch)
    STAGE["exefs"] = {"code.bin"}  # served, but no success evidence
    with pytest.raises(RunnerError) as exc:
        runner.extract_code(handle, "akaneko/code.bin")
    assert exc.value.code == "code-missing"
    with pytest.raises(RunnerError):  # handle burned
        runner.extract_code(handle, "akaneko/code.bin")


def test_extract_code_atomic_overwrite_and_tmp_cleanup(tmp_path, monkeypatch):
    session, handle = _sd_session_with_handle(tmp_path, monkeypatch)
    _serve_code(session, handle)
    ws = tmp_path / "ws"
    dest = ws / "akaneko" / "code.bin"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"old")
    out = runner.extract_code(handle, "akaneko/code.bin")
    assert out.dest_path == str(dest)
    assert dest.read_bytes() != b"old"
    leftovers = [p for p in dest.parent.iterdir() if ".tmp-" in p.name]
    assert leftovers == []
    # copy failure: tmp removed, old file kept, handle burned
    session2, handle2 = _sd_session_with_handle(tmp_path, monkeypatch)
    _serve_code(session2, handle2)
    import shutil

    real_copy = shutil.copyfile
    monkeypatch.setattr(shutil, "copyfile", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(RunnerError):
        runner.extract_code(handle2, "akaneko/code.bin")
    assert dest.read_bytes() != b"old"  # first copy still intact
    assert [p for p in dest.parent.iterdir() if ".tmp-" in p.name] == []
    assert real_copy is not None
    with pytest.raises(RunnerError):
        runner.extract_code(handle2, "akaneko/code.bin")


def test_extract_code_cancel_before_copy(tmp_path, monkeypatch):
    session, handle = _sd_session_with_handle(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(runner, "CANCEL_CHECK", lambda: calls.append(1) or (_ for _ in ()).throw(CancelRequested()))
    with pytest.raises(RunnerError) as exc:
        runner.extract_code(handle, "akaneko/code.bin")
    assert exc.value.code == "cancelled"
    assert list((tmp_path / "ws").rglob("*.tmp-*")) == []
    with pytest.raises(RunnerError):  # handle burned
        runner.extract_code(handle, "akaneko/code.bin")


def test_extract_code_cancel_before_replace_keeps_old(tmp_path, monkeypatch):
    session, handle = _sd_session_with_handle(tmp_path, monkeypatch)
    _serve_code(session, handle)
    ws = tmp_path / "ws"
    dest = ws / "keep.bin"
    dest.write_bytes(b"old")
    calls = []

    def tripwire():
        calls.append(1)
        if len(calls) == 3:  # resolve, pre-copy, pre-replace
            raise CancelRequested()

    monkeypatch.setattr(runner, "CANCEL_CHECK", tripwire)
    with pytest.raises(RunnerError) as exc:
        runner.extract_code(handle, "keep.bin")
    assert exc.value.code == "cancelled"
    assert dest.read_bytes() == b"old"
    assert list(ws.rglob("*.tmp-*")) == []
    assert calls == [1, 1, 1]  # copy ran, replace did not


def test_extract_code_happy_path_reuse_and_invalidation(tmp_path, monkeypatch):
    import hashlib

    session, handle = _sd_session_with_handle(tmp_path, monkeypatch)
    _serve_code(session, handle)
    out = runner.extract_code(handle, "akaneko/code.bin")
    assert out.code_entry == "code-decompressed.bin"
    data = (tmp_path / "ws" / "akaneko" / "code.bin").read_bytes()
    assert out.size == len(data)
    assert out.sha256 == hashlib.sha256(data).hexdigest()
    out2 = runner.extract_code(handle, "akaneko/code2.bin")  # reusable
    assert out2.sha256 == out.sha256
    import shutil

    shutil.rmtree(tmp_path / "sd")  # WinFsp deletes the served tree itself
    _teardown_served(session)
    assert runner.unmount("S").ok is True
    with pytest.raises(RunnerError):
        runner.extract_code(handle, "akaneko/code3.bin")


def test_extract_code_failure_isolation_across_handles(tmp_path, monkeypatch):
    sdroot = tmp_path / "sd"
    _title_content(str(sdroot), "id1", "00040000", "0016c700",
                   [("a.tmd", 5, [("abcdef01", 10, True)])])
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    monkeypatch.setenv("NINFS_WORKSPACE", str(ws))
    session = _session(tmp_path, str(sdroot))
    h1 = runner.find_title(session, BASE)
    h2 = runner.find_title(session, BASE)
    _serve_code(session, h1)
    for m in session.children:  # kill h2's exefs mount only
        if m.handle_id == h2.handle_id and m.kind == "exefs":
            m.proc.terminate()
    with pytest.raises(RunnerError):
        runner.extract_code(h2, "b.bin")
    out = runner.extract_code(h1, "a.bin")  # h1 unaffected
    assert out.code_entry == "code-decompressed.bin"


def test_find_title_spawn_oserror_cleans_up(tmp_path, monkeypatch):
    sdroot = tmp_path / "sd"
    _title_content(str(sdroot), "id1", "00040000", "0016c700",
                   [("a.tmd", 5, [("abcdef01", 10, True)])])
    session = _session(tmp_path, str(sdroot))
    real_spawn = runner.spawn_mount
    calls = []

    def flaky(argv):
        calls.append(argv[3])
        if argv[3] == "ncch":
            raise OSError("exec missing")
        return real_spawn(argv)

    monkeypatch.setattr(runner, "spawn_mount", flaky)
    with pytest.raises(RunnerError):
        runner.find_title(session, BASE)
    assert session.title_handles == {}
    assert [m.kind for m in session.children] == ["sd"]  # sdtitle cleaned
    sdtitle_procs = [p for p in FakePopen.instances if "sdtitle" in p.argv]
    assert all(p.poll() is not None for p in sdtitle_procs)


def test_mount_stop_failure_tracked_residual(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    n3ds = tmp_path / "Nintendo 3DS"
    n3ds.mkdir()
    monkeypatch.setenv("NINFS_WORKSPACE", str(ws))
    monkeypatch.setattr(runner, "_drives", lambda: [str(tmp_path)])
    monkeypatch.setattr(runner, "READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(os, "listdir", lambda p: (_ for _ in ()).throw(OSError("nope")))
    monkeypatch.setattr(runner, "stop_proc", lambda m: (_ for _ in ()).throw(RuntimeError("stuck")))
    with pytest.raises(RunnerError) as exc:
        runner.mount_sd()
    assert exc.value.incomplete is True
    assert len(runner._RESIDUALS) == 1
    assert runner._RESIDUALS[0].proc.poll() is None  # still alive, now tracked


def test_find_title_multi_handle_coexistence(tmp_path):
    sdroot = tmp_path / "sd"
    _title_content(str(sdroot), "id1", "00040000", "0016c700",
                   [("a.tmd", 5, [("abcdef01", 10, True)])])
    session = _session(tmp_path, str(sdroot))
    h1 = runner.find_title(session, BASE)
    h2 = runner.find_title(session, BASE)
    assert h1.handle_id != h2.handle_id
    dirs = {os.path.dirname(m.mount_point) for m in session.children
            if m.handle_id}
    assert len(dirs) == 2  # distinct per-handle staging
    # emulate FUSE unmount: served content vanishes, staging returns empty.
    # WinFsp deletes the leaf mount dir itself, so remove the whole fixture.
    import shutil

    _teardown_served(session)
    shutil.rmtree(str(sdroot))
    summary = runner.unmount("S")
    assert summary.ok is True
    assert all(p.poll() is not None for p in FakePopen.instances)
