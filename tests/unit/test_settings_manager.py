"""SettingsManager のユニットテスト。

U7: BR3.1、BR3.3、BR5.1、BR5.2、NFR1.72、NFR8.23、NFR8.24、AC5.2.1、AC5.2.3、AC5.3.1、
AC5.3.3。KB とデータ分析エージェントの設定は settings:manage（なければ 404）。
DynamoDB は moto（tests/unit/survey_world.py）。
"""

import pytest
from unittest.mock import Mock, patch

from src.managers.settings_manager import SettingsManager, SettingsManagerError
from src.models.errors import CodedError, ErrorCode
from src.models.sharing import AssetScopeChange
from tests.error_helpers import raises_code
from src.services.stores.knowledge_base_store import KnowledgeBaseStore
from tests.log_helpers import LogCapture
from tests.unit.survey_world import ADMIN, MEMBER_A, SurveyWorld


@pytest.fixture
def manager(survey_world: SurveyWorld) -> SettingsManager:
    return SettingsManager(
        knowledge_base_store=survey_world.kb_store,
        departments=survey_world.departments,
    )


@pytest.fixture
def logs(capsys) -> LogCapture:
    return LogCapture(capsys)


class TestSettingsAreAdminOnly:
    @pytest.mark.parametrize(
        "call",
        [
            lambda m: m.get_all_knowledge_bases(MEMBER_A),
            lambda m: m.create_knowledge_base(MEMBER_A, "KB1", "n"),
            lambda m: m.delete_knowledge_base(MEMBER_A, "kb"),
            lambda m: m.scope_dialog(MEMBER_A, "kb"),
            lambda m: m.update_scope(MEMBER_A, "kb", AssetScopeChange()),
            lambda m: m.save_data_agent_settings(MEMBER_A, "arn", "us-east-1", True),
            lambda m: m.test_data_agent_connection(MEMBER_A),
            lambda m: m.get_data_agent_settings(MEMBER_A),
        ],
    )
    def test_member_gets_404_and_nothing_changes(
        self, manager, survey_world, logs, call
    ):
        scan = survey_world.spy("scan")
        put = survey_world.spy("put_item")
        with patch("src.managers.settings_manager.config") as config:
            config.DATA_AGENT_RUNTIME_ARN = "arn:before"
            with raises_code(CodedError, ErrorCode.SETTINGS_NOT_FOUND):
                call(manager)
            assert config.DATA_AGENT_RUNTIME_ARN == "arn:before"
        assert scan.call_count == 0
        assert put.call_count == 0
        denied = logs.events("authz.denied")
        assert [e["reason"] for e in denied] == ["hidden"]

    def test_availability_needs_no_permission(self):
        with patch("src.managers.settings_manager.config") as config:
            config.ENABLE_DATA_AGENT = True
            config.DATA_AGENT_RUNTIME_ARN = "arn"
            assert SettingsManager.is_data_agent_available() is True

    def test_admin_reads_the_data_agent_settings(self, manager):
        with patch("src.managers.settings_manager.config") as config:
            config.ENABLE_DATA_AGENT = True
            config.DATA_AGENT_RUNTIME_ARN = "arn:agent"
            config.DATA_AGENT_REGION = "ap-northeast-1"
            settings = manager.get_data_agent_settings(ADMIN)
        assert settings == {
            "data_agent_enabled": True,
            "data_agent_runtime_arn": "arn:agent",
            "data_agent_region": "ap-northeast-1",
        }


class TestKnowledgeBases:
    def test_create_strips_and_starts_admin_only(self, manager):
        kb = manager.create_knowledge_base(ADMIN, "  KB99999  ", "  名前  ", "  説明  ")
        assert kb.knowledge_base_id == "KB99999"
        assert kb.name == "名前"
        assert kb.description == "説明"
        listing = manager.get_all_knowledge_bases(ADMIN)
        assert [k.id for k in listing.items] == [kb.id]
        assert listing.items[0].shared_group_ids == frozenset()
        assert listing.scope_labels[kb.id] == []

    def test_delete(self, manager, survey_world):
        kb = survey_world.knowledge_base()
        manager.delete_knowledge_base(ADMIN, kb.id)
        assert manager.get_all_knowledge_bases(ADMIN).items == []

    def test_listing_labels_the_scope(self, manager, survey_world):
        kb = survey_world.knowledge_base("plan")
        listing = manager.get_all_knowledge_bases(ADMIN)
        assert listing.scope_labels[kb.id] == ["企画部"]


