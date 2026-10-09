"""Build dist/ninfs-mcp.mcpb from packaging/ + src/. Stdlib only."""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = [
    "manifest.json",
    "run_server.py",
    "pyproject.toml",
    "README.md",
    "ninfs_mcp/__init__.py",
    "ninfs_mcp/server.py",
    "ninfs_mcp/runner.py",
    "ninfs_mcp/policy.py",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "dist" / "ninfs-mcp.mcpb"))
    parser.add_argument("--check", action="store_true",
                        help="validate only, do not write the bundle")
    args = parser.parse_args()

    manifest = json.loads((ROOT / "packaging" / "manifest.json").read_text(encoding="utf-8"))
    if manifest["manifest_version"] != "0.4":
        raise SystemExit("unsupported manifest_version")
    if not manifest["server"]["mcp_config"]["command"]:
        raise SystemExit("mcp_config.command is empty")

    with tempfile.TemporaryDirectory() as stage:
        staged = Path(stage)
        shutil.copy(ROOT / "packaging" / "manifest.json", staged / "manifest.json")
        shutil.copy(ROOT / "packaging" / "run_server.py", staged / "run_server.py")
        shutil.copy(ROOT / "pyproject.toml", staged / "pyproject.toml")
        shutil.copy(ROOT / "README.md", staged / "README.md")
        shutil.copytree(ROOT / "src" / "ninfs_mcp", staged / "ninfs_mcp")
        missing = [name for name in REQUIRED if not (staged / name).exists()]
        if missing:
            raise SystemExit(f"missing bundle files: {missing}")
        if args.check:
            print("bundle content ok")
            return 0
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(staged.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(staged).as_posix())
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
