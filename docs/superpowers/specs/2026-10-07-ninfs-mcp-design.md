# ninfs-mcp 設計書 (MVP)

日付: 2026-10-07 / 改訂 2026-10-08 rev7 (レビューround5反映)
状態: 改訂済み設計 (A案・Windows特化MVP)
実行区分: Native (`Implementation → Review`)

## 1. 目的と成功基準

### 目的
「赤猫団の実行コードを解析できる状態にして」の一言で、SD検出から `.code` 抽出までを自律実行できる MCP サーバーを作る。

### 成功基準
- `000400000016c700` (ベース) と `0004000e0016c700` (更新) を検出し、完全な更新を優先して選択できる (IDは小文字正規化)
- ExeFS の展開済みコード (約6MB) を専用 workspace へ全体コピーできる
- 全 mount を後片付けできる (失敗時は残存を報告・再解除できる)
- `boot9.bin` / `movable.sed` の中身・SD復号キーを AI へ返さない

### 非目的 (MVP外)
- Ghidra 連携、3GX ビルド、RomFS 全体読み込み、HTTP 常駐、Linux/mac 対応
- 部分読み出し (offset/size 指定) は Ghidra 連携時に別途追加する
- 再起動をまたぐ残存復旧 (起動時sweepはプロセス内残存のみ対象)

### 未検証の後続事項
- 実機 Windows+WinFsp での結合試験 (計画 Task 6の手動チェックリストで実施する)

## 2. 前提 (合意済み・実測済み)

- OS: Windows + WinFsp 特化。ninfs は引数解析前に FUSE を import するため、`--help` の表示にも WinFsp が必要である。WinFsp不在時は live CLI 照合を Task 6 へ延期し、wheel展開物を参照資料とする。
- 接続: stdio (ローカル起動)
- ninfs 利用: 2.0 の CLI の subprocess ラッパー。import しない。起動は `sys.executable -m ninfs <kind>` に統一する。
- 依存pin: `ninfs==2.0`、`pyctr==0.7.6` (実測版)。
- 秘密ファイル: env のパスのみ (`NINFS_BOOT9_PATH`, `NINFS_MOVABLE_PATH`, 任意で `NINFS_SD_ROOT`)、中身非開示
- 作業場所: `NINFS_WORKSPACE` (必須の絶対パス。存在しなければ作成する)
- 実測根拠: ninfs 2.0 と pyctr 0.7.6 の wheel展開物。調査物は `graft/.cache/` 配下の throwaway。

## 3. 採用構成 (A案・最小)

