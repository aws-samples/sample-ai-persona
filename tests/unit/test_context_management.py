"""
Agentモード議論のコンテキスト管理改善テスト

SPEC: 20260330-1606_agent-discussion-context-management
"""

from datetime import datetime
from unittest.mock import Mock, patch

from src.models.message import Message
from src.models.persona import Persona
from src.services.agent_service import FacilitatorAgent, PersonaAgent


def _create_test_persona(persona_id: str = "test-persona-1", name: str = "田中太郎") -> Persona:
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


class TestFacilitatorPromptWithSummaries:
    """FacilitatorAgent.create_prompt_for_persona() の要約コンテキスト対応テスト"""

    def setup_method(self):
        self.mock_agent = Mock()
        self.facilitator = FacilitatorAgent(5, "", self.mock_agent)

    def test_first_round_no_summaries(self):
        """ラウンド1: 要約なし、コンテキストなし"""
        persona_agent = Mock(get_persona_name=Mock(return_value="田中太郎"))
        prompt = self.facilitator.create_prompt_for_persona(
            persona_agent, "新商品について", []
        )
        assert "新商品について" in prompt
        assert "これまでの議論の要約" not in prompt

    def test_with_round_summaries(self):
        """ラウンド2以降: 要約コンテキストが含まれること"""
        persona_agent = Mock(get_persona_name=Mock(return_value="田中太郎"))
        summaries = [
            "ラウンド1では価格と品質のトレードオフが議論された",
            "ラウンド2ではターゲット層の絞り込みが論点に",
        ]
        recent = [
            Message.create_new("p1", "佐藤", "品質が重要です"),
            Message.create_new("p2", "鈴木", "コストも考慮すべき"),
        ]
        prompt = self.facilitator.create_prompt_for_persona(
            persona_agent, "新商品について", recent, round_summaries=summaries
        )
        assert "これまでの議論の要約" in prompt
        assert "ラウンド1" in prompt
        assert "ラウンド2" in prompt
        assert "佐藤" in prompt
        assert "鈴木" in prompt

    def test_with_summaries_no_recent(self):
        """要約ありだが直近発言なし（ラウンド最初の発言者で前ラウンドの発言がない場合）"""
        persona_agent = Mock(get_persona_name=Mock(return_value="田中太郎"))
        summaries = ["ラウンド1の要約内容"]
        prompt = self.facilitator.create_prompt_for_persona(
            persona_agent, "テーマ", [], round_summaries=summaries
        )
        assert "これまでの議論の要約" in prompt
        assert "直近の発言" not in prompt

    def test_recent_messages_include_facilitator(self):
        """直近発言にファシリテータの要約が含まれること"""
        persona_agent = Mock(get_persona_name=Mock(return_value="田中太郎"))
        summaries = ["ラウンド1の要約"]
        recent = [
            Message.create_new("p1", "佐藤", "意見です", message_type="statement"),
            Message.create_new("facilitator", "ファシリテータ", "論点整理: 次は価格について", message_type="summary"),
            Message.create_new("p2", "鈴木", "賛成です", message_type="statement"),
        ]
        prompt = self.facilitator.create_prompt_for_persona(
            persona_agent, "テーマ", recent, round_summaries=summaries
        )
        assert "ファシリテータ" in prompt
        assert "論点整理" in prompt

    def test_backward_compatibility_no_summaries_param(self):
        """round_summaries未指定でも動作すること（後方互換性）"""
        persona_agent = Mock(get_persona_name=Mock(return_value="田中太郎"))
        recent = [Message.create_new("p1", "佐藤", "意見です")]
        prompt = self.facilitator.create_prompt_for_persona(
            persona_agent, "テーマ", recent
        )
        assert "テーマ" in prompt
        assert "佐藤" in prompt


class TestPersonaAgentBuildPrompt:
    """PersonaAgent._build_prompt_with_context() 簡素化テスト"""

    def setup_method(self):
        self.persona = _create_test_persona()
        self.mock_agent = Mock()
        self.persona_agent = PersonaAgent(self.persona, "test", self.mock_agent)

    def test_prompt_returned_as_is(self):
        """コンテキストが渡されてもプロンプトがそのまま返ること"""
        prompt = "構築済みプロンプト"
        context = [Message.create_new("p1", "佐藤", "発言")]
        result = self.persona_agent._build_prompt_with_context(prompt, context)
        assert result == prompt

    def test_prompt_without_context(self):
        """コンテキストなしでもプロンプトがそのまま返ること"""
        prompt = "構築済みプロンプト"
        result = self.persona_agent._build_prompt_with_context(prompt, None)
        assert result == prompt


class TestSummarizeRoundImproved:
    """FacilitatorAgent.summarize_round() の改善テスト"""

    def setup_method(self):
        self.mock_agent = Mock()
        self.facilitator = FacilitatorAgent(3, "", self.mock_agent)

    def test_summarize_round_prompt_contains_structure(self):
        """要約プロンプトに構造的な指示が含まれること"""
        self.mock_agent.return_value = "要約テキスト"
        messages = [
            Message.create_new("p1", "田中", "価格が重要", message_type="statement"),
            Message.create_new("p2", "佐藤", "品質が重要", message_type="statement"),
        ]
        self.facilitator.summarize_round(1, messages, "新商品")

        # エージェントに渡されたプロンプトを検証
        call_args = self.mock_agent.call_args
        prompt = call_args[0][0] if call_args[0] else call_args[1].get("prompt", "")
        assert "共通点" in prompt or "対立点" in prompt
        assert "深掘り" in prompt or "論点" in prompt
