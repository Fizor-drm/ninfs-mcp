# ninfs-mcp 設計書 (MVP)

日付: 2026-10-07 / 改訂 2026-10-08 rev4 (スパイク実測反映)
状態: 改訂済み設計 (A案・Windows特化MVP)
実行区分: Native (`Implementation → Review`)

## 1. 目的と成功基準

### 目的
「赤猫団の実行コードを解析できる状態にして」の一言で、SD検出から `.code` 抽出までを自律実行できる MCP サーバーを作る。

### 成功基準
- `000400000016C700` (ベース) と `0004000E0016C700` (更新) を検出し、更新を優先して選択できる
- ExeFS のコード (圧縮時は展開済みを優先、約6MB) を専用 workspace へ全体コピーできる
- 全 mount を後片付けできる (失敗時は残存を報告・再解除できる)
- `boot9.bin` / `movable.sed` の中身・SD復号キーを AI へ返さない

### 非目的 (MVP外)
- Ghidra 連携、3GX ビルド、RomFS 全体読み込み、HTTP 常駐、Linux/mac 対応
- 部分読み出し (offset/size 指定) は Ghidra 連携時に別途追加する

## 2. 前提 (合意済み・実測済み)

- OS: Windows + WinFsp 特化
- 接続: stdio (ローカル起動)
- ninfs 利用: 2.0 の CLI entry point の subprocess ラッパー (`mount_sd`, `mount_sdtitle`, `mount_ncch`, `mount_exefs`)。import しない。
- 秘密ファイル: env のパスのみ (`NINFS_BOOT9_PATH`, `NINFS_MOVABLE_PATH`, 任意で `NINFS_SD_ROOT`)、中身非開示
- 作業場所: `NINFS_WORKSPACE` (必須の絶対パス。存在しなければ作成する)
- 依存バージョンは `pyproject.toml` に pin し、採用版の CLI 引数と出力名を実装時に照合する
- 実測根拠: ninfs 2.0 wheel展開物の `mount/sd.py`, `sdtitle.py`, `ncch.py`, `exefs.py`, `_common.py` および pyctr の `type/tmd.py`。調査物は `graft/.cache/` 配下の throwaway。

## 3. 採用構成 (A案・最小)

```text
ninfs-mcp/
├─ src/ninfs_mcp/
│  ├─ server.py   # stdio MCP、5ツール公開。runnerの内部型を公開dictへ変換する
│  ├─ runner.py   # ninfs CLI の常駐プロセス管理・MountSession 所有
│  └─ policy.py   # read-only強制・workspace制限・sanitizer・secrets集約
├─ tests/
│  ├─ test_policy.py
│  ├─ test_runner.py
│  └─ test_server.py
└─ pyproject.toml
```

ツールは5個に固定する:

- `detect_sd() -> { sd_root, has_n3ds_dir, has_boot9, has_movable } | { error: ambiguous, candidates }`
  - 中身は返さない。存在真偽のみ。
  - 候補が1件なら採用、複数なら `ambiguous` エラーを返す。`NINFS_SD_ROOT` 指定時はそれを優先検証する。
- `mount_sd() -> { mount_id, mount_point }`
  - 引数なし。`mount_id` はサーバー側で発行する。呼び出し側指定は受け付けない。
- `find_title(mount_id, title_id) -> { requested_title_id, resolved_title_id, kind, tmd_found, title_handle }`
  - `title_id` は16桁16進のみ受け付け、小文字に正規化する。不正形式は子プロセス起動前に `ValueError`。
  - SD mount 上で選択してから子 mount を起動する。更新 (`0004000E...`) が完全 (ディレクトリ+TMDあり) なら更新優先。
  - `title_handle` は opaque ID。実パスや内部 mount 構造を露出させない。
- `extract_code(title_handle, dest_rel) -> { dest_path, size, sha256, code_entry }`
  - `code_entry` は `code-decompressed.bin` があればそれ、なければ `.code` / `code.bin`。どれも無い場合や展開失敗時はエラー。
  - ファイル全体を workspace へコピーする。サイズ上限なし (赤猫団で約6MB)。
  - AI にはメタデータのみ返し、ファイル内容は返さない。
- `unmount(mount_id) -> { ok, incomplete, errors_sanitized }`
  - 公開 ID を即無効化し、逆順で cleanup する。残存は保持して次回 `unmount` /終了時に再解除する。

エラー応答の共通形: `{"error": <code>, "message_sanitized": <str>, "incomplete": <bool>}`。`message_sanitized` は共通 sanitizer 済み。

### MountSession 所有関係

```text
MountSession (mount_id)
├─ sd (handle_id=None)
├─ sdtitle (handle_id)
├─ ncch (handle_id)
└─ exefs (handle_id)
```

