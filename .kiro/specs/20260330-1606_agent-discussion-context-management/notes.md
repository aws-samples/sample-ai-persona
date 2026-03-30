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
