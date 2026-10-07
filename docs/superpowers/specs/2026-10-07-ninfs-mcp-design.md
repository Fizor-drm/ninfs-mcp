# ninfs-mcp 設計書 (MVP)

日付: 2026-10-07 / 改訂 2026-10-08 rev5 (再レビュー2反映・実測裏取り済み)
状態: 改訂済み設計 (A案・Windows特化MVP)
実行区分: Native (`Implementation → Review`)

## 1. 目的と成功基準

### 目的
「赤猫団の実行コードを解析できる状態にして」の一言で、SD検出から `.code` 抽出までを自律実行できる MCP サーバーを作る。

### 成功基準
- `000400000016C700` (ベース) と `0004000E0016C700` (更新) を検出し、完全な更新を優先して選択できる
- ExeFS の展開済みコード (約6MB) を専用 workspace へ全体コピーできる
- 全 mount を後片付けできる (失敗時は残存を報告・再解除できる)
- `boot9.bin` / `movable.sed` の中身・SD復号キーを AI へ返さない

### 非目的 (MVP外)
- Ghidra 連携、3GX ビルド、RomFS 全体読み込み、HTTP 常駐、Linux/mac 対応
- 部分読み出し (offset/size 指定) は Ghidra 連携時に別途追加する

### 未検証の後続事項
- 実機 Windows+WinFsp での結合試験 (計画 Task 5の手動チェックリストで実施する)

## 2. 前提 (合意済み・実測済み)

- OS: Windows + WinFsp 特化
- 接続: stdio (ローカル起動)
- ninfs 利用: 2.0 の CLI entry point の subprocess ラッパー (`mount_sd`, `mount_sdtitle`, `mount_ncch`, `mount_exefs`)。import しない。pyctr は TMD構造の参照資料とし、import しない。
- 秘密ファイル: env のパスのみ (`NINFS_BOOT9_PATH`, `NINFS_MOVABLE_PATH`, 任意で `NINFS_SD_ROOT`)、中身非開示
- 作業場所: `NINFS_WORKSPACE` (必須の絶対パス。存在しなければ作成する)
- 依存バージョンは `pyproject.toml` に pin し、採用版の CLI 引数と出力名を実装時に照合する
- 実測根拠: ninfs 2.0 と pyctr 0.7.6 の wheel展開物 (`mount/*.py`, `pyctr/type/{tmd,exefs}.py`)。調査物は `graft/.cache/` 配下の throwaway。

## 3. 採用構成 (A案・最小)

```text
ninfs-mcp/
├─ src/ninfs_mcp/
│  ├─ server.py   # stdio MCP、5ツール公開。runnerの内部型を公開dictへ変換する
│  ├─ runner.py   # ninfs CLI の常駐プロセス管理・MountSession 所有。公開dictは返さない
│  └─ policy.py   # read-only強制・workspace制限・sanitizer・secrets集約
├─ tests/
│  ├─ test_policy.py
│  ├─ test_runner.py
│  └─ test_server.py
└─ pyproject.toml
```

ツールは5個に固定する:

- `detect_sd() -> { sd_root, has_n3ds_dir, has_boot9, has_movable } | { error: ambiguous, candidates }`
  - 中身は返さない。存在真偽のみ。状態を持たない照会のため runner でも dict を返す (例外扱い。計画に明記)。
- `mount_sd() -> { mount_id, mount_point }`
  - 引数なし。`mount_id` はサーバー側で発行する。呼び出し側指定は受け付けない。
- `find_title(mount_id, title_id) -> { requested_title_id, resolved_title_id, kind, tmd_found, title_handle }`
  - `title_id` は16桁16進のみ受け付け、小文字に正規化して応答も小文字に統一する。上位8桁は `00040000` のみ対応 (他は `unsupported-category` エラー)。更新IDの明示指定時は更新を直接解決し、base への fallback はしない。
  - SD mount 上で選択してから子 mount を起動する。不完全な更新は無視して base へ fallback する。
  - `title_handle` は opaque ID。実パスや内部 mount 構造を露出させない。