class TestKnowledgeBaseScope:
    def test_dialog(self, manager, survey_world):
        kb = survey_world.knowledge_base("sales", "gone")
        dialog = manager.scope_dialog(ADMIN, kb.id)
        assert dialog.selected == frozenset({"sales", "gone"})
        assert dialog.vanished == ("gone",)
        assert dialog.name == "社内資料"

    def test_add_then_remove(self, manager, survey_world, logs):
        kb = survey_world.knowledge_base("sales")
        updated = manager.update_scope(
            ADMIN,
            kb.id,
            AssetScopeChange(add=frozenset({"plan"}), remove=frozenset({"sales"})),
        )
        assert updated.shared_group_ids == frozenset({"plan"})
        event = logs.events("asset.scope_changed")[0]
        assert event["resource_type"] == "knowledge_base"
        assert event["resource_id"] == kb.id
        assert event["actor_sub"] == "admin"

    def test_removing_all_is_admin_only_again(self, manager, survey_world):
        kb = survey_world.knowledge_base("sales")
        updated = manager.update_scope(
            ADMIN, kb.id, AssetScopeChange(remove=frozenset({"sales"}))
        )
        assert updated.shared_group_ids == frozenset()

    def test_unknown_department_is_refused(self, manager, survey_world):
        kb = survey_world.knowledge_base()
        with raises_code(CodedError, ErrorCode.SHARE_SCOPE_INVALID):
            manager.update_scope(ADMIN, kb.id, AssetScopeChange(add=frozenset({"x"})))

    def test_missing_kb_creates_nothing(self, manager, survey_world):
        with raises_code(CodedError, ErrorCode.SETTINGS_NOT_FOUND):
            manager.update_scope(
                ADMIN, "missing", AssetScopeChange(add=frozenset({"sales"}))
            )
        with raises_code(CodedError, ErrorCode.SETTINGS_NOT_FOUND):
            manager.scope_dialog(ADMIN, "missing")
        assert survey_world.raw(survey_world.kb_table, "missing") is None


@pytest.mark.unit
class TestSettingsManagerDataAgent:
    """データ分析エージェント接続テストのテスト。"""

    def setup_method(self):
        self.manager = SettingsManager(
            knowledge_base_store=Mock(spec=KnowledgeBaseStore), departments=Mock()
        )

    @patch("src.managers.settings_manager.config")
    def test_save_settings(self, mock_config):
        self.manager.save_data_agent_settings(ADMIN, "arn:x", "us-west-2", True)
        assert mock_config.DATA_AGENT_RUNTIME_ARN == "arn:x"
        assert mock_config.DATA_AGENT_REGION == "us-west-2"
        assert mock_config.ENABLE_DATA_AGENT is True

    @patch("src.managers.settings_manager.config")
    def test_no_runtime_arn_raises_error(self, mock_config):
        """Runtime ARN未設定時にエラーを返すこと"""
        mock_config.DATA_AGENT_RUNTIME_ARN = None

        with raises_code(SettingsManagerError, ErrorCode.DATA_AGENT_NOT_CONFIGURED):
            self.manager.test_data_agent_connection(ADMIN)

    @patch("src.managers.settings_manager.service_factory")
    @patch("src.managers.settings_manager.config")
    def test_service_returns_none_raises_error(self, mock_config, mock_sf):
        """サービスがNoneを返した場合エラーになること"""
        mock_config.DATA_AGENT_RUNTIME_ARN = (
            "arn:aws:bedrock:us-east-1:123:agent-runtime/xxx"
        )
        mock_sf.get_data_agent_service.return_value = None

        with raises_code(SettingsManagerError, ErrorCode.DATA_AGENT_CONNECTION_FAILED):
            self.manager.test_data_agent_connection(ADMIN)

    @patch("src.managers.settings_manager.service_factory")
    @patch("src.managers.settings_manager.config")
    def test_success(self, mock_config, mock_sf):
        """正常系でクエリ結果テキストを返すこと"""
        mock_config.DATA_AGENT_RUNTIME_ARN = (
            "arn:aws:bedrock:us-east-1:123:agent-runtime/xxx"
        )
        mock_data_agent = Mock()
        mock_result = Mock()
        mock_result.text = "テーブル一覧: users, orders"
        mock_data_agent.query.return_value = mock_result
        mock_sf.get_data_agent_service.return_value = mock_data_agent

        result = self.manager.test_data_agent_connection(ADMIN)

        assert result == "テーブル一覧: users, orders"
        mock_data_agent.query.assert_called_once_with(
            "利用可能なテーブル一覧を教えてください"
        )

    @patch("src.managers.settings_manager.service_factory")
    @patch("src.managers.settings_manager.config")
    def test_exception_wrapping(self, mock_config, mock_sf):
        """内部例外がSettingsManagerErrorにラップされること"""
        mock_config.DATA_AGENT_RUNTIME_ARN = (
            "arn:aws:bedrock:us-east-1:123:agent-runtime/xxx"
        )
        mock_data_agent = Mock()
        mock_data_agent.query.side_effect = RuntimeError("接続タイムアウト")
        mock_sf.get_data_agent_service.return_value = mock_data_agent

        with raises_code(
            SettingsManagerError, ErrorCode.DATA_AGENT_CONNECTION_FAILED
        ) as exc_info:
            self.manager.test_data_agent_connection(ADMIN)

        assert isinstance(exc_info.value.__cause__, RuntimeError)
