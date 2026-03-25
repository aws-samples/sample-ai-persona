"""
Config クラスの単体テスト

設定管理、環境変数読み込み、デフォルト値をテストします。
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch


class TestConfigDefaults:
    """Config デフォルト値のテスト"""

    def test_default_dynamodb_settings(self):
        """デフォルトのDynamoDB設定を確認"""
        from src.config import Config

        with patch.dict(os.environ, {}, clear=True):
            config = Config()
            assert config.DYNAMODB_TABLE_PREFIX == "AIPersona"
            assert config.DYNAMODB_REGION == "us-east-1"

    def test_default_file_settings(self):
        """デフォルトのファイル設定を確認"""
        from src.config import Config

        config = Config()
        assert config.UPLOAD_DIR == "uploads/"
        assert config.MAX_FILE_SIZE == 10 * 1024 * 1024  # 10MB
        assert ".txt" in config.ALLOWED_FILE_EXTENSIONS
        assert ".md" in config.ALLOWED_FILE_EXTENSIONS

    def test_default_aws_settings(self):
        """デフォルトのAWS設定を確認"""
        from src.config import Config

        with patch.dict(os.environ, {}, clear=True):
            config = Config()
            assert config.AWS_REGION == "us-east-1"
            assert (
                "claude" in config.BEDROCK_MODEL_ID.lower()
                or "anthropic" in config.BEDROCK_MODEL_ID.lower()
            )

    def test_default_app_settings(self):
        """デフォルトのアプリケーション設定を確認"""
        from src.config import Config

        config = Config()
        assert config.APP_TITLE == "AIペルソナシステム"
        assert config.APP_HOST == "0.0.0.0"
        assert config.APP_PORT == 8000

    def test_default_ai_settings(self):
        """デフォルトのAI生成設定を確認"""
        from src.config import Config

        config = Config()
        assert config.MAX_TOKENS == 4000
        assert config.TEMPERATURE == 0.7

    def test_default_agent_settings(self):
        """デフォルトのエージェント設定を確認"""
        from src.config import Config

        config = Config()
        assert config.DEFAULT_ROUNDS == 3
        assert config.MIN_ROUNDS == 1
        assert config.MAX_ROUNDS == 10


class TestConfigEnvironmentVariables:
    """環境変数からの設定読み込みテスト"""

    def test_aws_region_from_env(self):
        """AWS_REGIONが環境変数から読み込まれることを確認"""
        from src.config import Config

        with patch.dict(os.environ, {"AWS_REGION": "ap-northeast-1"}):
            config = Config()
            assert config.AWS_REGION == "ap-northeast-1"

    def test_bedrock_model_id_from_env(self):
        """BEDROCK_MODEL_IDが環境変数から読み込まれることを確認"""
        from src.config import Config

        with patch.dict(os.environ, {"BEDROCK_MODEL_ID": "custom-model-id"}):
            config = Config()
            assert config.BEDROCK_MODEL_ID == "custom-model-id"

    def test_dynamodb_table_prefix_from_env(self):
        """DYNAMODB_TABLE_PREFIXが環境変数から読み込まれることを確認"""
        from src.config import Config

        with patch.dict(os.environ, {"DYNAMODB_TABLE_PREFIX": "CustomPrefix"}):
            config = Config()
            assert config.DYNAMODB_TABLE_PREFIX == "CustomPrefix"

    def test_dynamodb_region_from_env(self):
        """DYNAMODB_REGIONが環境変数から読み込まれることを確認"""
        from src.config import Config

        with patch.dict(os.environ, {"DYNAMODB_REGION": "eu-west-1"}):
            config = Config()
            assert config.DYNAMODB_REGION == "eu-west-1"


class TestConfigProperties:
    """Config プロパティのテスト"""

    def test_upload_dir_property(self):
        """upload_dirプロパティがPathオブジェクトを返すことを確認"""
        from src.config import Config

        config = Config()
        assert isinstance(config.upload_dir, Path)
        # UPLOAD_DIRの末尾スラッシュを除去して比較
        expected = config.UPLOAD_DIR.rstrip("/")
        assert (
            str(config.upload_dir) == expected
            or str(config.upload_dir) == config.UPLOAD_DIR
        )


class TestConfigFileExtensionValidation:
    """ファイル拡張子検証のテスト"""

    def test_allowed_txt_extension(self):
        """txtファイルが許可されることを確認"""
        from src.config import Config

        config = Config()
        assert config.is_allowed_file_extension("interview.txt") is True
        assert config.is_allowed_file_extension("INTERVIEW.TXT") is True

    def test_allowed_md_extension(self):
        """mdファイルが許可されることを確認"""
        from src.config import Config

        config = Config()
        assert config.is_allowed_file_extension("document.md") is True
        assert config.is_allowed_file_extension("DOCUMENT.MD") is True

    def test_disallowed_extensions(self):
        """許可されていない拡張子が拒否されることを確認"""
        from src.config import Config

        config = Config()
        assert config.is_allowed_file_extension("file.pdf") is False
        assert config.is_allowed_file_extension("file.exe") is False
        assert config.is_allowed_file_extension("file.py") is False
        assert config.is_allowed_file_extension("file.html") is False


class TestConfigAWSCredentials:
    """AWS認証情報取得のテスト"""

    def test_get_aws_credentials_from_env(self):
        """AWS認証情報が環境変数から取得されることを確認"""
        from src.config import Config

        with patch.dict(
            os.environ,
            {
                "AWS_ACCESS_KEY_ID": "test-access-key",
                "AWS_SECRET_ACCESS_KEY": "test-secret-key",
                "AWS_SESSION_TOKEN": "test-session-token",
            },
        ):
            config = Config()
            credentials = config.get_aws_credentials()

            assert credentials["aws_access_key_id"] == "test-access-key"
            assert credentials["aws_secret_access_key"] == "test-secret-key"
            assert credentials["aws_session_token"] == "test-session-token"
            assert credentials["region_name"] == config.AWS_REGION

    def test_get_aws_credentials_missing(self):
        """AWS認証情報が設定されていない場合、Noneを返すことを確認"""
        from src.config import Config

        with patch.dict(os.environ, {}, clear=True):
            # 環境変数をクリアしてテスト
            for key in [
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
            ]:
                os.environ.pop(key, None)

            config = Config()
            credentials = config.get_aws_credentials()

            assert credentials["aws_access_key_id"] is None
            assert credentials["aws_secret_access_key"] is None
            assert credentials["aws_session_token"] is None


class TestConfigDirectoryCreation:
    """ディレクトリ作成のテスト"""

    def test_ensure_directories_creates_upload_dir(self):
        """アップロードディレクトリが作成されることを確認"""
        from src.config import Config

        with tempfile.TemporaryDirectory() as tmp_dir:
            config = Config()
            original_upload_dir = config.UPLOAD_DIR

            try:
                config.UPLOAD_DIR = f"{tmp_dir}/testuploads/"
                config._ensure_directories()

                assert Path(f"{tmp_dir}/testuploads").exists()
            finally:
                config.UPLOAD_DIR = original_upload_dir

    def test_ensure_directories_handles_existing(self):
        """既存のディレクトリがあっても問題なく動作することを確認"""
        from src.config import Config

        with tempfile.TemporaryDirectory() as tmp_dir:
            # 事前にディレクトリを作成
            Path(f"{tmp_dir}/existinguploads").mkdir(parents=True)

            config = Config()
            original_upload_dir = config.UPLOAD_DIR

            try:
                config.UPLOAD_DIR = f"{tmp_dir}/existinguploads/"

                # エラーが発生しないことを確認
                config._ensure_directories()

                assert Path(f"{tmp_dir}/existinguploads").exists()
            finally:
                config.UPLOAD_DIR = original_upload_dir


class TestConfigMemorySettings:
    """AgentCore Memory設定のテスト"""

    def test_default_memory_settings(self):
        """デフォルトのメモリ設定を確認"""
        from src.config import Config

        # 環境変数をクリアして、メモリ関連の変数も明示的にNoneに設定
        with patch.dict(
            os.environ,
            {"AGENTCORE_MEMORY_ID": "", "ENABLE_LONG_TERM_MEMORY": "false"},
            clear=True,
        ):
            config = Config()
            assert (
                config.AGENTCORE_MEMORY_ID is None or config.AGENTCORE_MEMORY_ID == ""
            )
            assert config.AGENTCORE_MEMORY_REGION == "us-east-1"
            assert config.ENABLE_LONG_TERM_MEMORY is False
            assert config.MEMORY_STRATEGY == "summary"
            assert config.MEMORY_MAX_RESULTS == 5

    def test_agentcore_memory_id_from_env(self):
        """AGENTCORE_MEMORY_IDが環境変数から読み込まれることを確認"""
        from src.config import Config

        with patch.dict(os.environ, {"AGENTCORE_MEMORY_ID": "test-memory-id-123"}):
            config = Config()
            assert config.AGENTCORE_MEMORY_ID == "test-memory-id-123"

    def test_agentcore_memory_region_from_env(self):
        """AGENTCORE_MEMORY_REGIONが環境変数から読み込まれることを確認"""
        from src.config import Config

        with patch.dict(os.environ, {"AGENTCORE_MEMORY_REGION": "ap-northeast-1"}):
            config = Config()
            assert config.AGENTCORE_MEMORY_REGION == "ap-northeast-1"

    def test_enable_long_term_memory_true(self):
        """ENABLE_LONG_TERM_MEMORYがtrueで有効になることを確認"""
        from src.config import Config

        for value in ["true", "1", "yes", "TRUE", "True"]:
            with patch.dict(os.environ, {"ENABLE_LONG_TERM_MEMORY": value}):
                config = Config()
                assert config.ENABLE_LONG_TERM_MEMORY is True

    def test_enable_long_term_memory_false(self):
        """ENABLE_LONG_TERM_MEMORYがfalseで無効になることを確認"""
        from src.config import Config

        for value in ["false", "0", "no", "FALSE", "False"]:
            with patch.dict(os.environ, {"ENABLE_LONG_TERM_MEMORY": value}):
                config = Config()
                assert config.ENABLE_LONG_TERM_MEMORY is False

    def test_memory_strategy_from_env(self):
        """MEMORY_STRATEGYが環境変数から読み込まれることを確認"""
        from src.config import Config

        with patch.dict(os.environ, {"MEMORY_STRATEGY": "semantic"}):
            config = Config()
            assert config.MEMORY_STRATEGY == "semantic"

    def test_memory_max_results_from_env(self):
        """MEMORY_MAX_RESULTSが環境変数から読み込まれることを確認"""
        from src.config import Config

        with patch.dict(os.environ, {"MEMORY_MAX_RESULTS": "10"}):
            config = Config()
            assert config.MEMORY_MAX_RESULTS == 10


class TestGlobalConfigInstance:
    """グローバル設定インスタンスのテスト"""

    def test_global_config_exists(self):
        """グローバルconfigインスタンスが存在することを確認"""
        from src.config import config

        assert config is not None

    def test_global_config_is_config_instance(self):
        """グローバルconfigがConfigインスタンスであることを確認"""
        from src.config import config, Config

        assert isinstance(config, Config)


class TestConfigInsightCategories:
    """インサイトカテゴリー設定のテスト"""

    def test_get_default_insight_categories(self):
        """デフォルトのインサイトカテゴリーが取得できることを確認"""
        from src.config import Config
        from src.models.insight_category import InsightCategory

        config = Config()
        categories = config.get_default_insight_categories()

        assert len(categories) == 5
        assert all(isinstance(cat, InsightCategory) for cat in categories)

    def test_default_insight_categories_content(self):
        """デフォルトのインサイトカテゴリーの内容を確認"""
        from src.config import Config

        config = Config()
        categories = config.get_default_insight_categories()

        category_names = [cat.name for cat in categories]
        assert "顧客ニーズ" in category_names
        assert "市場機会" in category_names
        assert "商品開発" in category_names
        assert "マーケティング" in category_names
        assert "その他" in category_names

        # すべてのカテゴリーに説明があることを確認
        assert all(len(cat.description) > 0 for cat in categories)
