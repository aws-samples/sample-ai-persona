# Requirements: D360 連携ペルソナ生成

## Background & Context

### User Problems
- 現在のペルソナ生成はインタビューテキストやレポートなどの定性データに依存しており、実際の購買・注文データに基づいたペルソナを作れない
- DWH に蓄積された定量データ（売上・注文・顧客・商品）を活用してペルソナを生成したいが、手動でデータを抽出・整形してからアップロードする手間がかかる

### Related Issues
- sample-dwh-agent-main（D360）プロジェクトが AgentCore Runtime 上でデータ分析エージェントを提供済み
- D360 は自然言語で問い合わせると Redshift に SQL を実行し、分析結果をテキストで返す

## Objectives
- ペルソナ生成画面に「DWH（D360連携）」データソースオプションを追加する
- ユーザーが分析の切り口を入力すると、D360 に自動で問い合わせ、その分析結果をもとにインタビュー用ペルソナを生成する

## Scope

### In Scope
- ペルソナ生成画面への「DWH（D360連携）」オプション追加
- D360 AgentCore Runtime への問い合わせサービス（`d360_service.py`）
- 分析の切り口入力 → D360 への質問自動生成 → 分析結果取得 → ペルソナ生成の一連フロー
- 非同期処理（ThreadPoolExecutor + SSE、既存パターン踏襲）
- 設定画面での D360 接続情報管理（Runtime ARN, Region）
- Config への D360 関連設定追加

### Out of Scope
- マスアンケート用ペルソナリスト生成（Phase 2）
- D360 側の機能変更・Knowledge 追加
- CSV エクスポート機能
- D360 の認証・権限管理の自動化（IAM 権限は手動設定前提）

## Detailed Requirements

### 1. データソース選択
- ペルソナ生成画面の data_type に `"dwh"` を追加（表示名: 「DWH（D360連携）」）
- `"dwh"` 選択時はファイルアップロードを非表示にし、代わりに「分析の切り口」テキスト入力を表示
- ペルソナ生成数は既存通り 1-10 の範囲

### 2. D360 問い合わせフロー
- ユーザーが入力した切り口（例:「高単価商品のリピーター層」「20代女性の購買傾向」）をもとに、AI が D360 への適切な質問を自動生成
- D360 AgentCore Runtime に `invoke_agent_runtime` で問い合わせ
- SSE レスポンスからテキストチャンクを収集し、分析結果テキストを取得
- 分析結果テキストをペルソナ生成の入力として使用

### 3. 非同期処理
- D360 問い合わせ + ペルソナ生成を ThreadPoolExecutor で実行
- SSE で進捗表示（「D360 にデータを問い合わせ中...」→「分析結果をもとにペルソナ生成中...」）
- 既存の `persona.py` の SSE パターン（`executor.submit` → keepalive → result）を踏襲

### 4. 設定管理
- `src/config.py` に以下を追加:
  - `D360_RUNTIME_ARN`: AgentCore Runtime ARN（環境変数 `D360_RUNTIME_ARN`）
  - `D360_REGION`: D360 のリージョン（環境変数 `D360_REGION`、デフォルト: `ap-northeast-1`）
  - `ENABLE_D360_INTEGRATION`: 機能の有効/無効フラグ
- 設定画面に D360 接続設定セクションを追加

### 5. エラーハンドリング
- D360 未設定時: 「D360 の接続設定がされていません。設定画面から設定してください」
- D360 応答エラー時: エラー内容を SSE で返し、ユーザーに通知
- タイムアウト: `read_timeout=600` で D360 の SQL 実行待ちに対応

---
**Created**: 2026-04-22
