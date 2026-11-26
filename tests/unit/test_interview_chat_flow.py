"""
Integration tests for interview chat functionality.
Tests the complete flow from session creation to message exchange.
"""

import pytest
from unittest.mock import Mock

from src.models.persona import Persona
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
        """Test complete interview session creation and messaging flow (moto)."""
        moto = pytest.importorskip("moto")
        import boto3

        from src.services.stores.interview_session_store import InterviewSessionStore
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
                dataset_store=Mock(),
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
