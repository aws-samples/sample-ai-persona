"""
Integration tests for AI agent mode discussion display functionality.
Tests the rendering data of agent mode discussions with rounds and facilitator messages.

U6: the messages live outside the body (``DiscussionMessages``) and the reader
gets them in a :class:`DiscussionView`.
"""

from typing import Dict, List

import pytest

from src.models.discussion import Discussion, DiscussionView
from src.models.insight import Insight
from src.models.message import DiscussionMessage, Message
from src.models.persona import Persona


def _msg(persona_id, name, content, message_type="statement", round_number=None):
    return Message.create_new(
        persona_id=persona_id,
        persona_name=name,
        content=content,
        message_type=message_type,
        round_number=round_number,
    )


def _view(discussion: Discussion, messages: List[Message]) -> DiscussionView:
    return DiscussionView(discussion, messages, [], [])


class TestAgentModeDiscussionDisplay:
    """Test suite for agent mode discussion display functionality."""

    def test_agent_mode_discussion_creation(self):
        """Test creating an agent mode discussion with proper structure."""
        persona1 = Persona.create_new(
            name="田中太郎",
            age=35,
            occupation="マーケティングマネージャー",
            background="大手IT企業で10年以上のマーケティング経験",
            values=["顧客第一", "データ駆動"],
            pain_points=["市場調査の時間不足"],
            goals=["効率的な市場分析"],
        )
        persona2 = Persona.create_new(
            name="佐藤花子",
            age=28,
            occupation="プロダクトマネージャー",
            background="スタートアップでの製品開発経験",
            values=["ユーザー体験", "イノベーション"],
            pain_points=["リソース制約"],
            goals=["革新的な製品開発"],
        )

        discussion = Discussion.create_new(
            topic="新製品のマーケティング戦略",
            participants=[persona1.id, persona2.id],
            mode="agent",
            agent_config={"rounds": 3, "additional_instructions": ""},
        )

        assert discussion.mode == "agent"
        assert discussion.agent_config is not None
        assert discussion.agent_config["rounds"] == 3
        assert len(discussion.participants) == 2

    def test_agent_mode_messages_with_rounds(self):
        """Test agent mode messages with round numbers and message types."""
        discussion = Discussion.create_new(
            topic="テストトピック",
            participants=["persona1", "persona2"],
            mode="agent",
            agent_config={"rounds": 2},
        )
        messages = [
            _msg("facilitator", "ファシリテータ", "議論を開始します", "facilitation"),
            _msg("persona1", "田中太郎", "私の意見は...", round_number=1),
            _msg("facilitator", "ファシリテータ", "要約...", "summary", 1),
            _msg("persona2", "佐藤花子", "私はこう考えます...", round_number=1),
            _msg("facilitator", "ファシリテータ", "要約...", "summary", 1),
        ]
        view = _view(discussion, messages)

        assert len(view.messages) == 5
        facilitator_messages = [
            m for m in view.messages if m.message_type in ["facilitation", "summary"]
        ]
        assert len(facilitator_messages) == 3
        assert len([m for m in view.messages if m.message_type == "statement"]) == 2
        assert len([m for m in view.messages if m.round_number == 1]) == 4

    def test_message_grouping_by_round(self):
        """Test that messages can be properly grouped by round number."""
        messages = [
            _msg(pid, f"Persona {pid}", f"Round {r} statement", round_number=r)
            for r in range(1, 4)
            for pid in ["p1", "p2"]
        ]

        rounds_dict: Dict[int, List[Message]] = {}
        for message in messages:
            if message.round_number is not None:
                rounds_dict.setdefault(message.round_number, []).append(message)

        assert len(rounds_dict) == 3
        assert all(len(rounds_dict[i]) == 2 for i in range(1, 4))

    def test_classic_mode_discussion(self):
        """Test that classic mode discussions still work correctly."""
        discussion = Discussion.create_new(
            topic="従来モードのテスト", participants=["p1", "p2"], mode="classic"
        )

        assert discussion.mode == "classic"
        assert discussion.agent_config is None

        view = _view(discussion, [_msg("p1", "Persona 1", "Classic mode message")])
        assert view.messages[0].round_number is None
        assert view.messages[0].message_type == "statement"
        assert view.mode == "classic"

    def test_discussion_mode_serialization(self):
        """Agent mode bodies and their messages serialize and deserialize."""
        original = Discussion.create_new(
            topic="シリアライゼーションテスト",
            participants=["p1", "p2"],
            mode="agent",
            agent_config={"rounds": 5, "additional_instructions": "テスト指示"},
        )
        data = original.to_dict()
        assert data["mode"] == "agent"
        assert data["agent_config"]["rounds"] == 5
        assert "messages" not in data

        restored = Discussion.from_dict(data)
        assert restored.mode == "agent"
        assert restored.agent_config["rounds"] == 5

        stored = DiscussionMessage.from_message(
            original.id, 1, _msg("p1", "Test Persona", "Test content", round_number=1)
        )
        back = DiscussionMessage.from_dict(stored.to_dict()).to_message()
        assert back.message_type == "statement"
        assert back.round_number == 1

    def test_agent_mode_insights_are_kept_with_the_mode(self):
        """Insights added to an agent mode discussion keep its mode."""
        discussion = Discussion.create_new(
            topic="新製品のマーケティング戦略",
            participants=["p1", "p2"],
            mode="agent",
            agent_config={"rounds": 2},
        )
        insight = Insight.create_new(
            category="テストカテゴリ",
            description="これはテストインサイトです",
            supporting_messages=["message1", "message2"],
            confidence_score=0.85,
        )

        with_insight = discussion.with_insights([insight])

        assert discussion.insights == []
        assert len(with_insight.insights) == 1
        assert with_insight.insights[0].category == "テストカテゴリ"
        assert with_insight.mode == "agent"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
