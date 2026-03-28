"""
ServiceFactory の単体テスト

サービスファクトリのシングルトン管理をテストします。
"""

import pytest
from unittest.mock import Mock, patch


class TestServiceFactory:
    """ServiceFactory のテストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化"""
        # シングルトンをリセット
        from src.services.service_factory import ServiceFactory

        ServiceFactory._instance = None
        ServiceFactory._ai_service = None
        ServiceFactory._agent_service = None
        ServiceFactory._database_service = None

    def test_singleton_pattern(self):
        """シングルトンパターンが正しく動作することを確認"""
        from src.services.service_factory import ServiceFactory

        factory1 = ServiceFactory()
        factory2 = ServiceFactory()

        assert factory1 is factory2

    @patch("src.services.service_factory.AIService")
    def test_get_ai_service_creates_instance(self, mock_ai_service_class):
        """AIServiceインスタンスが作成されることを確認"""
        from src.services.service_factory import ServiceFactory

        mock_ai_service = Mock()
        mock_ai_service_class.return_value = mock_ai_service

        factory = ServiceFactory()
        service = factory.get_ai_service()

        assert service is mock_ai_service
        mock_ai_service_class.assert_called_once()

    @patch("src.services.service_factory.AIService")
    def test_get_ai_service_returns_same_instance(self, mock_ai_service_class):
        """AIServiceが同じインスタンスを返すことを確認"""
        from src.services.service_factory import ServiceFactory

        mock_ai_service = Mock()
        mock_ai_service_class.return_value = mock_ai_service

        factory = ServiceFactory()
        service1 = factory.get_ai_service()
        service2 = factory.get_ai_service()

        assert service1 is service2
        # 1回だけ作成されることを確認
        mock_ai_service_class.assert_called_once()

    @patch("src.services.service_factory.AgentService")
    def test_get_agent_service_creates_instance(self, mock_agent_service_class):
        """AgentServiceインスタンスが作成されることを確認"""
        from src.services.service_factory import ServiceFactory

        mock_agent_service = Mock()
        mock_agent_service_class.return_value = mock_agent_service

        factory = ServiceFactory()
        service = factory.get_agent_service()

        assert service is mock_agent_service
        mock_agent_service_class.assert_called_once()

    @patch("src.services.service_factory.AgentService")
    def test_get_agent_service_returns_same_instance(self, mock_agent_service_class):
        """AgentServiceが同じインスタンスを返すことを確認"""
        from src.services.service_factory import ServiceFactory

        mock_agent_service = Mock()
        mock_agent_service_class.return_value = mock_agent_service

        factory = ServiceFactory()
        service1 = factory.get_agent_service()
        service2 = factory.get_agent_service()

        assert service1 is service2
        mock_agent_service_class.assert_called_once()

    @patch("src.services.service_factory.DatabaseService")
    def test_get_database_service_creates_instance(self, mock_db_service_class):
        """DatabaseServiceインスタンスが作成されることを確認"""
        from src.services.service_factory import ServiceFactory

        mock_db_service = Mock()
        mock_db_service_class.return_value = mock_db_service

        factory = ServiceFactory()
        service = factory.get_database_service()

        assert service is mock_db_service
        mock_db_service_class.assert_called_once()

    @patch("src.services.service_factory.DatabaseService")
    def test_get_database_service_returns_same_instance(self, mock_db_service_class):
        """DatabaseServiceが同じインスタンスを返すことを確認"""
        from src.services.service_factory import ServiceFactory

        mock_db_service = Mock()
        mock_db_service_class.return_value = mock_db_service

        factory = ServiceFactory()
        service1 = factory.get_database_service()
        service2 = factory.get_database_service()

        assert service1 is service2
        mock_db_service_class.assert_called_once()

    @patch("src.services.service_factory.AIService")
    def test_ai_service_creation_error_handling(self, mock_ai_service_class):
        """AIService作成エラーが適切に処理されることを確認"""
        from src.services.service_factory import ServiceFactory

        mock_ai_service_class.side_effect = Exception(
            "AI Service initialization failed"
        )

        factory = ServiceFactory()

        with pytest.raises(Exception) as exc_info:
            factory.get_ai_service()

        assert "AI Service initialization failed" in str(exc_info.value)

    @patch("src.services.service_factory.AgentService")
    def test_agent_service_creation_error_handling(self, mock_agent_service_class):
        """AgentService作成エラーが適切に処理されることを確認"""
        from src.services.service_factory import ServiceFactory

        mock_agent_service_class.side_effect = Exception(
            "Agent Service initialization failed"
        )

        factory = ServiceFactory()

        with pytest.raises(Exception) as exc_info:
            factory.get_agent_service()

        assert "Agent Service initialization failed" in str(exc_info.value)


