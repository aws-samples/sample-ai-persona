"""
Unit tests for data models.
"""

import unittest
from datetime import datetime
import json
from src.models import Persona, Discussion, Message, Insight


class TestPersona(unittest.TestCase):
    """Test cases for Persona model."""

    def setUp(self):
        """Set up test data."""
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
        """Test creating a new persona."""
        persona = Persona.create_new(**self.sample_persona_data)

        self.assertIsNotNone(persona.id)
        self.assertEqual(persona.name, "田中太郎")
        self.assertEqual(persona.age, 35)
        self.assertEqual(persona.occupation, "システムエンジニア")
        self.assertIsInstance(persona.created_at, datetime)
        self.assertIsInstance(persona.updated_at, datetime)
        self.assertEqual(persona.created_at, persona.updated_at)

    def test_update_persona(self):
        """Test updating persona fields."""
        persona = Persona.create_new(**self.sample_persona_data)
        original_created_at = persona.created_at

        # Wait a moment to ensure different timestamp
        import time

        time.sleep(0.001)

        updated_persona = persona.update(name="田中次郎", age=36)

        self.assertEqual(updated_persona.id, persona.id)
        self.assertEqual(updated_persona.name, "田中次郎")
        self.assertEqual(updated_persona.age, 36)
        self.assertEqual(updated_persona.occupation, persona.occupation)
        self.assertEqual(updated_persona.created_at, original_created_at)
        self.assertGreater(updated_persona.updated_at, persona.updated_at)

    def test_persona_serialization(self):
        """Test persona to_dict and from_dict methods."""
        persona = Persona.create_new(**self.sample_persona_data)

        # Test to_dict
        persona_dict = persona.to_dict()
        self.assertIsInstance(persona_dict, dict)
        self.assertEqual(persona_dict["name"], "田中太郎")
        self.assertIsInstance(persona_dict["created_at"], str)

        # Test from_dict
        restored_persona = Persona.from_dict(persona_dict)
        self.assertEqual(restored_persona.id, persona.id)
        self.assertEqual(restored_persona.name, persona.name)
        self.assertEqual(restored_persona.created_at, persona.created_at)

    def test_persona_json_serialization(self):
        """Test persona JSON serialization."""
        persona = Persona.create_new(**self.sample_persona_data)

        # Test to_json
        json_str = persona.to_json()
        self.assertIsInstance(json_str, str)

        # Verify it's valid JSON
        parsed_json = json.loads(json_str)
        self.assertEqual(parsed_json["name"], "田中太郎")

        # Test from_json
        restored_persona = Persona.from_json(json_str)
        self.assertEqual(restored_persona.id, persona.id)
        self.assertEqual(restored_persona.name, persona.name)


class TestMessage(unittest.TestCase):
    """Test cases for Message model."""

    def test_create_new_message(self):
        """Test creating a new message."""
        message = Message.create_new(
            persona_id="test-id",
            persona_name="田中太郎",
            content="こんにちは、よろしくお願いします。",
        )

        self.assertEqual(message.persona_id, "test-id")
        self.assertEqual(message.persona_name, "田中太郎")
        self.assertEqual(message.content, "こんにちは、よろしくお願いします。")
        self.assertIsInstance(message.timestamp, datetime)

    def test_message_serialization(self):
        """Test message serialization."""
        message = Message.create_new(
            persona_id="test-id", persona_name="田中太郎", content="テストメッセージ"
        )

        # Test to_dict
        message_dict = message.to_dict()
        self.assertIsInstance(message_dict, dict)
        self.assertEqual(message_dict["persona_id"], "test-id")
        self.assertIsInstance(message_dict["timestamp"], str)

        # Test from_dict
        restored_message = Message.from_dict(message_dict)
        self.assertEqual(restored_message.persona_id, message.persona_id)
        self.assertEqual(restored_message.content, message.content)
        self.assertEqual(restored_message.timestamp, message.timestamp)

    def test_message_json_serialization(self):
        """Test message JSON serialization."""
        message = Message.create_new(
            persona_id="test-id", persona_name="田中太郎", content="JSONテスト"
        )

        # Test to_json
        json_str = message.to_json()
        self.assertIsInstance(json_str, str)

        # Test from_json
        restored_message = Message.from_json(json_str)
        self.assertEqual(restored_message.persona_id, message.persona_id)
        self.assertEqual(restored_message.content, message.content)

    def test_message_with_agent_mode_fields(self):
        """Test message with AI agent mode fields."""
        # Test with agent mode fields
        message = Message.create_new(
            persona_id="test-id",
            persona_name="田中太郎",
            content="エージェントモードのテスト",
            message_type="summary",
            round_number=2,
        )

        self.assertEqual(message.message_type, "summary")
        self.assertEqual(message.round_number, 2)

        # Test serialization with agent mode fields
        message_dict = message.to_dict()
        self.assertEqual(message_dict["message_type"], "summary")
        self.assertEqual(message_dict["round_number"], 2)

        # Test deserialization
        restored_message = Message.from_dict(message_dict)
        self.assertEqual(restored_message.message_type, "summary")
        self.assertEqual(restored_message.round_number, 2)

        # Test default values
        default_message = Message.create_new(
            persona_id="test-id",
            persona_name="田中太郎",
            content="デフォルト値のテスト",
        )
        self.assertEqual(default_message.message_type, "statement")
        self.assertIsNone(default_message.round_number)


