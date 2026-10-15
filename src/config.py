"""
設定管理クラス
システム全体の設定を管理する
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from src.models.insight_category import InsightCategory

#: テーブルの種類（DynamoDB の物理名の接尾辞）→ 物理名を渡す設定名。
#: 設定があればそれを使い、なければ ``{DYNAMODB_TABLE_PREFIX}_{接尾辞}`` にする。
TABLE_NAME_SETTINGS: dict[str, str] = {
    "Personas": "PERSONAS_TABLE_NAME",
    "Discussions": "DISCUSSIONS_TABLE_NAME",
    "UploadedFiles": "UPLOADED_FILES_TABLE_NAME",
    "Datasets": "DATASETS_TABLE_NAME",
    "SurveyTemplates": "SURVEY_TEMPLATES_TABLE_NAME",
    "Surveys": "SURVEYS_TABLE_NAME",
    "KnowledgeBases": "KNOWLEDGE_BASES_TABLE_NAME",
    "Jobs": "JOBS_TABLE_NAME",
}
# U5: 古い 2 つの紐付けのテーブル（データセット・KB）は PersonaBindings にまとめたため
# 読まない（テーブルは CDK に RETAIN で残し、外すのは U9）。


@dataclass
class Config:
    """システム設定クラス"""

    # DynamoDBデータベース設定
    DYNAMODB_TABLE_PREFIX: str = "AIPersona"
    DYNAMODB_REGION: str = "us-east-1"

    # ファイルストレージ設定
    UPLOAD_DIR: str = "uploads/"
    MAX_FILE_SIZE: int = 10 * 1024 * 1024  # 10MB
    MAX_IMAGE_SIZE: int = (
        5 * 1024 * 1024
    )  # 5MB (Claude(Bedrock)モデルの画像1枚あたり上限。超過時Converseが ValidationException を返す)
    MAX_REQUEST_PAYLOAD_SIZE: int = (
        32 * 1024 * 1024
    )  # 32MB (Claude(Bedrock)のリクエストペイロード全体上限。議論ドキュメント合計に適用)
    # ペルソナ生成ソース: 1ファイルあたりの生バイト粗ガード。
    # 生バイトはBedrockに届かない（テキスト抽出後にプロンプト連結される）ため、
    # この上限の役割はmarkitdownが巨大バイナリでOOM/長時間化するのを防ぐ粗ガードに限る。
    # 真の上限はPERSONA_SOURCE_MAX_CHARS（抽出後の文字数）が担う。
    PERSONA_SOURCE_MAX_BYTES: int = 10 * 1024 * 1024  # 10MB/file
    # ペルソナ生成ソース: 抽出後テキストの合計文字数（真の上限）。
    # Sonnet 5 on Bedrockの既定context window 200Kトークンから逆算した安全値。
    # 日本語実文(≈2 chars/token)で20万字 ≈ 10万トークン。超過時はBedrock呼び出し前に
    # CAPACITYエラーで穏当に失敗させ、無駄なAPIコストを防ぐ。
    PERSONA_SOURCE_MAX_CHARS: int = 200_000
    S3_BUCKET_NAME: Optional[str] = None  # .envで設定、未設定時はローカルストレージ

    # AWS Bedrock設定
    AWS_REGION: str = "us-east-1"
    BEDROCK_MODEL_ID: str = "global.anthropic.claude-sonnet-5"

    # AI生成設定
    MAX_TOKENS: int = 4000
    # Strands Agent経由（ペルソナ生成・レポート等）の出力トークン上限。
    # 未設定だとBedrock/モデル側の補完デフォルト(Sonnet 5で4,096)に張り付き、
    # 複数件の一括生成やadaptive thinkingで上限超過(MaxTokensReachedException)する。
    AGENT_MAX_TOKENS: int = 32000

    # Agent Mode設定
    AGENT_MODEL_ID: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    DEFAULT_ROUNDS: int = 3
    MIN_ROUNDS: int = 1
    MAX_ROUNDS: int = 10

    # ペルソナ議論・インタビューでの追加ペルソナベースモデル(GPT5.6・Gemma4)
    ENABLE_ADDITIONAL_PERSONA_MODELS: bool = False

    # AgentCore Memory設定
    AGENTCORE_MEMORY_ID: Optional[str] = None
    AGENTCORE_MEMORY_REGION: str = "us-east-1"
    ENABLE_LONG_TERM_MEMORY: bool = False
    MEMORY_MAX_RESULTS: int = 5
    SUMMARY_MEMORY_STRATEGY_ID: Optional[str] = None
    SEMANTIC_MEMORY_STRATEGY_ID: Optional[str] = None

    # マスアンケート機能設定
    BATCH_INFERENCE_MODEL_ID: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0"  # バッチ推論用モデル（Claude 4.5 Haiku、クロスリージョン推論プロファイル）
    BEDROCK_BATCH_ROLE_ARN: Optional[str] = None

    # データ分析エージェント連携設定
    DATA_AGENT_RUNTIME_ARN: Optional[str] = None
    DATA_AGENT_REGION: str = "ap-northeast-1"
    ENABLE_DATA_AGENT: bool = False

    # データセット構造化分析ツール(analyze_dataset)のグローバルkill switch。
    # process起動ではなくtool登録・プロンプト付与をこのフラグで制御する。
    # セッション単位のenable_datasetフラグとANDで評価される。
    ENABLE_DATASET_ANALYSIS: bool = True

    # analyze_dataset の DuckDB backend が同時実行を許すクエリ数（BoundedSemaphore）。
    # 各クエリは in-memory DB にデータセットを丸ごと展開するため、ピークメモリは
    # 同時実行数×データセットサイズに比例する。デプロイ先のメモリに応じて調整する。
    DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES: int = 4

    # 生成候補（PersonaCandidates）の保持時間。候補の expires_at の計算にだけ使う（U5）
    PERSONA_CACHE_TTL_SECONDS: int = 28800  # 8 hours

    # --- ログイン（v1.0.0 U1） ---
    # ログイン方式。oidc（公開構成の Cognito Managed Login）か local（手元専用）。
    # 未設定・未知の値・未実装の admin_api は起動時の検査で止める（BR1.15）。
    AUTH_MODE: Optional[str] = None
    # アプリ自身の URL（スキーム・ホスト・ポート）。コールバック URL の組み立てと、
    # ログイン関連のフォーム送信の Origin 照合に使う（BR1.19）。
    APP_BASE_URL: str = "http://localhost:8000"
    COGNITO_USER_POOL_ID: Optional[str] = None
    COGNITO_CLIENT_ID: Optional[str] = None
    COGNITO_CLIENT_SECRET: Optional[str] = None
    # Managed Login のドメイン（例: ai-persona-v1-dev-123.auth.ap-northeast-1.amazoncognito.com）
    COGNITO_DOMAIN: Optional[str] = None
    # トークンの暗号化鍵の一覧（JSON: {"current": "v1", "k_v1": "..."}）。
    # ECS のタスク定義の secrets で環境変数として受け取る。
    TOKEN_ENCRYPTION_KEYS: Optional[str] = None
    # ログイン途中の Cookie の署名鍵（ECS の secrets で受け取る）
    LOGIN_COOKIE_SIGNING_KEY: Optional[str] = None
    SESSIONS_TABLE_NAME: Optional[str] = None
    # テーブルごとの物理名（CDK が自動生成した名前を App スタックが渡す）。
    # 未設定なら DYNAMODB_TABLE_PREFIX から組み立てる（手元の開発の .env のため）。
    PERSONAS_TABLE_NAME: Optional[str] = None
    DISCUSSIONS_TABLE_NAME: Optional[str] = None
    UPLOADED_FILES_TABLE_NAME: Optional[str] = None
    DATASETS_TABLE_NAME: Optional[str] = None
    SURVEY_TEMPLATES_TABLE_NAME: Optional[str] = None
    SURVEYS_TABLE_NAME: Optional[str] = None
    KNOWLEDGE_BASES_TABLE_NAME: Optional[str] = None
    JOBS_TABLE_NAME: Optional[str] = None
    # 部署ごとの共有の索引（U4）。TABLE_NAME_SETTINGS に入れないのは、App スタックの
    # 一括の権限付与（Scan を含む）と同じ扱いにしないため。未設定なら接頭辞から組み立てる
    RESOURCE_SHARES_TABLE_NAME: str = ""
    # 生成候補と紐付け（U5）。ResourceShares と同じく操作を絞った専用の項目
    PERSONA_CANDIDATES_TABLE_NAME: str = ""
    PERSONA_BINDINGS_TABLE_NAME: str = ""
    # AUTH_MODE=local の開発用の利用者（ロール・部署はカンマ区切り）
    LOCAL_DEV_USER_NAME: str = "開発ユーザー"
    LOCAL_DEV_USER_EMAIL: str = "dev@example.com"
    LOCAL_DEV_USER_ROLES: str = "member"
    LOCAL_DEV_USER_GROUPS: str = ""
    # auth.request_checked を出す割合（0〜1）。測定時は 1 にする
    AUTH_TIMING_SAMPLE_RATE: float = 0.1
    # ECS の中で動いているか（ECS_CONTAINER_METADATA_URI_V4 の有無）。
    # AUTH_MODE=local を AWS 上で起動させないために使う（NFR4.7）
    RUNNING_IN_ECS: bool = False

    def __post_init__(self) -> None:
        """設定の初期化後処理"""
        # 環境変数から設定を上書き
        self.AWS_REGION = os.getenv("AWS_REGION", self.AWS_REGION)
        self.BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", self.BEDROCK_MODEL_ID)
        self.AGENT_MODEL_ID = os.getenv("AGENT_MODEL_ID", self.AGENT_MODEL_ID)
        agent_max_tokens = os.getenv("AGENT_MAX_TOKENS")
        if agent_max_tokens:
            self.AGENT_MAX_TOKENS = int(agent_max_tokens)

        enable_additional_persona_models = os.getenv(
            "ENABLE_ADDITIONAL_PERSONA_MODELS", ""
        ).lower()
        if enable_additional_persona_models in ("true", "1", "yes"):
            self.ENABLE_ADDITIONAL_PERSONA_MODELS = True
        elif enable_additional_persona_models in ("false", "0", "no"):
            self.ENABLE_ADDITIONAL_PERSONA_MODELS = False

        # DynamoDB設定を環境変数から上書き
        self.DYNAMODB_TABLE_PREFIX = os.getenv(
            "DYNAMODB_TABLE_PREFIX", self.DYNAMODB_TABLE_PREFIX
        )
        self.DYNAMODB_REGION = os.getenv("DYNAMODB_REGION", self.DYNAMODB_REGION)

        # AgentCore Memory設定を環境変数から上書き
        self.AGENTCORE_MEMORY_ID = os.getenv(
            "AGENTCORE_MEMORY_ID", self.AGENTCORE_MEMORY_ID
        )
        self.AGENTCORE_MEMORY_REGION = os.getenv(
            "AGENTCORE_MEMORY_REGION", self.AGENTCORE_MEMORY_REGION
        )
        enable_memory = os.getenv("ENABLE_LONG_TERM_MEMORY", "").lower()
        if enable_memory in ("true", "1", "yes"):
            self.ENABLE_LONG_TERM_MEMORY = True
        elif enable_memory in ("false", "0", "no"):
            self.ENABLE_LONG_TERM_MEMORY = False
        memory_max_results = os.getenv("MEMORY_MAX_RESULTS")
        if memory_max_results:
            self.MEMORY_MAX_RESULTS = int(memory_max_results)
        self.SUMMARY_MEMORY_STRATEGY_ID = os.getenv(
            "SUMMARY_MEMORY_STRATEGY_ID", self.SUMMARY_MEMORY_STRATEGY_ID
        )
        self.SEMANTIC_MEMORY_STRATEGY_ID = os.getenv(
            "SEMANTIC_MEMORY_STRATEGY_ID", self.SEMANTIC_MEMORY_STRATEGY_ID
        )

        # S3設定を環境変数から上書き
        self.S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", self.S3_BUCKET_NAME)

        # マスアンケート機能設定を環境変数から上書き
        self.BATCH_INFERENCE_MODEL_ID = os.getenv(
            "BATCH_INFERENCE_MODEL_ID", self.BATCH_INFERENCE_MODEL_ID
        )
        self.BEDROCK_BATCH_ROLE_ARN = os.getenv(
            "BEDROCK_BATCH_ROLE_ARN", self.BEDROCK_BATCH_ROLE_ARN
        )

        # データ分析エージェント連携設定
        self.DATA_AGENT_RUNTIME_ARN = os.getenv(
            "DATA_AGENT_RUNTIME_ARN", self.DATA_AGENT_RUNTIME_ARN
        )
        self.DATA_AGENT_REGION = os.getenv("DATA_AGENT_REGION", self.DATA_AGENT_REGION)
        enable_val = os.getenv("ENABLE_DATA_AGENT", "").lower()
        if enable_val in ("true", "1", "yes"):
            self.ENABLE_DATA_AGENT = True
        elif enable_val in ("false", "0", "no"):
            self.ENABLE_DATA_AGENT = False

        enable_dataset_analysis = os.getenv("ENABLE_DATASET_ANALYSIS", "").lower()
        if enable_dataset_analysis in ("true", "1", "yes"):
            self.ENABLE_DATASET_ANALYSIS = True
        elif enable_dataset_analysis in ("false", "0", "no"):
            self.ENABLE_DATASET_ANALYSIS = False

        max_concurrent = os.getenv("DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES")
        if max_concurrent:
            # 不正値は既定にフォールバックせず即失敗させる（BoundedSemaphore は
            # 0/負値で ValueError になるため、誤設定を起動時に顕在化させる）。
            value = int(max_concurrent)
            if value < 1:
                raise ValueError(
                    f"DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES must be >= 1, got {value}"
                )
            self.DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES = value

        # ペルソナ生成キャッシュ設定を環境変数から上書き
        persona_cache_ttl = os.getenv("PERSONA_CACHE_TTL_SECONDS")
        if persona_cache_ttl:
            self.PERSONA_CACHE_TTL_SECONDS = int(persona_cache_ttl)

        self._load_auth_settings()

        # ディレクトリの存在確認と作成
        self._ensure_directories()

    def _load_auth_settings(self) -> None:
        """Override login-related settings from environment variables."""
        optional_names = (
            "AUTH_MODE",
            "COGNITO_USER_POOL_ID",
            "COGNITO_CLIENT_ID",
            "COGNITO_CLIENT_SECRET",
            "COGNITO_DOMAIN",
            "TOKEN_ENCRYPTION_KEYS",
            "LOGIN_COOKIE_SIGNING_KEY",
            "SESSIONS_TABLE_NAME",
            *TABLE_NAME_SETTINGS.values(),
        )
        for name in optional_names:
            env_value = os.getenv(name)
            if env_value:
                setattr(self, name, env_value)
        for name in (
            "APP_BASE_URL",
            "LOCAL_DEV_USER_NAME",
            "LOCAL_DEV_USER_EMAIL",
            "LOCAL_DEV_USER_ROLES",
            "LOCAL_DEV_USER_GROUPS",
        ):
            setattr(self, name, os.getenv(name, getattr(self, name)))
        self.RUNNING_IN_ECS = bool(os.getenv("ECS_CONTAINER_METADATA_URI_V4"))
        self.RESOURCE_SHARES_TABLE_NAME = (
            os.getenv("RESOURCE_SHARES_TABLE_NAME")
            or f"{self.DYNAMODB_TABLE_PREFIX}_ResourceShares"
        )
        self.PERSONA_CANDIDATES_TABLE_NAME = (
            os.getenv("PERSONA_CANDIDATES_TABLE_NAME")
            or f"{self.DYNAMODB_TABLE_PREFIX}_PersonaCandidates"
        )
        self.PERSONA_BINDINGS_TABLE_NAME = (
            os.getenv("PERSONA_BINDINGS_TABLE_NAME")
            or f"{self.DYNAMODB_TABLE_PREFIX}_PersonaBindings"
        )
        sample_rate = os.getenv("AUTH_TIMING_SAMPLE_RATE")
        if sample_rate:
            rate = float(sample_rate)
            if not 0.0 <= rate <= 1.0:
                raise ValueError(
                    f"AUTH_TIMING_SAMPLE_RATE must be between 0 and 1, got {rate}"
                )
            self.AUTH_TIMING_SAMPLE_RATE = rate

    def table_name(self, kind: str) -> str:
        """Physical DynamoDB name of one table kind (FR9.2, contract C10).

        Args:
            kind: A key of :data:`TABLE_NAME_SETTINGS` such as ``"Personas"``.

        Returns:
            The name passed by CDK (``PERSONAS_TABLE_NAME`` etc.), or
            ``{DYNAMODB_TABLE_PREFIX}_{kind}`` for local development.

        Raises:
            KeyError: ``kind`` is not one of the eight bulk-granted tables.
        """
        explicit = getattr(self, TABLE_NAME_SETTINGS[kind])
        return explicit or f"{self.DYNAMODB_TABLE_PREFIX}_{kind}"

    def table_names(self) -> dict[str, str]:
        """Physical names of the eight bulk-granted tables, keyed by kind."""
        return {kind: self.table_name(kind) for kind in TABLE_NAME_SETTINGS}

    def _ensure_directories(self) -> None:
        """必要なディレクトリが存在することを確認し、なければ作成"""
        directories = [
            Path(self.UPLOAD_DIR),
        ]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def upload_dir(self) -> Path:
        """アップロードディレクトリのPathオブジェクトを返す"""
        return Path(self.UPLOAD_DIR)

    def get_aws_credentials(self) -> dict:
        """AWS認証情報を環境変数から取得"""
        return {
            "aws_access_key_id": os.getenv("AWS_ACCESS_KEY_ID"),
            "aws_secret_access_key": os.getenv("AWS_SECRET_ACCESS_KEY"),
            "aws_session_token": os.getenv("AWS_SESSION_TOKEN"),
            "region_name": self.AWS_REGION,
        }

    def get_default_insight_categories(self) -> List["InsightCategory"]:
        """Get default insight categories for the system."""
        from src.models.insight_category import InsightCategory

        return InsightCategory.get_default_categories()


# グローバル設定インスタンス
config = Config()
