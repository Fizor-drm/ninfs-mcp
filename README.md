# ninfs-mcp

[日本語](README.ja.md) · MIT · Windows only

**Turn your 3DS SD card into something an AI can work with — safely.**

You say: *"get the Red Cat Corps executable ready for analysis."*
The AI does the rest: finds your SD card, mounts it read-only, picks the update
data over the base game, pulls out the decompressed program code, and cleans up.
Your SD card is never written to. Your keys never leave your machine.

## How it works (30 seconds)

```text
You ──"analyze the Red Cat Corps code"──▶ AI agent
                                              │
                                              ▼
                                    ┌── ninfs-mcp (5 tools) ──┐
                                    │ detect_sd → mount_sd    │
                                    │ → find_title → extract │
                                    │ → unmount               │
                                    └──────────┬──────────────┘
                                               ▼
                                    workspace/akaneko/code.bin
                                    (+ size, SHA-256)
                                               │
                                               ▼
                                    Ghidra / your analysis
```

The AI only ever receives file *metadata* (path, size, hash) — never ROM contents,
never your `movable.sed` / `boot9.bin` / SD key.

## Install in one line

Requirements first (one time): Windows + [WinFsp](https://winfsp.dev/rel/) 2.x,
a C++ compiler (for one dependency), your SD backup, and that console's
`movable.sed` + `boot9.bin`.

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Fizor-drm/ninfs-mcp/main/tools/install.ps1))) `
  -Workspace C:/analysis -Movable G:/keys/movable.sed -Boot9 G:/keys/boot9.bin
```

That fetches the code, builds an isolated environment, and **registers the
server in the AI clients it finds** (Claude Code, OpenCode, Cursor, Windsurf —
your existing configs are backed up first). Restart the AI client afterwards.

Prefer manual setup? `powershell -File tools/setup.ps1`, then point your client at
`.venv\Scripts\python.exe -m ninfs_mcp.server` (see examples below).

## Tell it these four paths

| Setting | What it is | Required? |
|---|---|---|
| `NINFS_WORKSPACE` | Folder where extracted files go (created if missing) | Yes |
| `NINFS_MOVABLE_PATH` | Your `movable.sed` | Yes |
| `NINFS_BOOT9_PATH` | Your `boot9.bin` | Yes |
| `NINFS_SD_ROOT` | Your `Nintendo 3DS` folder | No — auto-detected |

Only *paths* are configured. Contents are never read except by ninfs to mount.

## Connect your AI client

<details>
<summary>Claude Code CLI (one line)</summary>

```powershell
claude mcp add ninfs-mcp --transport stdio `
  --env NINFS_MOVABLE_PATH=G:/keys/movable.sed `
  --env NINFS_BOOT9_PATH=G:/keys/boot9.bin `
  --env NINFS_WORKSPACE=C:/analysis `
  -- C:/path/to/ninfs-mcp/.venv/Scripts/python.exe -m ninfs_mcp.server
```
</details>

<details>
<summary>Claude Code / OpenCode (JSON)</summary>

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
</details>

<details>
<summary>Claude Desktop (one click)</summary>

`python tools/build_mcpb.py`, then drag `dist/ninfs-mcp.mcpb` into
Settings → Extensions and fill in the four paths.
</details>

<details>
<summary>Cursor / VS Code / Windsurf / Cline</summary>

Same JSON as above, in the host's MCP config file
(`mcp.json` / `mcp_config.json` / `cline_mcp_settings.json` /
`.vscode/mcp.json` under `servers`).
</details>

## What the AI can (and cannot) do

| Can | Cannot |
|---|---|
| Detect SD, mount read-only | Write to, or delete from, the SD card |
| Pick update data over base game | Run arbitrary commands (5 tools only) |
| Copy files *into your workspace* | See file contents, keys, or secrets |
| Unmount + clean up everything | Leave mounts behind (auto-cleaned on exit) |

Copy destinations are locked inside your workspace — `..`, absolute paths,
drive specs, UNC paths and reserved names are all rejected.

## Analyze with Ghidra (recommended)

Verified companions: **Ghidra 12.1.4** + Java 21, plus **ghidra-ctr-loader
v1.3.0** in `Ghidra/Extensions`. Start headless with
`JAVA_TOOL_OPTIONS=-Xmx8G` (`analyzeHeadless` rejects `-Xmx` itself).

```powershell
& "<ghidra>/support/analyzeHeadless.bat" <projdir> <proj> `
  -import <code.bin> -processor "ARM:LE:32:v7" -loader BinaryLoader -loader-baseAddr 100000
```

That took a 5.7MB `.code` to 15,194 functions. Two gotchas we hit so you don't
have to: `-loader` wants the Loader **class** name (not the display name),
and there is no `-baseAddr` (it's `-loader-baseAddr <hex, no 0x>`). Mounting a
CXI container directly can run out of memory on some titles — raw import is
the reliable path.

## Troubleshooting

- **`--help` fails with a FUSE error** → WinFsp isn't installed or running.
  Even `--help` needs it.
- **`haccrypto` won't build** → install Visual Studio Build Tools with the
  "Desktop development with C++" workload, then re-run the installer.
- **Mount fails / hangs** → mount points must *not* exist beforehand;
  WinFsp creates and deletes them itself.
- **UAC prompt during install** → that's the toolchain/SDK setup. Approve it.

## For developers

Design history lives in `docs/superpowers/specs/`. 67 tests, all mocked —
`python -m pytest tests/`. MIT — see [LICENSE](LICENSE).