```text
ninfs-mcp/
├─ src/ninfs_mcp/
│  ├─ server.py   # stdio MCP、5ツール公開。IDだけをworkerへ渡す
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
  - `sd_root` は CLI に渡す `Nintendo 3DS` ディレクトリのパスである。中身は返さない。存在真偽のみ。
  - 候補ゼロ件時は `{ error: not-found, incomplete: false }`。複数時は `ambiguous` (2キー。共通エラー形の文書化された例外)。
  - `candidates: list[str]`。`NINFS_SD_ROOT` 指定時はその単独検証になる。
- `mount_sd() -> { mount_id, mount_point }`
  - 引数なし。選択→重なり検査→staging作成の順。`mount_id` は成功時に発行する。
- `find_title(mount_id, title_id) -> { requested_title_id, resolved_title_id, kind, tmd_found, title_handle }`
  - `title_id` は16桁16進のみ受け付け、小文字に正規化して応答も小文字に統一する。上位は `00040000` / `0004000e` のみ対応。
  - `kind`: `base` (ベース解決) / `update` (更新解決)。`tmd_found`: TMDファイルの特定・解析に成功したこと。
  - base 指定時は完全な更新があれば更新優先、更新の明示指定時は更新を直接解決し base fallback はしない。
- `extract_code(title_handle, dest_rel) -> { dest_path, size, sha256, code_entry }`
  - `code-decompressed.bin` の存在を成功証拠とする。無ければエラー。
- `unmount(mount_id) -> { ok, incomplete, errors_sanitized }`
  - 公開 ID を即無効化し、当該セッション分に加えて全残存の再試行も行う。結果は `RetryReport` を集約する。削除済み ID の `unmount` は所属残存へルーティングし、無ければ `unknown-id` エラー (その際も全体sweepを行う)。

エラー応答の共通形: `{"error", "message_sanitized", "incomplete"}` (`ambiguous` のみ2キーの例外)。

### ID 状態遷移 (確定)

- `find_title` の入力検証失敗 → 何も無効化しない。同一セッションで再試行できる。
- `find_title` の子 mount 失敗 → handle 未発行。セッションは再利用できる。
- `extract_code` 失敗 → その handle を無効化する。セッションは再利用できる。
- `extract_code` 成功 → handle は再抽出に再利用できる。
- `unmount` → 当該セッション配下の全公開 ID を無効化する。

### MountSession 所有関係

```text
MountSession (mount_id, sd_root)
├─ sd (session_id=自身, handle_id=None)
├─ sdtitle (session_id, handle_id)
├─ ncch (session_id, handle_id)
└─ exefs (session_id, handle_id)
```

- staging は `.mounts/<mount_id>/<handle_id|sd>/<kind>`。内部 handle ID は起動成功時に確保し、公開登録は全段成功時に行う。
- 選択直後にエラー用文脈 (`ErrorContext`: secrets スナップショット) を確保し、セッション発行前の失敗も sanitizer 済み例外にする。残存 (`InternalMount`) も secrets スナップショットを保持する。
- 再試行結果は `RetryReport{recovered, remaining, errors_sanitized}` の構造で返し、`unmount` は一次結果と集約する。
- runner 操作は単一の `RLock` で直列化する。ID解決・レジストリ所属確認・操作は worker 内の同一 `_LOCK` 区間で行い、server は ID のみを渡す (解決済みオブジェクトの持越し禁止)。
- キャンセルは操作境界 (段間・replace直前) の協調チェックで受け付け、cleanup は shield して worker 終了を待つ。`os.replace` 前受理は既存保持、replace 後はコミット扱い。

### 常駐プロセス契約 (実測準拠)

- 起動: 全 mount に `-f` を付け、`sys.executable -m ninfs <kind>` を `Popen(argv, stdout=DEVNULL, stderr=PIPE, stdin=DEVNULL)`。継承しない。`Popen` オブジェクト自体が mount 本体である。
- mount先のリーフディレクトリは事前作成しない。WinFspが作成し、解除時に削除する (既存dirへのmountは `in use` で失敗する)。親チェーンのみ事前作成する。
- stdout は常に DEVNULL へ捨てる。stderr は起動直後から排出スレッドが有界バッファ (末尾64KB) へ送る。
- 利用可能確認: `proc.poll() is None` かつ種別固有の必須エントリが見えること (sd=`<mp>/<ID1>/` 1件以上、sdtitle=`tmd.bin`、ncch=`exefs.bin`、exefs=`code-decompressed.bin`/`code.bin`)。
- staging は作成前に既存祖先を解決し、管理領域 (`.mounts` 配下実体) に留まることを確認する。`.mounts` 自体が外部への junction 等の場合は mkdir・spawn ともにゼロで拒否する。
- 停止は冪等にする: 終了済み proc には `terminate`/`wait` を行わない。解除確認はリーフの消失で行うが、WinFspの削除は非同期のため猶予 (既定5秒) を見る。猶予内の消失は成功、残存は `_RESIDUALS` へ。
- 発行前失敗 (選択後・返却前) もまず即時 cleanup を試み、解除不能分だけ `_RESIDUALS` へ。公開IDがない残存は次回 `unmount`/起動時/終了時に回収する。
- MCP 終了時は `finally` を本体、`atexit` を補助として全残存に同手順を適用する。

### 段別 CLI 事実 (ninfs 2.0・pyctr 0.7.6)

- mount 上の探索起点は `<mp>/<ID1>/title/...` である。
- `mount_sd --movable <f> --boot9 <b> --ro <Nintendo 3DS dir> <mp> -f`。read-only 指定が必要なのは `sd` のみ。
- `mount_sdtitle [--boot9 <b>] <tmdファイル> <mp> -f`。TMDファイルパスを明示する。
- TMDバージョンは署名対応 `header+0x9C` のu16BE。未知署名・短小は除外する。
- 順位付け範囲: 完全な候補だけを抽出してから順位付けする。完全な更新群 (版降順→名昇順) の先頭。無ければ完全な base 群の先頭。
- 完全性 = TMDが解析可能 かつ実行コンテンツ (index 0) に対応する `.app` が存在し非空であること。
- `sdtitle` mount内は `/tmd.bin` と `/%04x.%id.ncch`。実行コンテンツは `0000.*.ncch` を第一選択、無ければ最大サイズの `.ncch`。
- `mount_ncch [--boot9 <b>] <ncchファイル> <mp> -f` → `/exefs.bin`。
- `mount_exefs --decompress-code <exefs.bin> <mp> -f`。成功時は `code-decompressed.bin` が必ず登録される。
- WinFsp 前提確認は分離 subprocess (`python -m ninfs sd --help` の終了コード) で行う。

## 4. データフロー

```text
detect_sd (ゼロ→not-found / 単一→採用 / 複数→ambiguous)
 ↓ 真偽のみ確認
