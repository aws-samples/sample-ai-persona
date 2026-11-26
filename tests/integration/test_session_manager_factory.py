"""Session Manager Factory Unit Tests
Unit tests for the AgentCoreMemorySessionManager factory
"""

import pytest
from unittest.mock import Mock, patch

# Mark tests that need the bedrock_agentcore module
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
    """Tests for the session manager factory"""

    def setup_method(self):
        """Initialization run before each test method"""
        pass

    @patch("src.services.memory.session_manager_factory.config")
    def test_is_memory_enabled_returns_true_when_configured(self, mock_config):
        """Return True when memory is configured correctly"""
        from src.services.memory.session_manager_factory import is_memory_enabled

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"

        result = is_memory_enabled()

        assert result is True

    @patch("src.services.memory.session_manager_factory.config")
    def test_is_memory_enabled_returns_false_when_disabled(self, mock_config):
        """Return False when ENABLE_LONG_TERM_MEMORY is False"""
        from src.services.memory.session_manager_factory import is_memory_enabled

        mock_config.ENABLE_LONG_TERM_MEMORY = False
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"

        result = is_memory_enabled()

        assert result is False

    @patch("src.services.memory.session_manager_factory.config")
    def test_is_memory_enabled_returns_false_when_memory_id_not_set(self, mock_config):
        """Return False when AGENTCORE_MEMORY_ID is not set"""
        from src.services.memory.session_manager_factory import is_memory_enabled

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = None

        result = is_memory_enabled()

        assert result is False

    @patch("src.services.memory.session_manager_factory.config")
    def test_is_memory_enabled_returns_false_when_memory_id_empty(self, mock_config):
        """Return False when AGENTCORE_MEMORY_ID is an empty string"""
        from src.services.memory.session_manager_factory import is_memory_enabled

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = ""

        result = is_memory_enabled()

        assert result is False

    @patch("src.services.memory.session_manager_factory.config")
    def test_create_session_manager_returns_none_when_disabled(self, mock_config):
        """Return None when memory is disabled"""
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
        """Return None when AGENTCORE_MEMORY_ID is not set"""
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
        """Create a session manager when memory is enabled"""
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

    @patch(
        "bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager"
    )
    @patch("bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig")
    @patch("bedrock_agentcore.memory.integrations.strands.config.RetrievalConfig")
    @patch("src.services.memory.session_manager_factory.config")
    def test_default_summary_namespace_spans_all_sessions(
        self,
        mock_config,
        mock_retrieval_config,
        mock_memory_config,
        mock_session_manager,
    ):
        """The default Summary search namespace spans sessions.

        Including sessions/{sessionId} would pin the search to the current session and
        lose memories of past sessions; this guards against that regression.
        """
        from src.services.memory.session_manager_factory import (
            create_agentcore_session_manager,
        )

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"
        mock_config.AGENTCORE_MEMORY_REGION = "us-east-1"
        mock_config.SUMMARY_MEMORY_STRATEGY_ID = "summary-test"
        mock_config.SEMANTIC_MEMORY_STRATEGY_ID = None
        mock_config.MEMORY_MAX_RESULTS = 5

        create_agentcore_session_manager(
            actor_id="test-actor", session_id="test-session"
        )

        retrieval_namespaces = mock_memory_config.call_args.kwargs[
            "retrieval_config"
        ].keys()
        assert "/strategies/summary-test/actors/{actorId}" in retrieval_namespaces
        assert all("{sessionId}" not in namespace for namespace in retrieval_namespaces)

    @patch("src.services.memory.session_manager_factory.config")
    def test_create_session_manager_handles_import_error(self, mock_config):
        """Raise an error when bedrock_agentcore is not installed"""
        from src.services.memory.session_manager_factory import (
            create_agentcore_session_manager,
            SessionManagerFactoryError,
        )

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"

        # Mock the bedrock_agentcore import to raise ImportError
        with patch.dict("sys.modules", {"bedrock_agentcore": None}):
            with patch("builtins.__import__", side_effect=ImportError("No module")):
                with pytest.raises(SessionManagerFactoryError):
                    create_agentcore_session_manager(
                        actor_id="test-actor", session_id="test-session"
                    )


class TestSessionManagerFactoryWithCustomConfig:
    """Tests for the session manager factory with custom settings"""

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
        """Create a session manager with a custom retrieval_config"""
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
        # Check that RetrievalConfig was called
        mock_retrieval_config_class.assert_called()


