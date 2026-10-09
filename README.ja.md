# ninfs-mcp

[English](README.md) · MIT · Windows専用

**3DSのSDカードを、AIが扱える形に変える——安全に。**

あなたが言うのは「赤猫団の実行コードを解析できる状態にして」の一言。
あとはAIがやります: SDカードを見つけてread-onlyでマウントし、ベースより
更新データを選び、展開済みのプログラムコードを取り出して後片付け。
SDへの書込みは一切なし。鍵がマシンの外に出ることもありません。

## 仕組み (30秒で理解)

```text
あなた ──「赤猫団のコードを解析して」──▶ AIエージェント
                                              │
                                              ▼
                                    ┌── ninfs-mcp (5ツール) ┐
                                    │ detect_sd → mount_sd   │
                                    │ → find_title → extract │
                                    │ → unmount              │
                                    └──────────┬─────────────┘
                                               ▼
                                    workspace/akaneko/code.bin
                                    (+ サイズ、SHA-256)
                                               │
                                               ▼
                                    Ghidra / 解析作業
```

AIに渡るのはファイルの*メタデータ* (パス・サイズ・ハッシュ) だけです。
ROMの中身も、`movable.sed` / `boot9.bin` / SD復号キーも渡りません。

## 1行で導入

事前準備 (初回のみ): Windows + [WinFsp](https://winfsp.dev/rel/) 2.x、
C++コンパイラ (依存1件のビルド用)、SDバックアップ、その本体の
`movable.sed` + `boot9.bin`。

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Fizor-drm/ninfs-mcp/main/tools/install.ps1))) `
  -Workspace C:/analysis -Movable G:/keys/movable.sed -Boot9 G:/keys/boot9.bin
```

コード取得・隔離環境の構築から、見つけたAIクライアント
(Claude Code・OpenCode・Cursor・Windsurf) への登録まで自動で行います
(既存設定はバックアップ後に更新)。最後にAIクライアントを再起動してください。

手動が良ければ: `powershell -File tools/setup.ps1` の後、
`.venv\Scripts\python.exe -m ninfs_mcp.server` をクライアントに指定します
(例は下記)。

## 教えるのは4つのパスだけ

| 設定 | 内容 | 必須? |
|---|---|---|
| `NINFS_WORKSPACE` | 抽出先フォルダ (なければ作成) | はい |
| `NINFS_MOVABLE_PATH` | `movable.sed` の場所 | はい |
| `NINFS_BOOT9_PATH` | `boot9.bin` の場所 | はい |
| `NINFS_SD_ROOT` | `Nintendo 3DS` フォルダの場所 | いいえ (自動検出) |

設定するのは*パス*だけです。中身を読むのはマウントに必要な範囲のninfsだけです。

## AIクライアントの接続方法

<details>
<summary>Claude Code CLI (1行)</summary>

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
<summary>Claude Desktop (ワンクリック)</summary>

`python tools/build_mcpb.py` でビルドし、`dist/ninfs-mcp.mcpb` を
Settings → Extensionsへドラッグ＆ドロップ。4つのパスを入力するだけです。
</details>

<details>
<summary>Cursor / VS Code / Windsurf / Cline</summary>

上と同じJSONを各ホストのMCP設定ファイルに記載
(`mcp.json` / `mcp_config.json` / `cline_mcp_settings.json` /
`servers` 配下の `.vscode/mcp.json`)。
</details>

## AIにできること・できないこと

| できる | できない |
|---|---|
| SD検出・read-onlyマウント | SDへの書込・削除 |
| 更新データをベースより優先選択 | 任意コマンドの実行 (5ツールのみ) |
| workspaceへのコピー | ファイル中身・鍵・秘密の閲覧 |
| 全解除 + 後片付け | マウントの置き去り (終了時に自動解除) |

コピー先はworkspace配下に固定。`..`・絶対パス・ドライブ指定・UNC・予約名は
すべて拒否されます。

## Ghidraで解析する (おすすめ)

動作確認済みの組合せ: **Ghidra 12.1.4** + Java 21、
**ghidra-ctr-loader v1.3.0** を `Ghidra/Extensions` へ。headless起動は
`JAVA_TOOL_OPTIONS=-Xmx8G` で (`analyzeHeadless` 自体は `-Xmx` を受け付けません)。

```powershell
& "<ghidra>/support/analyzeHeadless.bat" <projdir> <proj> `
  -import <code.bin> -processor "ARM:LE:32:v7" -loader BinaryLoader -loader-baseAddr 100000
```

5.7MBの `.code` から15,194関数を確認済み。ハマりどころ2件:
`-loader` は表示名ではなくLoader**クラス**名、`-baseAddr` は存在しない
(`-loader-baseAddr <16進・0xなし>` を使う)。CXIコンテナの直接mountは
一部タイトルでメモリ不足になるため、raw importが確実です。

## 困ったら

- **`--help` でFUSEエラー** → WinFspが未導入か未起動です。`--help` 表示にも必要です。
- **`haccrypto` がビルドできない** → Visual Studio Build Toolsの
  「C++ によるデスクトップ開発」を入れて、インストーラーを再実行してください。
- **マウント失敗・ハング** → マウント先は事前に作らないでください。
  WinFspが作成・削除します。
- **インストール中のUAC** → ツールチェーン/SDKのセットアップです。許可してください。

## 開発者向け

設計経緯は `docs/superpowers/specs/` にあります。テスト67件・全mock —
`python -m pytest tests/`。MIT — [LICENSE](LICENSE) を参照。
