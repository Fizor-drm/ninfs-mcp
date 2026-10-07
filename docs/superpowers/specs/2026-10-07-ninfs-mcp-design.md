# ninfs-mcp 設計書 (MVP)

日付: 2026-10-07 / 改訂 2026-10-08 rev6 (レビューround4反映)
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

### 未検証の後続事項
- 実機 Windows+WinFsp での結合試験 (計画 Task 6の手動チェックリストで実施する)

## 2. 前提 (合意済み・実測済み)

- OS: Windows + WinFsp 特化
- 接続: stdio (ローカル起動)
- ninfs 利用: 2.0 の CLI の subprocess ラッパー。import しない。同一Python環境保証のため、起動は裸の `mount_*` ではなく `sys.executable -m ninfs <kind>` に統一する (`__main__.py` が kind dispatch する実測済み)。
- 秘密ファイル: env のパスのみ (`NINFS_BOOT9_PATH`, `NINFS_MOVABLE_PATH`, 任意で `NINFS_SD_ROOT`)、中身非開示
- 作業場所: `NINFS_WORKSPACE` (必須の絶対パス。存在しなければ作成する)
- 依存バージョンは `pyproject.toml` に pin し、採用版の CLI 引数と出力名を実装時に照合する
- 実測根拠: ninfs 2.0 と pyctr 0.7.6 の wheel展開物。調査物は `graft/.cache/` 配下の throwaway。

## 3. 採用構成 (A案・最小)

