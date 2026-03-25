"""
Integration tests for Agent Discussion Manager
"""

import pytest
from unittest.mock import Mock

from src.managers.agent_discussion_manager import (
    AgentDiscussionManager,
    AgentDiscussionManagerError,
)
from src.models.persona import Persona
from src.models.discussion import Discussion
from src.services.agent_service import PersonaAgent, FacilitatorAgent


@pytest.fixture
def sample_personas():
    """Create sample personas for testing"""
    return [
        Persona.create_new(
            name="田中太郎",
            age=35,
            occupation="マーケティングマネージャー",
            background="大手IT企業で10年以上のマーケティング経験",
            values=["データ駆動", "顧客中心"],
            pain_points=["予算制約", "リソース不足"],
            goals=["ROI向上", "ブランド認知度向上"],
        ),
        Persona.create_new(
            name="佐藤花子",
            age=28,
            occupation="プロダクトマネージャー",
            background="スタートアップで製品開発を担当",
            values=["イノベーション", "ユーザー体験"],
            pain_points=["技術的制約", "市場競争"],
            goals=["製品改善", "ユーザー満足度向上"],
        ),
    ]


@pytest.fixture
def mock_persona_agents(sample_personas):
    """Create mock persona agents"""
    agents = []
    for persona in sample_personas:
        agent = Mock(spec=PersonaAgent)
        agent.get_persona_id.return_value = persona.id
        agent.get_persona_name.return_value = persona.name
        agent.respond.return_value = f"{persona.name}の発言内容です。"
        agents.append(agent)
    return agents


@pytest.fixture
def mock_facilitator():
    """Create mock facilitator agent"""
    facilitator = Mock(spec=FacilitatorAgent)
    facilitator.rounds = 2
    facilitator.current_round = 0
    facilitator.additional_instructions = "追加の指示"

    # Mock methods - using current FacilitatorAgent interface
    facilitator.start_discussion.return_value = "議論を開始します"
    facilitator.should_continue.side_effect = [True, True, False]  # 2 rounds
    facilitator.select_next_speaker.side_effect = lambda agents, spoken: (
        agents[0]
        if agents[0].get_persona_id() not in spoken
        else agents[1]
        if len(agents) > 1 and agents[1].get_persona_id() not in spoken
        else None
    )
    facilitator.summarize_round.return_value = "ラウンドの要約"
    facilitator.create_prompt_for_persona.return_value = "発言を促すプロンプト"

    # Mock increment_round to update current_round
    def increment_round():
        facilitator.current_round += 1

    facilitator.increment_round.side_effect = increment_round

    return facilitator