class TestServiceFactoryMemoryService:
    """ServiceFactory のMemoryService関連テストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化"""
        from src.services.service_factory import ServiceFactory

        ServiceFactory._instance = None

    @patch("src.services.service_factory.config")
    def test_get_memory_service_returns_none_when_disabled(self, mock_config):
        """ENABLE_LONG_TERM_MEMORYがFalseの場合Noneを返すことを確認"""
        from src.services.service_factory import ServiceFactory

        mock_config.ENABLE_LONG_TERM_MEMORY = False
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"

        factory = ServiceFactory()
        result = factory.get_memory_service()

        assert result is None

    @patch("src.services.service_factory.config")
    def test_get_memory_service_returns_none_when_memory_id_not_set(self, mock_config):
        """AGENTCORE_MEMORY_IDが設定されていない場合Noneを返すことを確認"""
        from src.services.service_factory import ServiceFactory

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = None

        factory = ServiceFactory()
        result = factory.get_memory_service()

        assert result is None

    @patch("src.services.service_factory.config")
    def test_get_memory_service_returns_none_when_memory_id_empty(self, mock_config):
        """AGENTCORE_MEMORY_IDが空文字の場合Noneを返すことを確認"""
        from src.services.service_factory import ServiceFactory

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = ""

        factory = ServiceFactory()
        result = factory.get_memory_service()

        assert result is None

    @patch("src.services.memory.memory_service.MemoryService")
    @patch("src.services.service_factory.config")
    def test_get_memory_service_creates_instance(
        self, mock_config, mock_memory_service_class
    ):
        """MemoryServiceインスタンスが作成されることを確認"""
        from src.services.service_factory import ServiceFactory

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"
        mock_config.AGENTCORE_MEMORY_REGION = "us-east-1"

        mock_memory_service = Mock()
        mock_memory_service_class.return_value = mock_memory_service

        factory = ServiceFactory()
        service = factory.get_memory_service()

        assert service is mock_memory_service
        mock_memory_service_class.assert_called_once_with(
            memory_id="test-memory-id", region="us-east-1", validate_connection=True
        )

    @patch("src.services.memory.memory_service.MemoryService")
    @patch("src.services.service_factory.config")
    def test_get_memory_service_returns_same_instance(
        self, mock_config, mock_memory_service_class
    ):
        """MemoryServiceが同じインスタンスを返すことを確認"""
        from src.services.service_factory import ServiceFactory

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"
        mock_config.AGENTCORE_MEMORY_REGION = "us-east-1"

        mock_memory_service = Mock()
        mock_memory_service_class.return_value = mock_memory_service

        factory = ServiceFactory()
        service1 = factory.get_memory_service()
        service2 = factory.get_memory_service()

        assert service1 is service2
        mock_memory_service_class.assert_called_once()

    @patch("src.services.memory.memory_service.MemoryService")
    @patch("src.services.service_factory.config")
    def test_get_memory_service_handles_creation_error(
        self, mock_config, mock_memory_service_class
    ):
        """MemoryService作成エラーが適切に処理されることを確認"""
        from src.services.service_factory import ServiceFactory

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"
        mock_config.AGENTCORE_MEMORY_REGION = "us-east-1"

        mock_memory_service_class.side_effect = Exception(
            "Memory Service initialization failed"
        )

        factory = ServiceFactory()
        result = factory.get_memory_service()

        # エラー時はNoneを返す（例外は投げない）
        assert result is None

    @patch("src.services.memory.session_manager_factory.config")
    def test_is_memory_enabled_returns_false_when_disabled(self, mock_config):
        """ENABLE_LONG_TERM_MEMORYがFalseの場合Falseを返すことを確認"""
        from src.services.service_factory import ServiceFactory

        mock_config.ENABLE_LONG_TERM_MEMORY = False
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"

        factory = ServiceFactory()
        result = factory.is_memory_enabled()

        assert result is False

    @patch("src.services.memory.session_manager_factory.config")
    def test_is_memory_enabled_returns_false_when_memory_id_not_set(self, mock_config):
        """AGENTCORE_MEMORY_IDが設定されていない場合Falseを返すことを確認"""
        from src.services.service_factory import ServiceFactory

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = None

        factory = ServiceFactory()
        result = factory.is_memory_enabled()

        assert result is False

    @patch("src.services.memory.session_manager_factory.config")
    def test_is_memory_enabled_returns_false_when_memory_id_empty(self, mock_config):
        """AGENTCORE_MEMORY_IDが空文字の場合Falseを返すことを確認"""
        from src.services.service_factory import ServiceFactory

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = ""

        factory = ServiceFactory()
        result = factory.is_memory_enabled()

        assert result is False

    @patch("src.services.memory.session_manager_factory.config")
    def test_is_memory_enabled_returns_true_when_enabled(self, mock_config):
        """機能が有効な場合Trueを返すことを確認"""
        from src.services.service_factory import ServiceFactory

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"

        factory = ServiceFactory()
        result = factory.is_memory_enabled()

        assert result is True

    @patch("src.services.memory.session_manager_factory.config")
    def test_get_service_status_includes_memory_enabled(self, mock_config):
        """get_service_statusがmemory_enabled情報を含むことを確認"""
        from src.services.service_factory import ServiceFactory

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"
        mock_config.S3_BUCKET_NAME = None

        factory = ServiceFactory()
        status = factory.get_service_status()

        assert "memory_enabled" in status
        assert status["memory_enabled"] is True