- `find_title` 成功時に `sdtitle → ncch → exefs` の順で確保し、`title_handle` に紐づけて保持する。
- 解除は `exefs → ncch → sdtitle → sd` の逆順で行う。
- 途中失敗時は確保済み分を逆順で破棄し、`title_handle` を発行しない。
- 公開レジストリ (`_SESSIONS`) とは別に解除失敗残存を `_RESIDUALS: list[InternalMount]` に保持する。`unmount` は当該セッション分に加えて所属残存の再解除を試みる。終了時は全残存を対象とする。

### 常駐プロセス契約 (実測準拠)

ninfs の各 mount CLI は FUSE の foreground 動作でブロックする。`communicate` 待ちはしない。

- 起動: 全 mount に `-f` を付け、`Popen(argv, stdout=PIPE|DEVNULL, stderr=PIPE, stdin=DEVNULL)`。継承しない。`Popen` オブジェクト自体が mount 本体である。
- stdout: 既定は DEVNULL へ捨てる (`ID0:` / `Key:` 行をどこにも残さない)。例外として ExeFS のみ有界 capture (上限64KB) し、許可トークン (`ExeFS: Done!` / `ExeFS: No decompression needed` / `ExeFS: Failed to decompress code:` の前缀) の走査だけに使い、全文は保持・記録しない。
- stderr: 起動直後から排出スレッドが有界バッファ (末尾64KB) へ送る。失敗時は sanitizer 適用後の要約のみ返す。
- 利用可能確認: `proc.poll() is None` かつ mount_point が非空かつ種別固有の中身が見えること。空dirの `listdir` 成功は成功扱いにしない。
- 解除: `terminate()` → `wait(timeout=10)` → 生存確認 (mount_point の列挙失敗 + `poll() is not None`)。Windows の `terminate()`/`kill()` は同一操作のため段階的エスカレーションはしない。残れば `_RESIDUALS` へ。
- MCP 終了時は `finally` を本体、`atexit` を補助として全残存に同手順を適用する。

### 段別 CLI 事実 (ninfs 2.0)

- `mount_sd --movable <f> --boot9 <b> --ro <Nintendo 3DS dir> <mp> -f`。read-only 指定が必要なのは `sd` (とnand系) のみ。他は `ro=True` 固定。
- `mount_sdtitle [--boot9 <b>] <tmdファイル> <mp> -f`。ディレクトリ指定だと先頭 `.tmd` を勝手に選ぶため、TMDファイルパスを明示する。
- TMDバージョンはヘッダ `0x9C` のu16BE。pyctr不要で読める。更新 > バージョン最大 > ファイル名辞書順先頭。
- `sdtitle` mount内は `/tmd.bin` と `/%04x.%id.ncch` (+同名dir)。実行コンテンツは `0000.*.ncch` を第一選択、無ければ最大サイズの `.ncch`。
- `mount_ncch [--boot9 <b>] <ncchファイル> <mp> -f` → `/exefs.bin`。
- `mount_exefs --decompress-code <exefs.bin> <mp> -f` → `code-decompressed.bin` (要展開時のみ生成) / `.code`。
- 展開成否の一次判定はエントリ有無 (構造判定)。`code-decompressed.bin` 無し + `.code` 有りの場合のみ stdout 許可トークンを補助参照し、`Failed` ならエラー、`No decompression needed` なら `.code` を採用する。
- WinFsp 前提確認は分離 subprocess (`mount_sd --help` の終了コード) で行う。`_common` の FUSE import 失敗は `SystemExit` のため、サーバー内 import では判定しない。
- SD 内探索: `<ID0>/<ID1>/title/00040000/<low8>/content/*.tmd` と `0004000E` 側を走査する。複数 ID1 は辞書順に走査し最初の完全ヒットを採用する。不完全な更新 (dir有・TMD無) は無視してベースへ fallback する。

## 4. データフロー

```text
detect_sd (単一候補 or ambiguous)
 ↓ Nintendo 3DS / movable.sed / boot9.bin の存在確認 (真偽のみ)
mount_sd (--ro, -f, mount_id発行, stdout破棄・stderr排出)
 ↓
find_title(mount_id, "000400000016C700")
 ↓ SD mount上でTMD列挙→更新優先解決→TMDファイル明示でSD Title mount→0000.ncch選択→NCCH mount→exefs.bin→ExeFS mount(--decompress-code)→title_handle発行
extract_code(title_handle, "akaneko/code.bin") (code-decompressed優先で全体コピー、親mkdir、tmp+replace)
 ↓
unmount(mount_id) (逆順cleanup, 残存は保持・再解除)
```