class TestAgentDiscussionManager:
    """Test cases for AgentDiscussionManager"""

    def test_start_agent_discussion_success(
        self, sample_personas, mock_persona_agents, mock_facilitator
    ):
        """Test successful agent discussion execution"""
        # Setup
        mock_agent_service = Mock()
        mock_database_service = Mock()
        mock_database_service.initialize_database.return_value = None

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_database_service
        )
        topic = "新製品のマーケティング戦略について"

        # Execute
        discussion = manager.start_agent_discussion(
            personas=sample_personas,
            topic=topic,
            persona_agents=mock_persona_agents,
            facilitator=mock_facilitator,
        )

        # Verify
        assert discussion is not None
        assert discussion.topic == topic
        assert discussion.mode == "agent"
        assert len(discussion.participants) == 2
        assert len(discussion.messages) > 0

        # Verify facilitator was called
        mock_facilitator.start_discussion.assert_called_once()
        assert mock_facilitator.increment_round.call_count == 2

        # Verify persona agents responded
        for agent in mock_persona_agents:
            assert agent.respond.called

    def test_start_agent_discussion_with_messages(
        self, sample_personas, mock_persona_agents, mock_facilitator
    ):
        """Test that discussion contains statements"""
        # Setup
        mock_agent_service = Mock()
        mock_database_service = Mock()
        mock_database_service.initialize_database.return_value = None

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_database_service
        )
        topic = "顧客満足度向上施策"

        # Execute
        discussion = manager.start_agent_discussion(
            personas=sample_personas,
            topic=topic,
            persona_agents=mock_persona_agents,
            facilitator=mock_facilitator,
        )

        # Verify message types
        statement_messages = [
            msg for msg in discussion.messages if msg.message_type == "statement"
        ]

        assert len(statement_messages) > 0

        # Verify round numbers are set
        for msg in discussion.messages:
            if msg.message_type == "statement":
                assert msg.round_number is not None
                assert msg.round_number > 0

    def test_start_agent_discussion_invalid_personas(
        self, mock_persona_agents, mock_facilitator
    ):
        """Test error handling for invalid personas"""
        mock_agent_service = Mock()
        mock_database_service = Mock()
        mock_database_service.initialize_database.return_value = None

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_database_service
        )

        # Test with empty personas
        with pytest.raises(AgentDiscussionManagerError):
            manager.start_agent_discussion(
                personas=[],
                topic="テスト",
                persona_agents=mock_persona_agents,
                facilitator=mock_facilitator,
            )

        # Test with single persona
        with pytest.raises(AgentDiscussionManagerError):
            manager.start_agent_discussion(
                personas=[
                    Persona.create_new(
                        name="Test",
                        age=30,
                        occupation="Test",
                        background="Test",
                        values=[],
                        pain_points=[],
                        goals=[],
                    )
                ],
                topic="テスト",
                persona_agents=mock_persona_agents,
                facilitator=mock_facilitator,
            )

    def test_start_agent_discussion_invalid_topic(
        self, sample_personas, mock_persona_agents, mock_facilitator
    ):
        """Test error handling for invalid topic"""
        mock_agent_service = Mock()
        mock_database_service = Mock()
        mock_database_service.initialize_database.return_value = None

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_database_service
        )

        # Test with empty topic
        with pytest.raises(AgentDiscussionManagerError):
            manager.start_agent_discussion(
                personas=sample_personas,
                topic="",
                persona_agents=mock_persona_agents,
                facilitator=mock_facilitator,
            )

        # Test with short topic
        with pytest.raises(AgentDiscussionManagerError):
            manager.start_agent_discussion(
                personas=sample_personas,
                topic="短い",
                persona_agents=mock_persona_agents,
                facilitator=mock_facilitator,
            )

    def test_start_agent_discussion_round_progression(
        self, sample_personas, mock_persona_agents, mock_facilitator
    ):
        """Test that rounds progress correctly"""
        # Setup
        mock_agent_service = Mock()
        mock_database_service = Mock()
        mock_database_service.initialize_database.return_value = None

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_database_service
        )
        topic = "製品ロードマップ"

        # Execute
        discussion = manager.start_agent_discussion(
            personas=sample_personas,
            topic=topic,
            persona_agents=mock_persona_agents,
            facilitator=mock_facilitator,
        )

        # Verify round progression
        assert mock_facilitator.increment_round.call_count == 2

        # Verify messages have correct round numbers
        round_numbers = [
            msg.round_number
            for msg in discussion.messages
            if msg.round_number is not None
        ]
        assert 1 in round_numbers
        assert 2 in round_numbers

    def test_save_agent_discussion_success(
        self, sample_personas, mock_persona_agents, mock_facilitator
    ):
        """Test successful saving of agent discussion with agent_config"""
        # Setup
        mock_agent_service = Mock()
        mock_database_service = Mock()
        mock_database_service.initialize_database.return_value = None
        mock_database_service.save_discussion.return_value = "test-discussion-id"

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_database_service
        )
        topic = "新製品のマーケティング戦略について"

        # Create discussion
        discussion = manager.start_agent_discussion(
            personas=sample_personas,
            topic=topic,
            persona_agents=mock_persona_agents,
            facilitator=mock_facilitator,
        )

        # Verify discussion has agent_config
        assert discussion.mode == "agent"
        assert discussion.agent_config is not None
        assert discussion.agent_config["rounds"] == 2
        assert discussion.agent_config["additional_instructions"] == "追加の指示"

        # Save discussion
        discussion_id = manager.save_agent_discussion(discussion)

        # Verify save was called with correct discussion
        assert mock_database_service.save_discussion.called
        saved_discussion = mock_database_service.save_discussion.call_args[0][0]
        assert saved_discussion.mode == "agent"
        assert saved_discussion.agent_config is not None
        assert saved_discussion.agent_config["rounds"] == 2
        assert saved_discussion.agent_config["additional_instructions"] == "追加の指示"
        assert discussion_id == "test-discussion-id"

    def test_save_agent_discussion_invalid_mode(self):
        """Test that saving a non-agent discussion raises error"""
        # Setup
        mock_agent_service = Mock()
        mock_database_service = Mock()
        mock_database_service.initialize_database.return_value = None

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_database_service
        )

        # Create a classic mode discussion
        discussion = Discussion.create_new(
            topic="Test topic", participants=["persona1", "persona2"], mode="classic"
        )

        # Verify error is raised
        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager.save_agent_discussion(discussion)

        assert "Invalid discussion mode" in str(exc_info.value)
        assert "Expected 'agent'" in str(exc_info.value)
