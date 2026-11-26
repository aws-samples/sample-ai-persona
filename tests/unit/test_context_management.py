"""
Tests for context management in agent-mode discussions.
"""

from datetime import datetime
from unittest.mock import Mock

from src.managers.agent_discussion_manager import AgentDiscussionManager
from src.models.message import Message
from src.models.persona import Persona
from src.services.agent_service import FacilitatorAgent, PersonaAgent


def _create_test_persona(
    persona_id: str = "test-persona-1", name: str = "田中太郎"
) -> Persona:
    return Persona(
        id=persona_id,
        name=name,
        age=35,
        occupation="会社員",
        background="IT企業で働く中堅社員",
        values=["効率性", "品質"],
        pain_points=["時間不足"],
        goals=["キャリアアップ"],
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


class TestPersonaAgentClearHistory:
    """Tests for PersonaAgent.clear_conversation_history()."""

    def setup_method(self):
        self.persona = _create_test_persona()
        self.mock_agent = Mock()
        self.mock_agent.messages = [
            {"role": "user", "content": [{"text": "msg1"}]},
            {"role": "assistant", "content": [{"text": "resp1"}]},
        ]
        self.persona_agent = PersonaAgent(self.persona, "test prompt", self.mock_agent)

    def test_clear_conversation_history(self):
        """Conversation history is cleared."""
        assert len(self.mock_agent.messages) == 2
        self.persona_agent.clear_conversation_history()
        assert len(self.mock_agent.messages) == 0

    def test_clear_conversation_history_already_empty(self):
        """An empty history does not raise."""
        self.mock_agent.messages = []
        self.persona_agent.clear_conversation_history()
        assert len(self.mock_agent.messages) == 0

    def test_clear_conversation_history_agent_none(self):
        """agent=None (after dispose) does not raise."""
        self.persona_agent.agent = None
        self.persona_agent.clear_conversation_history()  # should not raise

    def test_clear_conversation_history_no_messages_attr(self):
        """An agent without a messages attribute does not raise."""
        self.mock_agent = Mock(spec=[])
        self.persona_agent.agent = self.mock_agent
        self.persona_agent.clear_conversation_history()  # should not raise


class TestFacilitatorAgentClearHistory:
    """Tests for FacilitatorAgent.clear_conversation_history()."""

    def setup_method(self):
        self.mock_agent = Mock()
        self.mock_agent.messages = [
            {"role": "user", "content": [{"text": "summarize"}]},
            {"role": "assistant", "content": [{"text": "summary"}]},
        ]
        self.facilitator = FacilitatorAgent(3, "", self.mock_agent)

    def test_clear_conversation_history(self):
        """Conversation history is cleared."""
        assert len(self.mock_agent.messages) == 2
        self.facilitator.clear_conversation_history()
        assert len(self.mock_agent.messages) == 0

    def test_clear_conversation_history_agent_none(self):
        """agent=None does not raise."""
        self.facilitator.agent = None
        self.facilitator.clear_conversation_history()  # should not raise


class TestManagerBuildPersonaPrompt:
    """Tests for AgentDiscussionManager._build_persona_prompt()."""

    def setup_method(self):
        from src.managers.agent_discussion_manager import AgentDiscussionManager

        self.manager = AgentDiscussionManager(
            agent_service=Mock(), dataset_store=Mock()
        )

    def test_first_round_no_context(self):
        """Round 1 without context: prompt draws out concrete episodes."""
        persona_agent = Mock(
            get_persona_name=Mock(return_value="田中太郎"),
            get_persona_id=Mock(return_value="p1"),
        )
        prompt = self.manager._build_persona_prompt(
            persona_agent, "新商品について", [], 1, 5
        )
        assert "新商品について" in prompt
        assert "具体的な場面" in prompt

    def test_with_round_summaries(self):
        """Round 2 onward: the summary context is included."""
        persona_agent = Mock(
            get_persona_name=Mock(return_value="田中太郎"),
            get_persona_id=Mock(return_value="p1"),
        )
        summaries = [
            "ラウンド1では価格と品質のトレードオフが議論された",
            "ラウンド2ではターゲット層の絞り込みが論点に",
        ]
        recent = [
            Message.create_new("p1", "佐藤", "品質が重要です"),
            Message.create_new("p2", "鈴木", "コストも考慮すべき"),
        ]
        prompt = self.manager._build_persona_prompt(
            persona_agent,
            "新商品について",
            recent,
            3,
            5,
            round_summaries=summaries,
        )
        assert "これまでの議論の要約" in prompt
        assert "ラウンド1" in prompt
        assert "ラウンド2" in prompt

    def test_facilitator_message_section(self):
        """The facilitator question appears in its dedicated section."""
        persona_agent = Mock(
            get_persona_name=Mock(return_value="田中太郎"),
            get_persona_id=Mock(return_value="p1"),
        )
        recent = [
            Message.create_new("p1", "佐藤", "意見です", message_type="statement"),
            Message.create_new("p2", "鈴木", "賛成です", message_type="statement"),
        ]
        prompt = self.manager._build_persona_prompt(
            persona_agent,
            "テーマ",
            recent,
            2,
            5,
            round_summaries=["ラウンド1の要約"],
            latest_facilitator_message="論点整理: 次は価格について",
        )
        assert "ファシリテータからの問いかけ" in prompt
        assert "論点整理" in prompt

    def test_mid_round_phase_instruction(self):
        """Middle rounds: prompt encourages changes of mind and new viewpoints."""
        persona_agent = Mock(
            get_persona_name=Mock(return_value="田中太郎"),
            get_persona_id=Mock(return_value="p1"),
        )
        recent = [Message.create_new("p1", "佐藤", "意見です")]
        prompt = self.manager._build_persona_prompt(
            persona_agent,
            "テーマ",
            recent,
            3,
            5,
            round_summaries=["要約1", "要約2"],
        )
        assert "変化" in prompt or "気づ" in prompt

    def test_final_round_phase_instruction(self):
        """Final round: prompt asks for a conclusion."""
        persona_agent = Mock(
            get_persona_name=Mock(return_value="田中太郎"),
            get_persona_id=Mock(return_value="p1"),
        )
        recent = [Message.create_new("p1", "佐藤", "意見です")]
        prompt = self.manager._build_persona_prompt(
            persona_agent,
            "テーマ",
            recent,
            5,
            5,
            round_summaries=["要約1", "要約2", "要約3", "要約4"],
        )
        assert "最終" in prompt or "結論" in prompt


class TestManagerBuildSummaryPrompt:
    """Tests for AgentDiscussionManager._build_summary_prompt()."""

    def setup_method(self):
        from src.managers.agent_discussion_manager import AgentDiscussionManager

        self.manager = AgentDiscussionManager(
            agent_service=Mock(), dataset_store=Mock()
        )

    def test_summary_prompt_contains_structure(self):
        """The summary prompt contains structural instructions."""
        messages = [
            Message.create_new("p1", "田中", "価格が重要", message_type="statement"),
            Message.create_new("p2", "佐藤", "品質が重要", message_type="statement"),
        ]
        prompt = self.manager._build_summary_prompt(1, messages, "新商品", 3)
        assert "共通点" in prompt or "対立点" in prompt
        assert "問い" in prompt

    def test_summary_prompt_with_previous_summaries(self):
        """Previous summaries are included."""
        messages = [
            Message.create_new(
                "p1", "田中", "具体的な価格帯は...", message_type="statement"
            ),
        ]
        prev = ["ラウンド1では価格vs品質が論点になった"]
        prompt = self.manager._build_summary_prompt(
            2, messages, "新商品", 3, previous_summaries=prev
        )
        assert "これまでの議論の流れ" in prompt
        assert "ラウンド1" in prompt
        assert "価格vs品質" in prompt

    def test_summary_prompt_without_previous_summaries(self):
        """Round 1 has no previous-summary section."""
        messages = [
            Message.create_new("p1", "田中", "意見です", message_type="statement"),
        ]
        prompt = self.manager._build_summary_prompt(1, messages, "新商品", 3)
        assert "これまでの議論の流れ" not in prompt


def _run(manager, personas, persona_agents, facilitator, streaming=False) -> list:
    """Consume the round loop (the runner is a member who may read everyone)."""
    from tests.unit.discussion_world import OWNER

    return list(
        manager._rounds(
            OWNER,
            personas,
            "テストトピック",
            persona_agents,
            facilitator,
            streaming=streaming,
        )
    )


class TestAgentDiscussionContextManagement:
    """Context management tests for AgentDiscussionManager."""

    def _setup_manager_and_mocks(self, sample_persona, sample_persona_2, rounds=2):
        """Common setup."""
        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service,
            dataset_store=Mock(),
            # The pre-utterance recheck always passes
            participants=Mock(can_read_all=Mock(return_value=True)),
        )

        # Persona agents (messages is a list)
        mock_agent_1 = Mock(spec=PersonaAgent)
        mock_agent_1.get_persona_id.return_value = sample_persona.id
        mock_agent_1.get_persona_name.return_value = sample_persona.name
        mock_agent_1.respond.return_value = "テスト応答1"
        mock_agent_1.respond_streaming.side_effect = lambda *a, **kw: iter(
            ["テスト", "応答", "1"]
        )
        mock_agent_1.clear_conversation_history = Mock()

        mock_agent_2 = Mock(spec=PersonaAgent)
        mock_agent_2.get_persona_id.return_value = sample_persona_2.id
        mock_agent_2.get_persona_name.return_value = sample_persona_2.name
        mock_agent_2.respond.return_value = "テスト応答2"
        mock_agent_2.respond_streaming.side_effect = lambda *a, **kw: iter(
            ["テスト", "応答", "2"]
        )
        mock_agent_2.clear_conversation_history = Mock()

        persona_agents = [mock_agent_1, mock_agent_2]

        # Facilitator
        mock_facilitator = Mock(spec=FacilitatorAgent)
        mock_facilitator.start_discussion.return_value = "議論を開始します"
        mock_facilitator.invoke.return_value = "ラウンドの要約"
        mock_facilitator.invoke_streaming.side_effect = lambda *a, **kw: iter(
            ["ラウンド", "の", "要約"]
        )
        mock_facilitator.rounds = rounds
        mock_facilitator.additional_instructions = ""
        mock_facilitator.clear_conversation_history = Mock()

        return manager, persona_agents, mock_facilitator

    def test_history_cleared_between_rounds(self, sample_persona, sample_persona_2):
        """History is cleared from round 2 onward."""
        manager, persona_agents, facilitator = self._setup_manager_and_mocks(
            sample_persona, sample_persona_2, rounds=2
        )

        _run(manager, [sample_persona, sample_persona_2], persona_agents, facilitator)

        # Cleared when round 2 starts
        for agent in persona_agents:
            agent.clear_conversation_history.assert_called_once()
        facilitator.clear_conversation_history.assert_called_once()

    def test_history_not_cleared_in_round_1(self, sample_persona, sample_persona_2):
        """History is not cleared in round 1."""
        manager, persona_agents, facilitator = self._setup_manager_and_mocks(
            sample_persona, sample_persona_2, rounds=1
        )

        _run(manager, [sample_persona, sample_persona_2], persona_agents, facilitator)

        for agent in persona_agents:
            agent.clear_conversation_history.assert_not_called()
        facilitator.clear_conversation_history.assert_not_called()

    def test_round_summaries_passed_to_prompt(self, sample_persona, sample_persona_2):
        """Round summaries are used for prompt building (round 2 prompt includes them)."""
        manager, persona_agents, facilitator = self._setup_manager_and_mocks(
            sample_persona, sample_persona_2, rounds=2
        )

        _run(manager, [sample_persona, sample_persona_2], persona_agents, facilitator)

        # The prompt passed to respond() in round 2 includes the round 1 summary
        round2_respond_calls = persona_agents[0].respond.call_args_list[1:]
        for call in round2_respond_calls:
            prompt = call[0][0]
            assert "ラウンドの要約" in prompt

    def test_respond_called_with_none_context(self, sample_persona, sample_persona_2):
        """respond() is called with context=None (avoids double context)."""
        manager, persona_agents, facilitator = self._setup_manager_and_mocks(
            sample_persona, sample_persona_2, rounds=1
        )

        _run(manager, [sample_persona, sample_persona_2], persona_agents, facilitator)

        # context=None on every respond call
        for agent in persona_agents:
            for call in agent.respond.call_args_list:
                args = call[0]
                context_arg = args[1] if len(args) > 1 else call[1].get("context")
                assert context_arg is None

    def test_streaming_history_cleared_between_rounds(
        self, sample_persona, sample_persona_2
    ):
        """The streaming variant also clears history between rounds."""
        manager, persona_agents, facilitator = self._setup_manager_and_mocks(
            sample_persona, sample_persona_2, rounds=2
        )

        # The streaming variant is a generator, so consume it fully
        results = _run(
            manager,
            [sample_persona, sample_persona_2],
            persona_agents,
            facilitator,
            streaming=True,
        )

        # Cleared when round 2 starts
        for agent in persona_agents:
            agent.clear_conversation_history.assert_called_once()
        facilitator.clear_conversation_history.assert_called_once()

        # The result contains messages and complete
        message_end_results = [r for r in results if r[0] == "message_end"]
        complete_results = [r for r in results if r[0] == "complete"]
        assert len(message_end_results) > 0
        assert len(complete_results) == 1
