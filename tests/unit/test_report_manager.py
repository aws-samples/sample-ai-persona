"""ReportManager のユニットテスト。"""

import pytest
from unittest.mock import Mock, patch

from src.managers.report_manager import ReportManager, ReportManagerError
from src.models.discussion import Discussion, Message
from src.models.discussion_report import DiscussionReport
from src.models.insight import Insight


@pytest.mark.unit
class TestReportManagerCRUD:
    """レポートCRUD操作のテスト。"""

    def setup_method(self):
        self.mock_ai_service = Mock()
        self.mock_agent_service = Mock()
        self.mock_database_service = Mock()
        self.manager = ReportManager(
            ai_service=self.mock_ai_service,
            agent_service=self.mock_agent_service,
            database_service=self.mock_database_service,
        )
        self.discussion = Discussion.create_new(
            topic="テスト議論", participants=["p1", "p2"]
        )

    def test_save_report_success(self):
        """レポート保存が成功すること"""
        self.mock_database_service.get_discussion.return_value = self.discussion
        self.mock_database_service.save_discussion.return_value = self.discussion.id

        report = DiscussionReport.create_new(template_type="summary", content="テスト")
        self.manager.save_report(self.discussion.id, report)

        saved = self.mock_database_service.save_discussion.call_args[0][0]
        assert len(saved.reports) == 1
        assert saved.reports[0].id == report.id

    def test_save_report_exceeds_limit(self):
        """レポート3件上限でエラーになること"""
        self.discussion.reports = [
            DiscussionReport.create_new(template_type="summary", content=f"r{i}")
            for i in range(3)
        ]
        self.mock_database_service.get_discussion.return_value = self.discussion

        with pytest.raises(ReportManagerError, match="最大3件"):
            self.manager.save_report(
                self.discussion.id,
                DiscussionReport.create_new(template_type="summary", content="4th"),
            )

    def test_save_report_discussion_not_found(self):
        """議論が見つからない場合エラーになること"""
        self.mock_database_service.get_discussion.return_value = None

        with pytest.raises(ReportManagerError, match="議論が見つかりません"):
            self.manager.save_report(
                "nonexistent",
                DiscussionReport.create_new(template_type="summary", content="test"),
            )

    def test_update_report_content_success(self):
        """レポート内容更新が成功すること"""
        report = DiscussionReport.create_new(
            template_type="summary", content="元の内容"
        )
        self.discussion.reports = [report]
        self.mock_database_service.get_discussion.return_value = self.discussion

        self.manager.update_report_content(
            self.discussion.id, report.id, "更新後の内容"
        )

        saved = self.mock_database_service.save_discussion.call_args[0][0]
        assert saved.reports[0].content == "更新後の内容"

    def test_update_report_content_not_found(self):
        """存在しないレポートの更新でエラーになること"""
        self.mock_database_service.get_discussion.return_value = self.discussion

        with pytest.raises(ReportManagerError, match="レポートが見つかりません"):
            self.manager.update_report_content(
                self.discussion.id, "nonexistent", "content"
            )

    def test_delete_report_success(self):
        """レポート削除が成功すること"""
        report = DiscussionReport.create_new(template_type="summary", content="テスト")
        self.discussion.reports = [report]
        self.mock_database_service.get_discussion.return_value = self.discussion

        result = self.manager.delete_report(self.discussion.id, report.id)

        assert result is True
        saved = self.mock_database_service.save_discussion.call_args[0][0]
        assert len(saved.reports) == 0

    def test_delete_report_not_found(self):
        """存在しないレポートの削除でエラーになること"""
        self.mock_database_service.get_discussion.return_value = self.discussion

        with pytest.raises(ReportManagerError, match="レポートが見つかりません"):
            self.manager.delete_report(self.discussion.id, "nonexistent")

    def test_get_discussion(self):
        """議論取得が動作すること"""
        self.mock_database_service.get_discussion.return_value = self.discussion
        result = self.manager.get_discussion(self.discussion.id)
        assert result == self.discussion


