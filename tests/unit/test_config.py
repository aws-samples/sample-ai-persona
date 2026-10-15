"""
Config クラスの単体テスト

設定管理、環境変数読み込み、デフォルト値をテストします。
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest  # noqa: F401 - used by test_invalid_value_raises


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
        assert config.MAX_IMAGE_SIZE == 5 * 1024 * 1024  # 5MB
        assert config.MAX_REQUEST_PAYLOAD_SIZE == 32 * 1024 * 1024  # 32MB

    def test_size_limits_are_single_source_of_truth(self):
        """サイズ上限がconfigを single source of truth として参照されることを確認

        画像5MB・リクエスト合計32MBの定数が各Managerでハードコードされず
        configに集約されていることを検証する（重複定義の再発防止）。
        """
        from src.config import config
        from src.managers.file_manager import FileManager

        assert FileManager.DISCUSSION_IMAGE_MAX_SIZE == config.MAX_IMAGE_SIZE
        assert FileManager.SURVEY_IMAGE_MAX_SIZE == config.MAX_IMAGE_SIZE
        assert (
            FileManager.DISCUSSION_DOCUMENT_TOTAL_MAX_SIZE
            == config.MAX_REQUEST_PAYLOAD_SIZE
        )

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

    def test_default_ai_settings(self):
        """デフォルトのAI生成設定を確認"""
        from src.config import Config

        config = Config()
        assert config.MAX_TOKENS == 4000

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

    def test_memory_max_results_from_env(self):
        """MEMORY_MAX_RESULTSが環境変数から読み込まれることを確認"""
        from src.config import Config

        with patch.dict(os.environ, {"MEMORY_MAX_RESULTS": "10"}):
            config = Config()
            assert config.MEMORY_MAX_RESULTS == 10


class TestDatasetAnalysisConcurrency:
    """analyze_dataset の同時実行数設定のテスト"""

    def test_default_is_four(self):
        from src.config import Config

        with patch.dict(os.environ, {}, clear=True):
            config = Config()
            assert config.DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES == 4

    def test_from_env(self):
        from src.config import Config

        with patch.dict(os.environ, {"DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES": "8"}):
            config = Config()
            assert config.DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES == 8

    def test_invalid_value_raises(self):
        from src.config import Config

        with patch.dict(os.environ, {"DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES": "0"}):
            with pytest.raises(ValueError):
                Config()


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


class TestTableNames:
    """テーブルごとの物理名（v1.0.0 U1、FR9.2、約束 C10）。

    CDK が自動生成した名前を App スタックが環境変数で渡す。なければ手元の .env のため
    DYNAMODB_TABLE_PREFIX から組み立てる。
    """

    def test_explicit_table_name_wins(self):
        from src.config import Config

        with patch.dict(
            os.environ,
            {
                "DYNAMODB_TABLE_PREFIX": "Local",
                "PERSONAS_TABLE_NAME": "AIPersonaDev-Data-PersonasABC123",
            },
            clear=True,
        ):
            config = Config()
            assert config.table_name("Personas") == "AIPersonaDev-Data-PersonasABC123"
            assert config.table_name("Discussions") == "Local_Discussions"

    def test_prefix_is_the_fallback_for_every_table(self):
        from src.config import Config

        with patch.dict(os.environ, {"DYNAMODB_TABLE_PREFIX": "Local"}, clear=True):
            names = Config().table_names()
        # U5: 古い 2 つの紐付けのテーブルは読まない（PersonaBindings にまとめた）
        assert len(names) == 8
        assert names["Jobs"] == "Local_Jobs"
        assert "PersonaKBBindings" not in names
        assert "PersonaDatasetBindings" not in names

    def test_unknown_table_kind_is_an_error(self):
        from src.config import Config

        with pytest.raises(KeyError):
            Config().table_name("Sessions")


class TestResourceSharesTableName:
    """ResourceShares の物理名（U4。TABLE_NAME_SETTINGS とは別の項目）。"""

    def test_falls_back_to_the_prefix(self):
        from src.config import Config

        with patch.dict(os.environ, {"DYNAMODB_TABLE_PREFIX": "Local"}, clear=True):
            config = Config()
        assert config.RESOURCE_SHARES_TABLE_NAME == "Local_ResourceShares"

    def test_reads_the_cdk_name(self):
        from src.config import Config

        with patch.dict(
            os.environ, {"RESOURCE_SHARES_TABLE_NAME": "Data-SharesXYZ"}, clear=True
        ):
            config = Config()
        assert config.RESOURCE_SHARES_TABLE_NAME == "Data-SharesXYZ"

    def test_is_not_one_of_the_bulk_granted_tables(self):
        from src.config import TABLE_NAME_SETTINGS, Config

        assert "ResourceShares" not in TABLE_NAME_SETTINGS
        with patch.dict(os.environ, {}, clear=True):
            assert "ResourceShares" not in Config().table_names()


class TestPersonaU5Tables:
    """U5 の 2 テーブルの物理名と候補の保持時間（infrastructure-specification.md）。"""

    @pytest.mark.parametrize(
        ("setting", "suffix"),
        [
            ("PERSONA_CANDIDATES_TABLE_NAME", "PersonaCandidates"),
            ("PERSONA_BINDINGS_TABLE_NAME", "PersonaBindings"),
        ],
    )
    def test_falls_back_to_the_prefix(self, setting, suffix):
        from src.config import Config

        with patch.dict(os.environ, {"DYNAMODB_TABLE_PREFIX": "Local"}, clear=True):
            config = Config()
        assert getattr(config, setting) == f"Local_{suffix}"

    @pytest.mark.parametrize(
        "setting", ["PERSONA_CANDIDATES_TABLE_NAME", "PERSONA_BINDINGS_TABLE_NAME"]
    )
    def test_reads_the_cdk_name(self, setting):
        from src.config import Config

        with patch.dict(os.environ, {setting: "Data-TableXYZ"}, clear=True):
            config = Config()
        assert getattr(config, setting) == "Data-TableXYZ"

    def test_new_tables_are_not_bulk_granted(self):
        from src.config import TABLE_NAME_SETTINGS

        assert "PersonaCandidates" not in TABLE_NAME_SETTINGS
        assert "PersonaBindings" not in TABLE_NAME_SETTINGS

    def test_old_binding_table_settings_are_gone(self):
        from src.config import Config

        config = Config()
        assert not hasattr(config, "PERSONA_DATASET_BINDINGS_TABLE_NAME")
        assert not hasattr(config, "PERSONA_KB_BINDINGS_TABLE_NAME")

    def test_candidates_live_eight_hours_by_default(self):
        from src.config import Config

        with patch.dict(os.environ, {}, clear=True):
            assert Config().PERSONA_CACHE_TTL_SECONDS == 28800
