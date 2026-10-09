"""Distribution packaging tests: manifest validity + bundle contents."""
import json
import zipfile

import pytest

from ninfs_mcp import policy  # noqa: F401  (ensures src layout importable)

MANIFEST = "packaging/manifest.json"
REQUIRED_ENV = {"NINFS_MOVABLE_PATH", "NINFS_BOOT9_PATH", "NINFS_WORKSPACE", "NINFS_SD_ROOT"}


def _manifest():
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


def test_manifest_required_fields():
    m = _manifest()
    assert m["manifest_version"] == "0.4"
    assert m["name"] == "ninfs-mcp"
    assert m["server"]["type"] == "uv"
    assert "win32" in m["compatibility"]["platforms"]
    assert m["server"]["mcp_config"]["command"] == "uv"


def test_manifest_env_matches_server_contract():
    m = _manifest()
    env = m["server"]["mcp_config"]["env"]
    assert REQUIRED_ENV <= set(env.keys())
    assert set(m["user_config"].keys()) >= {"movable_sed", "boot9_bin", "workspace"}


def test_manifest_version_matches_pyproject():
    import re

    m = _manifest()
    pyproject = open("pyproject.toml", encoding="utf-8").read()
    version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    assert m["version"] == version


def test_bundle_contains_server(tmp_path):
    import subprocess
    import sys

    out = tmp_path / "ninfs-mcp.mcpb"
    subprocess.run([sys.executable, "tools/build_mcpb.py", "--out", str(out)],
                   check=True, capture_output=True)
    names = set(zipfile.ZipFile(out).namelist())
    assert "manifest.json" in names
    assert "ninfs_mcp/server.py" in names
    assert "ninfs_mcp/runner.py" in names
    assert "ninfs_mcp/policy.py" in names
    assert "pyproject.toml" in names
    assert "run_server.py" in names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