```text
ninfs-mcp/
├─ src/ninfs_mcp/
│  ├─ server.py   # stdio MCP、5ツール公開。runner呼出しはワーカースレッド経由
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
  - 中身は返さない。存在真偽のみ。状態を持たない照会のため runner でも dict を返す。
- `mount_sd() -> { mount_id, mount_point }`
  - 引数なし。`mount_id` はサーバー側で発行する。選択後に重なり検査を行い、通過後に staging を作成する。
- `find_title(mount_id, title_id) -> { requested_title_id, resolved_title_id, kind, tmd_found, title_handle }`
  - `title_id` は16桁16進のみ受け付け、小文字に正規化して応答も小文字に統一する。上位は `00040000` / `0004000e` のみ対応 (他は `unsupported-category`)。base 指定時は完全な更新があれば更新優先、更新の明示指定時は更新を直接解決し base fallback はしない。
  - `title_handle` は opaque ID。実パスや内部 mount 構造を露出させない。
- `extract_code(title_handle, dest_rel) -> { dest_path, size, sha256, code_entry }`
  - `code-decompressed.bin` の存在を成功証拠とする。無ければエラー。
  - ファイル全体を workspace へコピーする。サイズ上限なし。
  - AI にはメタデータのみ返し、ファイル内容は返さない。
- `unmount(mount_id) -> { ok, incomplete, errors_sanitized }`
  - 公開 ID を即無効化し、当該セッション分に加えて全残存の再試行も行う。削除済み ID の `unmount` は所属残存へルーティングし、無ければ `unknown-id` エラー。

エラー応答の共通形: `{"error": <code>, "message_sanitized": <str>, "incomplete": <bool>}`。

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

- staging は `.mounts/<mount_id>/<handle_id|sd>/<kind>` とし、複数 `find_title` の常駐を共存させる。
- 途中失敗・timeout・readiness失敗の確保済み分は逆順破棄し、破棄不能分は `_RESIDUALS` へ。`title_handle` は成功時のみ発行する。
- 残存 (`InternalMount`) は `session_id` と secrets スナップショット (選択 SD パス等) を保持する。セッション削除後の再試行エラーも sanitizer 済みで返す。
- runner 操作は単一の `RLock` で直列化する (MVP)。キャンセルは操作境界で協調的に受け付け、in-flight 操作は安全点まで実行する。`os.replace` 前受理のキャンセルは既存ファイルを保持し、replace 後はコミット済みとして扱う。server は runner 呼出しをワーカースレッド (`anyio.to_thread`) 経由で行い、イベントループを塞がない。

### 常駐プロセス契約 (実測準拠)

- 起動: 全 mount に `-f` を付け、`sys.executable -m ninfs <kind>` を `Popen(argv, stdout=DEVNULL, stderr=PIPE, stdin=DEVNULL)`。継承しない。
- stdout は常に DEVNULL へ捨てる。stderr は起動直後から排出スレッドが有界バッファ (末尾64KB) へ送る。
- 利用可能確認: `proc.poll() is None` かつ種別固有の必須エントリが見えること。
  - sd: `<mp>/<ID1>/` が1件以上列挙できること
  - sdtitle: `<mp>/tmd.bin` が存在すること
  - ncch: `<mp>/exefs.bin` が存在すること
  - exefs: `<mp>/code-decompressed.bin` または `<mp>/code.bin` が存在すること
- 停止は冪等にする: 終了済み proc には `terminate`/`wait` を行わない。ただし staging 確認 (空に戻ったこと) は必ず行い、非空・列挙不能は残存とする。稼働中は `terminate()` → `wait(timeout=10)` → proc終了 + staging空の確認。エスカレーションはしない。
- MCP 終了時は `finally` を本体、`atexit` を補助として全残存に同手順を適用する。

### 段別 CLI 事実 (ninfs 2.0・pyctr 0.7.6)

- 生SD階層と復号 mount 階層を区別する。mount 上の探索起点は `<mp>/<ID1>/title/...` である。
- `mount_sd --movable <f> --boot9 <b> --ro <Nintendo 3DS dir> <mp> -f`。read-only 指定が必要なのは `sd` のみ。他は `ro=True` 固定。
- `mount_sdtitle [--boot9 <b>] <tmdファイル> <mp> -f`。TMDファイルパスを明示する。
- TMDバージョンは署名種別→署名→paddingを読み飛ばしたヘッダ先頭 `+0x9C` のu16BE。未知署名種別・短小ファイルはその TMD を除外する。
- 更新の完全性 = TMDが解析可能 かつ実行コンテンツ (index 0 のレコード) に対応する `.app` が存在し非空であること。不完全な更新は無視して base へ fallback する。
- `sdtitle` mount内は `/tmd.bin` と `/%04x.%id.ncch` (+同名dir)。実行コンテンツは `0000.*.ncch` を第一選択、無ければ最大サイズの `.ncch`。
- `mount_ncch [--boot9 <b>] <ncchファイル> <mp> -f` → `/exefs.bin`。
- `mount_exefs --decompress-code <exefs.bin> <mp> -f`。成功時は `code-decompressed.bin` が必ず登録される。
- コピー対象は `code-decompressed.bin` の存在をもって成功証拠とする。不在時はエラー。
- WinFsp 前提確認は分離 subprocess (`python -m ninfs sd --help` の終了コード) で行う。

## 4. データフロー

```text
detect_sd (単一候補 or ambiguous)
 ↓ Nintendo 3DS / movable.sed / boot9.bin の存在確認 (真偽のみ)
mount_sd (選択→重なり検査→staging作成→--ro -f起動→readiness→mount_id発行)
 ↓