class TestSessionManagerFactoryWithSemanticStrategy:
    """Tests for the session manager factory with the Semantic strategy"""

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
        """When the Semantic strategy is set, its namespace is also searched"""
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
        # Check that RetrievalConfig was called twice (Summary + Semantic)
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
        """When the Semantic strategy is not set, only Summary is searched"""
        from src.services.memory.session_manager_factory import (
            create_agentcore_session_manager,
        )

        mock_config.ENABLE_LONG_TERM_MEMORY = True
        mock_config.AGENTCORE_MEMORY_ID = "test-memory-id"
        mock_config.AGENTCORE_MEMORY_REGION = "us-east-1"
        mock_config.SUMMARY_MEMORY_STRATEGY_ID = "summary-test"
        mock_config.SEMANTIC_MEMORY_STRATEGY_ID = None  # no Semantic strategy
        mock_config.MEMORY_MAX_RESULTS = 5

        mock_session_manager_instance = Mock()
        mock_session_manager.return_value = mock_session_manager_instance

        result = create_agentcore_session_manager(
            actor_id="test-actor", session_id="test-session"
        )

        assert result is mock_session_manager_instance
        # Check that RetrievalConfig was called once (Summary only)
        assert mock_retrieval_config_class.call_count == 1


class TestSessionManagerFactoryMemoryMode:
    """Tests for the memory_mode parameter"""

    @patch("src.services.memory.session_manager_factory.config")
    def test_create_session_manager_returns_none_when_memory_mode_disabled(
        self, mock_config
    ):
        """Return None when memory_mode='disabled'"""
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
        """Return the base session manager when memory_mode='full'"""
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

        # The base session manager is returned
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
        """Return RetrieveOnlySessionManager when memory_mode='retrieve_only'"""
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

        # RetrieveOnlySessionManager is returned
        assert isinstance(result, RetrieveOnlySessionManager)
        assert result._actor_id == "test-actor"
        assert result._session_id == "test-session"


class TestRetrieveOnlySessionManager:
    """Tests for RetrieveOnlySessionManager"""

    def test_append_message_does_not_save(self):
        """Check that append_message does not save to STM"""
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

        # Call append_message
        manager.append_message(mock_message, mock_agent)

        # Check that the base manager's append_message was not called
        mock_base_manager.append_message.assert_not_called()

    def test_create_message_does_not_save(self):
        """Check that create_message does not save to STM"""
        from src.services.memory.retrieve_only_session_manager import (
            RetrieveOnlySessionManager,
        )

        mock_base_manager = Mock()
        manager = RetrieveOnlySessionManager(
            base_manager=mock_base_manager,
            actor_id="test-actor",
            session_id="test-session",
        )

        # Call create_message
        result = manager.create_message("session-id", "agent-id", Mock())

        # None is returned (nothing is saved)
        assert result is None
        # Check that the base manager's create_message was not called
        mock_base_manager.create_message.assert_not_called()

    def test_create_session_does_not_save(self):
        """Check that create_session does not save to STM"""
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

        # Call create_session
        result = manager.create_session(mock_session)

        # The session is returned as is
        assert result is mock_session
        # Check that the base manager's create_session was not called
        mock_base_manager.create_session.assert_not_called()

    def test_save_does_not_save(self):
        """Check that save does not save to STM"""
        from src.services.memory.retrieve_only_session_manager import (
            RetrieveOnlySessionManager,
        )

        mock_base_manager = Mock()
        manager = RetrieveOnlySessionManager(
            base_manager=mock_base_manager,
            actor_id="test-actor",
            session_id="test-session",
        )

        # Call save
        manager.save([Mock(), Mock()])

        # Check that the base manager's save was not called
        mock_base_manager.save.assert_not_called()

    def test_retrieve_customer_context_delegates_to_base(self):
        """Check that retrieve_customer_context is delegated to the base manager"""
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

        # Call retrieve_customer_context
        manager.retrieve_customer_context(mock_event)

        # Check that the base manager's retrieve_customer_context was called
        mock_base_manager.retrieve_customer_context.assert_called_once_with(mock_event)

    def test_config_property_delegates_to_base(self):
        """Check that the config property is delegated to the base manager"""
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

        # Access the config property
        result = manager.config

        # The base manager's config is returned
        assert result is mock_base_manager.config

    def test_register_hooks_registers_retrieve_only(self):
        """Check that register_hooks registers only retrieve_customer_context"""
        # Skip if the strands module is missing
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

        # Call register_hooks
        manager.register_hooks(mock_registry)

        # Check that the base manager's register_hooks was not called
        mock_base_manager.register_hooks.assert_not_called()

        # Check that the registry's add_callback was called
        mock_registry.add_callback.assert_called_once()

        # Check that the registered callback is retrieve_customer_context
        call_args = mock_registry.add_callback.call_args
        assert call_args[0][1] == manager.retrieve_customer_context

    def test_initialize_does_not_call_base_manager(self):
        """Check that initialize does not call the base manager's initialize"""
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

        # Call initialize
        manager.initialize(mock_agent)

        # Check that the base manager's initialize was not called
        mock_base_manager.initialize.assert_not_called()
