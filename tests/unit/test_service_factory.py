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
    def test_get_s3_service_raises_when_no_bucket(self, mock_config):
        from src.services.service_factory import ServiceFactory

        mock_config.S3_BUCKET_NAME = None
        factory = ServiceFactory()
        with pytest.raises(RuntimeError, match="S3_BUCKET_NAME"):
            factory.get_s3_service()

    @patch("src.services.service_factory.config")
    def test_get_s3_service_creates_instance(self, mock_config):
        from src.services.service_factory import ServiceFactory

        mock_config.S3_BUCKET_NAME = "test-bucket"
        mock_config.AWS_REGION = "us-east-1"
        factory = ServiceFactory()
        with patch(
            "src.services.service_factory.ServiceFactory.get_s3_service"
        ) as mock_get:
            mock_get.return_value = Mock()
            result = factory.get_s3_service()
            assert result is not None


class TestServiceFactorySurveyBatchService:
    """SurveyBatchServiceの取得テスト"""

    def setup_method(self):
        from src.services.service_factory import ServiceFactory

        ServiceFactory._instance = None

    @patch("src.services.service_factory.config")
    def test_get_survey_batch_service_creates_instance(self, mock_config):
        from src.services.service_factory import ServiceFactory

        mock_config.S3_BUCKET_NAME = "test-bucket"
        mock_config.AWS_REGION = "us-east-1"
        mock_config.DYNAMODB_TABLE_PREFIX = "Test"
        mock_config.DYNAMODB_REGION = "us-east-1"
        mock_config.BEDROCK_MODEL_ID = "test-model"
        mock_config.BATCH_INFERENCE_MODEL_ID = "test-model"

        factory = ServiceFactory()
        service = factory.get_survey_batch_service()
        assert service is not None
        assert service.bucket_name == "test-bucket"


class TestServiceFactoryDatasetAnalysisService:
    """DatasetAnalysisServiceの取得テスト"""

    def setup_method(self):
        from src.services.service_factory import ServiceFactory

        ServiceFactory._instance = None

    @patch("src.services.service_factory.config")
    def test_get_dataset_analysis_service_creates_instance(self, mock_config):
        from src.services.service_factory import ServiceFactory

        mock_config.S3_BUCKET_NAME = "test-bucket"
        mock_config.AWS_REGION = "us-east-1"
        mock_config.DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES = 4

        factory = ServiceFactory()
        service = factory.get_dataset_analysis_service()
        assert service is not None

    @patch("src.services.service_factory.config")
    def test_get_dataset_analysis_service_singleton(self, mock_config):
        from src.services.service_factory import ServiceFactory

        mock_config.S3_BUCKET_NAME = "test-bucket"
        mock_config.AWS_REGION = "us-east-1"
        mock_config.DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES = 4

        factory = ServiceFactory()
        s1 = factory.get_dataset_analysis_service()
        s2 = factory.get_dataset_analysis_service()
        assert s1 is s2


class TestServiceFactoryResetServices:
    """reset_servicesのテスト"""

    def setup_method(self):
        from src.services.service_factory import ServiceFactory

        ServiceFactory._instance = None


