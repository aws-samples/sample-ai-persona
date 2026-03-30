# Notes: Agentモード議論のコンテキスト管理改善

Guidelines:
- **Purpose**: Record sufficient information so that:
  - Future sessions can understand the full context
  - Knowledge notes and learning materials can be generated from this log
  - Issues can be traced and verified through the timeline
- **What to capture**:
  - **Facts**: What happened, what was tried, results and errors
  - **Decisions**: Why this approach, what alternatives were considered
  - **Impressions**: Concerns, surprises, discoveries, evaluations
- **When to record**: After each meaningful unit of work
- **Append-only**: Never edit or delete existing content

## Log

### [2026-03-30 16:06] 調査・設計完了
- 問題: Strands Agent内部の `agent.messages` がラウンドごとに蓄積、後半ラウンドで15K-25K+トークン
- 3つのアプローチを検討: 階層的要約、構造化コンテキスト、ハイブリッド
- 事例調査: Anthropic、MemGPT/Letta、adidas PPC等で裏付けあり
- 決定: アプローチ1（階層的要約）を採用
  - メタ要約なし（全ラウンド要約を持ち回し、重要コンテキスト抜け落ち防止）
  - Strands履歴は `agent.messages.clear()` でリセット（listなので安全に操作可能）
  - 直近3件はラウンドをまたいで `all_messages[-3:]`（ファシリテータ含む）
- `agent.messages` が通常のPython listであることを確認済み（`clear()`, `__setitem__` 対応）

---
**Created**: 2026-03-30

### [2026-03-30 16:30] 実装完了
- Step 1: `clear_conversation_history()` を PersonaAgent / FacilitatorAgent に追加。`agent.messages` は通常のPython listなので `clear()` で安全にリセット可能。
- Step 2: `create_prompt_for_persona()` に `round_summaries` パラメータ追加。要約 + 直近3件（ファシリテータ含む）のプロンプト構成。`summarize_round()` に構造的な要約指示を追加。
- Step 3: `_build_prompt_with_context()` を簡素化。コンテキストは `create_prompt_for_persona()` で構築済みのため、二重付加を防止。
- Step 4: `start_agent_discussion()` / `start_agent_discussion_streaming()` のラウンドループに `round_summaries` 蓄積 + ラウンド間リセットを適用。
- テスト: 19テスト新規作成、既存606テスト含め625テスト全パス。
- 発見: ストリーミング版と非ストリーミング版でほぼ同一のラウンドループコードが重複している。将来的にリファクタリングの余地あり。
