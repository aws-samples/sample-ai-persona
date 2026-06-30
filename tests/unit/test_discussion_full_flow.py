"""DiscussionManager / AgentDiscussionManager フルフローAPIのテスト。"""

import pytest
from unittest.mock import Mock

from src.models.persona import Persona
from src.models.discussion import Discussion
from src.models.message import Message
from src.managers.discussion_manager import DiscussionManager, DiscussionManagerError
from src.managers.agent_discussion_manager import (
    AgentDiscussionManager,
    AgentDiscussionManagerError,
)


def _make_persona(pid: str, name: str) -> Persona:
    from datetime import datetime

    return Persona(
        id=pid,
        name=name,
        age=30,
        occupation="会社員",
        background="テスト背景",
        values=["効率"],
        pain_points=["忙しい"],
        goals=["成長"],
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


@pytest.mark.unit
class TestDiscussionManagerFullFlow:
    """DiscussionManager.run_classic_discussion のテスト"""

    def setup_method(self):
        self.mock_ai = Mock()
        self.mock_db = Mock()
        self.manager = DiscussionManager(
            ai_service=self.mock_ai, database_service=self.mock_db
        )
        self.personas = [_make_persona("p1", "太郎"), _make_persona("p2", "花子")]

    def test_run_classic_discussion_returns_discussion(self):
        """フルフローがDiscussionオブジェクトを返すこと"""
        msg1 = Message.create_new(
            persona_id="p1",
            persona_name="太郎",
            content="私はこのテーマについて非常に強い関心を持っています。日常生活の中で具体的に感じたことを共有させていただきます。特に通勤時間の使い方について深く考えることがあります。",
            message_type="statement",
        )
        msg2 = Message.create_new(
            persona_id="p2",
            persona_name="花子",
            content="太郎さんの意見に大変共感します。私の場合は在宅勤務が中心なので少し異なる視点がありますが、時間の有効活用という点では同じ課題を感じています。",
            message_type="statement",
        )
        self.mock_ai.facilitate_discussion.return_value = [msg1, msg2]
        self.mock_ai.extract_insights.return_value = []
        self.mock_db.save_discussion.return_value = "disc-id"

        result = self.manager.run_classic_discussion(
            personas=self.personas, topic="テストトピック"
        )

        assert isinstance(result, Discussion)
        assert result.topic == "テストトピック"
        self.mock_db.save_discussion.assert_called_once()

    def test_run_classic_discussion_validation_error(self):
        """ペルソナ不足でDiscussionManagerErrorが投げられること"""
        with pytest.raises(DiscussionManagerError):
            self.manager.run_classic_discussion(
                personas=[self.personas[0]], topic="テスト"
            )

    def test_run_classic_discussion_streaming_yields_events(self):
        """ストリーミングフルフローがSSEイベントをyieldすること"""
        mock_msg = Message.create_new(
            persona_id="p1",
            persona_name="太郎",
            content="発言",
            message_type="statement",
        )
        self.mock_ai.facilitate_discussion_streaming.return_value = iter([mock_msg])
        self.mock_ai.extract_insights.return_value = []
        self.mock_db.save_discussion.return_value = "disc-id"

        events = list(
            self.manager.run_classic_discussion_streaming(
                personas=self.personas, topic="テストトピック"
            )
        )

        assert len(events) >= 2
        assert '"type": "message"' in events[0]
        assert '"type": "complete"' in events[-1]

    def test_get_default_categories(self):
        """デフォルトカテゴリーがList[dict]で返ること"""
        result = self.manager.get_default_categories()

        assert isinstance(result, list)
        assert len(result) > 0
        assert "name" in result[0]


@pytest.mark.unit
class TestAgentDiscussionManagerFullFlow:
    """AgentDiscussionManager.run_agent_discussion_full のテスト"""

    def setup_method(self):
        self.mock_agent_service = Mock()
        self.mock_db = Mock()
        self.manager = AgentDiscussionManager(
            agent_service=self.mock_agent_service, database_service=self.mock_db
        )
        self.personas = [_make_persona("p1", "太郎"), _make_persona("p2", "花子")]

    def test_validate_memory_mode_invalid(self):
        """不正なmemory_modeでAgentDiscussionManagerErrorが投げられること"""
        with pytest.raises(AgentDiscussionManagerError, match="無効なmemory_mode"):
            self.manager._validate_memory_mode("invalid_mode")

    def test_validate_memory_mode_valid(self):
        """正常なmemory_modeで例外が投げられないこと"""
        for mode in ["full", "retrieve_only", "disabled"]:
            self.manager._validate_memory_mode(mode)
