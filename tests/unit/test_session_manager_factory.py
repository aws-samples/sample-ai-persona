"""
Session Manager Factory Unit Tests
AgentCoreMemorySessionManagerファクトリのユニットテスト
"""

import pytest
from unittest.mock import Mock, patch

# bedrock_agentcoreモジュールが必要なテストをマーク
try:
    import bedrock_agentcore  # noqa: F401
    HAS_BEDROCK_AGENTCORE = True
except ImportError:
    HAS_BEDROCK_AGENTCORE = False

pytestmark = pytest.mark.skipif(
    not HAS_BEDROCK_AGENTCORE,
    reason="bedrock_agentcore is required for these tests",
)


class TestSessionManagerFactory:
    """セッションマネージャーファクトリのテスト"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化"""
        pass

    @patch("src.services.memory.session_manager_factory.config")
    def test_is_memory_enabled_returns_true_when_configured(self, mock_config):
        """メモリが正しく設定されている場合Trueを返す"""
        from src.services.memory.session_manager_factory import is_memory_enabled

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"

        result = is_memory_enabled()

        assert result is True

    @patch("src.services.memory.session_manager_factory.config")
    def test_is_memory_enabled_returns_false_when_disabled(self, mock_config):
        """ENABLE_LONG_TERM_MEMORYがFalseの場合Falseを返す"""
        from src.services.memory.session_manager_factory import is_memory_enabled

        mock_config.ENABLE_LONG_TERM_MEMORY = False
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"

        result = is_memory_enabled()

        assert result is False

    @patch("src.services.memory.session_manager_factory.config")
    def test_is_memory_enabled_returns_false_when_memory_id_not_set(self, mock_config):
        """AGENTCORE_MEMORY_IDが設定されていない場合Falseを返す"""
        from src.services.memory.session_manager_factory import is_memory_enabled

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = None

        result = is_memory_enabled()

        assert result is False

    @patch("src.services.memory.session_manager_factory.config")
    def test_is_memory_enabled_returns_false_when_memory_id_empty(self, mock_config):
        """AGENTCORE_MEMORY_IDが空文字の場合Falseを返す"""
        from src.services.memory.session_manager_factory import is_memory_enabled

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = ""

        result = is_memory_enabled()

        assert result is False

    @patch("src.services.memory.session_manager_factory.config")
    def test_create_session_manager_returns_none_when_disabled(self, mock_config):
        """メモリが無効の場合Noneを返す"""
        from src.services.memory.session_manager_factory import (
            create_agentcore_session_manager,
        )

        mock_config.ENABLE_LONG_TERM_MEMORY = False
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"

        result = create_agentcore_session_manager(
            actor_id="test-actor", session_id="test-session"
        )

        assert result is None

    @patch("src.services.memory.session_manager_factory.config")
    def test_create_session_manager_returns_none_when_memory_id_not_set(
        self, mock_config
    ):
        """AGENTCORE_MEMORY_IDが設定されていない場合Noneを返す"""
        from src.services.memory.session_manager_factory import (
            create_agentcore_session_manager,
        )

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = None

        result = create_agentcore_session_manager(
            actor_id="test-actor", session_id="test-session"
        )

        assert result is None

    @patch(
        "bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager"
    )
    @patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig")
    @patch("bedrock_agentcore.memory.integrations.strands.config.RetrievalConfig")
    @patch("src.services.memory.session_manager_factory.config")
    def test_create_session_manager_creates_instance_when_enabled(
        self,
        mock_config,
        mock_retrieval_config,
        mock_memory_config,
        mock_session_manager,
    ):
        """メモリが有効な場合セッションマネージャーを作成する"""
        from src.services.memory.session_manager_factory import (
            create_agentcore_session_manager,
        )

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"
        mock_config.AGENTCORE_MEMORY_REGION = "us-east-1"
        mock_config.SUMMARY_MEMORY_STRATEGY_ID = "summary-test"
        mock_config.MEMORY_MAX_RESULTS = 5

        mock_session_manager_instance = Mock()
        mock_session_manager.return_value = mock_session_manager_instance

        result = create_agentcore_session_manager(
            actor_id="test-actor", session_id="test-session"
        )

        assert result is mock_session_manager_instance
        mock_session_manager.assert_called_once()

    @patch("src.services.memory.session_manager_factory.config")
    def test_create_session_manager_handles_import_error(self, mock_config):
        """bedrock_agentcoreがインストールされていない場合エラーを発生させる"""
        from src.services.memory.session_manager_factory import (
            create_agentcore_session_manager,
            SessionManagerFactoryError,
        )

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"

        # bedrock_agentcoreのインポートをモックしてImportErrorを発生させる
        with patch.dict("sys.modules", {"bedrock_agentcore": None}):
            with patch("builtins.__import__", side_effect=ImportError("No module")):
                with pytest.raises(SessionManagerFactoryError):
                    create_agentcore_session_manager(
                        actor_id="test-actor", session_id="test-session"
                    )