find_title(mount_id, "000400000016c700")
 ↓ <mp>/<ID1>/title/00040000/<low>/content/*.tmd と0004000e側を列挙→完全性検査→解決(更新優先/明示更新は直接)→TMDファイル明示でSD Title mount→0000.ncch選択→NCCH mount→exefs.bin→ExeFS mount(--decompress-code)→title_handle発行
extract_code(title_handle, "akaneko/code.bin") (code-decompressed.bin存在確認→同dir tmp→hash→os.replace)
 ↓
unmount(mount_id) (逆順cleanup + 全残存再試行)
```

## 5. セキュリティ制約
- 全 mount は read-only 強制 (`sd` は `--ro` 指定、他は組込み固定)。SD への write API は作らない。
- `boot9.bin` / `movable.sed` の中身・SD復号キーを返さない。`ID0:` / `Key:` 行はログにも残さない。
- マスク対象は `policy.get_secrets()` に集約する (env パス + runnerが保持する選択 SD パス)。runner は残存にも secrets スナップショットを持たせ、セッション削除後のエラーもマスクする。`sanitize` の secrets は必須引数とする。
- コピー先は `NINFS_WORKSPACE` 配下のみ。三段構え: (a) 形式拒否 (`..` 成分、絶対パス、ドライブ指定、`C:foo`、ルート相対、UNC、デバイスパス、各成分の `:`・Windows予約デバイス名・末尾ドット/空白、先頭成分 `.mounts` の予約除外)、(b) 正規化後の包含確認、(c) workspace/SD重なり拒否と秘密ファイル同一先の拒否。`/` と `\` の混在自体は (b) で判定する。
- 重なり検査 `check_no_overlap(workspace, sd_root)` は明示入力で、選択後・staging 作成前に実行する。
- SD mount 上の TMD 読取りは許可する。書込みは一切行わない。
- mount ごとに `mount_id` をサーバー発行する。呼び出し側指定は不可。
- MCP 終了時に残存 mount を cleanup する (`finally` 本体、`atexit` 補助)。
- 元 ROM / SD データの削除 API は作らない。
- 巨大 RomFS 全体を既定で AI へ読み込ませない。
- AI に任意コマンド実行は渡さない。公開するのは上記5ツールのみ。

## 6. エラーハンドリング
- 共通形 `{"error", "message_sanitized", "incomplete"}`。
- subprocess 失敗時は sanitizer 済み stderr 要約のみ返す。stdout は破棄する。
- 失敗した公開 ID は状態遷移表に従って無効化し、確保済み内部リソースは逆順 cleanup する。
- WinFsp 不在、SD 不在、Title 不在は検出順に早期リターンする。Title 不在時は子プロセスを起動しない。
- `detect_sd` 複数候補は `ambiguous` エラーとし、先頭の勝手採用はしない。
- 展開失敗・`code-decompressed.bin` 不在時はエラーとする。`code.bin` への黙示 fallback はしない。
- コピーは同親dirの一意な tmp へ書込み→hash→`os.replace` で原子化し、既存ファイルは上書きする。親は `mkdir(parents=True, exist_ok=True)`。失敗・キャンセル時は tmp を削除し、既存完成ファイルを残し、handle を無効化する。
- タイムアウト時は `terminate()` → 10秒待機 → 生存確認 → 残存なら `_RESIDUALS` へ。

## 7. テスト
- `tests/test_policy.py` (10件):
  1. 正常混在区切りの許容
  2. `..` 成分の形式拒否 (内側に収まる場合も拒否)
  3. workspace 外拒否 (絶対・ドライブ・`C:foo`・`\foo`・UNC・デバイスパス)
  4. 予約デバイス・`:` 成分・末尾ドット/空白・`.mounts` 先頭成分の拒否
  5. symlink/junction 解決後の workspace 外拒否
  6. workspace/SD重なり拒否と秘密ファイル同一先の拒否 (`check_no_overlap` + 解決時検査)
  7. `get_secrets` 集約と sanitizer
  8. read-only 強制
  9. Task 1 追加分: SDパス入力付きの重なり検査 (選択後・作成前の順序)
  10. Task 1 追加分: `.mounts` の直接指定とリンク経由指定の拒否
- `tests/test_runner.py` (subprocess を mock、実機 mount なし): round4指摘の全項目 (同一Python起動・種別readiness・署名対応TMD・`<ID1>`起点・完全性・NCCH選択・存在規則・逆順継続・`sd`末尾・削除済みID routing・`retry_residuals`・冪等停止・ロック直列化・ID状態遷移・明示更新の3状態)。
- `tests/test_server.py` (runner は mock): 5ツール・キー集合・変換の非露出・到達前検証・写像マスク・workspace起動・終了cleanup・MCPキャンセル通知 (mock runner で replace 前後の境界確認)。
- 実機 mount の結合試験は手動 (Task 6) とし、CI では回さない。

## 8. 検討した代替案
- B案 (層別構成): MVP では空抽象が残るため見送り。
- `extract_code` の 2MB + offset/size 案: 主要ユースケースが通らないため廃止した。
- stdout許可トークン走査案: エントリ存在規則で代替できるため廃止した。
- 裸 `mount_*` 起動案: 同一Python環境を保証できないため、`sys.executable -m ninfs` に統一した。

## 9. 次段階
- 計画rev5で実装する (Task 1〜5 + 手動 Task 6)
- 実装は Native で行い、各コミット後に `security-audit quick`、完了後に独立 Review を行う
