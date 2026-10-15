"""
Integration tests for Agent Discussion Manager

U6: the round loop is ``_rounds`` (run by a principal, checking the
participants before each statement) and the result is saved through
``DiscussionRecords``.
"""

import pytest

from src.models.errors import ErrorCode
from unittest.mock import Mock

from src.managers.agent_discussion_manager import (
    AgentDiscussionManager,
    AgentDiscussionManagerError,
)
from src.models.persona import Persona
from src.models.discussion import Discussion
from src.services.agent_service import PersonaAgent, FacilitatorAgent
from tests.unit.discussion_world import OWNER, World


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


def _agents(personas):
    agents = []
    for persona in personas:
        agent = Mock(spec=PersonaAgent)
        agent.get_persona_id.return_value = persona.id
        agent.get_persona_name.return_value = persona.name
        agent.respond.return_value = f"{persona.name}の発言内容です。"
        agents.append(agent)
    return agents


@pytest.fixture
def mock_persona_agents(sample_personas):
    """Create mock persona agents"""
    return _agents(sample_personas)


def _facilitator():
    facilitator = Mock(spec=FacilitatorAgent)
    facilitator.rounds = 2
    facilitator.additional_instructions = "追加の指示"
    facilitator.start_discussion.return_value = "議論を開始します"
    facilitator.invoke.return_value = "ラウンドの要約"
    facilitator.invoke_streaming.side_effect = lambda prompt: iter(
        ["ラウンド", "の", "要約"]
    )
    facilitator.clear_conversation_history = Mock()
    return facilitator


@pytest.fixture
def mock_facilitator():
    """Create mock facilitator agent"""
    return _facilitator()


def _manager() -> AgentDiscussionManager:
    return AgentDiscussionManager(
        agent_service=Mock(),
        database_service=Mock(),
        dataset_analysis_service=Mock(),
        binding_store=Mock(),
        participants=Mock(can_read_all=Mock(return_value=True)),
        resource_lookup=Mock(),
        discussion_records=Mock(),
    )


def _messages(manager, personas, topic, agents, facilitator):
    events = list(
        manager._rounds(OWNER, personas, topic, agents, facilitator, streaming=False)
    )
    kind, messages = events[-1]
    assert kind == "complete"
    return messages


class TestAgentDiscussionManager:
    """Test cases for AgentDiscussionManager"""

    def test_rounds_success(
        self, sample_personas, mock_persona_agents, mock_facilitator
    ):
        """Test successful agent discussion execution"""
        messages = _messages(
            _manager(),
            sample_personas,
            "新製品のマーケティング戦略について",
            mock_persona_agents,
            mock_facilitator,
        )

        assert len(messages) > 0
        mock_facilitator.start_discussion.assert_called_once()
        assert mock_facilitator.invoke.call_count == 2
        for agent in mock_persona_agents:
            assert agent.respond.called

    def test_rounds_produce_numbered_statements(
        self, sample_personas, mock_persona_agents, mock_facilitator
    ):
        """Test that discussion contains statements with round numbers"""
        messages = _messages(
            _manager(),
            sample_personas,
            "顧客満足度向上施策",
            mock_persona_agents,
            mock_facilitator,
        )

        statements = [m for m in messages if m.message_type == "statement"]
        assert len(statements) > 0
        for msg in statements:
            assert msg.round_number is not None and msg.round_number > 0
        assert {m.round_number for m in messages} == {1, 2}

    def test_invalid_personas(self, mock_persona_agents, mock_facilitator):
        """Test error handling for invalid personas"""
        manager = _manager()
        with pytest.raises(AgentDiscussionManagerError):
            list(
                manager._rounds(
                    OWNER,
                    [],
                    "テストトピック",
                    mock_persona_agents,
                    mock_facilitator,
                    streaming=False,
                )
            )

        single = Persona.create_new(
            name="Test",
            age=30,
            occupation="Test",
            background="Test",
            values=[],
            pain_points=[],
            goals=[],
        )
        with pytest.raises(AgentDiscussionManagerError):
            list(
                manager._rounds(
                    OWNER,
                    [single],
                    "テストトピック",
                    mock_persona_agents,
                    _facilitator(),
                    streaming=False,
                )
            )

    @pytest.mark.parametrize("topic", ["", "短い"])
    def test_invalid_topic(
        self, topic, sample_personas, mock_persona_agents, mock_facilitator
    ):
        """Test error handling for invalid topic"""
        with pytest.raises(AgentDiscussionManagerError):
            list(
                _manager()._rounds(
                    OWNER,
                    sample_personas,
                    topic,
                    mock_persona_agents,
                    mock_facilitator,
                    streaming=False,
                )
            )

    def test_saved_discussion_keeps_the_agent_config(self):
        """The saved body is an agent discussion with its rounds and instructions."""
        world = World()
        personas = [world.persona_bodies["p1"], world.persona_bodies["p2"]]
        agent_service = Mock()
        agent_service.create_persona_agent.side_effect = lambda **kw: _agents(
            [kw["persona"]]
        )[0]
        agent_service.create_facilitator_agent.return_value = _facilitator()
        ai = Mock()
        ai.extract_insights.return_value = []
        manager = AgentDiscussionManager(
            agent_service=agent_service,
            database_service=Mock(),
            dataset_analysis_service=Mock(),
            file_attachments=world.files,
            binding_store=Mock(list_for_persona=Mock(return_value=[])),
            discussion_records=world.records,
            participants=world.participants,
            resource_lookup=world.lookup,
            ai_service=ai,
        )

        discussion = manager.run_agent_discussion_full(
            OWNER,
            personas,
            "新製品のマーケティング戦略について",
            rounds=2,
            additional_instructions="追加の指示",
        )

        body = world.store.bodies[discussion.id]
        assert body["mode"] == "agent"
        assert body["agent_config"]["rounds"] == 2
        assert body["agent_config"]["additional_instructions"] == "追加の指示"
        assert body["message_count"] == len(world.store.messages[discussion.id])

    def test_save_refuses_a_non_agent_discussion(self):
        """Saving a non-agent body through the agent path is refused."""
        from src.managers.components import discussion_validation

        discussion = Discussion.create_new(
            topic="Test topic", participants=["persona1", "persona2"], mode="classic"
        )
        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            discussion_validation.validate_for_save(
                discussion,
                error_cls=AgentDiscussionManagerError,
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
                require_mode="agent",
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_OPERATION_FAILED
        # 診断メッセージには不正だったモードが残る（ログ用）
        assert "'classic'" in str(exc_info.value)
