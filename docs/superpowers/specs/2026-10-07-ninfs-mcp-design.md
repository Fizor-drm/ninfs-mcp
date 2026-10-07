# ninfs-mcp 設計書 (MVP)

日付: 2026-10-07 / 改訂 2026-10-08 rev2 (独立レビュー反映)
状態: 改訂済み設計 (A案・Windows特化MVP)
実行区分: Native (`Implementation → Review`)

## 1. 目的と成功基準

### 目的
「赤猫団の実行コードを解析できる状態にして」の一言で、SD検出から `.code` 抽出までを自律実行できる MCP サーバーを作る。

### 成功基準
- `000400000016C700` (ベース) と `0004000E0016C700` (更新) を検出し、更新を優先して選択できる
- ExeFS のコード (圧縮時は展開済みを優先、約6MB) を専用 workspace へ全体コピーできる
- 全 mount を後片付けできる (失敗時は残存を報告する)
- `boot9.bin` / `movable.sed` の中身・SD復号キーを AI へ返さない

### 非目的 (MVP外)
- Ghidra 連携、3GX ビルド、RomFS 全体読み込み、HTTP 常駐、Linux/mac 対応
- 部分読み出し (offset/size 指定) は Ghidra 連携時に別途追加する

## 2. 前提 (合意済み)
- OS: Windows + WinFsp 特化
- 接続: stdio (ローカル起動)
- ninfs 利用: CLI entry point の subprocess ラッパー (`mount_sd`, `mount_sdtitle`, `mount_ncch`, `mount_exefs`, `mount_romfs`)
- 秘密ファイル: env のパスのみ (`NINFS_BOOT9_PATH`, `NINFS_MOVABLE_PATH`, 任意で `NINFS_SD_ROOT`)、中身非開示
- 作業場所: `NINFS_WORKSPACE` (必須の絶対パス。存在しなければ作成する)
- 依存バージョンは `pyproject.toml` に pin し、採用版の CLI 引数 (`--decompress-code` 等) と出力名を実装時に照合する

## 3. 採用構成 (A案・最小)

```text
ninfs-mcp/
├─ src/ninfs_mcp/
│  ├─ server.py   # stdio MCP、5ツール公開
│  ├─ runner.py   # ninfs CLI の subprocess 実行・MountSession 管理
│  └─ policy.py   # read-only強制・workspace制限・sanitizer
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
  - read-only 強制。書込フラグは受け付けない。
- `find_title(mount_id, title_id) -> { requested_title_id, resolved_title_id, kind, tmd_found, title_handle }`
  - `mount_id` に紐づく SD mount に対して解決する。
  - ベース ID 要求時に更新 (`0004000E...`) が存在すれば更新を優先し、`requested` と `resolved` を分けて返す。
  - 複数 TMD 候補がある場合は更新 > バージョン番号最大 > 先頭の順で選択する。
  - `title_handle` は opaque ID とし、SD Title / NCCH / ExeFS の実パスや内部 mount 構造を露出させない。
- `extract_code(title_handle, dest_rel) -> { dest_path, size, sha256, code_entry }`
  - ExeFS mount 時に `--decompress-code` を付ける。
  - `code_entry` は `code-decompressed.bin` が存在すればそれ、なければ `.code` / `code.bin` とする。どれも無い場合や展開失敗時はエラーを返す。
  - ファイル全体を workspace へコピーする。サイズ上限は設けない (赤猫団で約6MB)。
  - AI にはパス・サイズ・ハッシュ・選択エントリ名のメタデータのみ返し、ファイル内容は返さない。
- `unmount(mount_id) -> { ok, incomplete, errors_sanitized }`
  - `MountSession` 内の内部リソースを逆順で best-effort cleanup する。プロセス終了時は残存全てを cleanup する。
  - 公開 ID (`mount_id` / `title_handle`) は要求時点で即無効化し、解除失敗した内部リソースは `incomplete: true` と sanitizer 済み `errors_sanitized` で報告する。残存は次回 `unmount` /終了時 cleanup の対象として保持する。

`list_titles` / `inspect_*` 系は `find_title` に畳む。増やすのは RomFS 対応時だけ。

### MountSession 所有関係

```text
MountSession (mount_id)
├─ sd
├─ sdtitle
├─ ncch
└─ exefs (+ title_handle)
```

- `find_title` 成功時に `sdtitle → ncch → exefs` をこの順で確保し、`title_handle` に紐づけて保持する。
- 解除は `exefs → ncch → sdtitle → sd` の逆順で行う。
- 途中失敗時 (例: `ncch` 成功後に `exefs` 失敗) は確保済み分を逆順で破棄し、`title_handle` を発行しない。
- `unmount(mount_id)` は公開 ID を即無効化した上で逆順 cleanup し、失敗残存があれば `incomplete: true` で返す。

### subprocess 隔離契約 (long-lived mount プロセス)

ninfs の各 mount CLI は FUSE の foreground 動作でブロックする常駐プロセスであり、解除はプロセス終了 (Ctrl+C 相当) で行う。そのため run-to-completion (`communicate` 待ち) は使わない。契約は以下とする:

- 起動: `Popen(argv, stdout=DEVNULL, stderr=PIPE, stdin=DEVNULL)`。継承しない。stdout は DEVNULL へ捨てる (`ID0:` / `Key:` 行をどこにも残さない)。
- 利用可能確認: mount_point のポーリング (200ms間隔・上限10秒) で読み取り可能になったら成功とする。
- 失敗時: stderr を回収し、共通 sanitizer 適用後の要約 (末尾2000字) のみ返す。
- 解除: `terminate()` → 待機 → 残存すれば `kill()`。タイムアウト時も同じ手順の後に逆順 cleanup する。
- MCP 終了時は全残存プロセスに同手順を適用する。

## 4. データフロー

```text
detect_sd (単一候補 or ambiguous)
 ↓ Nintendo 3DS / movable.sed / boot9.bin の存在確認 (真偽のみ)
