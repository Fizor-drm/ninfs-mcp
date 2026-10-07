# ninfs-mcp 設計書 (MVP)

日付: 2026-10-07
状態: 承認済み設計 (A案・Windows特化MVP)
実行区分: Native (`Implementation → Review`)

## 1. 目的と成功基準

### 目的
「赤猫団の実行コードを解析できる状態にして」の一言で、SD検出から `.code` 抽出までを自律実行できる MCP サーバーを作る。

### 成功基準
- `000400000016C700` (ベース) と `0004000E0016C700` (更新) を検出し、更新を優先して選択できる
- ExeFS の `.code` / `code.bin` を専用 workspace へコピーできる
- 全 mount を後片付けできる
- `boot9.bin` / `movable.sed` の中身を AI へ返さない

### 非目的 (MVP外)
- Ghidra 連携、3GX ビルド、RomFS 全体読み込み、HTTP 常駐、Linux/mac 対応

## 2. 前提 (合意済み)
- OS: Windows + WinFsp 特化
- 接続: stdio (ローカル起動)
- ninfs 利用: CLI entry point の subprocess ラッパー (`mount_sd`, `mount_sdtitle`, `mount_ncch`, `mount_exefs`, `mount_romfs`)
- 秘密ファイル: env のパスのみ (`NINFS_BOOT9_PATH`, `NINFS_MOVABLE_PATH`)、中身非開示

## 3. 採用構成 (A案・最小)

```text
ninfs-mcp/
├─ src/ninfs_mcp/
│  ├─ server.py   # stdio MCP、5ツール公開
│  ├─ runner.py   # ninfs CLI の subprocess 実行・mount 管理
│  └─ policy.py   # read-only・workspace制限・traversal禁止
├─ tests/test_policy.py
└─ pyproject.toml
```

ツールは5個に固定する:

- `detect_sd() -> { sd_root, has_n3ds_dir, has_boot9, has_movable }`
  - 中身は返さない。存在真偽のみ。
- `mount_sd(mount_id?) -> { mount_id, mount_point }`
  - read-only 固定。書込フラグは受け付けない。
- `find_title(title_id) -> { title_id, kind: base|update, tmd_found, cxi_path }`
  - 内部でベース/更新の優先解決を行う。
- `extract_code(mount_id, dest_rel) -> { dest_path, size }`
  - ExeFS の `.code` / `code.bin` のみ。上限 2MB を超える場合は offset+size 指定を必須とする。
- `unmount(mount_id) -> { ok }`
  - プロセス終了時は残存 mount を全て cleanup する。

`list_titles` / `inspect_*` 系は `find_title` に畳む。増やすのは RomFS 対応時だけ。

## 4. データフロー

```text
detect_sd
 ↓ Nintendo 3DS / movable.sed / boot9.bin の存在確認 (真偽のみ)
mount_sd (read-only)
 ↓
find_title("000400000016C700")
 ↓ ベース確認 → 更新 "0004000E0016C700" があれば優先 → TMD特定 → SD Title mount → CXI判別 → ExeFS特定
extract_code (workspaceへコピー)
 ↓
unmount (残存cleanup)
```

## 5. セキュリティ制約
- mount は read-only 既定。SD への write API は作らない。
- `boot9.bin` / `movable.sed` の中身を返さない。env のパスもログではマスクする。
- コピー先は専用 workspace 配下のみ。`..`、絶対パス、ドライブ指定は拒否。
- mount ごとに `mount_id` を発行する。
- MCP 終了時に残存 mount を cleanup する。
- 元 ROM / SD データの削除 API は作らない。
- 巨大 RomFS 全体を既定で AI へ読み込ませない。
- AI に任意コマンド実行は渡さない。公開するのは上記5ツールのみ。

## 6. エラーハンドリング
- subprocess 失敗時は stderr 要約のみ返す。秘密パスはマスクする。
- 失敗した `mount_id` は即破棄する。
- WinFsp 不在、SD 不在、Title 不在は検出順に早期リターンする。
- タイムアウト時はプロセス kill 後に unmount する。

## 7. テスト
- `tests/test_policy.py` のみ:
  1. traversal 拒否 (`..` を含む相対パス)
  2. workspace 外拒否 (絶対パス)
  3. 秘密中身非露出 (存在真偽のみで内容を返さない)
  4. read-only 固定 (write フラグを受け付けない)
- 実機 mount の結合試験は手動とし、CI では回さない。

## 8. 検討した代替案
- B案 (層別構成 `tools/sd.py title.py ncch.py exefs.py mount.py`): 拡張性は高いが MVP では空抽象が残るため見送り。2タイトル目や RomFS 対応時に分割する。
- C案 (中間構成): A と B の妥協案。MVP の一本道フローには A で十分なため見送り。

## 9. 次段階
- `writing-plans` で実装計画化する
- 実装は Native で行い、完了後に独立 Review + `security-audit quick` を行う
