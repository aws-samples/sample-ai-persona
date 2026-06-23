"""
Integration tests for Discussion Manager.
Tests the discussion manager with mocked database and AI service.
"""

from unittest.mock import Mock

from src.managers.discussion_manager import DiscussionManager
from src.models.persona import Persona
from src.models.discussion import Discussion
from src.models.message import Message
from src.models.insight import Insight
from src.services.ai_service import AIService


class TestDiscussionManagerIntegration:
    """Integration test cases for DiscussionManager."""

    def setup_method(self):
        """Set up test fixtures with mocked services."""
        # Create mock database service
        self.discussions_storage = {}
        self.mock_database_service = Mock()

        def mock_save_discussion(discussion):
            self.discussions_storage[discussion.id] = discussion
            return discussion.id

        def mock_get_discussion(discussion_id):
            return self.discussions_storage.get(discussion_id)

        def mock_get_discussions(limit=21, cursor=None, mode=None, sort_ascending=False, search_all=False):
            return list(self.discussions_storage.values()), None

        def mock_discussion_exists(discussion_id):
            return discussion_id in self.discussions_storage

        def mock_get_discussion_count():
            return len(self.discussions_storage)

        def mock_delete_discussion(discussion_id):
            if discussion_id in self.discussions_storage:
                del self.discussions_storage[discussion_id]
                return True
            return False

        def mock_update_discussion_insights(discussion_id, insights):
            if discussion_id in self.discussions_storage:
                discussion = self.discussions_storage[discussion_id]
                # Create updated discussion with new insights
                from dataclasses import replace

                updated = replace(discussion, insights=insights)
                self.discussions_storage[discussion_id] = updated
                return True
            return False

        def mock_get_discussions_by_topic(topic_pattern):
            return [
                d for d in self.discussions_storage.values() if topic_pattern in d.topic
            ]

        def mock_get_discussions_by_participant(persona_id):
            return [
                d
                for d in self.discussions_storage.values()
                if persona_id in d.participants
            ]

        self.mock_database_service.save_discussion.side_effect = mock_save_discussion
        self.mock_database_service.get_discussion.side_effect = mock_get_discussion
        self.mock_database_service.get_discussions.side_effect = mock_get_discussions
        self.mock_database_service.discussion_exists.side_effect = (
            mock_discussion_exists
        )
        self.mock_database_service.get_discussion_count.side_effect = (
            mock_get_discussion_count
        )
        self.mock_database_service.delete_discussion.side_effect = (
            mock_delete_discussion
        )
        self.mock_database_service.update_discussion_insights.side_effect = (
            mock_update_discussion_insights
        )
        self.mock_database_service.get_discussions_by_topic.side_effect = (
            mock_get_discussions_by_topic
        )
        self.mock_database_service.get_discussions_by_participant.side_effect = (
            mock_get_discussions_by_participant
        )

        # Create mocked AI service
        self.mock_ai_service = Mock(spec=AIService)
        # Create discussion manager with real database and mocked AI
        self.discussion_manager = DiscussionManager(
            ai_service=self.mock_ai_service, database_service=self.mock_database_service
        )

        # Create test personas and save them to database
        self.persona1 = Persona.create_new(
            name="田中花子",
            age=30,
            occupation="マーケティング担当",
            background="東京在住のマーケティング担当者。効率性を重視し、データドリブンなアプローチを好む。",
            values=["効率性", "革新性", "データ重視"],
            pain_points=["時間不足", "情報過多", "意思決定の遅さ"],
            goals=["キャリアアップ", "スキル向上", "チーム成果向上"],
        )

        self.persona2 = Persona.create_new(
            name="佐藤太郎",
            age=35,
            occupation="商品開発者",
            background="大阪在住の商品開発者。品質と顧客満足を最優先に考え、技術的な課題解決に情熱を注ぐ。",
            values=["品質", "顧客満足", "技術革新"],
            pain_points=["予算制約", "技術的課題", "市場の変化"],
            goals=["新商品開発", "市場拡大", "技術力向上"],
        )

        # Save personas to database
        self.mock_database_service.save_persona(self.persona1)
        self.mock_database_service.save_persona(self.persona2)

        # Create realistic test messages
        self.test_messages = [
            Message.create_new(
                persona_id=self.persona1.id,
                persona_name=self.persona1.name,
                content="マーケティングの観点から言うと、新商品の成功には明確なターゲット設定が不可欠です。データ分析を通じて顧客セグメントを特定し、それぞれのニーズに合わせたメッセージングを展開する必要があります。特に効率性を重視する顧客層には、時間短縮や作業効率化のメリットを強調すべきです。",
            ),
            Message.create_new(
                persona_id=self.persona2.id,
                persona_name=self.persona2.name,
                content="商品開発の立場では、マーケティング部門の提案は理解できますが、まず製品の品質と信頼性を確保することが最優先です。顧客満足を得るためには、機能性だけでなく、耐久性や安全性も重要な要素です。技術的な制約もありますが、革新的なソリューションで課題を解決していきたいと思います。",
            ),
            Message.create_new(
                persona_id=self.persona1.id,
                persona_name=self.persona1.name,
                content="品質の重要性は十分理解しています。ただし、市場投入のタイミングも重要な要素です。競合他社に先を越されないよう、品質と開発スピードのバランスを取る必要があります。データ分析によると、顧客は完璧な製品よりも、適切なタイミングで提供される良質な製品を求める傾向があります。",
            ),
            Message.create_new(
                persona_id=self.persona2.id,
                persona_name=self.persona2.name,
                content="市場投入のタイミングについては同感です。しかし、品質に妥協することで後々の顧客満足度やブランドイメージに悪影響を与えるリスクも考慮すべきです。技術的な課題を早期に特定し、効率的な開発プロセスを構築することで、品質とスピードの両立を目指したいと思います。",
            ),
        ]

        # Create test insights (structured data)
        self.test_insight_data = [
            {
                "category": "顧客ニーズ",
                "description": "効率性を重視する顧客層が存在し、時間短縮や作業効率化のメリットを求めている",
                "confidence_score": 0.85,
            },
            {
                "category": "市場機会",
                "description": "品質とスピードのバランスを取った製品開発により競合優位性を確保できる",
                "confidence_score": 0.78,
            },
            {
                "category": "商品開発",
                "description": "技術的制約を考慮しながらも革新的なソリューションで課題解決を図る必要がある",
                "confidence_score": 0.72,
            },
            {
                "category": "マーケティング",
                "description": "データドリブンなアプローチによる顧客セグメント分析が重要",
                "confidence_score": 0.80,
            },
            {
                "category": "その他",
                "description": "市場投入タイミングと品質のバランスが成功の鍵となる",
                "confidence_score": 0.65,
            },
        ]

    def teardown_method(self):
        """Clean up test fixtures."""
        pass