- `extract_code(title_handle, dest_rel) -> { dest_path, size, sha256, code_entry }`
  - `code-decompressed.bin` の存在を成功証拠とする。無ければエラー (`.code`/`code.bin` への黙示 fallback はしない)。
  - ファイル全体を workspace へコピーする。サイズ上限なし。
  - AI にはメタデータのみ返し、ファイル内容は返さない。
- `unmount(mount_id) -> { ok, incomplete, errors_sanitized }`
  - 公開 ID を即無効化し、逆順で cleanup する。残存は保持して次回 `unmount` /終了時に再解除する。削除済み ID の `unmount` は残存があればそこへルーティングし、無ければ `unknown-id` エラー。

エラー応答の共通形: `{"error": <code>, "message_sanitized": <str>, "incomplete": <bool>}`。

### MountSession 所有関係

```text
MountSession (mount_id)
├─ sd (session_id=自身, handle_id=None)
├─ sdtitle (session_id, handle_id)
├─ ncch (session_id, handle_id)
└─ exefs (session_id, handle_id)
```

- `InternalMount` は所属 `session_id` を保持する。`find_title` 成功時に `sdtitle → ncch → exefs` の順で確保する。
- 解除は `exefs → ncch → sdtitle → sd` の逆順で行う。
- 途中失敗・timeout・readiness失敗の確保済み分は逆順破棄し、破棄不能分は `_RESIDUALS` へ。`title_handle` は成功時のみ発行する。
- 公開レジストリ (`_SESSIONS`) とは別に解除失敗残存を `_RESIDUALS: list[InternalMount]` に保持する。`retry_residuals()` で再試行し回収数を返す。
- runner 操作は単一の `RLock` で直列化する (MVP)。`unmount` と `find_title`/コピーの競合はロックで排除し、キャンセル時は安全点まで実行してから ID 無効化・残存化する。

### 常駐プロセス契約 (実測準拠)

- 起動: 全 mount に `-f` を付け、`Popen(argv, stdout=DEVNULL, stderr=PIPE, stdin=DEVNULL)`。継承しない。`Popen` オブジェクト自体が mount 本体である。
- stdout は常に DEVNULL へ捨てる。ExeFS の成否はエントリ有無で判定するため stdout 走査は行わない。
- stderr は起動直後から排出スレッドが有界バッファ (末尾64KB) へ送る。失敗時は sanitizer 適用後の要約のみ返す。
- 利用可能確認: `proc.poll() is None` かつ種別固有の必須エントリが見えること。汎用の非空判定は使わない。
  - sd: `<mp>/<ID1>/` が1件以上列挙できること
  - sdtitle: `<mp>/tmd.bin` が存在すること
  - ncch: `<mp>/exefs.bin` が存在すること
  - exefs: `<mp>/code-decompressed.bin` または `<mp>/code.bin` が存在すること
- mount_point は staging (`NINFS_WORKSPACE/.mounts/<mount_id>/<kind>`) に空dirとして確保する。解除成功 = proc 終了 + staging が空に戻ること。空dirの列挙成功を失敗扱いにしない。
- 停止は冪等にする: 終了済み proc への `terminate` は何もしない。停止手順は `terminate()` → `wait(timeout=10)` → proc終了 + staging空の確認。Windows の `terminate()`/`kill()` は同一操作のためエスカレーションはしない。
- MCP 終了時は `finally` を本体、`atexit` を補助として全残存に同手順を適用する。

### 段別 CLI 事実 (ninfs 2.0・pyctr 0.7.6)

