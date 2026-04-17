"""
Unit tests for data models.
"""

import time
import json
from datetime import datetime

import pytest

from src.models import Persona, Discussion, Message, Insight


class TestPersona:
    """Test cases for Persona model."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.sample_persona_data = {
            "name": "田中太郎",
            "age": 35,
            "occupation": "システムエンジニア",
            "background": "IT業界で10年の経験を持つ",
            "values": ["効率性", "技術革新", "チームワーク"],
            "pain_points": ["長時間労働", "技術の変化についていくこと"],
            "goals": ["ワークライフバランスの改善", "技術スキルの向上"],
        }

    def test_create_new_persona(self):
        persona = Persona.create_new(**self.sample_persona_data)
        assert persona.id is not None
        assert persona.name == "田中太郎"
        assert persona.age == 35
        assert persona.occupation == "システムエンジニア"
        assert isinstance(persona.created_at, datetime)
        assert isinstance(persona.updated_at, datetime)
        assert persona.created_at == persona.updated_at

    def test_update_persona(self):
        persona = Persona.create_new(**self.sample_persona_data)
        original_created_at = persona.created_at
        time.sleep(0.001)

        updated_persona = persona.update(name="田中次郎", age=36)

        assert updated_persona.id == persona.id
        assert updated_persona.name == "田中次郎"
        assert updated_persona.age == 36
        assert updated_persona.occupation == persona.occupation
        assert updated_persona.created_at == original_created_at
        assert updated_persona.updated_at > persona.updated_at

    def test_persona_serialization(self):
        persona = Persona.create_new(**self.sample_persona_data)

        persona_dict = persona.to_dict()
        assert isinstance(persona_dict, dict)
        assert persona_dict["name"] == "田中太郎"
        assert isinstance(persona_dict["created_at"], str)

        restored_persona = Persona.from_dict(persona_dict)
        assert restored_persona.id == persona.id
        assert restored_persona.name == persona.name
        assert restored_persona.created_at == persona.created_at

    def test_persona_json_serialization(self):
        persona = Persona.create_new(**self.sample_persona_data)

        json_str = persona.to_json()
        assert isinstance(json_str, str)

        parsed_json = json.loads(json_str)
        assert parsed_json["name"] == "田中太郎"

        restored_persona = Persona.from_json(json_str)
        assert restored_persona.id == persona.id
        assert restored_persona.name == persona.name


class TestMessage:
    """Test cases for Message model."""

    def test_create_new_message(self):
        message = Message.create_new(
            persona_id="test-id",
            persona_name="田中太郎",
            content="こんにちは、よろしくお願いします。",
        )
        assert message.persona_id == "test-id"
        assert message.persona_name == "田中太郎"
        assert message.content == "こんにちは、よろしくお願いします。"
        assert isinstance(message.timestamp, datetime)

    def test_message_serialization(self):
        message = Message.create_new(
            persona_id="test-id", persona_name="田中太郎", content="テストメッセージ"
        )

        message_dict = message.to_dict()
        assert isinstance(message_dict, dict)
        assert message_dict["persona_id"] == "test-id"
        assert isinstance(message_dict["timestamp"], str)

        restored_message = Message.from_dict(message_dict)
        assert restored_message.persona_id == message.persona_id
        assert restored_message.content == message.content
        assert restored_message.timestamp == message.timestamp

    def test_message_json_serialization(self):
        message = Message.create_new(
            persona_id="test-id", persona_name="田中太郎", content="JSONテスト"
        )

        json_str = message.to_json()
        assert isinstance(json_str, str)

        restored_message = Message.from_json(json_str)
        assert restored_message.persona_id == message.persona_id
        assert restored_message.content == message.content

    def test_message_with_agent_mode_fields(self):
        message = Message.create_new(
            persona_id="test-id",
            persona_name="田中太郎",
            content="エージェントモードのテスト",
            message_type="summary",
            round_number=2,
        )
        assert message.message_type == "summary"
        assert message.round_number == 2

        message_dict = message.to_dict()
        assert message_dict["message_type"] == "summary"
        assert message_dict["round_number"] == 2

        restored_message = Message.from_dict(message_dict)
        assert restored_message.message_type == "summary"
        assert restored_message.round_number == 2

        default_message = Message.create_new(
            persona_id="test-id",
            persona_name="田中太郎",
            content="デフォルト値のテスト",
        )
        assert default_message.message_type == "statement"
        assert default_message.round_number is None


class TestInsight:
    """Test cases for Insight model."""

    def test_create_new_insight(self):
        insight = Insight.create_new(
            category="ユーザビリティ",
            description="UIの改善が必要",
            supporting_messages=["msg1", "msg2"],
            confidence_score=0.8,
        )
        assert insight.category == "ユーザビリティ"
        assert insight.description == "UIの改善が必要"
        assert insight.supporting_messages == ["msg1", "msg2"]
        assert insight.confidence_score == 0.8

    def test_insight_confidence_score_validation(self):
        Insight.create_new("test", "test", [], 0.0)
        Insight.create_new("test", "test", [], 1.0)
        Insight.create_new("test", "test", [], 0.5)

        with pytest.raises(ValueError):
            Insight.create_new("test", "test", [], -0.1)

        with pytest.raises(ValueError):
            Insight.create_new("test", "test", [], 1.1)

    def test_insight_serialization(self):
        insight = Insight.create_new(
            category="テスト",
            description="テスト説明",
            supporting_messages=["msg1"],
            confidence_score=0.7,
        )

        insight_dict = insight.to_dict()
        assert isinstance(insight_dict, dict)
        assert insight_dict["category"] == "テスト"
        assert insight_dict["confidence_score"] == 0.7

        restored_insight = Insight.from_dict(insight_dict)
        assert restored_insight.category == insight.category
        assert restored_insight.confidence_score == insight.confidence_score

    def test_insight_json_serialization(self):
        insight = Insight.create_new(
            category="JSON",
            description="JSONテスト",
            supporting_messages=["msg1"],
            confidence_score=0.9,
        )

        json_str = insight.to_json()
        assert isinstance(json_str, str)

        restored_insight = Insight.from_json(json_str)
        assert restored_insight.category == insight.category
        assert restored_insight.confidence_score == insight.confidence_score


class TestDiscussion:
    """Test cases for Discussion model."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.sample_message = Message.create_new(
            persona_id="persona1", persona_name="田中太郎", content="テストメッセージ"
        )
        self.sample_insight = Insight.create_new(
            category="テスト",
            description="テストインサイト",
            supporting_messages=["msg1"],
            confidence_score=0.8,
        )

    def test_create_new_discussion(self):
        discussion = Discussion.create_new(
            topic="商品改善について", participants=["persona1", "persona2"]
        )
        assert discussion.id is not None
        assert discussion.topic == "商品改善について"
        assert discussion.participants == ["persona1", "persona2"]
        assert len(discussion.messages) == 0
        assert len(discussion.insights) == 0
        assert isinstance(discussion.created_at, datetime)
        assert discussion.mode == "classic"
        assert discussion.agent_config is None

    def test_create_new_discussion_with_agent_mode(self):
        agent_config = {"rounds": 3, "additional_instructions": "Be concise"}
        discussion = Discussion.create_new(
            topic="AIエージェントモードテスト",
            participants=["persona1", "persona2"],
            mode="agent",
            agent_config=agent_config,
        )
        assert discussion.id is not None
        assert discussion.topic == "AIエージェントモードテスト"
        assert discussion.mode == "agent"
        assert discussion.agent_config is not None
        assert discussion.agent_config["rounds"] == 3
        assert discussion.agent_config["additional_instructions"] == "Be concise"

    def test_add_message_to_discussion(self):
        discussion = Discussion.create_new(
            topic="テスト議論", participants=["persona1"]
        )
        updated_discussion = discussion.add_message(self.sample_message)

        assert len(updated_discussion.messages) == 1
        assert updated_discussion.messages[0].content == "テストメッセージ"
        assert len(discussion.messages) == 0
        assert updated_discussion.mode == discussion.mode
        assert updated_discussion.agent_config == discussion.agent_config

    def test_add_insight_to_discussion(self):
        discussion = Discussion.create_new(
            topic="テスト議論", participants=["persona1"]
        )
        updated_discussion = discussion.add_insight(self.sample_insight)

        assert len(updated_discussion.insights) == 1
        assert updated_discussion.insights[0].category == "テスト"
        assert len(discussion.insights) == 0
        assert updated_discussion.mode == discussion.mode
        assert updated_discussion.agent_config == discussion.agent_config

    def test_get_messages_by_persona(self):
        discussion = Discussion.create_new(
            topic="テスト議論", participants=["persona1", "persona2"]
        )
        message1 = Message.create_new("persona1", "田中", "メッセージ1")
        message2 = Message.create_new("persona2", "佐藤", "メッセージ2")
        message3 = Message.create_new("persona1", "田中", "メッセージ3")

        discussion = discussion.add_message(message1)
        discussion = discussion.add_message(message2)
        discussion = discussion.add_message(message3)

        persona1_messages = discussion.get_messages_by_persona("persona1")
        assert len(persona1_messages) == 2
        assert persona1_messages[0].content == "メッセージ1"
        assert persona1_messages[1].content == "メッセージ3"

    def test_discussion_serialization(self):
        discussion = Discussion.create_new(
            topic="シリアライゼーションテスト", participants=["persona1"]
        )
        discussion = discussion.add_message(self.sample_message)
        discussion = discussion.add_insight(self.sample_insight)

        discussion_dict = discussion.to_dict()
        assert isinstance(discussion_dict, dict)
        assert discussion_dict["topic"] == "シリアライゼーションテスト"
        assert isinstance(discussion_dict["created_at"], str)
        assert isinstance(discussion_dict["messages"], list)
        assert isinstance(discussion_dict["insights"], list)
        assert discussion_dict["mode"] == "classic"
        assert discussion_dict["agent_config"] is None

        restored_discussion = Discussion.from_dict(discussion_dict)
        assert restored_discussion.id == discussion.id
        assert restored_discussion.topic == discussion.topic
        assert len(restored_discussion.messages) == 1
        assert len(restored_discussion.insights) == 1
        assert restored_discussion.created_at == discussion.created_at
        assert restored_discussion.mode == "classic"
        assert restored_discussion.agent_config is None

    def test_discussion_serialization_with_agent_mode(self):
        agent_config = {"rounds": 5, "additional_instructions": "Focus on user needs"}
        discussion = Discussion.create_new(
            topic="エージェントモードシリアライゼーション",
            participants=["persona1", "persona2"],
            mode="agent",
            agent_config=agent_config,
        )
        discussion = discussion.add_message(self.sample_message)

        discussion_dict = discussion.to_dict()
        assert discussion_dict["mode"] == "agent"
        assert discussion_dict["agent_config"] is not None
        assert discussion_dict["agent_config"]["rounds"] == 5

        restored_discussion = Discussion.from_dict(discussion_dict)
        assert restored_discussion.mode == "agent"
        assert restored_discussion.agent_config is not None
        assert restored_discussion.agent_config["rounds"] == 5
        assert restored_discussion.agent_config["additional_instructions"] == "Focus on user needs"

    def test_discussion_backward_compatibility(self):
        old_discussion_dict = {
            "id": "test-id",
            "topic": "旧形式の議論",
            "participants": ["persona1"],
            "messages": [],
            "insights": [],
            "created_at": datetime.now().isoformat(),
        }
        restored_discussion = Discussion.from_dict(old_discussion_dict)
        assert restored_discussion.mode == "classic"
        assert restored_discussion.agent_config is None

    def test_discussion_json_serialization(self):
        discussion = Discussion.create_new(
            topic="JSONテスト", participants=["persona1"]
        )
        discussion = discussion.add_message(self.sample_message)

        json_str = discussion.to_json()
        assert isinstance(json_str, str)

        parsed_json = json.loads(json_str)
        assert parsed_json["topic"] == "JSONテスト"
        assert parsed_json["mode"] == "classic"

        restored_discussion = Discussion.from_json(json_str)
        assert restored_discussion.id == discussion.id
        assert restored_discussion.topic == discussion.topic
        assert len(restored_discussion.messages) == 1
        assert restored_discussion.mode == "classic"

    def test_discussion_json_serialization_with_agent_mode(self):
        agent_config = {"rounds": 4, "model": "claude-3"}
        discussion = Discussion.create_new(
            topic="JSONエージェントモードテスト",
            participants=["persona1", "persona2"],
            mode="agent",
            agent_config=agent_config,
        )

        json_str = discussion.to_json()
        assert isinstance(json_str, str)

        parsed_json = json.loads(json_str)
        assert parsed_json["mode"] == "agent"
        assert parsed_json["agent_config"]["rounds"] == 4

        restored_discussion = Discussion.from_json(json_str)
        assert restored_discussion.mode == "agent"
        assert restored_discussion.agent_config["rounds"] == 4

    def test_create_interview_session(self):
        participants = ["persona1", "persona2"]
        interview = Discussion.create_interview_session(participants)
        assert interview.mode == "interview"
        assert interview.topic == "Interview Session"
        assert interview.participants == participants
        assert len(interview.messages) == 0
        assert interview.is_interview_session()

    def test_add_user_message(self):
        interview = Discussion.create_interview_session(["persona1"])
        user_content = "こんにちは、質問があります。"
        updated_interview = interview.add_user_message(user_content)

        assert len(updated_interview.messages) == 1
        message = updated_interview.messages[0]
        assert message.persona_id == "user"
        assert message.persona_name == "User"
        assert message.content == user_content
        assert message.message_type == "user_message"
        assert message.is_user_message()

    def test_add_persona_response(self):
        interview = Discussion.create_interview_session(["persona1"])
        updated_interview = interview.add_persona_response(
            "persona1", "田中太郎", "はい、お答えします。"
        )

        assert len(updated_interview.messages) == 1
        message = updated_interview.messages[0]
        assert message.persona_id == "persona1"
        assert message.persona_name == "田中太郎"
        assert message.content == "はい、お答えします。"
        assert message.message_type == "statement"
        assert message.is_persona_response()

    def test_interview_message_filtering(self):
        interview = Discussion.create_interview_session(["persona1"])
        interview = interview.add_user_message("ユーザーの質問")
        interview = interview.add_persona_response("persona1", "田中太郎", "ペルソナの回答")
        interview = interview.add_user_message("別の質問")

        user_messages = interview.get_user_messages()
        persona_responses = interview.get_persona_responses()

        assert len(user_messages) == 2
        assert len(persona_responses) == 1

        for msg in user_messages:
            assert msg.is_user_message()
            assert msg.persona_id == "user"

        for msg in persona_responses:
            assert msg.is_persona_response()
            assert msg.persona_id == "persona1"

    def test_interview_session_serialization(self):
        interview = Discussion.create_interview_session(["persona1", "persona2"])
        interview = interview.add_user_message("テスト質問")
        interview = interview.add_persona_response("persona1", "田中太郎", "テスト回答")

        interview_dict = interview.to_dict()
        assert interview_dict["mode"] == "interview"
        assert interview_dict["topic"] == "Interview Session"

        restored_interview = Discussion.from_dict(interview_dict)
        assert restored_interview.mode == "interview"
        assert restored_interview.is_interview_session()
        assert len(restored_interview.messages) == 2

        json_str = interview.to_json()
        restored_from_json = Discussion.from_json(json_str)
        assert restored_from_json.mode == "interview"
        assert len(restored_from_json.messages) == 2