class TestInsight(unittest.TestCase):
    """Test cases for Insight model."""

    def test_create_new_insight(self):
        """Test creating a new insight."""
        insight = Insight.create_new(
            category="ユーザビリティ",
            description="UIの改善が必要",
            supporting_messages=["msg1", "msg2"],
            confidence_score=0.8,
        )

        self.assertEqual(insight.category, "ユーザビリティ")
        self.assertEqual(insight.description, "UIの改善が必要")
        self.assertEqual(insight.supporting_messages, ["msg1", "msg2"])
        self.assertEqual(insight.confidence_score, 0.8)

    def test_insight_confidence_score_validation(self):
        """Test confidence score validation."""
        # Valid scores
        Insight.create_new("test", "test", [], 0.0)
        Insight.create_new("test", "test", [], 1.0)
        Insight.create_new("test", "test", [], 0.5)

        # Invalid scores
        with self.assertRaises(ValueError):
            Insight.create_new("test", "test", [], -0.1)

        with self.assertRaises(ValueError):
            Insight.create_new("test", "test", [], 1.1)

    def test_insight_serialization(self):
        """Test insight serialization."""
        insight = Insight.create_new(
            category="テスト",
            description="テスト説明",
            supporting_messages=["msg1"],
            confidence_score=0.7,
        )

        # Test to_dict
        insight_dict = insight.to_dict()
        self.assertIsInstance(insight_dict, dict)
        self.assertEqual(insight_dict["category"], "テスト")
        self.assertEqual(insight_dict["confidence_score"], 0.7)

        # Test from_dict
        restored_insight = Insight.from_dict(insight_dict)
        self.assertEqual(restored_insight.category, insight.category)
        self.assertEqual(restored_insight.confidence_score, insight.confidence_score)

    def test_insight_json_serialization(self):
        """Test insight JSON serialization."""
        insight = Insight.create_new(
            category="JSON",
            description="JSONテスト",
            supporting_messages=["msg1"],
            confidence_score=0.9,
        )

        # Test to_json
        json_str = insight.to_json()
        self.assertIsInstance(json_str, str)

        # Test from_json
        restored_insight = Insight.from_json(json_str)
        self.assertEqual(restored_insight.category, insight.category)
        self.assertEqual(restored_insight.confidence_score, insight.confidence_score)


