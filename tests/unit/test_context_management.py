"""
Agentモード議論のコンテキスト管理改善テスト

SPEC: 20260330-1606_agent-discussion-context-management
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
    """PersonaAgent.clear_conversation_history() のテスト"""

    def setup_method(self):
        self.persona = _create_test_persona()
        self.mock_agent = Mock()
        self.mock_agent.messages = [
            {"role": "user", "content": [{"text": "msg1"}]},
            {"role": "assistant", "content": [{"text": "resp1"}]},
        ]
        self.persona_agent = PersonaAgent(self.persona, "test prompt", self.mock_agent)

    def test_clear_conversation_history(self):
        """会話履歴がクリアされること"""
        assert len(self.mock_agent.messages) == 2
        self.persona_agent.clear_conversation_history()
        assert len(self.mock_agent.messages) == 0

    def test_clear_conversation_history_already_empty(self):
        """空の履歴でもエラーにならないこと"""
        self.mock_agent.messages = []
        self.persona_agent.clear_conversation_history()
        assert len(self.mock_agent.messages) == 0

    def test_clear_conversation_history_agent_none(self):
        """agent=Noneでもエラーにならないこと（dispose後）"""
        self.persona_agent.agent = None
        self.persona_agent.clear_conversation_history()  # should not raise

    def test_clear_conversation_history_no_messages_attr(self):
        """messagesアトリビュートがないエージェントでもエラーにならないこと"""
        self.mock_agent = Mock(spec=[])
        self.persona_agent.agent = self.mock_agent
        self.persona_agent.clear_conversation_history()  # should not raise


class TestFacilitatorAgentClearHistory:
    """FacilitatorAgent.clear_conversation_history() のテスト"""

    def setup_method(self):
        self.mock_agent = Mock()
        self.mock_agent.messages = [
            {"role": "user", "content": [{"text": "summarize"}]},
            {"role": "assistant", "content": [{"text": "summary"}]},
        ]
        self.facilitator = FacilitatorAgent(3, "", self.mock_agent)

    def test_clear_conversation_history(self):
        """会話履歴がクリアされること"""
        assert len(self.mock_agent.messages) == 2
        self.facilitator.clear_conversation_history()
        assert len(self.mock_agent.messages) == 0

    def test_clear_conversation_history_agent_none(self):
        """agent=Noneでもエラーにならないこと"""
        self.facilitator.agent = None
        self.facilitator.clear_conversation_history()  # should not raise


class TestManagerBuildPersonaPrompt:
    """AgentDiscussionManager._build_persona_prompt() のテスト"""

    def setup_method(self):
        from src.managers.agent_discussion_manager import AgentDiscussionManager

        self.manager = AgentDiscussionManager(
            agent_service=Mock(), database_service=Mock()
        )

    def test_first_round_no_context(self):
        """ラウンド1・コンテキストなし: 具体的なエピソードを引き出す問いかけ"""
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
        """ラウンド2以降: 要約コンテキストが含まれること"""
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
        """ファシリテータの問いかけが専用セクションに表示されること"""
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
        """中盤ラウンド: 考えの変化・新たな観点を促す指示"""
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
        """最終ラウンド: 結論を促す指示"""
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
    """AgentDiscussionManager._build_summary_prompt() のテスト"""

    def setup_method(self):
        from src.managers.agent_discussion_manager import AgentDiscussionManager

        self.manager = AgentDiscussionManager(
            agent_service=Mock(), database_service=Mock()
        )

    def test_summary_prompt_contains_structure(self):
        """要約プロンプトに構造的な指示が含まれること"""
        messages = [
            Message.create_new("p1", "田中", "価格が重要", message_type="statement"),
            Message.create_new("p2", "佐藤", "品質が重要", message_type="statement"),
        ]
        prompt = self.manager._build_summary_prompt(1, messages, "新商品", 3)
        assert "共通点" in prompt or "対立点" in prompt
        assert "問い" in prompt

    def test_summary_prompt_with_previous_summaries(self):
        """過去の要約が含まれること"""
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
        """ラウンド1では過去要約セクションが含まれないこと"""
        messages = [
            Message.create_new("p1", "田中", "意見です", message_type="statement"),
        ]
        prompt = self.manager._build_summary_prompt(1, messages, "新商品", 3)
        assert "これまでの議論の流れ" not in prompt


class TestAgentDiscussionContextManagement:
    """AgentDiscussionManager のコンテキスト管理テスト

    SPEC: 20260330-1606_agent-discussion-context-management
    """

    def _setup_manager_and_mocks(self, sample_persona, sample_persona_2, rounds=2):
        """共通セットアップ"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None
        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        # ペルソナエージェント（messages属性をリストで持つ）
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

        # ファシリテータ
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
        """ラウンド2以降で会話履歴がクリアされること"""
        manager, persona_agents, facilitator = self._setup_manager_and_mocks(
            sample_persona, sample_persona_2, rounds=2
        )

        manager.start_agent_discussion(
            personas=[sample_persona, sample_persona_2],
            topic="テストトピック",
            persona_agents=persona_agents,
            facilitator=facilitator,
        )

        # ラウンド2開始時にクリアが呼ばれること
        for agent in persona_agents:
            agent.clear_conversation_history.assert_called_once()
        facilitator.clear_conversation_history.assert_called_once()

    def test_history_not_cleared_in_round_1(self, sample_persona, sample_persona_2):
        """ラウンド1ではクリアされないこと"""
        manager, persona_agents, facilitator = self._setup_manager_and_mocks(
            sample_persona, sample_persona_2, rounds=1
        )

        manager.start_agent_discussion(
            personas=[sample_persona, sample_persona_2],
            topic="テストトピック",
            persona_agents=persona_agents,
            facilitator=facilitator,
        )

        for agent in persona_agents:
            agent.clear_conversation_history.assert_not_called()
        facilitator.clear_conversation_history.assert_not_called()

    def test_round_summaries_passed_to_prompt(self, sample_persona, sample_persona_2):
        """ラウンド要約がプロンプト生成に使用されること（ラウンド2のプロンプトに要約が含まれる）"""
        manager, persona_agents, facilitator = self._setup_manager_and_mocks(
            sample_persona, sample_persona_2, rounds=2
        )

        manager.start_agent_discussion(
            personas=[sample_persona, sample_persona_2],
            topic="テストトピック",
            persona_agents=persona_agents,
            facilitator=facilitator,
        )

        # ラウンド2でrespond()に渡されたプロンプトにラウンド1の要約が含まれること
        round2_respond_calls = persona_agents[0].respond.call_args_list[1:]
        for call in round2_respond_calls:
            prompt = call[0][0]
            assert "ラウンドの要約" in prompt

    def test_respond_called_with_none_context(self, sample_persona, sample_persona_2):
        """respond()がcontext=Noneで呼ばれること（二重コンテキスト防止）"""
        manager, persona_agents, facilitator = self._setup_manager_and_mocks(
            sample_persona, sample_persona_2, rounds=1
        )

        manager.start_agent_discussion(
            personas=[sample_persona, sample_persona_2],
            topic="テストトピック",
            persona_agents=persona_agents,
            facilitator=facilitator,
        )

        # 全てのrespond呼び出しでcontext=Noneであること
        for agent in persona_agents:
            for call in agent.respond.call_args_list:
                args = call[0]
                context_arg = args[1] if len(args) > 1 else call[1].get("context")
                assert context_arg is None

    def test_streaming_history_cleared_between_rounds(
        self, sample_persona, sample_persona_2
    ):
        """ストリーミング版でもラウンド間で履歴がクリアされること"""
        manager, persona_agents, facilitator = self._setup_manager_and_mocks(
            sample_persona, sample_persona_2, rounds=2
        )

        # ストリーミング版はジェネレータなので全て消費する
        results = list(
            manager.start_agent_discussion_streaming(
                personas=[sample_persona, sample_persona_2],
                topic="テストトピック",
                persona_agents=persona_agents,
                facilitator=facilitator,
            )
        )

        # ラウンド2開始時にクリアが呼ばれること
        for agent in persona_agents:
            agent.clear_conversation_history.assert_called_once()
        facilitator.clear_conversation_history.assert_called_once()

        # 結果にメッセージとcompleteが含まれること
        message_end_results = [r for r in results if r[0] == "message_end"]
        complete_results = [r for r in results if r[0] == "complete"]
        assert len(message_end_results) > 0
        assert len(complete_results) == 1
