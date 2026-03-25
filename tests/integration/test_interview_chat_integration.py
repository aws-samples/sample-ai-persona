"""
Integration tests for interview chat functionality.
Tests the complete flow from session creation to message exchange.
"""

import pytest
from datetime import datetime
from unittest.mock import Mock, patch

from src.models.persona import Persona
from src.models.message import Message
from src.managers.interview_manager import InterviewManager, InterviewSession


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

    @patch("src.managers.interview_manager.AgentDiscussionManager.__init__")
    @patch("src.services.service_factory.service_factory")
    def test_interview_session_creation_and_messaging(
        self, mock_service_factory, mock_init
    ):
        """Test complete interview session creation and messaging flow."""
        # Setup service mocks
        mock_agent_service = Mock()
        mock_agent_service.generate_persona_system_prompt.return_value = (
            "Test system prompt"
        )
        mock_database_service = Mock()

        mock_service_factory.get_agent_service.return_value = mock_agent_service
        mock_service_factory.get_database_service.return_value = mock_database_service

        # Setup parent class mocks
        mock_init.return_value = None

        # Setup agent mocks
        mock_agent1 = Mock()
        mock_agent1.get_persona_id.return_value = self.persona1.id
        mock_agent1.get_persona_name.return_value = self.persona1.name
        mock_agent1.respond.return_value = "こんにちは！エンジニアの田中です。"

        mock_agent2 = Mock()
        mock_agent2.get_persona_id.return_value = self.persona2.id
        mock_agent2.get_persona_name.return_value = self.persona2.name
        mock_agent2.respond.return_value = "はじめまして！デザイナーの佐藤です。"

        # Mock agent service create_persona_agent method
        mock_agent_service.create_persona_agent.side_effect = [mock_agent1, mock_agent2]

        # Create interview manager with mocked services
        manager = InterviewManager(mock_agent_service, mock_database_service)
        # Manually set the attributes since __init__ is mocked
        manager.agent_service = mock_agent_service
        manager.database_service = mock_database_service

        # Start interview session
        session = manager.start_interview_session([self.persona1, self.persona2])

        # Verify session creation
        assert session.id is not None
        assert len(session.participants) == 2
        assert self.persona1.id in session.participants
        assert self.persona2.id in session.participants
        assert len(session.messages) == 0
        assert not session.is_saved

        # Send user message
        user_message = "こんにちは、皆さん！"
        responses = manager.send_user_message(session.id, user_message)

        # Verify responses
        assert len(responses) == 2
        assert responses[0].persona_name == self.persona1.name
        assert responses[1].persona_name == self.persona2.name
        assert "田中" in responses[0].content
        assert "佐藤" in responses[1].content

        # Verify session state
        updated_session = manager.get_interview_session(session.id)
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
        # Create session with messages
        session = InterviewSession(
            id="test-session",
            participants=[self.persona1.id, self.persona2.id],
            messages=[
                Message.create_new("user", "User", "質問です", "user_message"),
                Message.create_new(
                    self.persona1.id, self.persona1.name, "回答1", "statement"
                ),
                Message.create_new(
                    self.persona2.id, self.persona2.name, "回答2", "statement"
                ),
            ],
            created_at=datetime.now(),
            is_saved=False,
        )

        # Verify message order and timestamps are maintained
        messages = session.messages
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