class TestDiscussion(unittest.TestCase):
    """Test cases for Discussion model."""

    def setUp(self):
        """Set up test data."""
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
        """Test creating a new discussion."""
        discussion = Discussion.create_new(
            topic="商品改善について", participants=["persona1", "persona2"]
        )

        self.assertIsNotNone(discussion.id)
        self.assertEqual(discussion.topic, "商品改善について")
        self.assertEqual(discussion.participants, ["persona1", "persona2"])
        self.assertEqual(len(discussion.messages), 0)
        self.assertEqual(len(discussion.insights), 0)
        self.assertIsInstance(discussion.created_at, datetime)
        self.assertEqual(discussion.mode, "classic")
        self.assertIsNone(discussion.agent_config)

    def test_create_new_discussion_with_agent_mode(self):
        """Test creating a new discussion with agent mode."""
        agent_config = {"rounds": 3, "additional_instructions": "Be concise"}
        discussion = Discussion.create_new(
            topic="AIエージェントモードテスト",
            participants=["persona1", "persona2"],
            mode="agent",
            agent_config=agent_config,
        )

        self.assertIsNotNone(discussion.id)
        self.assertEqual(discussion.topic, "AIエージェントモードテスト")
        self.assertEqual(discussion.mode, "agent")
        self.assertIsNotNone(discussion.agent_config)
        self.assertEqual(discussion.agent_config["rounds"], 3)
        self.assertEqual(
            discussion.agent_config["additional_instructions"], "Be concise"
        )

    def test_add_message_to_discussion(self):
        """Test adding messages to discussion."""
        discussion = Discussion.create_new(
            topic="テスト議論", participants=["persona1"]
        )

        updated_discussion = discussion.add_message(self.sample_message)

        self.assertEqual(len(updated_discussion.messages), 1)
        self.assertEqual(updated_discussion.messages[0].content, "テストメッセージ")
        # Original discussion should remain unchanged
        self.assertEqual(len(discussion.messages), 0)
        # Mode and agent_config should be preserved
        self.assertEqual(updated_discussion.mode, discussion.mode)
        self.assertEqual(updated_discussion.agent_config, discussion.agent_config)

    def test_add_insight_to_discussion(self):
        """Test adding insights to discussion."""
        discussion = Discussion.create_new(
            topic="テスト議論", participants=["persona1"]
        )

        updated_discussion = discussion.add_insight(self.sample_insight)

        self.assertEqual(len(updated_discussion.insights), 1)
        self.assertEqual(updated_discussion.insights[0].category, "テスト")
        # Original discussion should remain unchanged
        self.assertEqual(len(discussion.insights), 0)
        # Mode and agent_config should be preserved
        self.assertEqual(updated_discussion.mode, discussion.mode)
        self.assertEqual(updated_discussion.agent_config, discussion.agent_config)

    def test_get_messages_by_persona(self):
        """Test filtering messages by persona."""
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
        self.assertEqual(len(persona1_messages), 2)
        self.assertEqual(persona1_messages[0].content, "メッセージ1")
        self.assertEqual(persona1_messages[1].content, "メッセージ3")

    def test_discussion_serialization(self):
        """Test discussion serialization."""
        discussion = Discussion.create_new(
            topic="シリアライゼーションテスト", participants=["persona1"]
        )
        discussion = discussion.add_message(self.sample_message)
        discussion = discussion.add_insight(self.sample_insight)

        # Test to_dict
        discussion_dict = discussion.to_dict()
        self.assertIsInstance(discussion_dict, dict)
        self.assertEqual(discussion_dict["topic"], "シリアライゼーションテスト")
        self.assertIsInstance(discussion_dict["created_at"], str)
        self.assertIsInstance(discussion_dict["messages"], list)
        self.assertIsInstance(discussion_dict["insights"], list)
        self.assertEqual(discussion_dict["mode"], "classic")
        self.assertIsNone(discussion_dict["agent_config"])

        # Test from_dict
        restored_discussion = Discussion.from_dict(discussion_dict)
        self.assertEqual(restored_discussion.id, discussion.id)
        self.assertEqual(restored_discussion.topic, discussion.topic)
        self.assertEqual(len(restored_discussion.messages), 1)
        self.assertEqual(len(restored_discussion.insights), 1)
        self.assertEqual(restored_discussion.created_at, discussion.created_at)
        self.assertEqual(restored_discussion.mode, "classic")
        self.assertIsNone(restored_discussion.agent_config)

    def test_discussion_serialization_with_agent_mode(self):
        """Test discussion serialization with agent mode."""
        agent_config = {"rounds": 5, "additional_instructions": "Focus on user needs"}
        discussion = Discussion.create_new(
            topic="エージェントモードシリアライゼーション",
            participants=["persona1", "persona2"],
            mode="agent",
            agent_config=agent_config,
        )
        discussion = discussion.add_message(self.sample_message)

        # Test to_dict
        discussion_dict = discussion.to_dict()
        self.assertEqual(discussion_dict["mode"], "agent")
        self.assertIsNotNone(discussion_dict["agent_config"])
        self.assertEqual(discussion_dict["agent_config"]["rounds"], 5)

        # Test from_dict
        restored_discussion = Discussion.from_dict(discussion_dict)
        self.assertEqual(restored_discussion.mode, "agent")
        self.assertIsNotNone(restored_discussion.agent_config)
        self.assertEqual(restored_discussion.agent_config["rounds"], 5)
        self.assertEqual(
            restored_discussion.agent_config["additional_instructions"],
            "Focus on user needs",
        )

    def test_discussion_backward_compatibility(self):
        """Test backward compatibility with discussions without mode field."""
        # Simulate old discussion data without mode and agent_config
        old_discussion_dict = {
            "id": "test-id",
            "topic": "旧形式の議論",
            "participants": ["persona1"],
            "messages": [],
            "insights": [],
            "created_at": datetime.now().isoformat(),
        }

        # Should successfully restore with default values
        restored_discussion = Discussion.from_dict(old_discussion_dict)
        self.assertEqual(restored_discussion.mode, "classic")
        self.assertIsNone(restored_discussion.agent_config)

    def test_discussion_json_serialization(self):
        """Test discussion JSON serialization."""
        discussion = Discussion.create_new(
            topic="JSONテスト", participants=["persona1"]
        )
        discussion = discussion.add_message(self.sample_message)

        # Test to_json
        json_str = discussion.to_json()
        self.assertIsInstance(json_str, str)

        # Verify it's valid JSON
        parsed_json = json.loads(json_str)
        self.assertEqual(parsed_json["topic"], "JSONテスト")
        self.assertEqual(parsed_json["mode"], "classic")

        # Test from_json
        restored_discussion = Discussion.from_json(json_str)
        self.assertEqual(restored_discussion.id, discussion.id)
        self.assertEqual(restored_discussion.topic, discussion.topic)
        self.assertEqual(len(restored_discussion.messages), 1)
        self.assertEqual(restored_discussion.mode, "classic")

    def test_discussion_json_serialization_with_agent_mode(self):
        """Test discussion JSON serialization with agent mode."""
        agent_config = {"rounds": 4, "model": "claude-3"}
        discussion = Discussion.create_new(
            topic="JSONエージェントモードテスト",
            participants=["persona1", "persona2"],
            mode="agent",
            agent_config=agent_config,
        )

        # Test to_json
        json_str = discussion.to_json()
        self.assertIsInstance(json_str, str)

        # Verify it's valid JSON
        parsed_json = json.loads(json_str)
        self.assertEqual(parsed_json["mode"], "agent")
        self.assertEqual(parsed_json["agent_config"]["rounds"], 4)

        # Test from_json
        restored_discussion = Discussion.from_json(json_str)
        self.assertEqual(restored_discussion.mode, "agent")
        self.assertEqual(restored_discussion.agent_config["rounds"], 4)

    def test_create_interview_session(self):
        """Test creating an interview session."""
        participants = ["persona1", "persona2"]
        interview = Discussion.create_interview_session(participants)

        self.assertEqual(interview.mode, "interview")
        self.assertEqual(interview.topic, "Interview Session")
        self.assertEqual(interview.participants, participants)
        self.assertEqual(len(interview.messages), 0)
        self.assertTrue(interview.is_interview_session())

    def test_add_user_message(self):
        """Test adding a user message to interview session."""
        interview = Discussion.create_interview_session(["persona1"])
        user_content = "こんにちは、質問があります。"

        updated_interview = interview.add_user_message(user_content)

        self.assertEqual(len(updated_interview.messages), 1)
        message = updated_interview.messages[0]
        self.assertEqual(message.persona_id, "user")
        self.assertEqual(message.persona_name, "User")
        self.assertEqual(message.content, user_content)
        self.assertEqual(message.message_type, "user_message")
        self.assertTrue(message.is_user_message())

    def test_add_persona_response(self):
        """Test adding a persona response to interview session."""
        interview = Discussion.create_interview_session(["persona1"])
        persona_id = "persona1"
        persona_name = "田中太郎"
        response_content = "はい、お答えします。"

        updated_interview = interview.add_persona_response(
            persona_id, persona_name, response_content
        )

        self.assertEqual(len(updated_interview.messages), 1)
        message = updated_interview.messages[0]
        self.assertEqual(message.persona_id, persona_id)
        self.assertEqual(message.persona_name, persona_name)
        self.assertEqual(message.content, response_content)
        self.assertEqual(message.message_type, "statement")
        self.assertTrue(message.is_persona_response())

    def test_interview_message_filtering(self):
        """Test filtering user messages and persona responses."""
        interview = Discussion.create_interview_session(["persona1"])

        # Add user message
        interview = interview.add_user_message("ユーザーの質問")
        # Add persona response
        interview = interview.add_persona_response(
            "persona1", "田中太郎", "ペルソナの回答"
        )
        # Add another user message
        interview = interview.add_user_message("別の質問")

        user_messages = interview.get_user_messages()
        persona_responses = interview.get_persona_responses()

        self.assertEqual(len(user_messages), 2)
        self.assertEqual(len(persona_responses), 1)

        # Check user messages
        for msg in user_messages:
            self.assertTrue(msg.is_user_message())
            self.assertEqual(msg.persona_id, "user")

        # Check persona responses
        for msg in persona_responses:
            self.assertTrue(msg.is_persona_response())
            self.assertEqual(msg.persona_id, "persona1")

    def test_interview_session_serialization(self):
        """Test interview session serialization and deserialization."""
        interview = Discussion.create_interview_session(["persona1", "persona2"])
        interview = interview.add_user_message("テスト質問")
        interview = interview.add_persona_response("persona1", "田中太郎", "テスト回答")

        # Test to_dict
        interview_dict = interview.to_dict()
        self.assertEqual(interview_dict["mode"], "interview")
        self.assertEqual(interview_dict["topic"], "Interview Session")

        # Test from_dict
        restored_interview = Discussion.from_dict(interview_dict)
        self.assertEqual(restored_interview.mode, "interview")
        self.assertTrue(restored_interview.is_interview_session())
        self.assertEqual(len(restored_interview.messages), 2)

        # Test JSON serialization
        json_str = interview.to_json()
        restored_from_json = Discussion.from_json(json_str)
        self.assertEqual(restored_from_json.mode, "interview")
        self.assertEqual(len(restored_from_json.messages), 2)


