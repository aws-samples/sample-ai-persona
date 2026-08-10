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

    # ペルソナ生成キャッシュ設定
    PERSONA_CACHE_TTL_SECONDS: int = 14400  # 4 hours

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

        # ペルソナ生成キャッシュ設定を環境変数から上書き
        persona_cache_ttl = os.getenv("PERSONA_CACHE_TTL_SECONDS")
        if persona_cache_ttl:
            self.PERSONA_CACHE_TTL_SECONDS = int(persona_cache_ttl)

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
