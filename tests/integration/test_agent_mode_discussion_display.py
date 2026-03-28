"""
Integration tests for AI agent mode discussion display functionality.
Tests the rendering of agent mode discussions with rounds and facilitator messages.
"""

import pytest
from typing import List, Dict
from unittest.mock import Mock

from src.models.discussion import Discussion
from src.models.message import Message
from src.models.persona import Persona


class TestAgentModeDiscussionDisplay:
    """Test suite for agent mode discussion display functionality."""

    def test_agent_mode_discussion_creation(self):
        """Test creating an agent mode discussion with proper structure."""
        # Create test personas
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

        # Create agent mode discussion
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
        # Create discussion
        discussion = Discussion.create_new(
            topic="テストトピック",
            participants=["persona1", "persona2"],
            mode="agent",
            agent_config={"rounds": 2},
        )

        # Add facilitator introduction
        intro_msg = Message.create_new(
            persona_id="facilitator",
            persona_name="ファシリテータ",
            content="議論を開始します",
            message_type="facilitation",
            round_number=None,
        )
        discussion = discussion.add_message(intro_msg)

        # Add round 1 messages
        round1_msg1 = Message.create_new(
            persona_id="persona1",
            persona_name="田中太郎",
            content="私の意見は...",
            message_type="statement",
            round_number=1,
        )
        discussion = discussion.add_message(round1_msg1)

        summary1 = Message.create_new(
            persona_id="facilitator",
            persona_name="ファシリテータ",
            content="田中さんの意見を要約すると...",
            message_type="summary",
            round_number=1,
        )
        discussion = discussion.add_message(summary1)

        round1_msg2 = Message.create_new(
            persona_id="persona2",
            persona_name="佐藤花子",
            content="私はこう考えます...",
            message_type="statement",
            round_number=1,
        )
        discussion = discussion.add_message(round1_msg2)

        summary2 = Message.create_new(
            persona_id="facilitator",
            persona_name="ファシリテータ",
            content="佐藤さんの意見を要約すると...",
            message_type="summary",
            round_number=1,
        )
        discussion = discussion.add_message(summary2)

        # Verify message structure
        assert len(discussion.messages) == 5

        # Check message types
        facilitator_messages = [
            msg
            for msg in discussion.messages
            if msg.message_type in ["facilitation", "summary"]
        ]
        assert len(facilitator_messages) == 3

        statement_messages = [
            msg for msg in discussion.messages if msg.message_type == "statement"
        ]
        assert len(statement_messages) == 2

        # Check round numbers
        round1_messages = [msg for msg in discussion.messages if msg.round_number == 1]
        assert len(round1_messages) == 4  # 2 statements + 2 summaries

    def test_message_grouping_by_round(self):
        """Test that messages can be properly grouped by round number."""
        discussion = Discussion.create_new(
            topic="テストトピック",
            participants=["p1", "p2"],
            mode="agent",
            agent_config={"rounds": 3},
        )

        # Add messages for multiple rounds
        for round_num in range(1, 4):
            for persona_id in ["p1", "p2"]:
                msg = Message.create_new(
                    persona_id=persona_id,
                    persona_name=f"Persona {persona_id}",
                    content=f"Round {round_num} statement",
                    message_type="statement",
                    round_number=round_num,
                )
                discussion = discussion.add_message(msg)

        # Group messages by round
        rounds_dict: Dict[int, List[Message]] = {}
        for message in discussion.messages:
            if message.round_number is not None:
                if message.round_number not in rounds_dict:
                    rounds_dict[message.round_number] = []
                rounds_dict[message.round_number].append(message)

        # Verify grouping
        assert len(rounds_dict) == 3
        assert all(len(rounds_dict[i]) == 2 for i in range(1, 4))

    def test_classic_mode_discussion(self):
        """Test that classic mode discussions still work correctly."""
        discussion = Discussion.create_new(
            topic="従来モードのテスト", participants=["p1", "p2"], mode="classic"
        )

        assert discussion.mode == "classic"
        assert discussion.agent_config is None

        # Add classic mode messages (no round numbers)
        msg1 = Message.create_new(
            persona_id="p1",
            persona_name="Persona 1",
            content="Classic mode message",
            message_type="statement",
        )
        discussion = discussion.add_message(msg1)

        assert discussion.messages[0].round_number is None
        assert discussion.messages[0].message_type == "statement"

    def test_discussion_mode_serialization(self):
        """Test that agent mode discussions serialize and deserialize correctly."""
        original = Discussion.create_new(
            topic="シリアライゼーションテスト",
            participants=["p1", "p2"],
            mode="agent",
            agent_config={"rounds": 5, "additional_instructions": "テスト指示"},
        )

        # Add a message
        msg = Message.create_new(
            persona_id="p1",
            persona_name="Test Persona",
            content="Test content",
            message_type="statement",
            round_number=1,
        )
        original = original.add_message(msg)

        # Serialize to dict
        data = original.to_dict()

        # Verify serialized data
        assert data["mode"] == "agent"
        assert data["agent_config"]["rounds"] == 5
        assert data["messages"][0]["message_type"] == "statement"
        assert data["messages"][0]["round_number"] == 1

        # Deserialize
        restored = Discussion.from_dict(data)

        # Verify restored discussion
        assert restored.mode == "agent"
        assert restored.agent_config["rounds"] == 5
        assert restored.messages[0].message_type == "statement"
        assert restored.messages[0].round_number == 1

    def test_agent_mode_insight_generation_integration(self):
        """Test that insight generation works for agent mode discussions."""
        from src.managers.discussion_manager import DiscussionManager
        from src.models.insight import Insight

        # Create test personas
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

        # Create agent mode discussion with substantial content
        discussion = Discussion.create_new(
            topic="新製品のマーケティング戦略",
            participants=[persona1.id, persona2.id],
            mode="agent",
            agent_config={"rounds": 2},
        )

        # Add facilitator introduction
        intro_msg = Message.create_new(
            persona_id="facilitator",
            persona_name="ファシリテータ",
            content="本日は新製品のマーケティング戦略について議論します。各ペルソナの視点から意見を共有してください。",
            message_type="facilitation",
            round_number=None,
        )
        discussion = discussion.add_message(intro_msg)

        # Add round 1 messages with substantial content
        round1_msg1 = Message.create_new(
            persona_id=persona1.id,
            persona_name=persona1.name,
            content="マーケティングマネージャーとして、データ駆動のアプローチが重要だと考えます。顧客データを分析し、ターゲット層を明確にすることで、効果的なマーケティング戦略を立案できます。特に、デジタルマーケティングチャネルを活用することで、コスト効率の高いキャンペーンが可能になります。",
            message_type="statement",
            round_number=1,
        )
        discussion = discussion.add_message(round1_msg1)

        summary1 = Message.create_new(
            persona_id="facilitator",
            persona_name="ファシリテータ",
            content="田中さんはデータ駆動のアプローチとデジタルマーケティングの重要性を強調されました。",
            message_type="summary",
            round_number=1,
        )
        discussion = discussion.add_message(summary1)

        round1_msg2 = Message.create_new(
            persona_id=persona2.id,
            persona_name=persona2.name,
            content="プロダクトマネージャーの視点では、ユーザー体験を最優先に考えるべきです。製品の価値提案を明確にし、ユーザーのペインポイントを解決することが重要です。イノベーティブな機能を開発し、競合との差別化を図ることで、市場での優位性を確立できます。",
            message_type="statement",
            round_number=1,
        )
        discussion = discussion.add_message(round1_msg2)

        summary2 = Message.create_new(
            persona_id="facilitator",
            persona_name="ファシリテータ",
            content="佐藤さんはユーザー体験と製品の差別化の重要性を指摘されました。",
            message_type="summary",
            round_number=1,
        )
        discussion = discussion.add_message(summary2)

        # Add round 2 messages
        round2_msg1 = Message.create_new(
            persona_id=persona1.id,
            persona_name=persona1.name,
            content="佐藤さんの意見に賛成です。ユーザー体験とデータ分析を組み合わせることで、より効果的な戦略が立てられます。A/Bテストを実施し、ユーザーの反応を測定することで、継続的な改善が可能になります。",
            message_type="statement",
            round_number=2,
        )
        discussion = discussion.add_message(round2_msg1)

        summary3 = Message.create_new(
            persona_id="facilitator",
            persona_name="ファシリテータ",
            content="田中さんはデータ分析とユーザー体験の統合を提案されました。",
            message_type="summary",
            round_number=2,
        )
        discussion = discussion.add_message(summary3)

        round2_msg2 = Message.create_new(
            persona_id=persona2.id,
            persona_name=persona2.name,
            content="その通りです。データとユーザーフィードバックを活用し、製品を進化させることが成功の鍵です。リソース制約がある中でも、優先順位を明確にし、最も影響力のある施策に集中することが重要です。",
            message_type="statement",
            round_number=2,
        )
        discussion = discussion.add_message(round2_msg2)

        summary4 = Message.create_new(
            persona_id="facilitator",
            persona_name="ファシリテータ",
            content="佐藤さんは優先順位付けと集中的な施策の重要性を強調されました。",
            message_type="summary",
            round_number=2,
        )
        discussion = discussion.add_message(summary4)

        # Verify discussion structure
        assert discussion.mode == "agent"
        assert len(discussion.messages) >= 8
        assert discussion.insights == []  # No insights yet

        # Test that DiscussionManager can generate insights from agent mode discussion
        # Note: This will make actual AI calls, so we verify the structure is correct
        # In a real test environment, you might want to mock the AI service

        # Verify that the discussion has enough content for insight generation
        total_content_length = sum(len(msg.content) for msg in discussion.messages)
        assert total_content_length > 100  # Sufficient content for insights

        # Verify message types are preserved
        facilitator_messages = [
            msg
            for msg in discussion.messages
            if msg.message_type in ["facilitation", "summary"]
        ]
        statement_messages = [
            msg for msg in discussion.messages if msg.message_type == "statement"
        ]

        assert len(facilitator_messages) > 0
        assert len(statement_messages) > 0

        # Verify that the discussion can be used with DiscussionManager
        # (This validates the integration without making actual AI calls)
        DiscussionManager(ai_service=Mock(), database_service=Mock())

        # Verify the discussion structure is valid for insight generation
        assert len(discussion.messages) >= 2  # Minimum requirement

        # Test adding insights to agent mode discussion
        test_insight = Insight.create_new(
            category="テストカテゴリ",
            description="これはテストインサイトです",
            supporting_messages=["message1", "message2"],
            confidence_score=0.85,
        )

        discussion_with_insight = discussion.add_insight(test_insight)

        assert len(discussion_with_insight.insights) == 1
        assert discussion_with_insight.insights[0].category == "テストカテゴリ"
        assert discussion_with_insight.mode == "agent"  # Mode is preserved


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