class TestMessageInterviewExtensions(unittest.TestCase):
    """Test cases for Message model interview extensions."""

    def test_user_message_creation(self):
        """Test creating a user message."""
        user_message = Message.create_new(
            persona_id="user",
            persona_name="User",
            content="ユーザーからの質問",
            message_type="user_message",
        )

        self.assertEqual(user_message.persona_id, "user")
        self.assertEqual(user_message.persona_name, "User")
        self.assertEqual(user_message.message_type, "user_message")
        self.assertTrue(user_message.is_user_message())
        self.assertFalse(user_message.is_persona_response())

    def test_persona_response_identification(self):
        """Test identifying persona responses."""
        statement_msg = Message.create_new(
            persona_id="persona1",
            persona_name="田中太郎",
            content="通常の発言",
            message_type="statement",
        )

        summary_msg = Message.create_new(
            persona_id="persona1",
            persona_name="田中太郎",
            content="まとめ",
            message_type="summary",
        )

        facilitation_msg = Message.create_new(
            persona_id="facilitator",
            persona_name="ファシリテーター",
            content="進行",
            message_type="facilitation",
        )

        # All should be identified as persona responses
        self.assertTrue(statement_msg.is_persona_response())
        self.assertTrue(summary_msg.is_persona_response())
        self.assertTrue(facilitation_msg.is_persona_response())

        # None should be user messages
        self.assertFalse(statement_msg.is_user_message())
        self.assertFalse(summary_msg.is_user_message())
        self.assertFalse(facilitation_msg.is_user_message())

    def test_discussion_with_documents(self):
        """Test creating discussion with documents (Task 1)."""
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

        self.assertIsNotNone(discussion.documents)
        self.assertEqual(len(discussion.documents), 1)
        self.assertEqual(discussion.documents[0]["filename"], "test.png")

    def test_discussion_without_documents(self):
        """Test creating discussion without documents (Task 1)."""
        discussion = Discussion.create_new(
            topic="通常の議論", participants=["persona1", "persona2"]
        )

        self.assertIsNone(discussion.documents)

    def test_discussion_documents_serialization(self):
        """Test discussion with documents serialization (Task 1)."""
        documents = [{"id": "doc1", "filename": "test.pdf"}]
        discussion = Discussion.create_new(
            topic="テスト", participants=["persona1"], documents=documents
        )

        # Test to_dict
        discussion_dict = discussion.to_dict()
        self.assertIn("documents", discussion_dict)
        self.assertEqual(discussion_dict["documents"], documents)

        # Test from_dict
        restored = Discussion.from_dict(discussion_dict)
        self.assertEqual(restored.documents, documents)

    def test_discussion_backward_compatibility(self):
        """Test backward compatibility with old data without documents (Task 1)."""
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
        self.assertIsNone(discussion.documents)


if __name__ == "__main__":
    unittest.main()