class TestSessionManagerFactoryWithCustomConfig:
    """カスタム設定でのセッションマネージャーファクトリのテスト"""

    @patch(
        "bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager"
    )
    @patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig")
    @patch("bedrock_agentcore.memory.integrations.strands.config.RetrievalConfig")
    @patch("src.services.memory.session_manager_factory.config")
    def test_create_session_manager_with_custom_retrieval_config(
        self,
        mock_config,
        mock_retrieval_config_class,
        mock_memory_config,
        mock_session_manager,
    ):
        """カスタムretrieval_configでセッションマネージャーを作成する"""
        from src.services.memory.session_manager_factory import (
            create_agentcore_session_manager,
        )

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"
        mock_config.AGENTCORE_MEMORY_REGION = "us-east-1"

        mock_session_manager_instance = Mock()
        mock_session_manager.return_value = mock_session_manager_instance

        custom_retrieval_config = {
            "/preferences/{actorId}": {"top_k": 10, "relevance_score": 0.8}
        }

        result = create_agentcore_session_manager(
            actor_id="test-actor",
            session_id="test-session",
            retrieval_config=custom_retrieval_config,
        )

        assert result is mock_session_manager_instance
        # RetrievalConfigが呼び出されたことを確認
        mock_retrieval_config_class.assert_called()


class TestSessionManagerFactoryWithSemanticStrategy:
    """Semantic戦略を含むセッションマネージャーファクトリのテスト"""

    @patch(
        "bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager"
    )
    @patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig")
    @patch("bedrock_agentcore.memory.integrations.strands.config.RetrievalConfig")
    @patch("src.services.memory.session_manager_factory.config")
    def test_create_session_manager_with_semantic_strategy(
        self,
        mock_config,
        mock_retrieval_config_class,
        mock_memory_config,
        mock_session_manager,
    ):
        """Semantic戦略が設定されている場合、そのnamespaceも検索対象に含める"""
        from src.services.memory.session_manager_factory import (
            create_agentcore_session_manager,
        )

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"
        mock_config.AGENTCORE_MEMORY_REGION = "us-east-1"
        mock_config.SUMMARY_MEMORY_STRATEGY_ID = "summary-test"
        mock_config.SEMANTIC_MEMORY_STRATEGY_ID = "semantic-test"
        mock_config.MEMORY_MAX_RESULTS = 5

        mock_session_manager_instance = Mock()
        mock_session_manager.return_value = mock_session_manager_instance

        result = create_agentcore_session_manager(
            actor_id="test-actor", session_id="test-session"
        )

        assert result is mock_session_manager_instance
        # RetrievalConfigが2回呼び出されたことを確認（Summary + Semantic）
        assert mock_retrieval_config_class.call_count == 2

    @patch(
        "bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager"
    )
    @patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig")
    @patch("bedrock_agentcore.memory.integrations.strands.config.RetrievalConfig")
    @patch("src.services.memory.session_manager_factory.config")
    def test_create_session_manager_without_semantic_strategy(
        self,
        mock_config,
        mock_retrieval_config_class,
        mock_memory_config,
        mock_session_manager,
    ):
        """Semantic戦略が設定されていない場合、Summaryのみ検索対象にする"""
        from src.services.memory.session_manager_factory import (
            create_agentcore_session_manager,
        )

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"
        mock_config.AGENTCORE_MEMORY_REGION = "us-east-1"
        mock_config.SUMMARY_MEMORY_STRATEGY_ID = "summary-test"
        mock_config.SEMANTIC_MEMORY_STRATEGY_ID = None  # Semantic戦略なし
        mock_config.MEMORY_MAX_RESULTS = 5

        mock_session_manager_instance = Mock()
        mock_session_manager.return_value = mock_session_manager_instance

        result = create_agentcore_session_manager(
            actor_id="test-actor", session_id="test-session"
        )

        assert result is mock_session_manager_instance
        # RetrievalConfigが1回だけ呼び出されたことを確認（Summaryのみ）
        assert mock_retrieval_config_class.call_count == 1


