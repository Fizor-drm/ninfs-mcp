# ninfs-mcp 設計書 (MVP)

日付: 2026-10-07 / 改訂 2026-10-08 rev1 (レビュー反映)
状態: 改訂済み設計 (A案・Windows特化MVP)
実行区分: Native (`Implementation → Review`)

## 1. 目的と成功基準

### 目的
「赤猫団の実行コードを解析できる状態にして」の一言で、SD検出から `.code` 抽出までを自律実行できる MCP サーバーを作る。

### 成功基準
- `000400000016C700` (ベース) と `0004000E0016C700` (更新) を検出し、更新を優先して選択できる
- ExeFS の `.code` / `code.bin` (約6MB) を専用 workspace へ全体コピーできる
- 全 mount を後片付けできる
- `boot9.bin` / `movable.sed` の中身を AI へ返さない

### 非目的 (MVP外)
- Ghidra 連携、3GX ビルド、RomFS 全体読み込み、HTTP 常駐、Linux/mac 対応
- 部分読み出し (offset/size 指定) は Ghidra 連携時に別途追加する

## 2. 前提 (合意済み)
- OS: Windows + WinFsp 特化
- 接続: stdio (ローカル起動)
- ninfs 利用: CLI entry point の subprocess ラッパー (`mount_sd`, `mount_sdtitle`, `mount_ncch`, `mount_exefs`, `mount_romfs`)
- 秘密ファイル: env のパスのみ (`NINFS_BOOT9_PATH`, `NINFS_MOVABLE_PATH`, 任意で `NINFS_SD_ROOT`)、中身非開示

## 3. 採用構成 (A案・最小)

```text
ninfs-mcp/
├─ src/ninfs_mcp/
│  ├─ server.py   # stdio MCP、5ツール公開
│  ├─ runner.py   # ninfs CLI の subprocess 実行・MountSession 管理
│  └─ policy.py   # read-only・workspace制限・sanitizer
├─ tests/
│  ├─ test_policy.py
│  └─ test_runner.py
└─ pyproject.toml
```

ツールは5個に固定する:

- `detect_sd() -> { sd_root, has_n3ds_dir, has_boot9, has_movable } | { error: ambiguous, candidates }`
  - 中身は返さない。存在真偽のみ。
  - 候補が1件なら採用、複数なら `ambiguous` エラーを返す。`NINFS_SD_ROOT` 指定時はそれを優先検証する。
- `mount_sd() -> { mount_id, mount_point }`
  - 引数なし。`mount_id` はサーバー側で発行する。呼び出し側指定は受け付けない。
  - read-only 固定。書込フラグは受け付けない。
- `find_title(mount_id, title_id) -> { requested_title_id, resolved_title_id, kind, tmd_found, title_handle }`
  - `mount_id` に紐づく SD mount に対して解決する。
  - ベース ID 要求時に更新 (`0004000E...`) が存在すれば更新を優先し、`requested` と `resolved` を分けて返す。
  - 複数 TMD 候補がある場合は更新 > バージョン番号最大 > 先頭の順で選択する。
  - `title_handle` は opaque ID とし、SD Title / NCCH / ExeFS の実パスや内部 mount 構造を露出させない。
- `extract_code(title_handle, dest_rel) -> { dest_path, size, sha256 }`
  - `.code` / `code.bin` 全体を workspace へコピーする。サイズ上限は設けない (赤猫団で約6MB)。
  - AI にはパス・サイズ・ハッシュ等のメタデータのみ返し、ファイル内容は返さない。
- `unmount(mount_id) -> { ok }`
  - `MountSession` 内の内部リソースを逆順で best-effort cleanup する。プロセス終了時は残存全てを cleanup する。

`list_titles` / `inspect_*` 系は `find_title` に畳む。増やすのは RomFS 対応時だけ。

### MountSession 所有関係

```text
MountSession (mount_id)
├─ sd
├─ sdtitle
├─ ncch
└─ exefs
```

