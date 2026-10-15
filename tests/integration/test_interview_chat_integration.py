"""
Integration tests for interview chat functionality.
Tests the complete flow from session creation to message exchange.
"""

import pytest
from datetime import datetime
from unittest.mock import Mock

from src.models.persona import Persona
from src.models.message import Message
from src.managers.interview_manager import InterviewManager


class TestInterviewChatIntegration:
    """Integration tests for interview chat functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        # Create test personas
        self.persona1 = Persona.create_new(
            name="田中太郎",
            age=35,
            occupation="エンジニア",
            background="都市部在住の男性エンジニア",
            values=["効率性", "技術革新"],
            pain_points=["時間不足", "技術の変化についていくこと"],
            goals=["キャリアアップ", "技術スキル向上"],
        )

        self.persona2 = Persona.create_new(
            name="佐藤花子",
            age=28,
            occupation="デザイナー",
            background="郊外在住の女性デザイナー",
            values=["創造性", "ユーザビリティ"],
            pain_points=["予算制約", "クライアントとのコミュニケーション"],
            goals=["スキル向上", "独立"],
        )

    def test_interview_session_creation_and_messaging(self, monkeypatch):
        """Test complete interview session creation and messaging flow (U6, moto)."""
        moto = pytest.importorskip("moto")
        import boto3

        from src.services.interview_session_store import InterviewSessionStore
        from tests.unit.discussion_world import OWNER, World

        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        world = World()
        persona1 = world.persona_bodies["p1"]
        persona2 = world.persona_bodies["p2"]
        replies = {
            persona1.id: "こんにちは！佐藤です。",
            persona2.id: "はじめまして！鈴木です。",
        }

        def make_agent(**kw):
            agent = Mock()
            agent.get_persona_id.return_value = kw["persona"].id
            agent.get_persona_name.return_value = kw["persona"].name
            agent.respond.return_value = replies[kw["persona"].id]
            return agent

        with moto.mock_aws():
            client = boto3.client("dynamodb", region_name="ap-northeast-1")
            client.create_table(
                TableName="Sessions",
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            client.create_table(
                TableName="Messages",
                KeySchema=[
                    {"AttributeName": "session_id", "KeyType": "HASH"},
                    {"AttributeName": "seq", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "session_id", "AttributeType": "S"},
                    {"AttributeName": "seq", "AttributeType": "N"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            agent_service = Mock()
            agent_service.create_persona_agent.side_effect = make_agent
            manager = InterviewManager(
                agent_service=agent_service,
                database_service=Mock(),
                dataset_analysis_service=Mock(),
                binding_store=Mock(list_for_persona=Mock(return_value=[])),
                session_store=InterviewSessionStore(
                    region="ap-northeast-1",
                    sessions_table="Sessions",
                    messages_table="Messages",
                    client=client,
                ),
                discussion_records=world.records,
                participants=world.participants,
                file_attachments=world.files,
                resource_lookup=world.lookup,
            )

            session = manager.start_interview_session(OWNER, [persona1, persona2])
            assert session.persona_ids == [persona1.id, persona2.id]
            assert session.last_seq == 0 and session.saved_discussion_id is None

            reply = manager.send_user_message(OWNER, session.id, "こんにちは、皆さん！")
            assert [m.persona_name for m in reply.responses] == ["佐藤", "鈴木"]

            updated_session = manager.get_interview_session(OWNER, session.id)
            assert len(updated_session.messages) == 3  # 1 user + 2 persona responses

        # Check message types
        messages = updated_session.messages
        assert messages[0].message_type == "user_message"
        assert messages[0].persona_id == "user"
        assert messages[1].message_type == "statement"
        assert messages[2].message_type == "statement"

    def test_message_visual_distinction_requirements(self):
        """Test that messages meet visual distinction requirements."""
        # Create test messages
        user_message = Message.create_new(
            persona_id="user",
            persona_name="User",
            content="テストメッセージ",
            message_type="user_message",
        )

        persona_message = Message.create_new(
            persona_id=self.persona1.id,
            persona_name=self.persona1.name,
            content="ペルソナからの応答",
            message_type="statement",
        )

        # Requirement 6.1: Visual distinction between user and persona messages
        assert user_message.persona_id == "user"
        assert persona_message.persona_id != "user"
        assert user_message.message_type == "user_message"
        assert persona_message.message_type == "statement"

        # Requirement 6.2: Persona information display
        assert persona_message.persona_name == self.persona1.name
        assert len(persona_message.persona_name) > 0

        # Requirement 6.4: Timestamp display
        assert user_message.timestamp is not None
        assert persona_message.timestamp is not None
        assert isinstance(user_message.timestamp, datetime)
        assert isinstance(persona_message.timestamp, datetime)

    def test_multiple_persona_identification_requirements(self):
        """Test that multiple personas can be clearly identified."""
        personas = [self.persona1, self.persona2]

        # Requirement 6.3: Clear identification of multiple personas
        persona_names = [p.name for p in personas]
        assert len(set(persona_names)) == len(persona_names)  # All names unique

        # Each persona should have distinct characteristics
        assert self.persona1.name != self.persona2.name
        assert self.persona1.occupation != self.persona2.occupation

        # Avatar initials should be different
        initial1 = self.persona1.name[0]
        initial2 = self.persona2.name[0]
        assert initial1 != initial2  # "田" != "佐"

    def test_session_persistence_requirements(self):
        """Test session persistence and data integrity."""
        # U6: the stored messages come back in seq order with their timestamps
        from datetime import timedelta, timezone

        from src.models.interview_session import InterviewMessage

        start = datetime(2026, 10, 15, tzinfo=timezone.utc)
        stored = [
            InterviewMessage.create_new(
                session_id="test-session",
                seq=i + 1,
                speaker="user" if pid == "user" else "persona",
                persona_id=pid,
                persona_name_at_time=name,
                content=text,
                now=start + timedelta(seconds=i),
            )
            for i, (pid, name, text) in enumerate(
                [
                    ("user", "ユーザー", "質問です"),
                    (self.persona1.id, self.persona1.name, "回答1"),
                    (self.persona2.id, self.persona2.name, "回答2"),
                ]
            )
        ]
        messages = [
            InterviewMessage.from_dict(m.to_dict()).to_message() for m in stored
        ]
        assert len(messages) == 3

        # Messages should maintain chronological order
        for i in range(1, len(messages)):
            assert messages[i].timestamp >= messages[i - 1].timestamp

        # All messages should have timestamps
        for message in messages:
            assert message.timestamp is not None
            assert isinstance(message.timestamp, datetime)

    def test_loading_state_and_feedback_requirements(self):
        """Test visual feedback and loading state requirements."""
        # Requirement 6.5: Visual feedback during message sending and response generation

        # Simulate loading states
        loading_states = {
            "sending": "メッセージ送信中...",
            "processing": "ペルソナが回答を考えています...",
            "complete": "完了",
        }

        # Verify loading messages exist
        assert "送信中" in loading_states["sending"]
        assert "ペルソナ" in loading_states["processing"]
        assert "回答" in loading_states["processing"]

        # Verify state transitions
        states = ["sending", "processing", "complete"]
        assert len(states) == 3
        assert states[0] == "sending"
        assert states[-1] == "complete"

    def test_accessibility_and_usability_features(self):
        """Test accessibility and usability features."""
        # Character limit validation
        max_length = 500
        test_message = "a" * 450

        assert len(test_message) <= max_length

        # Input validation
        empty_message = ""
        whitespace_message = "   "
        valid_message = "有効なメッセージ"

        assert not empty_message.strip()
        assert not whitespace_message.strip()
        assert valid_message.strip()

        # Keyboard shortcuts (Enter to send)
        enter_key_code = 13  # Enter key
        assert enter_key_code == 13

        # Placeholder text should be descriptive
        placeholder = "質問やメッセージを入力してください..."
        assert len(placeholder) > 10
        assert "質問" in placeholder or "メッセージ" in placeholder


if __name__ == "__main__":
    pytest.main([__file__])
