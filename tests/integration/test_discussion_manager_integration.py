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

    def test_complete_discussion_workflow(self):
        """Test complete discussion workflow from start to save."""
        topic = "新商品のマーケティング戦略と開発プロセスについて"
        personas = [self.persona1, self.persona2]

        # Mock AI service responses
        self.mock_ai_service.facilitate_discussion.return_value = self.test_messages
        self.mock_ai_service.extract_insights.return_value = self.test_insight_data

        # Step 1: Start discussion
        discussion = self.discussion_manager.start_discussion(personas, topic)

        # Verify discussion was created correctly
        assert isinstance(discussion, Discussion)
        assert discussion.topic == topic
        assert discussion.participants == [self.persona1.id, self.persona2.id]
        assert len(discussion.messages) == 4

        # Verify AI service was called correctly (with documents=None for no documents)
        self.mock_ai_service.facilitate_discussion.assert_called_once_with(
            personas, topic, documents=None
        )

        # Step 2: Generate insights
        insights = self.discussion_manager.generate_insights(discussion)

        # Verify insights were generated correctly
        assert isinstance(insights, list)
        assert len(insights) == 5
        assert isinstance(insights[0], Insight)

        # Verify AI service was called correctly
        self.mock_ai_service.extract_insights.assert_called_once_with(
            discussion.messages, categories=None, topic=discussion.topic
        )

        # Step 3: Save discussion with insights
        discussion_id = self.discussion_manager.save_discussion_with_insights(
            discussion, insights
        )

        # Verify discussion was saved
        assert discussion_id == discussion.id

        # Step 4: Retrieve saved discussion
        saved_discussion = self.discussion_manager.get_discussion(discussion_id)

        # Verify retrieved discussion
        assert saved_discussion is not None
        assert saved_discussion.id == discussion.id
        assert saved_discussion.topic == topic
        assert len(saved_discussion.messages) == 4
        assert len(saved_discussion.insights) == 5

        # Verify insight categories
        categories = [insight.category for insight in saved_discussion.insights]
        expected_categories = [
            "顧客ニーズ",
            "市場機会",
            "商品開発",
            "マーケティング",
            "その他",
        ]
        assert categories == expected_categories

    def test_discussion_history_and_search(self):
        """Test discussion history and search functionality."""
        # Create and save multiple discussions (all with 2+ participants)
        discussions_data = [
            ("マーケティング戦略について", [self.persona1.id, self.persona2.id]),
            ("商品開発プロセスについて", [self.persona1.id, self.persona2.id]),
            ("顧客満足度向上について", [self.persona1.id, self.persona2.id]),
        ]

        saved_discussion_ids = []
        for topic, participant_ids in discussions_data:
            discussion = Discussion.create_new(
                topic=topic, participants=participant_ids
            )
            # Add some messages
            for i, participant_id in enumerate(participant_ids):
                persona_name = (
                    self.persona1.name
                    if participant_id == self.persona1.id
                    else self.persona2.name
                )
                message = Message.create_new(
                    persona_id=participant_id,
                    persona_name=persona_name,
                    content=f"これは{topic}に関する議論のメッセージです。詳細な内容を含んでいます。",
                )
                discussion = discussion.add_message(message)

            discussion_id = self.discussion_manager.save_discussion(discussion)
            saved_discussion_ids.append(discussion_id)

        # Test get discussion history
        history, _ = self.discussion_manager.get_discussion_history()
        assert len(history) == 3

        # Test search by topic
        marketing_discussions = self.discussion_manager.get_discussions_by_topic(
            "マーケティング"
        )
        assert len(marketing_discussions) == 1
        assert "マーケティング" in marketing_discussions[0].topic

        # Test search by participant
        persona1_discussions = self.discussion_manager.get_discussions_by_participant(
            self.persona1.id
        )
        assert len(persona1_discussions) == 3  # persona1 participated in all 3 discussions

        persona2_discussions = self.discussion_manager.get_discussions_by_participant(
            self.persona2.id
        )
        assert len(persona2_discussions) == 3  # persona2 participated in all 3 discussions

    def test_discussion_insights_update(self):
        """Test updating insights for existing discussion."""
        topic = "商品改善について"

        # Create and save discussion without insights
        discussion = Discussion.create_new(
            topic=topic, participants=[self.persona1.id, self.persona2.id]
        )

        # Add messages
        for message in self.test_messages[:2]:  # Use first 2 messages
            discussion = discussion.add_message(message)

        discussion_id = self.discussion_manager.save_discussion(discussion)

        # Create insights
        insights = [
            Insight.create_new(
                category="改善提案",
                description="ユーザビリティの向上が必要",
                supporting_messages=[],
                confidence_score=0.9,
            ),
            Insight.create_new(
                category="技術課題",
                description="パフォーマンスの最適化が重要",
                supporting_messages=[],
                confidence_score=0.8,
            ),
        ]

        # Update discussion with insights
        success = self.discussion_manager.update_discussion_insights(
            discussion_id, insights
        )
        assert success

        # Retrieve updated discussion
        updated_discussion = self.discussion_manager.get_discussion(discussion_id)
        assert updated_discussion is not None
        assert len(updated_discussion.insights) == 2

        # Verify insight content
        categories = [insight.category for insight in updated_discussion.insights]
        assert "改善提案" in categories
        assert "技術課題" in categories

    def test_discussion_count_and_existence(self):
        """Test discussion count and existence check functionality."""
        # Initial count should be 0
        initial_count = self.discussion_manager.get_discussion_count()
        assert initial_count == 0

        # Create and save a discussion (with 2 participants)
        discussion = Discussion.create_new(
            topic="テスト議論", participants=[self.persona1.id, self.persona2.id]
        )

        # Add messages from both personas
        message1 = Message.create_new(
            persona_id=self.persona1.id,
            persona_name=self.persona1.name,
            content="これはテスト用の議論メッセージです。十分な長さを持つ内容を含んでいます。",
        )
        message2 = Message.create_new(
            persona_id=self.persona2.id,
            persona_name=self.persona2.name,
            content="私も同様にテスト用の議論に参加します。詳細な内容を含む長いメッセージです。",
        )
        discussion = discussion.add_message(message1)
        discussion = discussion.add_message(message2)

        discussion_id = self.discussion_manager.save_discussion(discussion)

        # Count should be 1
        count_after_save = self.discussion_manager.get_discussion_count()
        assert count_after_save == 1

        # Discussion should exist
        exists = self.discussion_manager.discussion_exists(discussion_id)
        assert exists

        # Non-existent discussion should not exist
        non_existent_exists = self.discussion_manager.discussion_exists(
            "non-existent-id"
        )
        assert not non_existent_exists

        # Delete discussion
        deleted = self.discussion_manager.delete_discussion(discussion_id)
        assert deleted

        # Count should be 0 again
        count_after_delete = self.discussion_manager.get_discussion_count()
        assert count_after_delete == 0

        # Discussion should not exist
        exists_after_delete = self.discussion_manager.discussion_exists(discussion_id)
        assert not exists_after_delete

    def test_error_handling_with_real_database(self):
        """Test error handling with real database operations."""
        # Test retrieving non-existent discussion
        non_existent_discussion = self.discussion_manager.get_discussion(
            "non-existent-id"
        )
        assert non_existent_discussion is None

        # Test deleting non-existent discussion
        delete_result = self.discussion_manager.delete_discussion("non-existent-id")
        assert not delete_result

        # Test updating insights for non-existent discussion
        insights = [
            Insight.create_new(
                category="テスト",
                description="これは十分な長さを持つテスト用インサイトの説明文です",
                supporting_messages=[],
                confidence_score=0.5,
            ),
            Insight.create_new(
                category="テスト2",
                description="これも十分な長さを持つ別のテスト用インサイトの説明文です",
                supporting_messages=[],
                confidence_score=0.7,
            ),
            Insight.create_new(
                category="テスト3",
                description="さらに別の十分な長さを持つテスト用インサイトの説明文です",
                supporting_messages=[],
                confidence_score=0.6,
            ),
        ]
        update_result = self.discussion_manager.update_discussion_insights(
            "non-existent-id", insights
        )
        assert not update_result
