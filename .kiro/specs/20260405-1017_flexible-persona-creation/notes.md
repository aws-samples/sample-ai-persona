# Notes: データ＋プロンプトによるペルソナ作成の柔軟性強化

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

### [2026-04-05 10:17] SPEC作成・設計完了

**Facts**:
- GitHub Issue #9 の要件をQ-SPECヒアリングで具体化
- 既存実装を調査: `AIService.generate_persona()` (インタビュー→1ペルソナ), `AgentService.generate_personas_from_report()` (レポート→Nペルソナ)
- feature/flexible-persona-creation ブランチを作成

**Decisions**:
- Phase 1: ファイルアップロード＋データ種別＋カスタムプロンプトの基本フローのみ
- Phase 2以降: マスサーベイデータ/外部データセット連携
- 統一エージェント方式: データ種別ごとのベースプロンプト＋カスタムプロンプトを結合してStrands Agentに渡す
- 既存の2つの生成ロジックはプリセットとして後方互換維持
- 旧エンドポイントは廃止し新エンドポイントに統合（UIが完全に置き換わるため）

**Impressions**:
- 既存の `AgentService.generate_personas_from_report()` のパース処理 (`_parse_personas_from_response`) は再利用可能
- `FileManager.extract_text_from_file()` も再利用可能で、PDF/Word/テキスト対応済み
- CSV対応は新規追加が必要（Polarsで読み込み→テキスト化）

---
**Created**: 2026-04-05

### [2026-04-05 10:25] SPEC修正 - 命名変更 & MotherDuck MCP連携

**Facts**:
- ユーザーフィードバック: `flexible` 命名はわかりにくい → `/persona/generate` をそのまま使う
- CSV系データ（購買データ、レビューデータ等）の場合、MotherDuck MCPを流用してデータ分析する方針に変更

**Decisions**:
- エンドポイント: `/persona/generate` を置き換え、`/persona/upload` を拡張
- `AgentService.create_persona_generation_agent()` でデータ種別に応じてMotherDuck MCPツールを付与
- 既存の `MCPServerManager` + `create_persona_agent_with_dataset()` パターンを参考に実装
- テキスト系データ（インタビュー、レポート）はテキスト抽出→プロンプトに埋め込み
- CSV系データはMotherDuck MCPでエージェントがSQL分析→ペルソナ生成

### [2026-04-05 10:27] Structured Output採用

**Facts**:
- Strands SDK に `agent.structured_output(output_model: Type[T])` メソッドが存在
- Pydantic BaseModel を渡すと、会話履歴から構造化データを抽出してくれる
- pydantic は strands-agents の依存に含まれている（追加インストール不要）

**Decisions**:
- `PersonaListOutput(BaseModel)` を定義し、`agent.structured_output()` でペルソナリストを取得
- 既存の `_parse_personas_from_response()` の正規表現JSONパースは不要に
- これによりJSONパースエラーを完全に防止
