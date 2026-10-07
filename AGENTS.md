## Agent selection

- 現行コード、設定、Source of Truth、実行結果を推測より優先する。
- 不要な Agent は起動せず、タスクを完了できる最小構成を選ぶ。
- T3 子エージェントでは `provider` / `model` を明示し、指定可能なら `reasoning_effort` も明示する。親設定を暗黙継承しない。
- 委任前に T3 の利用可能モデルを確認する。

### Analysis agent

- 設定: `antigravity` / `gemini-3.8-flash-high`
- 原因不明の不具合、複数ファイル、既存設計・影響範囲の調査が必要な場合に使用する。
- 関連コード、依存関係、変更範囲、制約、テスト観点を整理する。
- 事実と推測を区別し、原則読み取り専用。

### Implementation agent

- 設定: `opencode` / `opencode/muse-spark-1.3-contributor-free` / `xhigh`
- `superpowers:test-driven-development` に従い、必要なテスト作成、実装、検証まで担当する。
- 既存テストで十分なら不要なテストを追加しない。
- 最小変更とし、依頼範囲外の修正、不要な抽象化、既存変更の巻き戻しを行わない。
- 完了前に関連テストと必要な lint / typecheck / build を実行する。

### Review agent

- 設定: `codex` / `gpt-6.1-sol` / `high`
- `superpowers:requesting-code-review` に従う。
- 実際の diff、関連コード、テスト・検証結果を確認する。
- 原則読み取り専用。
- 問題は可能なら元の Implementation agent に修正させ、修正後は別 Agent が再レビューする。

### Consultation agent

- 設定: `codex` / `gpt-6.1-sol` / `max`
- Analysis でも判断が難しい設計、重大なトレードオフ、意見対立がある場合のみ使用する。
- 原則読み取り専用。

## Execution mode

- `Native`: 親が直接実装し、完了後に独立 Agent が全体レビューする。小規模・明確な変更や、計画で十分な文脈が保持されている場合に優先する。
- `Subagent-driven`: `superpowers:subagent-driven-development` を使い、Task 単位で委任・レビューする。複数 Task や独立性の価値が高い場合に使用する。
- T3 で指定モデルを利用できる場合は上記 Agent routing を使う。
- general subagent しか使えない環境では、指定モデルで実行したものとして扱わない。

## Flow

- 通常: `Implementation → Review`
- 調査あり: `Analysis → Implementation → Review`
- 難しい設計判断: `Analysis → Consultation → Implementation → Review`
- Review NG: `Implementation fix → Independent Review`

## Operation rules

- Agent 間には必要な文脈と結果だけを渡す。
- 完了通知で再開し、sleep 等でポーリングしない。
- 指定モデルを利用できない場合、別設定へ勝手に変更しない。
- 検証していない作業を成功扱いしない。
- Implementation と Review は分離する。
- 各実装コミット後に `security-audit quick` を実行し、`confirmed` を解消し `needs_validation` を確認する。
- リリース前は `security-audit` の全体監査を行う。