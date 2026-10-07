"""Task 3 tests: find_title resolution (appended to Task 2 file content).

Staging mounts are faked at the listdir/exists/getsize layer (kind-keyed),
while SD content (TMD/.app) uses real small fixture files.
"""
import os
import struct

import pytest

from ninfs_mcp import runner
from ninfs_mcp.runner import InternalMount, MountSession, RunnerError
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

    monkeypatch.setattr(os, "listdir", fake_listdir)
    monkeypatch.setattr(os.path, "exists", fake_exists)
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
    # emulate FUSE unmount: served content vanishes, staging returns empty
    import shutil

    shutil.rmtree(os.path.join(str(sdroot), "id1"))
    summary = runner.unmount("S")
    assert summary.ok is True
    assert all(p.poll() is not None for p in FakePopen.instances)
