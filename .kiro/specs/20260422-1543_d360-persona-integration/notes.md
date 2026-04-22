# Notes: D360 連携ペルソナ生成

Guidelines:
- **Purpose**: Record sufficient information so that:
  - Future sessions can understand the full context
  - Knowledge notes and learning materials can be generated from this log
  - Issues can be traced and verified through the timeline
- **What to capture**:
  - **Facts**: What happened, what was tried, results and errors
  - **Decisions**: Why this approach, what alternatives were considered
  - **Impressions**: Concerns, surprises, discoveries, evaluations
- **When to record**: After each meaningful unit of work (e.g., investigation, decision, problem resolution). Do not defer.
- **Append-only**: Never edit or delete existing content

## Log

### [2026-04-22 15:43] SPEC 作成・要件整理

Q-SPEC Framework でヒアリングを実施し、以下を決定:

**スコープ決定**:
- Phase 1 はインタビュー用ペルソナ生成のみ（マスアンケート用は Phase 2）
- 理由: 既存のペルソナ生成画面がインタビュー用に特化しているため

**技術的決定**:
- D360 呼び出し: `boto3 bedrock-agentcore invoke_agent_runtime` で直接呼び出し
  - Strands @tool ラップも可能だが、sample-ai-persona 側は Strands を使っていないため、シンプルな boto3 呼び出しで十分
- 非同期処理: ThreadPoolExecutor を採用（asyncio.to_thread ではなく）
  - 理由: D360 の SSE 読み取り（`iter_lines`）が同期ブロッキング I/O、既存の persona.py が ThreadPoolExecutor で統一されている
- data_type 値: `"dwh"` / 表示名: `"DWH（D360連携）"`

**D360 側の調査結果**:
- AgentCore Runtime は SSE ストリーミングで応答（`data: {type, content}\n\n`）
- イベント型: `token`（テキストチャンク）, `tool_use`, `chart`, `error`, `done`
- セッション ID は 33 文字以上が必須
- `read_timeout=600` が推奨（SQL 実行に時間がかかる場合あり）
- SQL_RESULT_THRESHOLD は 200 件

**既存コードの調査結果**:
- `persona.py`: `executor.submit` → `future.done()` ポーリング → SSE keepalive → result の非同期パターン
- `survey.py`: `asyncio.create_task(asyncio.to_thread(...))` パターンも存在するが、persona.py とは異なる
- `config.py`: 環境変数ベースの設定管理、`__post_init__` で `os.getenv` で上書き
- `settings.py`: データセット管理、MCP 設定、ナレッジベース管理の 3 セクション構成

---
**Created**: 2026-04-22