class TestSessionManagerFactoryMemoryMode:
    """memory_modeパラメータのテスト"""

    @patch("src.services.memory.session_manager_factory.config")
    def test_create_session_manager_returns_none_when_memory_mode_disabled(
        self, mock_config
    ):
        """memory_mode='disabled'の場合Noneを返す"""
        from src.services.memory.session_manager_factory import (
            create_agentcore_session_manager,
        )

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"

        result = create_agentcore_session_manager(
            actor_id="test-actor", session_id="test-session", memory_mode="disabled"
        )

        assert result is None

    @patch(
        "bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager"
    )
    @patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig")
    @patch("bedrock_agentcore.memory.integrations.strands.config.RetrievalConfig")
    @patch("src.services.memory.session_manager_factory.config")
    def test_create_session_manager_returns_base_manager_when_memory_mode_full(
        self,
        mock_config,
        mock_retrieval_config_class,
        mock_memory_config,
        mock_session_manager,
    ):
        """memory_mode='full'の場合ベースのセッションマネージャーを返す"""
        from src.services.memory.session_manager_factory import (
            create_agentcore_session_manager,
        )

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"
        mock_config.AGENTCORE_MEMORY_REGION = "us-east-1"
        mock_config.SUMMARY_MEMORY_STRATEGY_ID = "summary-test"
        mock_config.SEMANTIC_MEMORY_STRATEGY_ID = None
        mock_config.MEMORY_MAX_RESULTS = 5

        mock_session_manager_instance = Mock()
        mock_session_manager.return_value = mock_session_manager_instance

        result = create_agentcore_session_manager(
            actor_id="test-actor", session_id="test-session", memory_mode="full"
        )

        # ベースのセッションマネージャーが返される
        assert result is mock_session_manager_instance

    @patch(
        "bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager"
    )
    @patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig")
    @patch("bedrock_agentcore.memory.integrations.strands.config.RetrievalConfig")
    @patch("src.services.memory.session_manager_factory.config")
    def test_create_session_manager_returns_retrieve_only_manager_when_memory_mode_retrieve_only(
        self,
        mock_config,
        mock_retrieval_config_class,
        mock_memory_config,
        mock_session_manager,
    ):
        """memory_mode='retrieve_only'の場合RetrieveOnlySessionManagerを返す"""
        from src.services.memory.session_manager_factory import (
            create_agentcore_session_manager,
        )
        from src.services.memory.retrieve_only_session_manager import (
            RetrieveOnlySessionManager,
        )

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"
        mock_config.AGENTCORE_MEMORY_REGION = "us-east-1"
        mock_config.SUMMARY_MEMORY_STRATEGY_ID = "summary-test"
        mock_config.SEMANTIC_MEMORY_STRATEGY_ID = None
        mock_config.MEMORY_MAX_RESULTS = 5

        mock_session_manager_instance = Mock()
        mock_session_manager.return_value = mock_session_manager_instance

        result = create_agentcore_session_manager(
            actor_id="test-actor",
            session_id="test-session",
            memory_mode="retrieve_only",
        )

        # RetrieveOnlySessionManagerが返される
        assert isinstance(result, RetrieveOnlySessionManager)
        assert result._actor_id == "test-actor"
        assert result._session_id == "test-session"


