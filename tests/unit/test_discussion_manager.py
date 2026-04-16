"""
Unit tests for Discussion Manager.
"""

import pytest
from unittest.mock import Mock
from datetime import datetime

from src.managers.discussion_manager import DiscussionManager, DiscussionManagerError
from src.models.persona import Persona
from src.models.discussion import Discussion
from src.models.message import Message
from src.models.insight import Insight
from src.services.ai_service import AIServiceError
from src.services.database_service import DatabaseError


class TestDiscussionManager:
    """Test cases for DiscussionManager class."""

    def setup_method(self):
        """Set up test fixtures."""
        # Create mock services
        self.mock_ai_service = Mock()
        self.mock_database_service = Mock()

        # Create discussion manager with mocked services
        self.discussion_manager = DiscussionManager(
            ai_service=self.mock_ai_service, database_service=self.mock_database_service
        )

        # Create test personas
        self.persona1 = Persona.create_new(
            name="田中花子",
            age=30,
            occupation="マーケティング担当",
            background="東京在住のマーケティング担当者",
            values=["効率性", "革新性"],
            pain_points=["時間不足", "情報過多"],
            goals=["キャリアアップ", "スキル向上"],
        )

        self.persona2 = Persona.create_new(
            name="佐藤太郎",
            age=35,
            occupation="商品開発者",
            background="大阪在住の商品開発者",
            values=["品質", "顧客満足"],
            pain_points=["予算制約", "技術的課題"],
            goals=["新商品開発", "市場拡大"],
        )

        # Create test messages (longer content for validation)
        self.test_messages = [
            Message.create_new(
                persona_id=self.persona1.id,
                persona_name=self.persona1.name,
                content="私はマーケティングの観点から考えると、顧客のニーズを深く理解することが重要だと思います。特に効率性を重視する顧客層に対しては、時間短縮や作業効率化につながる機能を前面に押し出すべきです。",
            ),
            Message.create_new(
                persona_id=self.persona2.id,
                persona_name=self.persona2.name,
                content="商品開発の立場では、品質と顧客満足を最優先に考えています。マーケティング部門が提案する効率性の訴求は理解できますが、それと同時に製品の信頼性や耐久性も重要な要素として伝える必要があります。",
            ),
        ]

        # Create test insights
        self.test_insights = [
            Insight.create_new(
                category="顧客ニーズ",
                description="効率性を重視する顧客層が存在する",
                supporting_messages=[],
                confidence_score=0.8,
            ),
            Insight.create_new(
                category="市場機会",
                description="品質と効率性を両立した商品の需要がある",
                supporting_messages=[],
                confidence_score=0.9,
            ),
        ]

    def test_init_success(self):
        fresh_ai_service = Mock()
        fresh_database_service = Mock()
        manager = DiscussionManager(
            ai_service=fresh_ai_service, database_service=fresh_database_service
        )
        assert manager is not None
        assert manager.ai_service == fresh_ai_service
        assert manager.database_service == fresh_database_service

    def test_start_discussion_success(self):
        """Test successful discussion start."""
        topic = "新商品のマーケティング戦略について"
        personas = [self.persona1, self.persona2]

        # Mock AI service response
        self.mock_ai_service.facilitate_discussion.return_value = self.test_messages

        result = self.discussion_manager.start_discussion(personas, topic)

        # Verify result
        assert isinstance(result, Discussion)
        assert result.topic == topic
        assert result.participants == [self.persona1.id, self.persona2.id]
        assert len(result.messages) == 2

        # Verify AI service was called correctly (with documents=None)
        self.mock_ai_service.facilitate_discussion.assert_called_once_with(
            personas, topic, documents=None
        )

    def test_start_discussion_invalid_personas(self):
        """Test discussion start with invalid personas."""
        topic = "テストトピック"

        # Test with empty personas list
        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.start_discussion([], topic)
        assert "議論参加ペルソナが指定されていません" in str(exc_info.value)

        # Test with single persona
        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.start_discussion([self.persona1], topic)
        assert "議論には最低2つのペルソナが必要です" in str(exc_info.value)

        # Test with too many personas
        personas = [self.persona1] * 6
        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.start_discussion(personas, topic)
        assert "議論参加ペルソナは最大5つまでです" in str(exc_info.value)

    def test_start_discussion_invalid_topic(self):
        """Test discussion start with invalid topic."""
        personas = [self.persona1, self.persona2]

        # Test with empty topic
        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.start_discussion(personas, "")
        assert "議論トピックが空です" in str(exc_info.value)

        # Test with short topic
        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.start_discussion(personas, "短い")
        assert "議論トピックが短すぎます" in str(exc_info.value)

        # Test with long topic
        long_topic = "a" * 201
        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.start_discussion(personas, long_topic)
        assert "議論トピックが長すぎます" in str(exc_info.value)

    def test_start_discussion_duplicate_personas(self):
        """Test discussion start with duplicate personas."""
        topic = "テストトピック"
        personas = [self.persona1, self.persona1]  # Duplicate persona

        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.start_discussion(personas, topic)
        assert "重複したペルソナが含まれています" in str(exc_info.value)

    def test_start_discussion_ai_service_error(self):
        """Test discussion start with AI service error."""
        topic = "テストトピック"
        personas = [self.persona1, self.persona2]

        # Mock AI service error
        self.mock_ai_service.facilitate_discussion.side_effect = AIServiceError(
            "AI error"
        )

        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.start_discussion(personas, topic)

        assert "AI service error during discussion" in str(exc_info.value)

    def test_generate_insights_success(self):
        """Test successful insight generation."""
        # Create discussion with messages
        discussion = Discussion.create_new(
            topic="テストトピック", participants=[self.persona1.id, self.persona2.id]
        )
        for message in self.test_messages:
            discussion = discussion.add_message(message)

        # Mock AI service response (now returns structured data)
        insight_data = [
            {
                "category": "顧客ニーズ",
                "description": "効率性を重視する顧客層が存在する",
                "confidence_score": 0.8,
            },
            {
                "category": "市場機会",
                "description": "品質と効率性を両立した商品の需要がある",
                "confidence_score": 0.75,
            },
        ]
        self.mock_ai_service.extract_insights.return_value = insight_data

        result = self.discussion_manager.generate_insights(discussion)

        # Verify result
        assert isinstance(result, list)
        assert len(result) == 2
        assert isinstance(result[0], Insight)
        assert result[0].category == "顧客ニーズ"
        assert result[0].confidence_score == 0.8
        assert result[1].category == "市場機会"
        assert result[1].confidence_score == 0.75

        # Verify AI service was called correctly
        self.mock_ai_service.extract_insights.assert_called_once_with(
            discussion.messages, categories=None, topic=discussion.topic
        )

    def test_generate_insights_with_custom_categories(self):
        """Test insight generation with custom categories."""
        from src.models.insight_category import InsightCategory

        # Create discussion with messages
        discussion = Discussion.create_new(
            topic="テストトピック", participants=[self.persona1.id, self.persona2.id]
        )
        for message in self.test_messages:
            discussion = discussion.add_message(message)

        # Custom categories
        custom_categories = [
            InsightCategory(name="技術トレンド", description="技術的なトレンド"),
            InsightCategory(name="ユーザー体験", description="UX関連の洞察"),
        ]

        # Mock AI service response
        insight_data = [
            {
                "category": "技術トレンド",
                "description": "AIの活用が重要になっており、パーソナライズされた体験を提供できる",
                "confidence_score": 0.9,
            },
            {
                "category": "ユーザー体験",
                "description": "シンプルなUIが求められており、特にシニア層への配慮が必要",
                "confidence_score": 0.85,
            },
            {
                "category": "技術トレンド",
                "description": "セキュリティとプライバシー保護の機能が重要視されている",
                "confidence_score": 0.88,
            },
        ]
        self.mock_ai_service.extract_insights.return_value = insight_data

        result = self.discussion_manager.generate_insights(
            discussion, categories=custom_categories
        )

        # Verify result
        assert len(result) == 3
        assert result[0].category == "技術トレンド"
        assert result[1].category == "ユーザー体験"
        assert result[2].category == "技術トレンド"

        # Verify AI service was called with custom categories
        self.mock_ai_service.extract_insights.assert_called_once_with(
            discussion.messages, categories=custom_categories, topic=discussion.topic
        )

    def test_generate_insights_invalid_discussion(self):
        """Test insight generation with invalid discussion."""
        # Test with None discussion
        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.generate_insights(None)
        assert "議論オブジェクトが無効です" in str(exc_info.value)

        # Test with discussion without messages
        empty_discussion = Discussion.create_new(
            topic="テストトピック", participants=[self.persona1.id, self.persona2.id]
        )
        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.generate_insights(empty_discussion)
        assert "インサイト生成には最低2つのメッセージが必要です" in str(exc_info.value)

    def test_save_and_load_categories(self):
        """Test saving and loading categories to/from discussion config."""
        from src.models.insight_category import InsightCategory

        # Create discussion
        discussion = Discussion.create_new(
            topic="テストトピック", participants=[self.persona1.id]
        )

        # Create custom categories
        categories = [
            InsightCategory(name="カテゴリー1", description="説明1"),
            InsightCategory(name="カテゴリー2", description="説明2"),
        ]

        # Save categories to config
        updated_discussion = self.discussion_manager._save_categories_to_config(
            discussion, categories
        )

        # Verify categories are saved
        assert updated_discussion.agent_config is not None
        assert "insight_categories" in updated_discussion.agent_config

        # Load categories from config
        loaded_categories = self.discussion_manager._load_categories_from_config(
            updated_discussion
        )

        # Verify loaded categories
        assert loaded_categories is not None
        assert len(loaded_categories) == 2
        assert loaded_categories[0].name == "カテゴリー1"
        assert loaded_categories[1].name == "カテゴリー2"

    def test_generate_insights_ai_service_error(self):
        """Test insight generation with AI service error."""
        # Create discussion with messages
        discussion = Discussion.create_new(
            topic="テストトピック", participants=[self.persona1.id, self.persona2.id]
        )
        for message in self.test_messages:
            discussion = discussion.add_message(message)

        # Mock AI service error
        self.mock_ai_service.extract_insights.side_effect = AIServiceError("AI error")

        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.generate_insights(discussion)

        assert "AI service error during insight generation" in str(exc_info.value)

    def test_regenerate_insights_success(self):
        """Test successful insight regeneration."""
        from src.models.insight_category import InsightCategory

        # Create existing discussion with old insights
        discussion_id = "test-discussion-123"
        old_insights = [
            Insight.create_new(
                category="顧客ニーズ",
                description="古いインサイト1です。これは十分な長さがあります。",
                supporting_messages=[],
                confidence_score=0.7,
            )
        ]

        existing_discussion = Discussion(
            id=discussion_id,
            topic="テストトピック",
            participants=[self.persona1.id, self.persona2.id],
            messages=self.test_messages,
            insights=old_insights,
            created_at=datetime.now(),
            mode="classic",
            agent_config=None,
        )

        # Mock get_discussion to return existing discussion
        self.mock_database_service.get_discussion.return_value = existing_discussion

        # Mock AI service to return new insights
        new_insight_data = [
            {
                "category": "技術トレンド",
                "description": "新しいインサイト1です。これは十分な長さがあります。",
                "confidence_score": 0.9,
            },
            {
                "category": "ユーザー体験",
                "description": "新しいインサイト2です。これは十分な長さがあります。",
                "confidence_score": 0.85,
            },
            {
                "category": "技術トレンド",
                "description": "新しいインサイト3です。これは十分な長さがあります。",
                "confidence_score": 0.88,
            },
        ]
        self.mock_ai_service.extract_insights.return_value = new_insight_data

        # Custom categories for regeneration
        custom_categories = [
            InsightCategory(name="技術トレンド", description="技術的なトレンド"),
            InsightCategory(name="ユーザー体験", description="UX関連の洞察"),
        ]

        # Regenerate insights
        result = self.discussion_manager.regenerate_insights(
            discussion_id, categories=custom_categories
        )

        # Verify result
        assert len(result) == 3
        assert result[0].category == "技術トレンド"
        assert result[1].category == "ユーザー体験"

        # Verify get_discussion was called
        self.mock_database_service.get_discussion.assert_called_once_with(discussion_id)

        # Verify AI service was called with custom categories
        self.mock_ai_service.extract_insights.assert_called_once()

        # Verify save_discussion was called
        self.mock_database_service.save_discussion.assert_called_once()

        # Verify the saved discussion has new insights
        saved_discussion = self.mock_database_service.save_discussion.call_args[0][0]
        assert len(saved_discussion.insights) == 3
        assert saved_discussion.insights[0].description != old_insights[0].description

    def test_regenerate_insights_discussion_not_found(self):
        """Test insight regeneration when discussion not found."""
        # Mock get_discussion to return None
        self.mock_database_service.get_discussion.return_value = None

        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.regenerate_insights("nonexistent-id")

        assert "議論が見つかりません" in str(exc_info.value)

    def test_regenerate_insights_with_default_categories(self):
        """Test insight regeneration with default categories."""
        # Create existing discussion
        discussion_id = "test-discussion-456"
        existing_discussion = Discussion(
            id=discussion_id,
            topic="テストトピック",
            participants=[self.persona1.id],
            messages=self.test_messages,
            insights=[],
            created_at=datetime.now(),
            mode="classic",
            agent_config=None,
        )

        self.mock_database_service.get_discussion.return_value = existing_discussion

        # Mock AI service
        new_insight_data = [
            {
                "category": "顧客ニーズ",
                "description": "デフォルトカテゴリーでのインサイトです。十分な長さがあります。",
                "confidence_score": 0.8,
            },
            {
                "category": "市場機会",
                "description": "もう一つのインサイトです。十分な長さがあります。",
                "confidence_score": 0.75,
            },
            {
                "category": "商品開発",
                "description": "三つ目のインサイトです。十分な長さがあります。",
                "confidence_score": 0.82,
            },
        ]
        self.mock_ai_service.extract_insights.return_value = new_insight_data

        # Regenerate with default categories (None)
        result = self.discussion_manager.regenerate_insights(discussion_id)

        # Verify result
        assert len(result) == 3

        # Verify AI service was called with None categories (will use default)
        call_args = self.mock_ai_service.extract_insights.call_args
        assert call_args[1].get("categories") is None

    def test_save_discussion_success(self):
        """Test successful discussion save."""
        # Create discussion
        discussion = Discussion.create_new(
            topic="テストトピック", participants=[self.persona1.id, self.persona2.id]
        )

        # Mock database service response
        self.mock_database_service.save_discussion.return_value = discussion.id

        result = self.discussion_manager.save_discussion(discussion)

        # Verify result
        assert result == discussion.id

        # Verify database service was called correctly
        self.mock_database_service.save_discussion.assert_called_once_with(discussion)

    def test_save_discussion_invalid_discussion(self):
        """Test discussion save with invalid discussion."""
        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.save_discussion(None)
        assert "議論オブジェクトが無効です" in str(exc_info.value)

    def test_save_discussion_database_error(self):
        """Test discussion save with database error."""
        discussion = Discussion.create_new(
            topic="テストトピック", participants=[self.persona1.id, self.persona2.id]
        )

        # Mock database error
        self.mock_database_service.save_discussion.side_effect = DatabaseError(
            "Database error"
        )

        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.save_discussion(discussion)

        assert "Database error while saving discussion" in str(exc_info.value)

    def test_save_discussion_with_insights_success(self):
        """Test successful discussion save with insights."""
        # Create discussion
        discussion = Discussion.create_new(
            topic="テストトピック", participants=[self.persona1.id, self.persona2.id]
        )

        # Mock database service response
        self.mock_database_service.save_discussion.return_value = discussion.id

        result = self.discussion_manager.save_discussion_with_insights(
            discussion, self.test_insights
        )

        # Verify result
        assert result == discussion.id

        # Verify database service was called
        self.mock_database_service.save_discussion.assert_called_once()

        # Verify the discussion passed to save_discussion has insights
        saved_discussion = self.mock_database_service.save_discussion.call_args[0][0]
        assert len(saved_discussion.insights) == 2

    def test_get_discussion_success(self):
        """Test successful discussion retrieval."""
        discussion_id = "test-id"
        expected_discussion = Discussion.create_new(
            topic="テストトピック", participants=[self.persona1.id, self.persona2.id]
        )

        # Mock database service response
        self.mock_database_service.get_discussion.return_value = expected_discussion

        result = self.discussion_manager.get_discussion(discussion_id)

        # Verify result
        assert result == expected_discussion

        # Verify database service was called correctly
        self.mock_database_service.get_discussion.assert_called_once_with(discussion_id)

    def test_get_discussion_invalid_id(self):
        """Test discussion retrieval with invalid ID."""
        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.get_discussion("")
        assert "議論IDが無効です" in str(exc_info.value)

    def test_get_discussion_database_error(self):
        """Test discussion retrieval with database error."""
        discussion_id = "test-id"

        # Mock database error
        self.mock_database_service.get_discussion.side_effect = DatabaseError(
            "Database error"
        )

        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager.get_discussion(discussion_id)

        assert "Database error while retrieving discussion" in str(exc_info.value)

    def test_get_discussion_history_success(self):
        """Test successful discussion history retrieval."""
        expected_discussions = [
            Discussion.create_new(topic="トピック1", participants=[self.persona1.id]),
            Discussion.create_new(topic="トピック2", participants=[self.persona2.id]),
        ]

        # Mock database service response
        self.mock_database_service.get_discussions.return_value = expected_discussions

        result = self.discussion_manager.get_discussion_history()

        # Verify result
        assert result == expected_discussions

        # Verify database service was called correctly
        self.mock_database_service.get_discussions.assert_called_once()

    def test_get_discussions_by_topic_success(self):
        """Test successful discussion search by topic."""
        topic_pattern = "マーケティング"
        expected_discussions = [
            Discussion.create_new(
                topic="マーケティング戦略", participants=[self.persona1.id]
            )
        ]

        # Mock database service response
        self.mock_database_service.get_discussions_by_topic.return_value = (
            expected_discussions
        )

        result = self.discussion_manager.get_discussions_by_topic(topic_pattern)

        # Verify result
        assert result == expected_discussions

        # Verify database service was called correctly
        self.mock_database_service.get_discussions_by_topic.assert_called_once_with(
            topic_pattern
        )

    def test_get_discussions_by_participant_success(self):
        """Test successful discussion search by participant."""
        persona_id = self.persona1.id
        expected_discussions = [
            Discussion.create_new(topic="テストトピック", participants=[persona_id])
        ]

        # Mock database service response
        self.mock_database_service.get_discussions_by_participant.return_value = (
            expected_discussions
        )

        result = self.discussion_manager.get_discussions_by_participant(persona_id)

        # Verify result
        assert result == expected_discussions

        # Verify database service was called correctly
        self.mock_database_service.get_discussions_by_participant.assert_called_once_with(
            persona_id
        )

    def test_delete_discussion_success(self):
        """Test successful discussion deletion."""
        discussion_id = "test-id"

        # Mock database service response
        self.mock_database_service.delete_discussion.return_value = True

        result = self.discussion_manager.delete_discussion(discussion_id)

        # Verify result
        assert result

        # Verify database service was called correctly
        self.mock_database_service.delete_discussion.assert_called_once_with(
            discussion_id
        )

    def test_update_discussion_insights_success(self):
        """Test successful discussion insights update."""
        discussion_id = "test-id"

        # Mock database service response
        self.mock_database_service.update_discussion_insights.return_value = True

        result = self.discussion_manager.update_discussion_insights(
            discussion_id, self.test_insights
        )

        # Verify result
        assert result

        # Verify database service was called correctly
        self.mock_database_service.update_discussion_insights.assert_called_once_with(
            discussion_id, self.test_insights
        )

    def test_get_discussion_count_success(self):
        """Test successful discussion count retrieval."""
        expected_count = 5

        # Mock database service response
        self.mock_database_service.get_discussion_count.return_value = expected_count

        result = self.discussion_manager.get_discussion_count()

        # Verify result
        assert result == expected_count

        # Verify database service was called correctly
        self.mock_database_service.get_discussion_count.assert_called_once()

    def test_discussion_exists_success(self):
        """Test successful discussion existence check."""
        discussion_id = "test-id"

        # Mock database service response
        self.mock_database_service.discussion_exists.return_value = True

        result = self.discussion_manager.discussion_exists(discussion_id)

        # Verify result
        assert result

        # Verify database service was called correctly
        self.mock_database_service.discussion_exists.assert_called_once_with(
            discussion_id
        )

    def test_extract_category_and_description(self):
        """Test category and description extraction from insight text."""
        # Test with category pattern
        text1 = "[顧客ニーズ] 効率性を重視する顧客層が存在する"
        category1, description1 = (
            self.discussion_manager._extract_category_and_description(text1)
        )
        assert category1 == "顧客ニーズ"
        assert description1 == "効率性を重視する顧客層が存在する"

        # Test with category pattern and dash
        text2 = "[市場機会] - 品質と効率性を両立した商品の需要がある"
        category2, description2 = (
            self.discussion_manager._extract_category_and_description(text2)
        )
        assert category2 == "市場機会"
        assert description2 == "品質と効率性を両立した商品の需要がある"

        # Test without category pattern
        text3 = "これは重要なインサイトです"
        category3, description3 = (
            self.discussion_manager._extract_category_and_description(text3)
        )
        assert category3 == "その他"
        assert description3 == "これは重要なインサイトです"