- 生SD階層と復号 mount 階層を区別する。`mount_sd` の root は `<Nintendo 3DS>/<ID0>` (`sd.py`) のため、mount 上の探索起点は `<mp>/<ID1>/title/...` である。`<ID0>` を含めない。
- `mount_sd --movable <f> --boot9 <b> --ro <Nintendo 3DS dir> <mp> -f`。read-only 指定が必要なのは `sd` のみ。他は `ro=True` 固定。
- `mount_sdtitle [--boot9 <b>] <tmdファイル> <mp> -f`。ディレクトリ指定だと先頭 `.tmd` を勝手に選ぶため、TMDファイルパスを明示する。
- TMDバージョンは署名種別→署名→paddingを読み飛ばしたヘッダ先頭 `+0x9C` のu16BE。通常RSA2048ではファイル `0x1DC`。先頭固定オフセット読みはしない。未知署名種別・短小ファイルはその TMD を除外する。
- 更新の完全性 = TMDが解析可能 かつ `content/` が非空であること。不完全な更新は無視して base へ fallback する。
- `sdtitle` mount内は `/tmd.bin` と `/%04x.%id.ncch` (+同名dir)。実行コンテンツは `0000.*.ncch` を第一選択、無ければ最大サイズの `.ncch`。
- `mount_ncch [--boot9 <b>] <ncchファイル> <mp> -f` → `/exefs.bin`。
- `mount_exefs --decompress-code <exefs.bin> <mp> -f`。成功時は `code-decompressed.bin` が必ず登録される (未圧縮時も `.code` の別名として登録される。pyctr `decompress_code`)。表示名規則 (`/` + 先頭`.`除去 + `.bin`) により `.code` は `code.bin` として現れる。
- コピー対象は `code-decompressed.bin` の存在をもって成功証拠とする。不在時はエラー。
- WinFsp 前提確認は分離 subprocess (`mount_sd --help` の終了コード) で行う。サーバー内で `ninfs` を import しない。

## 4. データフロー

```text
detect_sd (単一候補 or ambiguous)
 ↓ Nintendo 3DS / movable.sed / boot9.bin の存在確認 (真偽のみ)
mount_sd (--ro, -f, mount_id発行, stdout破棄・stderr排出)
 ↓
find_title(mount_id, "000400000016c700")
 ↓ <mp>/<ID1>/title/00040000/<low>/content/*.tmd と0004000E側を列挙→完全性検査→更新優先解決(0x9C比較)→TMDファイル明示でSD Title mount→0000.ncch選択→NCCH mount→exefs.bin→ExeFS mount(--decompress-code)→title_handle発行
extract_code(title_handle, "akaneko/code.bin") (code-decompressed.bin存在確認→tmp書込→hash→os.replace)
 ↓
unmount(mount_id) (逆順cleanup, 残存は保持・再解除)
```

