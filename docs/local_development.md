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

# 2. DuckDB httpfs 拡張の事前インストール（S3 上のデータを扱う場合のみ必須）
#    アプリは実行時に拡張をダウンロードしない（閉域対応の前提）。コンテナは
#    ビルド時に組込済みだが、ローカルでは以下を一度だけ実行して事前導入する。
uv run python -c "import duckdb; c=duckdb.connect(); c.execute('INSTALL httpfs'); c.close()"

# 3. 環境変数を設定（.env.exampleを参考に実際の値を記入）
cp .env.example .env
# .env を編集してAWSリソース名等を設定
# AUTH_MODE=local は必須（未設定だと起動しない）。手元では Cognito を使わず、
# LOCAL_DEV_USER_* で決めた開発用の利用者としてログインする

# 4. Tailwind CSSビルド
./scripts/build-css.sh --minify

# 5. アプリケーション起動
uv run python run_htmx.py
```

ブラウザで http://localhost:8000 にアクセス

> **ログイン（v1.0.0）:** `AUTH_MODE=local` では、画面を開くと開発用の利用者
> （既定: 名前「開発ユーザー」、ロール `member`、部署なし）で自動的にログインし、
> 画面上部に名前・ロール・部署が出ます。利用者は `.env` の `LOCAL_DEV_USER_NAME`・
> `LOCAL_DEV_USER_EMAIL`・`LOCAL_DEV_USER_ROLES`（`admin` / `member`、カンマ区切り）・
> `LOCAL_DEV_USER_GROUPS`（部署、カンマ区切り）で変えられます。ログインセッションは
> プロセスの中に置くため、再起動すると再ログインになります。ログアウトすると
> 「ログアウトしました」の画面になり、「ログイン」ボタンでログインし直します。
> 無操作 8 時間・ログインから 7 日でセッションが切れるのは AWS 上と同じです。
>
> **テーブル名:** `DYNAMODB_TABLE_PREFIX` から `{接頭辞}_Personas` などを組み立てます。
> v1.0.0 の CDK（Data スタック）が作ったテーブルを手元から使う場合は、
> `PERSONAS_TABLE_NAME` などテーブルごとの物理名を `.env` に書きます（`.env.example` 参照）。

> **httpfs について:** マスアンケート（S3 上の Parquet）や S3 データセットの
> `analyze_dataset` は DuckDB の httpfs 拡張を使います。手順 2 を実行していないと
> 起動後の S3 アクセス時に `LOAD httpfs` が失敗します。ローカルの CSV 分析だけを
> 試す場合は httpfs は不要です（S3 パスのときだけ LOAD します）。拡張は
> `~/.duckdb/extensions/` に保存され、一度導入すれば再実行は不要です。

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
