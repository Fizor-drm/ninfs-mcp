# ninfs-mcp

[日本語](README.ja.md)

MCP server that exposes [ninfs](https://github.com/ihaveamac/ninfs) (3DS SD/title/ExeFS mounter) to AI agents as five safe, read-only tools.

With one prompt — *"get the Red Cat Corps executable ready for analysis"* — an agent can go from SD detection to an extracted, decompressed `.code` in a workspace, without ever touching the SD contents.

## Features

- 5 tools only: `detect_sd` / `mount_sd` / `find_title` / `extract_code` / `unmount`
- Update-aware title resolution (base `000400000016C700` → update `0004000E0016C700` preferred)
- Decompressed ExeFS code extraction with SHA-256 metadata (contents never returned to the AI)
- Read-only mounts enforced; no write or delete API exists
- Secret hygiene: `boot9.bin` / `movable.sed` contents and the SD key are never logged or returned

## Requirements

- Windows + WinFsp 2.x
- Python 3.10+
- `ninfs==2.0` (+ `pyctr==0.7.6`; needs a C compiler for `haccrypto` — e.g. VS Build Tools)
- A 3DS SD backup (`Nintendo 3DS` folder), plus that console's `movable.sed` and `boot9.bin`

## Install

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
```

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `NINFS_SD_ROOT` | No | Path to the `Nintendo 3DS` folder (auto-detected if omitted) |
| `NINFS_MOVABLE_PATH` | Yes | Path to `movable.sed` |
| `NINFS_BOOT9_PATH` | Yes | Path to `boot9.bin` |
| `NINFS_WORKSPACE` | Yes | Absolute path of the extraction workspace (created if missing) |

Only paths are configured — file contents are never read by this server beyond what ninfs needs to mount.

## MCP client setup (stdio)

Claude Code / OpenCode (`opencode.jsonc` / `mcpServers`):

```json
{
  "ninfs-mcp": {
    "command": "C:/path/to/ninfs-mcp/.venv/Scripts/python.exe",
    "args": ["-m", "ninfs_mcp.server"],
    "env": {
      "NINFS_MOVABLE_PATH": "G:/keys/movable.sed",
      "NINFS_BOOT9_PATH": "G:/keys/boot9.bin",
      "NINFS_WORKSPACE": "C:/analysis/workspace"
    }
  }
}
```

### Other providers

**Claude Code CLI (one line):**

```powershell
claude mcp add ninfs-mcp --transport stdio `
  --env NINFS_MOVABLE_PATH=G:/keys/movable.sed `
  --env NINFS_BOOT9_PATH=G:/keys/boot9.bin `
  --env NINFS_WORKSPACE=C:/analysis/workspace `
  -- C:/path/to/ninfs-mcp/.venv/Scripts/python.exe -m ninfs_mcp.server
```

**Claude Desktop:** build once with `python tools/build_mcpb.py`, then drag `dist/ninfs-mcp.mcpb` into Settings → Extensions and fill in the four paths.

**Cursor / VS Code / Windsurf / Cline:** same JSON shape as above, in the host's MCP config file (`mcp.json` / `mcp_config.json` / `cline_mcp_settings.json` / `.vscode/mcp.json` under `servers`):

```json
{
  "ninfs-mcp": {
    "command": "C:/path/to/ninfs-mcp/.venv/Scripts/python.exe",
    "args": ["-m", "ninfs_mcp.server"],
    "env": {
      "NINFS_MOVABLE_PATH": "G:/keys/movable.sed",
      "NINFS_BOOT9_PATH": "G:/keys/boot9.bin",
      "NINFS_WORKSPACE": "C:/analysis/workspace"
    }
  }
}
```

**Quick bootstrap (Windows):** `powershell -ExecutionPolicy Bypass -File tools/setup.ps1` creates the venv, installs, and verifies `--help`.

**One-line install with client auto-registration:**

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Fizor-drm/ninfs-mcp/main/tools/install.ps1))) `
  -Workspace C:/analysis -Movable G:/keys/movable.sed -Boot9 G:/keys/boot9.bin
```

This clones/updates the repo to `%LOCALAPPDATA%\ninfs-mcp`, builds the venv, and registers the server into detected clients (Claude Code, OpenCode, Cursor, Windsurf — each config backed up first). Restart the AI client afterwards.

## Typical flow

```text
detect_sd → mount_sd → find_title("000400000016C700")
→ extract_code → unmount
```

`extract_code` returns `{ dest_path, size, sha256, code_entry }` — metadata only.

## Security model

- Mounts are read-only. There is no tool that writes to the SD card or deletes anything.
- The AI cannot run arbitrary commands — only the five tools above.
- Copy destinations are confined to the workspace (traversal, absolute paths, drive specs, UNC, reserved names rejected).
- Subprocess stdout (which contains key material) is discarded; stderr is masked before it reaches logs or responses.

## Limitations

- Windows + WinFsp only. No Linux/macOS support.
- Real-hardware combination test status: verified with [Ghidra 12.1.4 + CTR loader](docs/superpowers/specs/2026-10-07-ninfs-mcp-design.md) for extraction; analysis-side notes live in your own workspace.
- See `docs/superpowers/specs/` for the full design history.

## Recommended companions: Ghidra + CTR loader

To analyze the extracted code, this stack is verified working:

- **Ghidra 12.1.4** (`G:\tools\ghidra_12.1.4_PUBLIC` used here) + Java 21+. Run headless with `JAVA_TOOL_OPTIONS=-Xmx8G` (`analyzeHeadless` rejects `-Xmx`).
- **ghidra-ctr-loader v1.3.0** (Raikaru fork) into `Ghidra/Extensions`. Registered loaders confirmed: `CROLoader`, `CRSLoader`, `CtrCodeSetLoader`.

```powershell
# raw import fallback (verified: 15,194 functions from a 5.7MB .code)
& "G:/tools/ghidra_12.1.4_PUBLIC/support/analyzeHeadless.bat" <projdir> <proj> `
  -import <code.bin> -processor "ARM:LE:32:v7" -loader BinaryLoader -loader-baseAddr 100000
```

Notes: `-loader` takes the Loader **class** name, not the display name; `-baseAddr` does not exist (use `-loader-baseAddr <hex, no 0x>`). Direct CXI-container mounting OOMs on some titles (loader-side issue); raw import is the reliable path. See `docs/superpowers/specs/` for the investigation trail.

## License

MIT — see [LICENSE](LICENSE).