## 5. セキュリティ制約
- 全 mount は read-only 強制 (`sd` は `--ro` 指定、他は組込み固定)。SD への write API は作らない。
- `boot9.bin` / `movable.sed` の中身・SD復号キーを返さない。`ID0:` / `Key:` 行はログにも残さない。
- マスク対象は `policy.get_secrets()` に集約する (env パス + runnerが保持する選択 SD パス)。runner は選択パスをセッションに保持し、ID無効化後のエラーを含む全 sanitizer 呼出しへ渡す。`sanitize` の secrets は必須引数とする。
- コピー先は `NINFS_WORKSPACE` 配下のみ。二段構え: (a) 形式拒否 (`..` 成分、絶対パス、ドライブ指定、`C:foo`、ルート相対、UNC、デバイスパス、各成分の `:`・Windows予約デバイス名・末尾ドット/空白)、(b) 正規化後の包含確認。`/` と `\` の混在自体は (b) で判定する。
- workspace と選択 SD 領域の重なりは拒否する。解決済みコピー先が秘密入力ファイルと同一の場合も拒否する (上書き規則の例外)。
- SD mount 上の TMD 読取り (先頭部のバイト読み) は許可する。書込みは一切行わない。
- mount ごとに `mount_id` をサーバー発行する。呼び出し側指定は不可。
- MCP 終了時に残存 mount を cleanup する (`finally` 本体、`atexit` 補助)。
- 元 ROM / SD データの削除 API は作らない。
- 巨大 RomFS 全体を既定で AI へ読み込ませない。
- AI に任意コマンド実行は渡さない。公開するのは上記5ツールのみ。

## 6. エラーハンドリング
- 共通形 `{"error", "message_sanitized", "incomplete"}`。
- subprocess 失敗時は sanitizer 済み stderr 要約のみ返す。stdout は破棄する。
- 失敗した公開 ID は即無効化し、確保済み内部リソースは逆順 cleanup する。`find_title`/`extract_code` の失敗も `incomplete` を含む内部エラー情報で伝える。
- WinFsp 不在、SD 不在、Title 不在は検出順に早期リターンする。Title 不在時は子プロセスを起動しない。
- `detect_sd` 複数候補は `ambiguous` エラーとし、先頭の勝手採用はしない。
- 展開失敗・`code-decompressed.bin` 不在時は `extract_code` をエラーとする。`code.bin` への黙示 fallback はしない。
- コピーは同親dirの一意な tmp へ書込み→hash→`os.replace` で原子化し、既存ファイルは上書きする。親は `mkdir(parents=True, exist_ok=True)`。失敗・キャンセル時は tmp を削除し、既存完成ファイルを残し、handle を無効化する。
- タイムアウト時は `terminate()` → 10秒待機 → 生存確認 → 残存なら `_RESIDUALS` へ。

## 7. テスト
- `tests/test_policy.py`:
  1. 正常混在区切りの許容
  2. `..` 成分の形式拒否 (内側に収まる場合も拒否)
  3. workspace 外拒否 (絶対・ドライブ・`C:foo`・`\foo`・UNC・デバイスパス)
  4. 予約デバイス・`:` 成分・末尾ドット/空白の拒否
  5. symlink/junction 解決後の workspace 外拒否
  6. workspace/SD重なり拒否と秘密ファイル同一先の拒否
  7. `get_secrets` 集約と sanitizer
  8. read-only 強制
- `tests/test_runner.py` (subprocess を mock、実機 mount なし):
  1. WinFsp 前提確認の分離 subprocess 化
  2. 正常系: 検出→mount (proc生存のまま返る) →更新優先解決→全体コピー (サイズ+SHA-256) →正常解除後のハンドル無効化
  3. `detect_sd` 4キー完全一致と `ambiguous`
  4. `-f`/`--ro`/DEVNULL の起動条件検証
  5. readiness の種別固有エントリ検証 (空dir・早期終了・必要エントリ欠如の失敗)
  6. TMD明示渡しと署名対応 `0x9C` 比較 (署名付きfixture・未知署名除外)
  7. `<ID1>` 起点の探索と base/update の完全性 fallback
  8. NCCH `0000` 優先選択と `code-decompressed.bin` 存在規則
  9. 途中 mount 失敗時の逆順 cleanup (解除失敗後も継続)
  10. `unmount` の `sd` 末尾順・`incomplete`・削除済みIDの残存ルーティング・`retry_residuals` 回収
  11. timeout 時の terminate→確認→残存化
  12. ロック直列化の競合試験
- `tests/test_server.py` (runner は mock):
  1. 5ツール完全一致と各応答キー集合の完全一致
  2. 内部型→公開dict変換 (内部情報の非露出)
  3. `dest_rel` の runner 到達前検証
  4. エラー写像の sanitizer 適用
  5. `NINFS_WORKSPACE` 起動検証・作成と終了時 cleanup
- 実機 mount の結合試験は手動 (Task 5) とし、CI では回さない。

## 8. 検討した代替案
- B案 (層別構成): MVP では空抽象が残るため見送り。
- `extract_code` の 2MB + offset/size 案: 主要ユースケースが通らないため廃止した。
- stdout許可トークン走査案: エントリ存在規則で代替できるため廃止した。stdout は常に破棄する。

## 9. 次段階
- 計画rev4で実装する (Task 1〜4 + 手動 Task 5)
- 実装は Native で行い、各コミット後に `security-audit quick`、完了後に独立 Review を行う
