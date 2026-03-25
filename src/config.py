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


@dataclass
class Config:
    """システム設定クラス"""

    # DynamoDBデータベース設定
    DYNAMODB_TABLE_PREFIX: str = "AIPersona"
    DYNAMODB_REGION: str = "us-east-1"

    # ファイルストレージ設定
    UPLOAD_DIR: str = "uploads/"
    MAX_FILE_SIZE: int = 10 * 1024 * 1024  # 10MB
    ALLOWED_FILE_EXTENSIONS: tuple = (".txt", ".md")
    S3_BUCKET_NAME: Optional[str] = None  # .envで設定、未設定時はローカルストレージ

    # AWS Bedrock設定
    AWS_REGION: str = "us-east-1"
    BEDROCK_MODEL_ID: str = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"

    # アプリケーション設定
    APP_TITLE: str = "AIペルソナシステム"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    # AI生成設定
    MAX_TOKENS: int = 4000
    TEMPERATURE: float = 0.7

    # Agent Mode設定
    AGENT_MODEL_ID: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    DEFAULT_ROUNDS: int = 3
    MIN_ROUNDS: int = 1
    MAX_ROUNDS: int = 10

    # AgentCore Memory設定
    AGENTCORE_MEMORY_ID: Optional[str] = None
    AGENTCORE_MEMORY_REGION: str = "us-east-1"
    ENABLE_LONG_TERM_MEMORY: bool = False
    MEMORY_STRATEGY: str = "summary"  # "summary", "semantic", etc.
    MEMORY_MAX_RESULTS: int = 5
    SUMMARY_MEMORY_STRATEGY_ID: Optional[str] = None
    SEMANTIC_MEMORY_STRATEGY_ID: Optional[str] = None

    # マスアンケート機能設定
    BATCH_INFERENCE_MODEL_ID: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0"  # バッチ推論用モデル（Claude 4.5 Haiku、クロスリージョン推論プロファイル）
    SURVEY_S3_PREFIX: str = "survey-results/"  # アンケート結果CSV保存先S3プレフィックス
    BATCH_INFERENCE_S3_PREFIX: str = (
        "batch-inference/"  # バッチ推論入出力S3プレフィックス
    )
    BEDROCK_BATCH_ROLE_ARN: Optional[str] = None

    # Dataset Integration設定
    ENABLE_DATASET_INTEGRATION: bool = False  # データセット連携機能の有効/無効

    def __post_init__(self) -> None:
        """設定の初期化後処理"""
        # 環境変数から設定を上書き
        self.AWS_REGION = os.getenv("AWS_REGION", self.AWS_REGION)
        self.BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", self.BEDROCK_MODEL_ID)

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
        self.MEMORY_STRATEGY = os.getenv("MEMORY_STRATEGY", self.MEMORY_STRATEGY)
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
        self.SURVEY_S3_PREFIX = os.getenv("SURVEY_S3_PREFIX", self.SURVEY_S3_PREFIX)
        self.BATCH_INFERENCE_S3_PREFIX = os.getenv(
            "BATCH_INFERENCE_S3_PREFIX", self.BATCH_INFERENCE_S3_PREFIX
        )
        self.BEDROCK_BATCH_ROLE_ARN = os.getenv(
            "BEDROCK_BATCH_ROLE_ARN", self.BEDROCK_BATCH_ROLE_ARN
        )

        # ディレクトリの存在確認と作成
        self._ensure_directories()

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

    def is_allowed_file_extension(self, filename: str) -> bool:
        """ファイル拡張子が許可されているかチェック"""
        return any(
            filename.lower().endswith(ext) for ext in self.ALLOWED_FILE_EXTENSIONS
        )

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
