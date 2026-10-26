"""ReportManager のユニットテスト（U6: 生成は読める人、保存・変更は管理できる人）。

BR4.1〜BR4.4、NFR1.45、NFR1.52、NFR1.62。DiscussionRecords・ParticipantReader・
AccessPolicy は本物、テーブルは tests/unit/discussion_world.py の偽物、AI はモック。
"""

from unittest.mock import Mock, patch

import pytest

from src.managers.report_manager import (
    ReportManager,
    ReportManagerError,
    report_session_id,
)
from src.models.discussion import Discussion
from src.models.errors import AccessDeniedError, CodedError, ErrorCode
from src.models.insight import Insight
from src.models.message import Message
from tests.error_helpers import raises_code
from tests.unit.discussion_world import (
    ADMIN,
    NO_ROLE,
    OWNER,
    PEER,
    STRANGER,
    World,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def ai() -> Mock:
    ai = Mock()
    ai.generate_standard_report_streaming.side_effect = lambda **kw: iter(
        ["chunk1", "chunk2"]
    )
    return ai


@pytest.fixture
def agent_service() -> Mock:
    agent = Mock()
    agent.run_report_agent_streaming.side_effect = lambda **kw: iter(["agent_result"])
    return agent


@pytest.fixture
def manager(world, ai, agent_service) -> ReportManager:
    return ReportManager(
        ai_service=ai,
        agent_service=agent_service,
        discussion_records=world.records,
        participants=world.participants,
    )


def _seed(world: World, principal=OWNER, participants=("p1", "p2")) -> Discussion:
    discussion = Discussion.create_new(
        topic="テスト議論のテーマ",
        participants=list(participants),
        mode="classic",
        participant_names={p: world.persona_bodies[p].name for p in participants},
    ).with_insights(
        [
            Insight(
                category="ニーズ",
                description="ユーザーは品質を重視する傾向がある",
                supporting_messages=[],
                confidence_score=0.8,
            )
        ]
    )
    messages = [
        Message.create_new(p, world.persona_bodies[p].name, f"{p}の十分に長い発言です")
        for p in participants
    ]
    return world.records.save_new(principal, discussion, messages)


def _generate(manager, principal, discussion_id, template_type="summary"):
    return list(
        manager.generate_report_streaming(principal, discussion_id, template_type)
    )


# =============================================================================
# 生成（BR4.1、BR4.3）
# =============================================================================


class TestGenerate:
    def test_owner_generates_a_standard_report(self, manager, world, ai):
        discussion = _seed(world)
        assert _generate(manager, OWNER, discussion.id) == ["chunk1", "chunk2"]
        content = ai.generate_standard_report_streaming.call_args.kwargs[
            "converse_messages"
        ][0]["content"][0]["text"]
        assert "p1の十分に長い発言です" in content
        assert "ユーザーは品質を重視する傾向がある" in content

    def test_shared_reader_may_generate(self, manager, world):
        discussion = _seed(world)
        world.share(discussion.id, "sales")
        assert _generate(manager, PEER, discussion.id) == ["chunk1", "chunk2"]

    def test_unrelated_user_gets_not_found_before_the_ai(self, manager, world, ai):
        discussion = _seed(world)
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            _generate(manager, STRANGER, discussion.id)
        ai.generate_standard_report_streaming.assert_not_called()

    def test_ai_execute_is_required(self, manager, world, ai):
        discussion = _seed(world)
        with pytest.raises(AccessDeniedError):
            _generate(manager, NO_ROLE, discussion.id)
        ai.generate_standard_report_streaming.assert_not_called()

    def test_missing_discussion_is_not_found(self, manager):
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            _generate(manager, OWNER, "nonexistent")

    def test_profiles_come_from_readable_participants_only(self, manager, world, ai):
        discussion = _seed(world, participants=("p1", "p2", "p3"))
        _generate(manager, OWNER, discussion.id)
        text = ai.generate_standard_report_streaming.call_args.kwargs[
            "converse_messages"
        ][0]["content"][0]["text"]
        # p1 は本人の、p2 は営業部に共有。p3 は読めない
        assert "### 佐藤" in text and "### 鈴木" in text
        assert "### 田中" not in text

    def test_data_driven_uses_a_session_of_the_discussion_and_user(
        self, manager, world, agent_service
    ):
        discussion = _seed(world)
        world.share(discussion.id, "sales")
        _generate(manager, OWNER, discussion.id, "data_driven")
        _generate(manager, PEER, discussion.id, "data_driven")
        sessions = [
            c.kwargs["session_id"]
            for c in agent_service.run_report_agent_streaming.call_args_list
        ]
        assert sessions[0] == report_session_id(discussion.id, OWNER.sub)
        assert sessions[1] == report_session_id(discussion.id, PEER.sub)
        assert sessions[0] != sessions[1]

    # --- 閉域対応 U3 BR3.2 の (5)、BR3.7 ---

    @pytest.fixture
    def not_allowed(self, monkeypatch):
        from src.config import config

        monkeypatch.setattr(config, "DATA_AGENT_ALLOWED", False)

    def test_data_driven_stops_before_the_agent_when_not_allowed(
        self, manager, world, agent_service, not_allowed
    ):
        discussion = _seed(world)
        with raises_code(ReportManagerError, ErrorCode.DATA_AGENT_NOT_ALLOWED):
            _generate(manager, OWNER, discussion.id, "data_driven")
        agent_service.run_report_agent_streaming.assert_not_called()

    def test_followup_stops_before_the_agent_when_not_allowed(
        self, manager, world, agent_service, not_allowed
    ):
        discussion = _seed(world)
        with patch("src.managers.report_manager.service_factory") as factory:
            factory.check_stm_session_exists.return_value = False
            with raises_code(ReportManagerError, ErrorCode.DATA_AGENT_NOT_ALLOWED):
                list(
                    manager.generate_followup_report_streaming(
                        OWNER, discussion.id, "追加", "前回"
                    )
                )
        agent_service.run_report_agent_streaming.assert_not_called()

    @pytest.mark.parametrize("template_type", ["summary", "review", "custom"])
    def test_other_templates_are_not_stopped(
        self, manager, world, not_allowed, template_type
    ):
        discussion = _seed(world)
        assert _generate(manager, OWNER, discussion.id, template_type) == [
            "chunk1",
            "chunk2",
        ]

    def test_pre_check_refuses_after_ai_execute(self, manager, not_allowed):
        with pytest.raises(AccessDeniedError):
            manager.ensure_data_agent_allowed(NO_ROLE)
        with raises_code(ReportManagerError, ErrorCode.DATA_AGENT_NOT_ALLOWED):
            manager.ensure_data_agent_allowed(OWNER)

    def test_pre_check_passes_when_allowed(self, manager):
        manager.ensure_data_agent_allowed(OWNER)

    def test_session_id_does_not_carry_the_raw_sub(self):
        session = report_session_id("d1", "user-sub-123")
        assert session.startswith("report-d1-")
        assert "user-sub-123" not in session
        assert session == report_session_id("d1", "user-sub-123")

    @patch("src.managers.report_manager.service_factory")
    def test_followup_with_stm_available(self, mock_sf, manager, world, agent_service):
        """STM有効時にフォローアップが追加指示のみで生成されること"""
        mock_sf.check_stm_session_exists.return_value = True
        discussion = _seed(world)

        chunks = list(
            manager.generate_followup_report_streaming(
                OWNER, discussion.id, "追加分析して", "前回レポート"
            )
        )

        assert chunks == ["agent_result"]
        expected = report_session_id(discussion.id, OWNER.sub)
        mock_sf.check_stm_session_exists.assert_called_once_with(expected)
        kwargs = agent_service.run_report_agent_streaming.call_args.kwargs
        assert kwargs["session_id"] == expected
        assert "前回レポート" not in kwargs["user_content"] + kwargs["system_prompt"]

    @patch("src.managers.report_manager.service_factory")
    def test_followup_with_stm_expired(self, mock_sf, manager, world, agent_service):
        """STM期限切れ時にフォールバックでレポートが注入されること"""
        mock_sf.check_stm_session_exists.return_value = False
        discussion = _seed(world)

        chunks = list(
            manager.generate_followup_report_streaming(
                OWNER, discussion.id, "追加分析して", "前回レポート内容"
            )
        )

        assert chunks == ["agent_result"]
        kwargs = agent_service.run_report_agent_streaming.call_args.kwargs
        assert "前回レポート内容" in kwargs["system_prompt"]

    def test_followup_needs_a_readable_discussion(self, manager, world):
        discussion = _seed(world)
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            list(
                manager.generate_followup_report_streaming(
                    STRANGER, discussion.id, "追加", "前回"
                )
            )


# =============================================================================
# 保存・更新・削除（BR4.2、BR4.4、NFR1.45、NFR1.62）
# =============================================================================


class TestSave:
    def test_owner_saves_with_a_server_id(self, manager, world):
        discussion = _seed(world)
        report = manager.save_report(OWNER, discussion.id, "summary", "内容", None)
        assert report.report_id
        assert world.store.bodies[discussion.id]["report_count"] == 1
        assert [
            r.report_id for r in manager.get_discussion(OWNER, discussion.id).reports
        ] == [report.report_id]

    def test_shared_reader_is_denied(self, manager, world):
        discussion = _seed(world)
        world.share(discussion.id, "sales")
        with pytest.raises(AccessDeniedError):
            manager.save_report(PEER, discussion.id, "summary", "内容", None)
        assert world.store.reports.get(discussion.id, {}) == {}

    def test_unrelated_user_gets_not_found(self, manager, world):
        discussion = _seed(world)
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            manager.save_report(STRANGER, discussion.id, "summary", "内容", None)

    def test_admin_may_save(self, manager, world):
        discussion = _seed(world, principal=STRANGER)
        assert manager.save_report(ADMIN, discussion.id, "summary", "内容", None)

    def test_fourth_report_is_refused(self, manager, world):
        discussion = _seed(world)
        for i in range(3):
            manager.save_report(OWNER, discussion.id, "summary", f"r{i}", None)
        with raises_code(CodedError, ErrorCode.REPORT_LIMIT_REACHED, max_reports=3):
            manager.save_report(OWNER, discussion.id, "summary", "4th", None)

    def test_invalid_template_type_is_refused(self, manager, world):
        discussion = _seed(world)
        with raises_code(ReportManagerError, ErrorCode.REPORT_OPERATION_FAILED):
            manager.save_report(OWNER, discussion.id, "poem", "内容", None)


class TestUpdateAndDelete:
    def test_owner_updates_and_deletes(self, manager, world):
        discussion = _seed(world)
        report = manager.save_report(OWNER, discussion.id, "summary", "元", None)

        manager.update_report_content(OWNER, discussion.id, report.report_id, "更新")
        (stored,) = manager.get_discussion(OWNER, discussion.id).reports
        assert stored.content == "更新"

        assert manager.delete_report(OWNER, discussion.id, report.report_id) is True
        assert manager.get_discussion(OWNER, discussion.id).reports == []
        assert world.store.bodies[discussion.id]["report_count"] == 0

    def test_shared_reader_cannot_change(self, manager, world):
        discussion = _seed(world)
        report = manager.save_report(OWNER, discussion.id, "summary", "元", None)
        world.share(discussion.id, "sales")
        with pytest.raises(AccessDeniedError):
            manager.update_report_content(PEER, discussion.id, report.report_id, "x")
        with pytest.raises(AccessDeniedError):
            manager.delete_report(PEER, discussion.id, report.report_id)

    def test_missing_report_is_not_found(self, manager, world):
        discussion = _seed(world)
        with raises_code(CodedError, ErrorCode.REPORT_NOT_FOUND):
            manager.update_report_content(OWNER, discussion.id, "nonexistent", "x")
        with raises_code(CodedError, ErrorCode.REPORT_NOT_FOUND):
            manager.delete_report(OWNER, discussion.id, "nonexistent")

    def test_get_and_export_a_report_for_readers(self, manager, world):
        discussion = _seed(world)
        report = manager.save_report(
            OWNER, discussion.id, "summary", "# 見出し\n**太字**", None
        )
        world.share(discussion.id, "sales")

        assert manager.get_report(PEER, discussion.id, report.report_id).content
        filename, text = manager.export_report(
            PEER, discussion.id, report.report_id, "txt"
        )
        assert filename.startswith("report_summary_") and filename.endswith(".txt")
        assert text == "見出し\n太字"
        _, markdown = manager.export_report(PEER, discussion.id, report.report_id, "md")
        assert markdown == "# 見出し\n**太字**"
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            manager.get_report(STRANGER, discussion.id, report.report_id)
        with raises_code(CodedError, ErrorCode.REPORT_NOT_FOUND):
            manager.export_report(OWNER, discussion.id, "nonexistent", "md")

    def test_can_manage_tells_the_screen_what_to_show(self, manager, world):
        discussion = _seed(world)
        world.share(discussion.id, "sales")
        assert manager.can_manage(OWNER, discussion.id)
        assert not manager.can_manage(PEER, discussion.id)
        assert not manager.can_manage(STRANGER, discussion.id)

    def test_get_discussion_is_for_readers(self, manager, world):
        discussion = _seed(world)
        assert manager.get_discussion(OWNER, discussion.id).id == discussion.id
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            manager.get_discussion(STRANGER, discussion.id)


class TestReportContext:
    def test_user_content_lists_profiles_messages_and_insights(self, manager):
        text = manager._build_report_user_content(
            [Message.create_new("p1", "田中", "テストメッセージです")],
            [{"category": "C", "description": "D", "confidence_score": 0.9}],
            "T",
            [
                {
                    "name": "田中",
                    "age": 30,
                    "occupation": "エンジニア",
                    "values": ["品質"],
                    "pain_points": ["予算"],
                    "goals": ["成長"],
                }
            ],
        )
        assert "### 田中（30歳 / エンジニア）" in text
        assert "**田中**: テストメッセージです" in text
        assert "- [C] D" in text
