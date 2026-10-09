# ninfs-mcp

[English](README.md)

[ninfs](https://github.com/ihaveamac/ninfs)（3DSのSD・タイトル・ExeFSをマウントするツール）を、安全な読み取り専用5ツールとしてAIエージェントに公開するMCPサーバーです。

「赤猫団の実行コードを解析できる状態にして」の一言で、SD検出から、展開済み `.code` のworkspaceへの抽出まで自律実行できます。SDの中身に書き込むことはありません。

## 機能

- 5ツールのみ: `detect_sd` / `mount_sd` / `find_title` / `extract_code` / `unmount`
- 更新データ優先のタイトル解決（ベース `000400000016C700` → 更新 `0004000E0016C700` を優先）
- 展開済みExeFSコードの抽出（SHA-256メタデータ付き。本文はAIに返さない）
- read-onlyマウント強制。書込・削除APIは存在しない
- 秘密情報の保護: `boot9.bin` / `movable.sed` の中身・SD復号キーをログにも応答にも出さない

## 必要環境

- Windows + WinFsp 2.x
- Python 3.10以降
- `ninfs==2.0`（+ `pyctr==0.7.6`。`haccrypto` のビルドにCコンパイラが必要。例: VS Build Tools）
- 3DSのSDバックアップ（`Nintendo 3DS` フォルダ）、同本体の `movable.sed` と `boot9.bin`

## インストール

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
```

## 設定

| 変数 | 必須 | 意味 |
|---|---|---|
| `NINFS_SD_ROOT` | 任意 | `Nintendo 3DS` フォルダのパス（省略時は自動検出） |
| `NINFS_MOVABLE_PATH` | 必須 | `movable.sed` のパス |
| `NINFS_BOOT9_PATH` | 必須 | `boot9.bin` のパス |
| `NINFS_WORKSPACE` | 必須 | 抽出先workspaceの絶対パス（なければ作成） |

設定するのはパスのみです。中身を本サーバーが読むことはありません（マウントに必要な範囲を除く）。

## MCPクライアント設定 (stdio)

Claude Code / OpenCode（`opencode.jsonc` / `mcpServers`）:

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

### 他プロバイダ

**Claude Code CLI (1行):**

```powershell
claude mcp add ninfs-mcp --transport stdio `
  --env NINFS_MOVABLE_PATH=G:/keys/movable.sed `
  --env NINFS_BOOT9_PATH=G:/keys/boot9.bin `
  --env NINFS_WORKSPACE=C:/analysis/workspace `
  -- C:/path/to/ninfs-mcp/.venv/Scripts/python.exe -m ninfs_mcp.server
```

**Claude Desktop:** `python tools/build_mcpb.py` でビルドし、`dist/ninfs-mcp.mcpb` をSettings → Extensionsへドラッグ＆ドロップ。4つのパスを入力するだけです。

**Cursor / VS Code / Windsurf / Cline:** 上と同じJSON形式を各ホストのMCP設定ファイルに記載 (`mcp.json` / `mcp_config.json` / `cline_mcp_settings.json` / `servers` 配下の `.vscode/mcp.json`):

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

**手早い導入 (Windows):** `powershell -ExecutionPolicy Bypass -File tools/setup.ps1` でvenv作成・インストール・`--help` 確認まで行います。

## 典型フロー

```text
detect_sd → mount_sd → find_title("000400000016C700")
→ extract_code → unmount
```

`extract_code` は `{ dest_path, size, sha256, code_entry }`（メタデータのみ）を返します。

## セキュリティモデル

- マウントはread-only。SDへの書込・削除手段はありません。
- AIは任意コマンドを実行できません。公開は上記5ツールのみです。
- コピー先はworkspace配下に限定（`..`・絶対パス・ドライブ指定・UNC・予約名を拒否）。
- 子プロセスのstdout（鍵情報を含む）は破棄し、stderrはマスクしてからログ・応答に渡します。

## 制限事項

- Windows + WinFsp専用。Linux/macOSは対象外です。
- 実機確認: 抽出までを検証済み。設計経緯は `docs/superpowers/specs/` を参照してください。

## おすすめ併用ツール: Ghidra + CTR loader

抽出したコードの解析には、次の構成で動作確認済みです:

- **Ghidra 12.1.4** + Java 21以降。headlessは `JAVA_TOOL_OPTIONS=-Xmx8G` で起動 (`-Xmx` フラグは受け付けません)。
- **ghidra-ctr-loader v1.3.0** (Raikaru fork) を `Ghidra/Extensions` へ。`CROLoader` / `CRSLoader` / `CtrCodeSetLoader` の登録を確認済み。

```powershell
# raw importの迂回路 (実績: 5.7MBの.codeから15,194関数)
& "G:/tools/ghidra_12.1.4_PUBLIC/support/analyzeHeadless.bat" <projdir> <proj> `
  -import <code.bin> -processor "ARM:LE:32:v7" -loader BinaryLoader -loader-baseAddr 100000
```

注意: `-loader` は表示名ではなくLoaderクラス名を指定します。`-baseAddr` は存在しません (`-loader-baseAddr <16進・0xなし>` を使用)。CXIコンテナの直接mountは一部タイトルでOOMします (loader側課題)。raw importが確実です。調査記録は `docs/superpowers/specs/` を参照してください。

## ライセンス

MIT — [LICENSE](LICENSE) を参照。
