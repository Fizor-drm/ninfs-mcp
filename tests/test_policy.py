"""Task 1 tests: policy path checks, secrets registry, sanitizer, read-only guard."""
import os
import subprocess

import pytest

from ninfs_mcp.policy import (
    check_no_overlap,
    get_secrets,
    reject_write_args,
    resolve_dest,
    sanitize,
)


def _make_link(link, target):
    """Directory link without elevation: try symlink, fall back to junction."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")


def test_resolve_dest_allows_normal_mixed_separators(tmp_path):
    p = resolve_dest(tmp_path, "akaneko/sub\\code.bin")
    assert p == (tmp_path / "akaneko" / "sub" / "code.bin").resolve()


def test_resolve_dest_rejects_dotdot_even_if_inside(tmp_path):
    (tmp_path / "akaneko").mkdir()
    with pytest.raises(ValueError):
        resolve_dest(tmp_path, "akaneko/../akaneko/code.bin")


def test_resolve_dest_rejects_absolute_drive_unc_device(tmp_path):
    for evil in [
        "C:\\Windows\\x.bin",
        "C:foo\\x.bin",
        "\\abs\\x.bin",
        "\\\\server\\share\\x.bin",
        "\\\\?\\C:\\x.bin",
        "/abs/x.bin",
    ]:
        with pytest.raises(ValueError):
            resolve_dest(tmp_path, evil)


def test_resolve_dest_rejects_reserved_and_colon_and_trailing_and_mounts(tmp_path):
    for evil in [
        "code.bin:stream",
        "NUL",
        "sub/NUL",
        "CON",
        "a/b:c.bin",
        "name.",
        "name ",
        ".mounts/x.bin",
    ]:
        with pytest.raises(ValueError):
            resolve_dest(tmp_path, evil)


def test_resolve_dest_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_link(ws / "link", outside)
    with pytest.raises(ValueError):
        resolve_dest(ws, "link/x.bin")


def test_overlap_guard_rejects_sd_overlap_and_secret_target(tmp_path, monkeypatch):
    ws = tmp_path / "SD" / "ws"
    ws.mkdir(parents=True)
    with pytest.raises(ValueError):
        check_no_overlap(ws, str(tmp_path / "SD"))
    with pytest.raises(ValueError):
        check_no_overlap(ws, str(ws))
    inner = tmp_path / "ws"
    inner.mkdir()
    check_no_overlap(inner, str(tmp_path / "SD"))  # disjoint: no error
    # secret target: dest resolving onto a secrets file is refused
    monkeypatch.setenv("NINFS_BOOT9_PATH", str(ws / "boot9.bin"))
    (ws / "boot9.bin").write_bytes(b"x")
    with pytest.raises(ValueError):
        resolve_dest(ws, "boot9.bin", secrets=get_secrets())


def test_get_secrets_collects_env_and_extra(monkeypatch):
    monkeypatch.setenv("NINFS_BOOT9_PATH", "C:\\k\\boot9.bin")
    monkeypatch.setenv("NINFS_MOVABLE_PATH", "C:\\k\\movable.sed")
    monkeypatch.setenv("NINFS_SD_ROOT", "D:\\Nintendo 3DS")
    secrets = get_secrets(["X"])
    assert "C:\\k\\boot9.bin" in secrets
    assert "C:\\k\\movable.sed" in secrets
    assert "D:\\Nintendo 3DS" in secrets
    assert "X" in secrets


def test_sanitize_masks_paths_keys_id0():
    out = sanitize(
        "err at C:\\k\\boot9.bin Key: aabbccddeeff00112233445566778899 ID0: 0123abcd",
        ["C:\\k\\boot9.bin"],
    )
    assert "C:\\k\\boot9.bin" not in out
    assert "aabbccddeeff00112233445566778899" not in out
    assert "0123abcd" not in out


def test_reject_write_args():
    for extra in [{"rw": True}, {"writable": 1}, {"read_only": False}, {"ro": False}]:
        with pytest.raises(ValueError):
            reject_write_args(extra)
    reject_write_args({})


def test_resolve_dest_rejects_mounts_via_link(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".mounts").mkdir(parents=True)
    with pytest.raises(ValueError):
        resolve_dest(ws, ".mounts")
    # link reaching .mounts is also refused
    _make_link(ws / "link", ws / ".mounts")
    with pytest.raises(ValueError):
        resolve_dest(ws, "link/x.bin")