class TestDiscussionManagerWithDocuments:
    """Test cases for DiscussionManager with document support."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_ai_service = Mock()
        self.mock_database_service = Mock()

        self.discussion_manager = DiscussionManager(
            ai_service=self.mock_ai_service, database_service=self.mock_database_service
        )

        self.persona1 = Persona.create_new(
            name="田中花子",
            age=30,
            occupation="マーケター",
            background="東京在住",
            values=["効率性"],
            pain_points=["時間不足"],
            goals=["成長"],
        )
        self.persona2 = Persona.create_new(
            name="佐藤太郎",
            age=35,
            occupation="開発者",
            background="大阪在住",
            values=["品質"],
            pain_points=["予算"],
            goals=["革新"],
        )

    def test_start_discussion_with_documents(self):
        """Test starting discussion with documents."""
        # Mock file info
        self.mock_database_service.get_uploaded_file_info.return_value = {
            "file_id": "doc1",
            "original_filename": "test.pdf",
            "file_path": "/tmp/test.pdf",
            "file_size": 1024,
            "mime_type": "application/pdf",
            "uploaded_at": "2024-01-01T00:00:00",
        }

        # Mock Path.exists
        from unittest.mock import patch

        with patch("pathlib.Path.exists", return_value=True):
            # Mock AI service with longer messages (total > 100 chars)
            self.mock_ai_service.facilitate_discussion.return_value = [
                Message.create_new(
                    self.persona1.id,
                    self.persona1.name,
                    "このドキュメントを見ると、顧客のニーズが明確に表れています。特に効率性を重視する傾向が強く、時間的な制約も大きな課題となっているようです。",
                ),
                Message.create_new(
                    self.persona2.id,
                    self.persona2.name,
                    "そうですね。品質も重要ですが、時間的な制約も考慮する必要があります。両立させるための工夫が求められますね。",
                ),
            ]

            # Execute
            discussion = self.discussion_manager.start_discussion(
                [self.persona1, self.persona2], "テストトピック", document_ids=["doc1"]
            )

            # Verify
            assert discussion is not None
            assert len(discussion.messages) == 2
            assert discussion.documents is not None
            assert len(discussion.documents) == 1
            assert discussion.documents[0]["filename"] == "test.pdf"

            # Verify AI service was called with documents
            call_args = self.mock_ai_service.facilitate_discussion.call_args
            assert call_args[1].get("documents") is not None

    def test_load_documents_not_found(self):
        """Test loading non-existent document."""
        self.mock_database_service.get_uploaded_file_info.return_value = None

        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager._load_documents(["invalid_id"])

        assert "ドキュメントが見つかりません" in str(exc_info.value)

    def test_load_documents_file_not_exists(self):
        """Test loading document with missing file."""
        self.mock_database_service.get_uploaded_file_info.return_value = {
            "file_id": "doc1",
            "file_path": "/nonexistent/file.pdf",
            "file_size": 1024,
            "mime_type": "application/pdf",
        }

        with pytest.raises(DiscussionManagerError) as exc_info:
            self.discussion_manager._load_documents(["doc1"])

        assert "ドキュメントファイルが存在しません" in str(exc_info.value)

    def test_load_documents_size_limit(self):
        """Test document total size limit."""
        self.mock_database_service.get_uploaded_file_info.return_value = {
            "file_id": "doc1",
            "file_path": "/tmp/large.pdf",
            "file_size": 33 * 1024 * 1024,  # 33MB
            "mime_type": "application/pdf",
        }

        from unittest.mock import patch

        with patch("pathlib.Path.exists", return_value=True):
            with pytest.raises(DiscussionManagerError) as exc_info:
                self.discussion_manager._load_documents(["doc1"])

            assert "合計サイズが制限を超えています" in str(exc_info.value)


class TestDiscussionManagerReports:
    """Test cases for report generation and deletion."""

    def setup_method(self):
        self.mock_ai_service = Mock()
        self.mock_database_service = Mock()
        self.manager = DiscussionManager(
            ai_service=self.mock_ai_service,
            database_service=self.mock_database_service,
        )

        self.discussion = Discussion.create_new(
            topic="テスト議論", participants=["p1", "p2"]
        )
        msg1 = Message.create_new(
            persona_id="p1", persona_name="田中", content="意見A", message_type="statement"
        )
        msg2 = Message.create_new(
            persona_id="p2", persona_name="佐藤", content="意見B", message_type="statement"
        )
        self.discussion = self.discussion.add_message(msg1).add_message(msg2)
        self.discussion.insights = [
            Insight(category="ニーズ", description="テストインサイト", supporting_messages=[], confidence_score=0.8)
        ]

    def test_generate_report_summary(self):
        """サマリレポート生成・保存テスト"""
        self.mock_database_service.get_discussion.return_value = self.discussion
        self.mock_ai_service.generate_discussion_report.return_value = "# サマリ"
        self.mock_database_service.save_discussion.return_value = self.discussion.id

        report = self.manager.generate_report(
            discussion_id=self.discussion.id,
            template_type="summary",
        )

        assert report.template_type == "summary"
        assert report.content == "# サマリ"
        assert report.custom_prompt is None
        self.mock_ai_service.generate_discussion_report.assert_called_once()
        self.mock_database_service.save_discussion.assert_called_once()

    def test_generate_report_custom(self):
        """カスタムプロンプトレポート生成テスト"""
        self.mock_database_service.get_discussion.return_value = self.discussion
        self.mock_ai_service.generate_discussion_report.return_value = "カスタム結果"
        self.mock_database_service.save_discussion.return_value = self.discussion.id

        report = self.manager.generate_report(
            discussion_id=self.discussion.id,
            template_type="custom",
            custom_prompt="箇条書きで",
        )

        assert report.template_type == "custom"
        assert report.custom_prompt == "箇条書きで"

    def test_generate_report_discussion_not_found(self):
        """存在しない議論のレポート生成テスト"""
        self.mock_database_service.get_discussion.return_value = None

        with pytest.raises(DiscussionManagerError, match="議論が見つかりません"):
            self.manager.generate_report(
                discussion_id="nonexistent",
                template_type="summary",
            )

    def test_delete_report(self):
        """レポート削除テスト"""
        from src.models.discussion_report import DiscussionReport

        report = DiscussionReport.create_new(template_type="summary", content="テスト")
        self.discussion.reports = [report]
        self.mock_database_service.get_discussion.return_value = self.discussion
        self.mock_database_service.save_discussion.return_value = self.discussion.id

        result = self.manager.delete_report(
            discussion_id=self.discussion.id,
            report_id=report.id,
        )

        assert result is True
        saved_discussion = self.mock_database_service.save_discussion.call_args[0][0]
        assert len(saved_discussion.reports) == 0

    def test_delete_report_not_found(self):
        """存在しないレポートの削除テスト"""
        self.mock_database_service.get_discussion.return_value = self.discussion

        with pytest.raises(DiscussionManagerError, match="レポートが見つかりません"):
            self.manager.delete_report(
                discussion_id=self.discussion.id,
                report_id="nonexistent",
            )