class TestServiceFactoryModuleLevelInstance:
    """モジュールレベルのservice_factoryインスタンスのテスト"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化"""
        from src.services.service_factory import ServiceFactory

        ServiceFactory._instance = None
        ServiceFactory._ai_service = None
        ServiceFactory._agent_service = None
        ServiceFactory._database_service = None

    def test_module_level_instance_exists(self):
        """モジュールレベルのインスタンスが存在することを確認"""
        from src.services.service_factory import service_factory

        assert service_factory is not None

    @patch("src.services.service_factory.AIService")
    def test_module_level_instance_provides_services(self, mock_ai_service_class):
        """モジュールレベルのインスタンスがサービスを提供することを確認"""
        # 新しいServiceFactoryインスタンスを作成してテスト
        from src.services.service_factory import ServiceFactory

        mock_ai_service = Mock()
        mock_ai_service_class.return_value = mock_ai_service

        # 新しいファクトリーを作成
        factory = ServiceFactory()
        factory._ai_service = None  # キャッシュをクリア

        service = factory.get_ai_service()

        assert service is mock_ai_service


class TestServiceFactoryS3Service:
    """S3Serviceの取得テスト"""

    def setup_method(self):
        from src.services.service_factory import ServiceFactory
        ServiceFactory._instance = None

    @patch("src.services.service_factory.config")
    def test_get_s3_service_returns_none_when_no_bucket(self, mock_config):
        from src.services.service_factory import ServiceFactory
        mock_config.S3_BUCKET_NAME = None
        factory = ServiceFactory()
        assert factory.get_s3_service() is None

    @patch("src.services.service_factory.config")
    def test_get_s3_service_creates_instance(self, mock_config):
        from src.services.service_factory import ServiceFactory
        mock_config.S3_BUCKET_NAME = "test-bucket"
        mock_config.AWS_REGION = "us-east-1"
        factory = ServiceFactory()
        with patch("src.services.service_factory.ServiceFactory.get_s3_service") as mock_get:
            mock_get.return_value = Mock()
            result = factory.get_s3_service()
            assert result is not None


class TestServiceFactorySurveyService:
    """SurveyServiceの取得テスト"""

    def setup_method(self):
        from src.services.service_factory import ServiceFactory
        ServiceFactory._instance = None

    @patch("src.services.service_factory.AIService")
    @patch("src.services.service_factory.config")
    def test_get_survey_service_creates_instance(self, mock_config, mock_ai_cls):
        from src.services.service_factory import ServiceFactory
        mock_config.S3_BUCKET_NAME = None
        mock_config.AWS_REGION = "us-east-1"
        mock_config.DYNAMODB_TABLE_PREFIX = "Test"
        mock_config.DYNAMODB_REGION = "us-east-1"
        mock_config.BEDROCK_MODEL_ID = "test-model"
        mock_config.BATCH_INFERENCE_MODEL_ID = "test-model"
        mock_ai_cls.return_value = Mock()

        factory = ServiceFactory()
        with patch("src.services.survey_service.SurveyService.__init__", return_value=None):
            service = factory.get_survey_service()
            assert service is not None


class TestServiceFactoryResetServices:
    """reset_servicesのテスト"""

    def setup_method(self):
        from src.services.service_factory import ServiceFactory
        ServiceFactory._instance = None

    def test_reset_services_clears_all(self):
        from src.services.service_factory import ServiceFactory
        factory = ServiceFactory()
        factory._ai_service = Mock()
        factory._database_service = Mock()
        factory._s3_service = Mock()
        factory._survey_service = Mock()
        factory._memory_service = Mock()

        factory.reset_services()

        assert factory._ai_service is None
        assert factory._database_service is None
        assert factory._s3_service is None
        assert factory._survey_service is None
        assert factory._memory_service is None
        assert factory._memory_service_attempted is False
