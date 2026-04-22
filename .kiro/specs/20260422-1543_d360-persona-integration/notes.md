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

### [2026-04-22 15:54] 設計変更: boto3 直接 → Strands Agent ツール方式

**発見**: 既存のペルソナ生成が既に Strands Agent を使っていた。
- `agent_service.py` の `generate_personas_with_agent()` が Strands Agent + structured_output で実装
- `create_persona_generation_agent()` で Agent を作成し、MCP ツール等を付与
- CSV データの場合は MCP の query ツールで SQL 分析している

**設計変更**:
- 当初: `PersonaManager` が D360Service を直接呼び、結果を `ai_service` に渡す 3 ステップ方式
- 変更後: Strands Agent のツールとして `ask_data_agent` を追加し、Agent が自律的に D360 に問い合わせる方式

**理由**:
- 既存の `generate_personas_with_agent` フローにそのまま乗れる
- Agent が「何を D360 に聞くか」「何回聞くか」を自律判断できる
- `create_persona_generation_agent(data_type="dwh")` に分岐を追加するだけで済む
- MCP ツールと同じパターンなので、コードの一貫性が保たれる

**影響範囲の縮小**:
- `persona_manager.py` の変更が最小限に（data_type="dwh" のファイル不要チェックのみ）
- 新規ファイルは `d360_service.py` のみ（D360Service + @tool ファクトリ）
- `agent_service.py` は `create_persona_generation_agent` に dwh 分岐を追加するだけ

### [2026-04-22 15:55] Phase 1-4 実装完了

**変更ファイル一覧**:
- `src/config.py` — D360_RUNTIME_ARN, D360_REGION, ENABLE_D360_INTEGRATION 追加
- `src/services/d360_service.py` — 新規。D360Service + create_d360_tool
- `src/services/agent_service.py` — DATA_TYPE_PROMPTS に dwh 追加、D360 ツール付与
- `src/managers/persona_manager.py` — data_type="dwh" で _generate_personas_from_dwh に分岐
- `web/routers/persona.py` — dwh ハンドリング（ファイル不要、analysis_angle 受け取り、SSE）
- `web/templates/persona/generation.html` — DWH オプション追加、動的 UI 切り替え
- `web/routers/settings.py` — D360 設定の取得・保存・接続テスト
- `web/templates/settings/index.html` — D360 セクション追加
- `web/templates/settings/partials/d360_settings.html` — 新規。D360 設定フォーム

**残タスク**: Phase 5（エラーハンドリング・仕上げ）+ E2E 動作確認