mount_sd (選択→ErrorContext確保→重なり検査→staging祖先確認→staging作成→--ro -f起動→readiness→mount_id発行)
 ↓
find_title(mount_id, "000400000016c700")
 ↓ <mp>/<ID1>/title/...列挙→完全な候補を抽出→順位付け(更新優先/明示更新は直接)→TMDファイル明示でmount→0000.ncch→NCCH→exefs.bin→ExeFS(--decompress-code)→公開登録
extract_code(title_handle, "akaneko/code.bin") (存在確認→同dir tmp→hash→os.replace)
 ↓
unmount(mount_id) (逆順cleanup + 全残存再試行 + 集約報告)
```

## 5. セキュリティ制約
- 全 mount は read-only 強制。SD への write API は作らない。
- `boot9.bin` / `movable.sed` の中身・SD復号キーを返さない。
- マスク対象は `policy.get_secrets()` に集約する (env + runner保持の選択 SD パス + 残存スナップショット)。セッション発行前の失敗も ErrorContext から供給する。
- コピー先は `NINFS_WORKSPACE` 配下のみ。三段構え (形式拒否・包含確認・重なり拒否+秘密同一先拒否)。先頭成分 `.mounts` は予約除外する。
- staging 作成前に既存祖先を解決し、管理領域外なら拒否する。
- SD mount 上の TMD 読取りは許可する。書込みは一切行わない。
- mount ごとに `mount_id` をサーバー発行する。呼び出し側指定は不可。
- MCP 終了時に残存 mount を cleanup する (`finally` 本体、`atexit` 補助)。
- 元 ROM / SD データの削除 API は作らない。
- 巨大 RomFS 全体を既定で AI へ読み込ませない。
- AI に任意コマンド実行は渡さない。公開するのは上記5ツールのみ。

## 6. エラーハンドリング
- 共通形 `{"error", "message_sanitized", "incomplete"}` (`ambiguous` のみ例外)。
- subprocess 失敗時は sanitizer 済み stderr 要約のみ返す。
- 失敗した公開 ID は状態遷移表に従って無効化する。
- WinFsp 不在、SD 不在、Title 不在は検出順に早期リターンする。Title 不在時は子プロセスを起動しない。
- `detect_sd` 複数候補は `ambiguous`、ゼロ件は `not-found`。
- 展開失敗・`code-decompressed.bin` 不在時はエラーとする。
- コピーは同親dirの一意な tmp へ書込み→hash→`os.replace` で原子化し、既存ファイルは上書きする。親は `mkdir(parents=True, exist_ok=True)`。失敗・キャンセル時は tmp を削除し、既存完成ファイルを残し、handle を無効化する。
- タイムアウト時は `terminate()` → 10秒待機 → 生存確認 → 残存なら `_RESIDUALS` へ。

## 7. テスト
- `tests/test_policy.py` (10件): rev5と同一。順序試験 (選択→検査→作成) は Task 2 の runner 試験に配置する。
- `tests/test_runner.py` (mock): rev5の全項目 + 追加 (override/ゼロ/単一/複数候補、完全性優先の順位付けと最高版不完全ケース、多handle共存と失敗隔離、発行前失敗の即時cleanupとゼロ残存、削除済みID再試行のマスク、無関係残存を含む `RetryReport` 集約、staging祖先検査、古い参照と `unmount` の競合)。
- `tests/test_server.py` (mock runner + 実runner/mock-subprocess の取消試験): 5ツール・キー集合・変換の非露出・到達前検証・写像マスク・workspace起動・終了cleanup・MCPキャンセル通知と replace 境界。
- 実機 mount の結合試験は手動 (Task 6) とし、CI では回さない。

## 8. 検討した代替案
- B案 (層別構成): MVP では空抽象が残るため見送り。
- `extract_code` の 2MB + offset/size 案: 廃止済み。
- stdout許可トークン走査案: 廃止済み。
- 裸 `mount_*` 起動案: 同一Python環境を保証できないため不採用。

## 9. 次段階
- 計画rev6で実装する (Task 1〜5 + 手動 Task 6)
- 実装は Native で行い、各コミット後に `security-audit quick`、完了後に独立 Review を行う