class TestServiceFactoryAuthzServices:
    """判定用の読み取りと部署の一覧の Service（U2）。"""

    def setup_method(self):
        from src.services.service_factory import ServiceFactory

        ServiceFactory._instance = None

    def teardown_method(self):
        from src.services.service_factory import ServiceFactory

        ServiceFactory._instance = None

    @patch("src.services.service_factory.config")
    def test_authz_lookup_service_uses_the_jobs_table(self, mock_config, monkeypatch):
        from src.services.service_factory import ServiceFactory

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        mock_config.DYNAMODB_REGION = "ap-northeast-1"
        mock_config.table_name.return_value = "Jobs-physical"

        factory = ServiceFactory()
        service = factory.get_authz_lookup_service()

        assert service is factory.get_authz_lookup_service()
        assert service.jobs_table_name == "Jobs-physical"
        mock_config.table_name.assert_called_with("Jobs")
        assert service.client.meta.config.connect_timeout == 0.5

    @patch("src.services.service_factory.config")
    def test_resource_share_store_uses_the_shares_table(self, mock_config, monkeypatch):
        from src.services.service_factory import ServiceFactory

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        mock_config.DYNAMODB_REGION = "ap-northeast-1"
        mock_config.RESOURCE_SHARES_TABLE_NAME = "Shares-physical"

        factory = ServiceFactory()
        store = factory.get_resource_share_store()

        assert store is factory.get_resource_share_store()
        assert store.shares_table == "Shares-physical"
        assert store.client.meta.region_name == "ap-northeast-1"

    @patch("src.services.service_factory.config")
    def test_u5_stores_use_their_tables(self, mock_config, monkeypatch):
        """U5 の 3 つの Service（候補・紐付け・アップロードファイル）。"""
        from src.services.service_factory import ServiceFactory

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        mock_config.DYNAMODB_REGION = "ap-northeast-1"
        mock_config.PERSONA_CANDIDATES_TABLE_NAME = "Candidates-physical"
        mock_config.PERSONA_BINDINGS_TABLE_NAME = "Bindings-physical"
        mock_config.table_name.return_value = "Files-physical"

        factory = ServiceFactory()
        candidates = factory.get_persona_candidate_store()
        bindings = factory.get_persona_binding_store()
        files = factory.get_uploaded_file_store()

        assert candidates is factory.get_persona_candidate_store()
        assert bindings is factory.get_persona_binding_store()
        assert files is factory.get_uploaded_file_store()
        assert candidates.table_name == "Candidates-physical"
        assert bindings.table_name == "Bindings-physical"
        assert files.table_name == "Files-physical"
        mock_config.table_name.assert_called_with("UploadedFiles")
        assert bindings.client.meta.region_name == "ap-northeast-1"

    @patch("src.services.service_factory.config")
    def test_job_store_uses_the_jobs_table(self, mock_config, monkeypatch):
        from src.services.service_factory import ServiceFactory

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        mock_config.DYNAMODB_REGION = "ap-northeast-1"
        mock_config.table_name.return_value = "Jobs-physical"

        factory = ServiceFactory()
        jobs = factory.get_job_store()

        assert jobs is factory.get_job_store()
        assert jobs.table_name == "Jobs-physical"
        mock_config.table_name.assert_called_with("Jobs")
        assert jobs.client.meta.region_name == "ap-northeast-1"

    @patch("src.services.service_factory.config")
    def test_dataset_store_uses_its_table(self, mock_config, monkeypatch):
        from src.services.service_factory import ServiceFactory

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        mock_config.DYNAMODB_REGION = "ap-northeast-1"
        mock_config.table_name.return_value = "Datasets-physical"

        factory = ServiceFactory()
        datasets = factory.get_dataset_store()

        assert datasets is factory.get_dataset_store()
        assert datasets.table_name == "Datasets-physical"
        mock_config.table_name.assert_called_with("Datasets")
        assert datasets.client.meta.region_name == "ap-northeast-1"

    @patch("src.services.service_factory.config")
    def test_knowledge_base_store_uses_its_table(self, mock_config, monkeypatch):
        from src.services.service_factory import ServiceFactory

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        mock_config.DYNAMODB_REGION = "ap-northeast-1"
        mock_config.table_name.return_value = "Kb-physical"

        factory = ServiceFactory()
        kbs = factory.get_knowledge_base_store()

        assert kbs is factory.get_knowledge_base_store()
        assert kbs.table_name == "Kb-physical"
        mock_config.table_name.assert_called_with("KnowledgeBases")
        assert kbs.client.meta.region_name == "ap-northeast-1"

    @patch("src.services.service_factory.config")
    def test_survey_template_store_uses_its_table(self, mock_config, monkeypatch):
        from src.services.service_factory import ServiceFactory

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        mock_config.DYNAMODB_REGION = "ap-northeast-1"
        mock_config.table_name.return_value = "Templates-physical"

        factory = ServiceFactory()
        templates = factory.get_survey_template_store()

        assert templates is factory.get_survey_template_store()
        assert templates.table_name == "Templates-physical"
        mock_config.table_name.assert_called_with("SurveyTemplates")
        assert templates.client.meta.region_name == "ap-northeast-1"

    @patch("src.services.service_factory.config")
    def test_u6_stores_use_their_tables(self, mock_config, monkeypatch):
        """U6 の 2 つの Service（議論・インタビュー）。"""
        from src.services.service_factory import ServiceFactory

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        mock_config.DYNAMODB_REGION = "ap-northeast-1"
        mock_config.table_name.return_value = "Discussions-physical"
        mock_config.DISCUSSION_MESSAGES_TABLE_NAME = "Messages-physical"
        mock_config.DISCUSSION_REPORTS_TABLE_NAME = "Reports-physical"
        mock_config.INTERVIEW_SESSIONS_TABLE_NAME = "Sessions-physical"
        mock_config.INTERVIEW_MESSAGES_TABLE_NAME = "IMessages-physical"

        factory = ServiceFactory()
        discussions = factory.get_discussion_store()
        interviews = factory.get_interview_session_store()

        assert discussions is factory.get_discussion_store()
        assert interviews is factory.get_interview_session_store()
        assert discussions.discussions_table == "Discussions-physical"
        assert discussions.messages_table == "Messages-physical"
        assert discussions.reports_table == "Reports-physical"
        mock_config.table_name.assert_called_with("Discussions")
        assert interviews.sessions_table == "Sessions-physical"
        assert interviews.messages_table == "IMessages-physical"
        assert interviews.client.meta.region_name == "ap-northeast-1"

    @patch("src.services.service_factory.config")
    def test_u7_custom_persona_data_store_uses_its_table(
        self, mock_config, monkeypatch
    ):
        """U7 の Service（カスタムペルソナデータの登録情報）。"""
        from src.services.service_factory import ServiceFactory

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        mock_config.DYNAMODB_REGION = "ap-northeast-1"
        mock_config.CUSTOM_PERSONA_DATA_TABLE_NAME = "Custom-physical"

        factory = ServiceFactory()
        store = factory.get_custom_persona_data_store()

        assert store is factory.get_custom_persona_data_store()
        assert store.table_name == "Custom-physical"
        assert store.client.meta.region_name == "ap-northeast-1"

    @patch("src.services.service_factory.config")
    def test_user_directory_service_needs_a_user_pool(self, mock_config):
        from src.models.errors import CodedError, ErrorCode
        from src.services.service_factory import ServiceFactory

        mock_config.COGNITO_USER_POOL_ID = None
        with pytest.raises(CodedError) as info:
            ServiceFactory().get_user_directory_service()
        assert info.value.code is ErrorCode.AUTH_CONFIG_INVALID

    @patch("src.services.service_factory.config")
    def test_user_directory_service_reads_the_pool(self, mock_config, monkeypatch):
        from src.services.service_factory import ServiceFactory

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        mock_config.COGNITO_USER_POOL_ID = "ap-northeast-1_Pool"

        factory = ServiceFactory()
        service = factory.get_user_directory_service()

        assert service is factory.get_user_directory_service()
        assert service.user_pool_id == "ap-northeast-1_Pool"

    @patch("src.services.service_factory.config")
    def test_cognito_auth_service_is_built_for_admin_api(
        self, mock_config, monkeypatch
    ):
        """U8: AUTH_MODE=admin_api の Cognito 管理 API の Service（1 つだけ作る）。"""
        from src.services.service_factory import ServiceFactory

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        mock_config.AUTH_MODE = "admin_api"
        mock_config.COGNITO_USER_POOL_ID = "ap-northeast-1_Pool"
        mock_config.COGNITO_CLIENT_ID = "client"
        mock_config.COGNITO_CLIENT_SECRET = "secret"

        factory = ServiceFactory()
        service = factory.get_cognito_auth_service()

        assert service is factory.get_cognito_auth_service()
        assert service.user_pool_id == "ap-northeast-1_Pool"
        assert service.client.meta.region_name == "ap-northeast-1"

    @pytest.mark.parametrize(
        "missing",
        ["COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID", "COGNITO_CLIENT_SECRET"],
    )
    @patch("src.services.service_factory.config")
    def test_cognito_auth_service_needs_its_settings(self, mock_config, missing):
        from src.models.errors import CodedError, ErrorCode
        from src.services.service_factory import ServiceFactory

        mock_config.AUTH_MODE = "admin_api"
        mock_config.COGNITO_USER_POOL_ID = "ap-northeast-1_Pool"
        mock_config.COGNITO_CLIENT_ID = "client"
        mock_config.COGNITO_CLIENT_SECRET = "secret"
        setattr(mock_config, missing, None)
        with pytest.raises(CodedError) as info:
            ServiceFactory().get_cognito_auth_service()
        assert info.value.code is ErrorCode.AUTH_CONFIG_INVALID

    @patch("src.services.service_factory.config")
    def test_cognito_groups_service_needs_a_user_pool(self, mock_config):
        from src.models.errors import CodedError, ErrorCode
        from src.services.service_factory import ServiceFactory

        mock_config.COGNITO_USER_POOL_ID = None
        with pytest.raises(CodedError) as info:
            ServiceFactory().get_cognito_groups_service()
        assert info.value.code is ErrorCode.AUTH_CONFIG_INVALID

    @patch("src.services.service_factory.config")
    def test_cognito_groups_service_reads_the_pool(self, mock_config, monkeypatch):
        from src.services.service_factory import ServiceFactory

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        mock_config.COGNITO_USER_POOL_ID = "ap-northeast-1_Pool"

        service = ServiceFactory().get_cognito_groups_service()

        assert service.user_pool_id == "ap-northeast-1_Pool"
        assert service.client.meta.region_name == "ap-northeast-1"

    @patch("src.services.service_factory.config")
    def test_sources_are_cognito_when_a_pool_is_set(self, mock_config, monkeypatch):
        from src.services.service_factory import ServiceFactory

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        mock_config.COGNITO_USER_POOL_ID = "ap-northeast-1_Pool"
        mock_config.AUTH_MODE = "local"
        factory = ServiceFactory()

        assert factory.get_department_source() is factory.get_cognito_groups_service()
        assert factory.get_user_source() is factory.get_user_directory_service()

    @patch("src.services.service_factory.config")
    def test_local_mode_without_a_pool_uses_the_development_user(self, mock_config):
        from src.services.local_directory_service import LocalDirectoryService
        from src.services.service_factory import ServiceFactory

        mock_config.COGNITO_USER_POOL_ID = None
        mock_config.AUTH_MODE = "local"
        mock_config.LOCAL_DEV_USER_GROUPS = "sales, plan"
        mock_config.LOCAL_DEV_USER_NAME = "開発ユーザー"
        mock_config.LOCAL_DEV_USER_EMAIL = "dev@example.com"
        factory = ServiceFactory()

        departments = factory.get_department_source()
        assert isinstance(departments, LocalDirectoryService)
        assert [d.group_id for d in departments.list_departments()] == [
            "sales",
            "plan",
        ]
        (user,) = factory.get_user_source().list_users()
        assert user.sub == "local:dev@example.com" and user.enabled

    @patch("src.services.service_factory.config")
    def test_oidc_without_a_pool_is_a_config_error(self, mock_config):
        from src.models.errors import CodedError, ErrorCode
        from src.services.service_factory import ServiceFactory

        mock_config.COGNITO_USER_POOL_ID = None
        mock_config.AUTH_MODE = "oidc"
        with pytest.raises(CodedError) as info:
            ServiceFactory().get_user_source()
        assert info.value.code is ErrorCode.AUTH_CONFIG_INVALID