class TestMessageInterviewExtensions:
    """Test cases for Message model interview extensions."""

    def test_user_message_creation(self):
        user_message = Message.create_new(
            persona_id="user",
            persona_name="User",
            content="ユーザーからの質問",
            message_type="user_message",
        )
        assert user_message.persona_id == "user"
        assert user_message.persona_name == "User"
        assert user_message.message_type == "user_message"
        assert user_message.is_user_message()
        assert not user_message.is_persona_response()

    def test_persona_response_identification(self):
        statement_msg = Message.create_new(
            persona_id="persona1", persona_name="田中太郎",
            content="通常の発言", message_type="statement",
        )
        summary_msg = Message.create_new(
            persona_id="persona1", persona_name="田中太郎",
            content="まとめ", message_type="summary",
        )
        facilitation_msg = Message.create_new(
            persona_id="facilitator", persona_name="ファシリテーター",
            content="進行", message_type="facilitation",
        )

        assert statement_msg.is_persona_response()
        assert summary_msg.is_persona_response()
        assert facilitation_msg.is_persona_response()

        assert not statement_msg.is_user_message()
        assert not summary_msg.is_user_message()
        assert not facilitation_msg.is_user_message()

    def test_discussion_with_documents(self):
        documents = [
            {
                "id": "doc1",
                "filename": "test.png",
                "file_path": "discussion_documents/uuid_test.png",
                "file_size": 1024,
                "mime_type": "image/png",
                "uploaded_at": "2026-01-26T20:00:00",
            }
        ]
        discussion = Discussion.create_new(
            topic="ドキュメント付き議論",
            participants=["persona1", "persona2"],
            documents=documents,
        )
        assert discussion.documents is not None
        assert len(discussion.documents) == 1
        assert discussion.documents[0]["filename"] == "test.png"

    def test_discussion_without_documents(self):
        discussion = Discussion.create_new(
            topic="通常の議論", participants=["persona1", "persona2"]
        )
        assert discussion.documents is None

    def test_discussion_documents_serialization(self):
        documents = [{"id": "doc1", "filename": "test.pdf"}]
        discussion = Discussion.create_new(
            topic="テスト", participants=["persona1"], documents=documents
        )

        discussion_dict = discussion.to_dict()
        assert "documents" in discussion_dict
        assert discussion_dict["documents"] == documents

        restored = Discussion.from_dict(discussion_dict)
        assert restored.documents == documents

    def test_discussion_backward_compatibility_no_documents(self):
        old_data = {
            "id": "old-id",
            "topic": "Old discussion",
            "participants": ["persona1"],
            "messages": [],
            "insights": [],
            "created_at": "2026-01-26T20:00:00",
            "mode": "classic",
        }
        discussion = Discussion.from_dict(old_data)
        assert discussion.documents is None

    def test_discussion_with_reports(self):
        from src.models.discussion_report import DiscussionReport

        report = DiscussionReport.create_new(
            template_type="summary",
            content="# サマリ",
        )
        discussion = Discussion.create_new(
            topic="レポートテスト", participants=["persona1"]
        )
        discussion.reports = [report]

        data = discussion.to_dict()
        assert len(data["reports"]) == 1
        assert data["reports"][0]["template_type"] == "summary"

        restored = Discussion.from_dict(data)
        assert len(restored.reports) == 1
        assert restored.reports[0].id == report.id
        assert restored.reports[0].content == "# サマリ"

    def test_discussion_backward_compatibility_no_reports(self):
        old_data = {
            "id": "old-id",
            "topic": "Old discussion",
            "participants": ["persona1"],
            "messages": [],
            "insights": [],
            "created_at": "2026-01-26T20:00:00",
            "mode": "classic",
        }
        discussion = Discussion.from_dict(old_data)
        assert discussion.reports == []

    def test_discussion_default_reports_empty(self):
        discussion = Discussion.create_new(
            topic="デフォルトテスト", participants=["persona1"]
        )
        assert discussion.reports == []