class TestRetrieveOnlySessionManager:
    """RetrieveOnlySessionManagerのテスト"""

    def test_append_message_does_not_save(self):
        """append_messageがSTMに保存しないことを確認"""
        from src.services.memory.retrieve_only_session_manager import (
            RetrieveOnlySessionManager,
        )

        mock_base_manager = Mock()
        manager = RetrieveOnlySessionManager(
            base_manager=mock_base_manager,
            actor_id="test-actor",
            session_id="test-session",
        )

        mock_message = Mock()
        mock_agent = Mock()

        # append_messageを呼び出し
        manager.append_message(mock_message, mock_agent)

        # ベースマネージャーのappend_messageが呼び出されていないことを確認
        mock_base_manager.append_message.assert_not_called()

    def test_create_message_does_not_save(self):
        """create_messageがSTMに保存しないことを確認"""
        from src.services.memory.retrieve_only_session_manager import (
            RetrieveOnlySessionManager,
        )

        mock_base_manager = Mock()
        manager = RetrieveOnlySessionManager(
            base_manager=mock_base_manager,
            actor_id="test-actor",
            session_id="test-session",
        )

        # create_messageを呼び出し
        result = manager.create_message("session-id", "agent-id", Mock())

        # Noneが返される（保存しない）
        assert result is None
        # ベースマネージャーのcreate_messageが呼び出されていないことを確認
        mock_base_manager.create_message.assert_not_called()

    def test_create_session_does_not_save(self):
        """create_sessionがSTMに保存しないことを確認"""
        from src.services.memory.retrieve_only_session_manager import (
            RetrieveOnlySessionManager,
        )

        mock_base_manager = Mock()
        manager = RetrieveOnlySessionManager(
            base_manager=mock_base_manager,
            actor_id="test-actor",
            session_id="test-session",
        )

        mock_session = Mock()

        # create_sessionを呼び出し
        result = manager.create_session(mock_session)

        # セッションがそのまま返される
        assert result is mock_session
        # ベースマネージャーのcreate_sessionが呼び出されていないことを確認
        mock_base_manager.create_session.assert_not_called()

    def test_save_does_not_save(self):
        """saveがSTMに保存しないことを確認"""
        from src.services.memory.retrieve_only_session_manager import (
            RetrieveOnlySessionManager,
        )

        mock_base_manager = Mock()
        manager = RetrieveOnlySessionManager(
            base_manager=mock_base_manager,
            actor_id="test-actor",
            session_id="test-session",
        )

        # saveを呼び出し
        manager.save([Mock(), Mock()])

        # ベースマネージャーのsaveが呼び出されていないことを確認
        mock_base_manager.save.assert_not_called()

    def test_retrieve_customer_context_delegates_to_base(self):
        """retrieve_customer_contextがベースマネージャーに委譲されることを確認"""
        from src.services.memory.retrieve_only_session_manager import (
            RetrieveOnlySessionManager,
        )

        mock_base_manager = Mock()
        manager = RetrieveOnlySessionManager(
            base_manager=mock_base_manager,
            actor_id="test-actor",
            session_id="test-session",
        )

        mock_event = Mock()

        # retrieve_customer_contextを呼び出し
        manager.retrieve_customer_context(mock_event)

        # ベースマネージャーのretrieve_customer_contextが呼び出されることを確認
        mock_base_manager.retrieve_customer_context.assert_called_once_with(mock_event)

    def test_config_property_delegates_to_base(self):
        """configプロパティがベースマネージャーに委譲されることを確認"""
        from src.services.memory.retrieve_only_session_manager import (
            RetrieveOnlySessionManager,
        )

        mock_base_manager = Mock()
        mock_base_manager.config = Mock()

        manager = RetrieveOnlySessionManager(
            base_manager=mock_base_manager,
            actor_id="test-actor",
            session_id="test-session",
        )

        # configプロパティにアクセス
        result = manager.config

        # ベースマネージャーのconfigが返される
        assert result is mock_base_manager.config

    def test_register_hooks_registers_retrieve_only(self):
        """register_hooksがretrieve_customer_contextのみを登録することを確認"""
        # strandsモジュールがない場合はスキップ
        try:
            from strands.hooks import MessageAddedEvent  # noqa: F401
        except ImportError:
            pytest.skip("strands module is required for this test")

        from src.services.memory.retrieve_only_session_manager import (
            RetrieveOnlySessionManager,
        )

        mock_base_manager = Mock()
        manager = RetrieveOnlySessionManager(
            base_manager=mock_base_manager,
            actor_id="test-actor",
            session_id="test-session",
        )

        mock_registry = Mock()

        # register_hooksを呼び出し
        manager.register_hooks(mock_registry)

        # ベースマネージャーのregister_hooksが呼び出されていないことを確認
        mock_base_manager.register_hooks.assert_not_called()

        # registryのadd_callbackが呼び出されたことを確認
        mock_registry.add_callback.assert_called_once()

        # 登録されたコールバックがretrieve_customer_contextであることを確認
        call_args = mock_registry.add_callback.call_args
        assert call_args[0][1] == manager.retrieve_customer_context

    def test_initialize_does_not_call_base_manager(self):
        """initializeがベースマネージャーのinitializeを呼び出さないことを確認"""
        from src.services.memory.retrieve_only_session_manager import (
            RetrieveOnlySessionManager,
        )

        mock_base_manager = Mock()
        manager = RetrieveOnlySessionManager(
            base_manager=mock_base_manager,
            actor_id="test-actor",
            session_id="test-session",
        )

        mock_agent = Mock()

        # initializeを呼び出し
        manager.initialize(mock_agent)

        # ベースマネージャーのinitializeが呼び出されていないことを確認
        mock_base_manager.initialize.assert_not_called()
