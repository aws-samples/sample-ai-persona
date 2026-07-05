# ローカル開発ガイド

コードのカスタマイズやローカルでの動作確認を行う場合の手順です。

## 前提条件

- Python 3.13+、[uv](https://docs.astral.sh/uv/)
- バックエンドリソース（DynamoDB、S3、AgentCore Memory）がAWS CDKで構築済み
- AWS認証情報（Bedrock、DynamoDB、S3へのアクセス）

## セットアップ

```bash
# 1. 依存関係のインストール
uv sync

# 2. 環境変数を設定（.env.exampleを参考に実際の値を記入）
cp .env.example .env
# .env を編集してAWSリソース名等を設定

# 3. Tailwind CSSビルド
./scripts/build-css.sh --minify

# 4. アプリケーション起動
uv run python run_htmx.py
```

ブラウザで http://localhost:8000 にアクセス

## テスト

```bash
uv sync --extra dev
uv run pytest                          # 全テスト
uv run pytest -m unit                  # 単体テスト（マーカー指定）
uv run pytest -m integration           # 統合テスト（マーカー指定）
uv run pytest -m api                   # APIテスト（マーカー指定）
uv run pytest --cov=src --cov-report=html  # カバレッジ付き
```

## リント・型チェック

```bash
uv run ruff check .          # リント
uv run ruff check --fix .    # 自動修正
uv run mypy src/ web/        # 型チェック
```

## プロジェクト構造

```
ai-persona-system/
├── run_htmx.py             # 起動スクリプト
├── web/                    # フロントエンド
│   ├── main.py            # FastAPIアプリケーション
│   ├── routers/           # APIルーター（persona, discussion, interview, survey, settings, api）
│   ├── templates/         # Jinja2テンプレート
│   └── static/            # 静的ファイル（CSS/JS）
├── src/
│   ├── managers/          # ビジネスロジック層
│   │   ├── shared/       # 複数Managerが共有するユーティリティ（file_utils, document_loader等）
│   │   ├── persona_manager.py           # ペルソナCRUD・検索
│   │   ├── persona_generation_manager.py # ペルソナ生成ワークフロー
│   │   ├── persona_memory_manager.py    # 長期記憶（AgentCore Memory）管理
│   │   ├── discussion_manager.py        # 簡易議論の実行・インサイト抽出
│   │   ├── agent_discussion_manager.py  # エージェント駆動議論（しっかり議論）
│   │   ├── interview_manager.py         # リアルタイムインタビュー
│   │   ├── survey_template_manager.py   # テンプレートCRUD + AI設問生成
│   │   ├── survey_dataset_manager.py    # データセット管理 + DWH連携
│   │   ├── survey_execution_manager.py  # アンケート実行制御
│   │   ├── survey_analysis_manager.py   # ビジュアル分析 + インサイトレポート
│   │   ├── report_manager.py            # レポート生成（議論・インタビュー）
│   │   ├── dataset_manager.py           # 行動データ管理
│   │   └── file_manager.py, settings_manager.py, job_manager.py
│   ├── services/          # 外部サービス連携層
│   │   ├── ai_service.py              # Bedrock Converse/Invoke API
│   │   ├── agent_service.py           # Strands Agent SDK操作
│   │   ├── database_service.py        # DynamoDB CRUD
│   │   ├── s3_service.py              # S3操作
│   │   ├── survey_batch_service.py    # DuckDB/Parquet + Bedrock Batch Inference
│   │   ├── data_agent_service.py      # DWHエージェントツール
│   │   └── service_factory.py         # DI用シングルトンファクトリー
│   ├── prompts/           # プロンプトテンプレート定数・ヘルパー関数
│   ├── models/            # データモデル（イミュータブル、標準ライブラリのみ依存）
│   └── config.py          # 設定管理
├── cdk/                   # AWS CDKインフラストラクチャコード
├── tests/                 # テストコード（unit, integration, api）
├── docs/                  # ドキュメント
├── scripts/               # ユーティリティスクリプト
└── sample_data/           # サンプルデータセット
```

## テスト構成

```
tests/
├── conftest.py        # 共通フィクスチャ（DB、モデル、モック）
├── unit/              # 単体テスト（外部依存をモック）
├── integration/       # 統合テスト（モックDB、AIモック）
└── api/               # APIエンドポイントテスト
```