## 5. セキュリティ制約
- 全 mount は read-only 強制 (`sd` は `--ro` 指定、他は組込み固定)。SD への write API は作らない。
- `boot9.bin` / `movable.sed` の中身・SD復号キーを返さない。`ID0:` / `Key:` 行はログにも残さない。
- マスク対象は `policy.get_secrets()` に集約する (env パス + 選択 SD パス)。`sanitize` の既定 `secrets=()` のまま呼ばない。全経路 (ログ・MCP応答・stderr要約) に通す。公開応答で許可された `sd_root` / `dest_path` とエラー文中の秘密パスは区別する (後者はマスク)。
- コピー先は `NINFS_WORKSPACE` 配下のみ。二段構え: (a) 形式拒否 (`..` 成分、絶対パス、ドライブ指定 `C:...` / `C:foo`、ルート相対 `\foo`、UNC、デバイスパス `\\?\` `\\.\`、各成分の `:`・Windows予約デバイス名 (`NUL`/`CON`等)・末尾ドット/空白)、(b) 正規化後の包含確認 (`realpath` + `is_relative_to`)。`/` と `\` の混在自体は正常相対パスとして (b) で判定する。
- SD mount 上の TMD 読取り (`*.tmd` のバイト読み) は許可する。書込みは一切行わない。
- mount ごとに `mount_id` をサーバー発行する。呼び出し側指定は不可。
- MCP 終了時に残存 mount を cleanup する (`finally` 本体、`atexit` 補助)。
- 元 ROM / SD データの削除 API は作らない。
- 巨大 RomFS 全体を既定で AI へ読み込ませない。
- AI に任意コマンド実行は渡さない。公開するのは上記5ツールのみ。

## 6. エラーハンドリング
- 共通形 `{"error", "message_sanitized", "incomplete"}`。`incomplete=True` の場合のみ残存が `_RESIDUALS` にある。
- subprocess 失敗時は sanitizer 済み stderr 要約のみ返す。stdout (キー含む) は破棄する。
- 失敗した公開 ID は即無効化し、確保済み内部リソースは逆順 cleanup する。
- WinFsp 不在、SD 不在、Title 不在は検出順に早期リターンする。Title 不在時は子プロセスを起動しない。
- `detect_sd` 複数候補は `ambiguous` エラーとし、先頭の勝手採用はしない。
- 展開失敗時は `extract_code` をエラーとし、対象エントリ名と sanitizer 済み理由を返す。`.code` への黙示 fallback はしない。
- コピーは tmp 書込み + `os.replace` で原子化し、既存ファイルは上書きする。親は `mkdir(parents=True, exist_ok=True)`。失敗・キャンセル時は部分ファイルを残さず、handle は無効化する。
- タイムアウト時は `terminate()` → 10秒待機 → 生存確認 → 残存なら `_RESIDUALS` へ。

## 7. テスト
- `tests/test_policy.py`:
  1. 正常混在区切り (`akaneko/sub\code.bin`) の許容
  2. `..` 成分の形式拒否 (内側に収まる場合も拒否)
  3. workspace 外拒否 (絶対・ドライブ・`C:foo`・`\foo`・UNC・デバイスパス)
  4. 予約デバイス・`:` 成分・末尾ドット/空白の拒否
  5. symlink/junction 解決後の workspace 外拒否
  6. sanitizer (パス・キー・ID0) と `get_secrets` 集約 (既定空呼出しの禁止)
  7. read-only 強制 (write フラグを受け付けない)
- `tests/test_runner.py` (subprocess を mock、実機 mount なし):
  1. WinFsp 前提確認の分離 subprocess 化
  2. 正常系: 検出→mount (proc生存のまま返る) →更新優先解決→全体コピー (サイズ+SHA-256) →正常解除後のハンドル無効化
  3. `detect_sd` 4キー完全一致と `ambiguous`
  4. `-f`/`--ro`/DEVNULL/stdin=DEVNULL の起動条件検証
  5. readiness の偽陽性排除 (空dir・早期終了proc)
  6. TMD明示渡しと `0x9C` バージョン比較、NCCH `0000` 優先選択
  7. 展開: 構造判定優先、トークン補助、失敗時エラー
  8. 途中 mount 失敗時の逆順 cleanup (解除失敗後も継続)
  9. `unmount` の `sd` 末尾順と `incomplete` + `retry_residuals` 回収
  10. timeout 時の terminate→確認→残存化
- `tests/test_server.py` (runner は mock):
  1. 5ツール完全一致と各応答キー集合の完全一致
  2. runner内部型→公開dictの変換 (内部proc等の非露出)
  3. `dest_rel` の runner 到達前検証
  4. エラー写像の sanitizer 適用
  5. `NINFS_WORKSPACE` 起動検証・作成と終了時 cleanup
- 実機 mount の結合試験は手動とし、CI では回さない。

## 8. 検討した代替案
- B案 (層別構成): MVP では空抽象が残るため見送り。2タイトル目や RomFS 対応時に分割する。
- `extract_code` の 2MB + offset/size 案: 主要ユースケース (約6MB) が通らないため廃止した。
- stdout全文保持案: キー混入域のため不採用。許可トークン走査のみとする。

## 9. 次段階
- `writing-plans` の計画rev3で実装する
- 実装は Native で行い、各コミット後に `security-audit quick`、完了後に独立 Review を行う