@pytest.mark.unit
class TestReportManagerStreaming:
    """レポートストリーミング生成のテスト。"""

    def setup_method(self):
        self.mock_ai_service = Mock()
        self.mock_agent_service = Mock()
        self.mock_database_service = Mock()
        self.manager = ReportManager(
            ai_service=self.mock_ai_service,
            agent_service=self.mock_agent_service,
            database_service=self.mock_database_service,
        )
        self.discussion = Discussion.create_new(
            topic="テスト議論", participants=["p1", "p2"]
        )
        msg1 = Message.create_new("p1", "田中", "意見Aです。テスト用に十分長い発言。")
        msg2 = Message.create_new("p2", "佐藤", "意見Bです。テスト用に十分長い発言。")
        self.discussion = self.discussion.add_message(msg1).add_message(msg2)
        self.discussion.insights = [
            Insight(
                category="ニーズ",
                description="ユーザーは品質を重視する傾向がある",
                supporting_messages=[],
                confidence_score=0.8,
            )
        ]

    def test_generate_standard_report_streaming(self):
        """標準レポート生成が成功すること"""
        self.mock_database_service.get_discussion.return_value = self.discussion
        self.mock_database_service.get_persona.return_value = None
        self.mock_ai_service.generate_standard_report_streaming.return_value = iter(
            ["chunk1", "chunk2"]
        )

        chunks = list(
            self.manager.generate_report_streaming(
                discussion_id=self.discussion.id,
                template_type="summary",
            )
        )

        assert chunks == ["chunk1", "chunk2"]
        self.mock_ai_service.generate_standard_report_streaming.assert_called_once()

    def test_generate_data_driven_report_streaming(self):
        """データドリブンレポート生成がAgentServiceに委譲されること"""
        self.mock_database_service.get_discussion.return_value = self.discussion
        self.mock_database_service.get_persona.return_value = None
        self.mock_agent_service.run_report_agent_streaming.return_value = iter(
            ["agent_result"]
        )

        chunks = list(
            self.manager.generate_report_streaming(
                discussion_id=self.discussion.id,
                template_type="data_driven",
            )
        )

        assert chunks == ["agent_result"]
        self.mock_agent_service.run_report_agent_streaming.assert_called_once()

    def test_generate_report_discussion_not_found(self):
        """議論が見つからない場合エラーになること"""
        self.mock_database_service.get_discussion.return_value = None

        with pytest.raises(ReportManagerError, match="議論が見つかりません"):
            list(
                self.manager.generate_report_streaming(
                    discussion_id="bad", template_type="summary"
                )
            )

    @patch("src.managers.report_manager.service_factory")
    def test_followup_with_stm_available(self, mock_sf):
        """STM有効時にフォローアップが追加指示のみで生成されること"""
        mock_sf.check_stm_session_exists.return_value = True
        self.mock_database_service.get_discussion.return_value = self.discussion
        self.mock_database_service.get_persona.return_value = None
        self.mock_agent_service.run_report_agent_streaming.return_value = iter(
            ["followup_result"]
        )

        chunks = list(
            self.manager.generate_followup_report_streaming(
                discussion_id=self.discussion.id,
                followup_prompt="追加分析して",
                previous_report="前回レポート",
                session_id="test-session",
            )
        )

        assert chunks == ["followup_result"]
        self.mock_agent_service.run_report_agent_streaming.assert_called_once()
        call_kwargs = (
            self.mock_agent_service.run_report_agent_streaming.call_args.kwargs
        )
        assert call_kwargs["session_id"] == "test-session"

    @patch("src.managers.report_manager.service_factory")
    def test_followup_with_stm_expired(self, mock_sf):
        """STM期限切れ時にフォールバックでレポートが注入されること"""
        mock_sf.check_stm_session_exists.return_value = False
        self.mock_database_service.get_discussion.return_value = self.discussion
        self.mock_database_service.get_persona.return_value = None
        self.mock_agent_service.run_report_agent_streaming.return_value = iter(
            ["fallback_result"]
        )

        chunks = list(
            self.manager.generate_followup_report_streaming(
                discussion_id=self.discussion.id,
                followup_prompt="追加分析して",
                previous_report="前回レポート内容",
                session_id="test-session",
            )
        )

        assert chunks == ["fallback_result"]
        self.mock_agent_service.run_report_agent_streaming.assert_called_once()


@pytest.mark.unit
class TestReportManagerContext:
    """レポートコンテキスト構築のテスト。"""

    def setup_method(self):
        self.mock_ai_service = Mock()
        self.mock_agent_service = Mock()
        self.mock_database_service = Mock()
        self.manager = ReportManager(
            ai_service=self.mock_ai_service,
            agent_service=self.mock_agent_service,
            database_service=self.mock_database_service,
        )

    def test_get_report_context(self):
        """インサイト・ペルソナデータが正しく抽出されること"""
        discussion = Discussion.create_new(topic="T", participants=["p1"])
        msg = Message.create_new("p1", "田中", "テストメッセージです。十分な長さ。")
        discussion = discussion.add_message(msg)
        discussion.insights = [
            Insight(
                category="C",
                description="D",
                supporting_messages=[],
                confidence_score=0.9,
            )
        ]
        persona_mock = Mock()
        persona_mock.name = "田中"
        persona_mock.age = 30
        persona_mock.occupation = "エンジニア"
        persona_mock.values = ["品質"]
        persona_mock.pain_points = ["予算"]
        persona_mock.goals = ["成長"]
        self.mock_database_service.get_persona.return_value = persona_mock

        insights_data, personas_data = self.manager._get_report_context(discussion)

        assert len(insights_data) == 1
        assert insights_data[0]["category"] == "C"
        assert len(personas_data) == 1
        assert personas_data[0]["name"] == "田中"
