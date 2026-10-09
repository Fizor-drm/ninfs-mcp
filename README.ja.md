# ninfs-mcp

[English](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Windows](https://img.shields.io/badge/OS-Windows%20%2B%20WinFsp-blue.svg)](#1行で導入)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-stdio-green.svg)](#aiクライアントの接続方法)
[![Tests](https://img.shields.io/badge/tests-67%20passing-brightgreen.svg)](#開発者向け)

> [!TIP]
> **1行・一言・1つの解析バイナリ。**
> あなたが言うのは「赤猫団の実行コードを解析できる状態にして」の一言。
> あとはAIがやります: SD検出 → read-onlyマウント → 更新データ優先解決 →
> 展開済みコード抽出 → 後片付け。SDへの書込みは一切なし。

## 目次

- [仕組み](#仕組み-30秒で理解)
- [1行で導入](#1行で導入)
- [教えるのは4つのパスだけ](#教えるのは4つのパスだけ)
- [AIクライアントの接続方法](#aiクライアントの接続方法)
- [AIにできること・できないこと](#aiにできることできないこと)
- [Ghidraで解析する](#ghidraで解析するおすすめ)
- [困ったら](#困ったら)
- [開発者向け](#開発者向け)

## 仕組み (30秒で理解)

```mermaid
flowchart LR
    You["あなた: 赤猫団のコードを解析して"] --> AI["AIエージェント"]
    AI --> MCP["ninfs-mcp - 5ツール"]
    MCP --> WS["workspace/akaneko/code.bin + サイズ、SHA-256"]
    WS --> GH["Ghidra / 解析作業"]
```

> [!NOTE]
> AIに渡るのはファイルの*メタデータ* (パス・サイズ・ハッシュ) だけです。
> ROMの中身も、`movable.sed` / `boot9.bin` / SD復号キーも渡りません。

## 1行で導入

> [!IMPORTANT]
> 事前準備 (初回のみ): Windows + WinFsp 2.x、C++コンパイラ (依存1件用)、
> SDバックアップ、その本体の `movable.sed` + `boot9.bin`。

- [x] インストーラーがやること:
  - [x] コード取得 (gitまたはzip)
  - [x] 隔離venv構築 + インストール + `--help` 確認
  - [x] 検出クライアントへ登録 (**事前にバックアップ**)

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Fizor-drm/ninfs-mcp/main/tools/install.ps1))) `
  -Workspace C:/analysis -Movable G:/keys/movable.sed -Boot9 G:/keys/boot9.bin
```

> [!WARNING]
> UACが出たら許可してください (ツールチェーン/SDKのセットアップ)。完了後は
> **AIクライアントの再起動** が必要です。

手動が良ければ: `powershell -File tools/setup.ps1` の後、
`.venv\Scripts\python.exe -m ninfs_mcp.server` をクライアントに指定します
(例は下記)。

## 教えるのは4つのパスだけ

| 設定 | 内容 | 必須? |
|:---|:---|:---:|
| `NINFS_WORKSPACE` | 抽出先フォルダ (なければ作成) | ✅ |
| `NINFS_MOVABLE_PATH` | `movable.sed` の場所 | ✅ |
| `NINFS_BOOT9_PATH` | `boot9.bin` の場所 | ✅ |
| `NINFS_SD_ROOT` | `Nintendo 3DS` フォルダの場所 | ➖ 自動検出 |

> [!NOTE]
> 設定するのは*パス*だけです。中身を読むのはマウントに必要な範囲のninfsだけです。

## AIクライアントの接続方法

<details>
<summary><b>Claude Code CLI</b> — 1行</summary>

```powershell
claude mcp add ninfs-mcp --transport stdio `
  --env NINFS_MOVABLE_PATH=G:/keys/movable.sed `
  --env NINFS_BOOT9_PATH=G:/keys/boot9.bin `
  --env NINFS_WORKSPACE=C:/analysis `
  -- C:/path/to/ninfs-mcp/.venv/Scripts/python.exe -m ninfs_mcp.server
```
</details>

<details>
<summary><b>Claude Code / OpenCode</b> — JSON</summary>

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
<summary><b>Claude Desktop</b> — ワンクリック</summary>

`python tools/build_mcpb.py` でビルドし、`dist/ninfs-mcp.mcpb` を
Settings → Extensionsへドラッグ＆ドロップ。4つのパスを入力するだけです。
</details>

<details>
<summary><b>Cursor / VS Code / Windsurf / Cline</b></summary>

上と同じJSONを各ホストのMCP設定ファイルに記載
(`mcp.json` / `mcp_config.json` / `cline_mcp_settings.json` /
`servers` 配下の `.vscode/mcp.json`)。
</details>

## AIにできること・できないこと

| ✅ できる | 🚫 できない |
|:---|:---|
| SD検出・read-onlyマウント | SDへの書込・削除 |
| 更新データをベースより優先選択 | 任意コマンドの実行 (5ツールのみ) |
| workspaceへのコピー | ファイル中身・鍵・秘密の閲覧 |
| 全解除 + 後片付け | マウントの置き去り (終了時に自動解除) |

> [!CAUTION]
> コピー先はworkspace配下に固定。`..`・絶対パス・ドライブ指定・UNC・予約名は
> すべて拒否されます。

## Ghidraで解析する (おすすめ)

動作確認済みの組合せ: **Ghidra 12.1.4** + Java 21、
**ghidra-ctr-loader v1.3.0** を `Ghidra/Extensions` へ。headless起動は
`JAVA_TOOL_OPTIONS=-Xmx8G` で (`analyzeHeadless` 自体は `-Xmx` を受け付けません)。

```powershell
& "<ghidra>/support/analyzeHeadless.bat" <projdir> <proj> `
  -import <code.bin> -processor "ARM:LE:32:v7" -loader BinaryLoader -loader-baseAddr 100000
```

5.7MBの `.code` から**15,194関数**を確認済み。ハマりどころ2件:
`-loader` は表示名ではなくLoader**クラス**名、`-baseAddr` は存在しない
(`-loader-baseAddr <16進・0xなし>` を使う)。CXIコンテナの直接mountは
一部タイトルでメモリ不足になるため、raw importが確実です。

> [!NOTE]
> CXIコンテナの直接mountはloader側課題として未解決です。

## 困ったら

| 症状 | 対処 |
|:---|:---|
| `--help` でFUSEエラー | WinFspが未導入か未起動です。`--help` 表示にも必要です |
| `haccrypto` がビルドできない | Visual Studio Build Toolsの「C++ によるデスクトップ開発」を入れて再実行 |
| マウント失敗・ハング | マウント先は事前に作らないでください。WinFspが作成・削除します |
| インストール中のUAC | ツールチェーン/SDKのセットアップです。許可してください |

## 開発者向け

設計経緯は `docs/superpowers/specs/` にあります。テスト67件・全mock —
`python -m pytest tests/`。MIT — [LICENSE](LICENSE) を参照。