- `find_title` 成功時に `sdtitle → ncch → exefs` をこの順で確保し、`title_handle` に紐づけて保持する。
- 解除は `exefs → ncch → sdtitle → sd` の逆順で行う。
- 途中失敗時 (例: `ncch` 成功後に `exefs` 失敗) は確保済み分を逆順で破棄し、`title_handle` を発行しない。
- `unmount(mount_id)` は `title_handle` を含めて全て破棄する。

## 4. データフロー

```text
detect_sd (単一候補 or ambiguous)
 ↓ Nintendo 3DS / movable.sed / boot9.bin の存在確認 (真偽のみ)
mount_sd (read-only, mount_id発行)
 ↓
find_title(mount_id, "000400000016C700")
 ↓ ベース確認 → 更新 "0004000E0016C700" があれば優先 → TMD特定 → SD Title mount → CXI判別 → ExeFS特定 → title_handle発行
extract_code(title_handle, "akaneko/code.bin") (全体コピー)
 ↓
unmount(mount_id) (逆順cleanup)
```

## 5. セキュリティ制約
- mount は read-only 既定。SD への write API は作らない。
- `boot9.bin` / `movable.sed` の中身を返さない。
- 共通 sanitizer をログ・MCP 応答・subprocess stderr 要約の全経路に通す。env パス・SD フルパスが ninfs の stderr に含まれてもマスクしてから返す。
- コピー先は専用 workspace 配下のみ。正規化後のパスが workspace 配下かで判定する (`Path.resolve()` + `is_relative_to`)。文字列検査のみに頼らない。
- 拒否対象: `..`、絶対パス、ドライブ指定 (`C:...`)、`C:foo`、ルート相対 (`\foo`)、UNC (`\\server\share`)、`/` と `\` の混在、symlink/junction 経由の脱出。
- mount ごとに `mount_id` をサーバー発行する。呼び出し側指定は不可。
- MCP 終了時に残存 mount を cleanup する。
- 元 ROM / SD データの削除 API は作らない。
- 巨大 RomFS 全体を既定で AI へ読み込ませない。
- AI に任意コマンド実行は渡さない。公開するのは上記5ツールのみ。

## 6. エラーハンドリング
- subprocess 失敗時は sanitizer 済み stderr 要約のみ返す。
- 失敗した `mount_id` / `title_handle` は即破棄し、確保済み内部リソースは逆順 cleanup する。
- WinFsp 不在、SD 不在、Title 不在は検出順に早期リターンする。
- `detect_sd` 複数候補は `ambiguous` エラーとし、先頭の勝手採用はしない。
- タイムアウト時はプロセス kill 後に逆順 unmount する。

## 7. テスト
- `tests/test_policy.py`:
  1. traversal 拒否 (`..` を含む相対パス)
  2. workspace 外拒否 (絶対パス、ドライブ指定、`C:foo`、`\foo`、UNC、`/` `\` 混在)
  3. symlink/junction 解決後の workspace 外拒否
  4. 秘密中身非露出 + sanitizer (ログ・応答の両方でパスがマスクされる)
  5. read-only 固定 (write フラグを受け付けない)
- `tests/test_runner.py` (subprocess を mock、実機 mount なし):
  1. 更新優先解決 (`requested` / `resolved` の分離)
  2. TMD 複数候補の選択規則
  3. `detect_sd` 複数候補で `ambiguous`
  4. subprocess 失敗時の stderr マスク
  5. 途中 mount 失敗時の逆順 cleanup
  6. timeout 時の kill + cleanup
- 実機 mount の結合試験は手動とし、CI では回さない。

## 8. 検討した代替案
- B案 (層別構成 `tools/sd.py title.py ncch.py exefs.py mount.py`): 拡張性は高いが MVP では空抽象が残るため見送り。2タイトル目や RomFS 対応時に分割する。
- C案 (中間構成): A と B の妥協案。MVP の一本道フローには A で十分なため見送り。
- `extract_code` の 2MB + offset/size 案: 主要ユースケース (約6MB) が通らず、コピー方式なら制限自体が不要なため廃止した。

## 9. 次段階
- `writing-plans` で実装計画化する
- 実装は Native で行い、完了後に独立 Review + `security-audit quick` を行う