mount_sd (read-only強制, mount_id発行, stdout隔離)
 ↓
find_title(mount_id, "000400000016C700")
 ↓ ベース確認 → 更新 "0004000E0016C700" があれば優先 → TMD特定 → SD Title mount → CXI判別 → ExeFS特定 (--decompress-code) → title_handle発行
extract_code(title_handle, "akaneko/code.bin") (code-decompressed優先で全体コピー)
 ↓
unmount(mount_id) (逆順cleanup, 残存はincomplete報告)
```

## 5. セキュリティ制約
- 全 mount は read-only 強制。SD への write API は作らない。
- `boot9.bin` / `movable.sed` の中身・SD復号キーを返さない。`ID0:` / `Key:` 行はログにも残さない。
- 共通 sanitizer をログ・MCP 応答・subprocess stderr 要約の全経路に通す。env パス・SD フルパスが ninfs の stderr に含まれてもマスクしてから返す。
- コピー先は専用 workspace 配下のみ。`NINFS_WORKSPACE` は必須の絶対パスとし、存在しなければ作成する。判定は二段構えとする: (a) 形式拒否 (`..` 成分、絶対パス、ドライブ指定 `C:...` / `C:foo`、ルート相対 `\foo`、UNC、デバイスパス `\\?\` `\\.\`)、(b) 正規化後の包含確認 (`Path.resolve()` + `is_relative_to`)。Windows の `/` と `\` の混在自体は正常な相対パスとして許容し、(b) で判定する。
- 拒否対象: `..` 成分、絶対パス、ドライブ指定 (`C:...`)、`C:foo`、ルート相対 (`\foo`)、UNC (`\\server\share`)、デバイスパス、symlink/junction 経由の脱出。
- mount ごとに `mount_id` をサーバー発行する。呼び出し側指定は不可。
- MCP 終了時に残存 mount を cleanup する。
- 元 ROM / SD データの削除 API は作らない。
- 巨大 RomFS 全体を既定で AI へ読み込ませない。
- AI に任意コマンド実行は渡さない。公開するのは上記5ツールのみ。

## 6. エラーハンドリング
- subprocess 失敗時は sanitizer 済み stderr 要約のみ返す。stdout (キー含む) は破棄する。
- 失敗した `mount_id` / `title_handle` の公開 ID は即無効化し、確保済み内部リソースは逆順 cleanup する。残存は `incomplete` で報告する。
- WinFsp 不在、SD 不在、Title 不在は検出順に早期リターンする。
- `detect_sd` 複数候補は `ambiguous` エラーとし、先頭の勝手採用はしない。
- 展開失敗時 (`.code` 展開エラー) は `extract_code` をエラーとし、対象エントリ名と sanitizer 済み理由を返す。
- タイムアウト時は `terminate()` → 残存すれば `kill()` の後に逆順 unmount する。

## 7. テスト
- `tests/test_policy.py`:
  1. traversal 拒否 (`..` を含む相対パス)
  2. workspace 外拒否 (絶対パス、ドライブ指定、`C:foo`、`\foo`、UNC、デバイスパス。`/` `\` 混在の正常相対パスは許容)
  2b. 形式拒否の単独検証 (`..` 成分を含むが正規化後は内側に収まる入力も拒否する)
  3. symlink/junction 解決後の workspace 外拒否
  4. 秘密中身非露出 + sanitizer (ログ・応答の両方でパス・キーがマスクされる、stdout破棄)
  5. read-only 強制 (write フラグを受け付けない)
- `tests/test_runner.py` (subprocess を mock、実機 mount なし):
  1. 正常系: 検出→mount→更新優先解決→全体コピー (サイズ+SHA-256) →正常解除後のハンドル無効化
  2. 更新優先解決 (`requested` / `resolved` の分離)
  3. TMD 複数候補の選択規則
  4. `code-decompressed.bin` 優先選択と、無い場合の `.code` フォールバック
  5. `detect_sd` 複数候補で `ambiguous`
  6. subprocess 失敗時の stderr マスク + stdout 破棄 (キー非露出)
  7. 途中 mount 失敗時の逆順 cleanup (解除失敗後も残りを処理する)
  8. timeout 時の kill + cleanup
  9. `unmount` 失敗残存時の `incomplete` 報告と公開 ID 無効化
- 実機 mount の結合試験は手動とし、CI では回さない。

## 8. 検討した代替案
- B案 (層別構成 `tools/sd.py title.py ncch.py exefs.py mount.py`): 拡張性は高いが MVP では空抽象が残るため見送り。2タイトル目や RomFS 対応時に分割する。
- C案 (中間構成): A と B の妥協案。MVP の一本道フローには A で十分なため見送り。
- `extract_code` の 2MB + offset/size 案: 主要ユースケース (約6MB) が通らず、コピー方式なら制限自体が不要なため廃止した。

## 9. 次段階
- `writing-plans` で実装計画化する
- 実装は Native で行い、完了後に独立 Review + `security-audit quick` を行う
