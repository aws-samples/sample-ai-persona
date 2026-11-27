"""Unit tests for SettingsManager.

KB and data analysis agent settings need settings:manage (404 otherwise).
DynamoDB is moto (tests/unit/survey_world.py).
"""

import json
from datetime import datetime

import pytest
from unittest.mock import Mock, patch

from src.config import config
from src.managers.settings_manager import SettingsManager, SettingsManagerError
from src.models.authz import Department
from src.services.stores.app_settings_store import (
    AppSettingsStore,
    AppSettingsStoreError,
    SettingsVersionConflict,
)
from src.models.errors import CodedError, ErrorCode
from src.models.knowledge_base import KB_TYPE_MANAGED, KB_TYPE_SELF_MANAGED
from src.models.sharing import AssetScopeChange
from src.models.persona_binding import PersonaBinding
from src.services.stores.persona_binding_store import PersonaBindingStoreError
from tests.error_helpers import raises_code
from src.services.stores.knowledge_base_store import KnowledgeBaseStore
from tests.log_helpers import LogCapture
from tests.unit.survey_world import ADMIN, MEMBER_A, SurveyWorld

NOW = datetime(2026, 10, 25, 0, 0, 0)


@pytest.fixture
def manager(survey_world: SurveyWorld) -> SettingsManager:
    return SettingsManager(
        knowledge_base_store=survey_world.kb_store,
        departments=survey_world.departments,
        binding_store=survey_world.binding_store,
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
        # the decision is DataAgentPolicy's (it reads the config itself)
        with patch("src.managers.components.data_agent_policy.config") as config:
            config.DATA_AGENT_ALLOWED = True
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
            "data_agent_allowed": True,
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

    def test_create_defaults_to_a_self_managed_kb(self, manager):
        kb = manager.create_knowledge_base(ADMIN, "KB1", "n")
        assert kb.kb_type == KB_TYPE_SELF_MANAGED

    def test_create_a_managed_kb(self, manager):
        kb = manager.create_knowledge_base(ADMIN, "KB1", "n", kb_type="managed")
        assert manager.get_all_knowledge_bases(ADMIN).items[0].kb_type == (
            KB_TYPE_MANAGED
        )
        assert kb.kb_type == KB_TYPE_MANAGED

    def test_unknown_kb_type_is_refused_and_creates_nothing(self, manager):
        with raises_code(SettingsManagerError, ErrorCode.REQUEST_INVALID):
            manager.create_knowledge_base(ADMIN, "KB1", "n", kb_type="kendra")
        assert manager.get_all_knowledge_bases(ADMIN).items == []

    def test_delete(self, manager, survey_world):
        kb = survey_world.knowledge_base()
        manager.delete_knowledge_base(ADMIN, kb.id)
        assert manager.get_all_knowledge_bases(ADMIN).items == []

    def test_delete_removes_the_bindings_to_the_kb(self, manager, survey_world):
        kb = survey_world.knowledge_base()
        bindings = survey_world.binding_store
        bindings.put(PersonaBinding.for_kb("p1", kb.id, now=NOW))
        bindings.put(PersonaBinding.for_kb("p2", "other", now=NOW))

        manager.delete_knowledge_base(ADMIN, kb.id)

        assert bindings.list_for_persona("p1") == []
        assert [b.target_id for b in bindings.list_for_persona("p2")] == ["other"]

    def test_delete_keeps_dataset_bindings_with_the_same_id(
        self, manager, survey_world
    ):
        """A different kind of ID for a KB delete does not remove dataset bindings."""
        bindings = survey_world.binding_store
        bindings.put(PersonaBinding.for_dataset("p1", "x", now=NOW))

        manager.delete_knowledge_base(ADMIN, "x")

        assert [b.target_id for b in bindings.list_for_persona("p1")] == ["x"]

    def test_delete_succeeds_when_the_bindings_cannot_be_removed(
        self, manager, survey_world, monkeypatch
    ):
        """A leftover binding is skipped on read as a binding without a body."""
        kb = survey_world.knowledge_base()
        monkeypatch.setattr(
            survey_world.binding_store,
            "delete_for_target",
            Mock(side_effect=PersonaBindingStoreError("down")),
        )
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
    """Tests for the data analysis agent connection test."""

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
        """Returns an error when the Runtime ARN is not set."""
        mock_config.DATA_AGENT_RUNTIME_ARN = None

        with raises_code(SettingsManagerError, ErrorCode.DATA_AGENT_NOT_CONFIGURED):
            self.manager.test_data_agent_connection(ADMIN)

    @patch("src.managers.settings_manager.service_factory")
    @patch("src.managers.settings_manager.config")
    def test_service_returns_none_raises_error(self, mock_config, mock_sf):
        """Errors when the service returns None."""
        mock_config.DATA_AGENT_RUNTIME_ARN = (
            "arn:aws:bedrock:us-east-1:123:agent-runtime/xxx"
        )
        mock_sf.get_data_agent_service.return_value = None

        with raises_code(SettingsManagerError, ErrorCode.DATA_AGENT_CONNECTION_FAILED):
            self.manager.test_data_agent_connection(ADMIN)

    @patch("src.managers.settings_manager.service_factory")
    @patch("src.managers.settings_manager.config")
    def test_success(self, mock_config, mock_sf):
        """Returns the query result text on success."""
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
        """Internal exceptions are wrapped in SettingsManagerError."""
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


# =============================================================================
# Closed network: environments where the Data Agent must not be used
# =============================================================================


@pytest.fixture
def agent_config(monkeypatch):
    """The real config with a configured agent; tests set DATA_AGENT_ALLOWED."""
    from src.config import config

    monkeypatch.setattr(config, "ENABLE_DATA_AGENT", True)
    monkeypatch.setattr(config, "DATA_AGENT_RUNTIME_ARN", "arn:agent")
    monkeypatch.setattr(config, "DATA_AGENT_REGION", "ap-northeast-1")
    monkeypatch.setattr(config, "DATA_AGENT_ALLOWED", True)
    return config


@pytest.mark.unit
class TestDataAgentNotAllowed:
    def test_save_stops_without_changing_the_config(self, manager, agent_config):
        agent_config.DATA_AGENT_ALLOWED = False
        with raises_code(SettingsManagerError, ErrorCode.DATA_AGENT_NOT_ALLOWED):
            manager.save_data_agent_settings(ADMIN, "arn:new", "us-west-2", False)
        assert agent_config.DATA_AGENT_RUNTIME_ARN == "arn:agent"
        assert agent_config.DATA_AGENT_REGION == "ap-northeast-1"
        assert agent_config.ENABLE_DATA_AGENT is True
        assert agent_config.DATA_AGENT_ALLOWED is False

    def test_connection_test_stops_without_connecting(self, manager, agent_config):
        agent_config.DATA_AGENT_ALLOWED = False
        with patch("src.managers.settings_manager.service_factory") as factory:
            with raises_code(SettingsManagerError, ErrorCode.DATA_AGENT_NOT_ALLOWED):
                manager.test_data_agent_connection(ADMIN)
        factory.get_data_agent_service.assert_not_called()

    def test_settings_hide_the_arn_and_region(self, manager, agent_config):
        agent_config.DATA_AGENT_ALLOWED = False
        assert manager.get_data_agent_settings(ADMIN) == {
            "data_agent_allowed": False,
            "data_agent_enabled": False,
        }

    def test_settings_stay_admin_only_when_not_allowed(self, manager, agent_config):
        agent_config.DATA_AGENT_ALLOWED = False
        with raises_code(CodedError, ErrorCode.SETTINGS_NOT_FOUND):
            manager.get_data_agent_settings(MEMBER_A)

    def test_allowed_needs_no_permission(self, agent_config):
        assert SettingsManager.is_data_agent_allowed() is True
        agent_config.DATA_AGENT_ALLOWED = False
        assert SettingsManager.is_data_agent_allowed() is False

    def test_not_allowed_is_never_available(self, agent_config):
        assert SettingsManager.is_data_agent_available() is True
        agent_config.DATA_AGENT_ALLOWED = False
        assert SettingsManager.is_data_agent_available() is False

    def test_allowed_environment_keeps_the_current_behaviour(
        self, manager, agent_config
    ):
        manager.save_data_agent_settings(ADMIN, "arn:new", "us-west-2", True)
        assert agent_config.DATA_AGENT_RUNTIME_ARN == "arn:new"
        assert manager.get_data_agent_settings(ADMIN) == {
            "data_agent_allowed": True,
            "data_agent_enabled": True,
            "data_agent_runtime_arn": "arn:new",
            "data_agent_region": "us-west-2",
        }


# --- The advanced generation section ---

_STORED = {
    "setting_key": "advanced_generation",
    "version": 2,
    "audience_mode": "departments",
    "department_groups": ["group:sales"],
    "allow_detailed_analysis": False,
    "updated_by": "admin-9",
    "updated_at": "2026-10-24T09:00:00+00:00",
}


@pytest.fixture
def section():
    settings = Mock(spec=AppSettingsStore)
    settings.get.return_value = dict(_STORED)
    settings.put_if_version.return_value = 3
    departments = Mock()
    departments.list_all.return_value = [
        Department("sales", "営業部"),
        Department("hr", "人事部"),
    ]
    owners = Mock()
    owners.label.return_value = "管理 太郎"
    manager = SettingsManager(
        knowledge_base_store=Mock(),
        departments=departments,
        binding_store=Mock(),
        settings_store=settings,
        owners=owners,
        clock=lambda: NOW,
    )
    return manager, settings, departments


def _form(**overrides):
    values = {
        "audience_mode": "departments",
        "department_groups": ["group:hr"],
        "allow_detailed_analysis": True,
        "version": 2,
    }
    values.update(overrides)
    return values


class TestAdvancedGenerationSection:
    def test_admin_reads_the_section(self, section):
        manager, _, _ = section

        view = manager.advanced_generation_section(ADMIN)

        assert view.setting.version == 2
        assert view.setting.department_groups == ("sales",)
        assert view.updated_by_name == "管理 太郎"

    def test_member_gets_404_and_nothing_is_read(self, section):
        manager, settings, departments = section

        with raises_code(CodedError, ErrorCode.SETTINGS_NOT_FOUND):
            manager.advanced_generation_section(MEMBER_A)
        settings.get.assert_not_called()
        departments.list_all.assert_not_called()

    def test_member_save_is_forbidden_and_nothing_is_read_or_written(self, section):
        manager, settings, departments = section

        with raises_code(CodedError, ErrorCode.ACCESS_DENIED):
            manager.save_advanced_generation_setting(MEMBER_A, _form())
        settings.get.assert_not_called()
        settings.put_if_version.assert_not_called()
        departments.list_all.assert_not_called()

    def test_a_save_is_logged_without_department_names(self, section, logs):
        manager, settings, _ = section

        view = manager.save_advanced_generation_setting(ADMIN, _form())

        assert view.setting.version == 3 and view.notice == "saved"
        (event,) = logs.events("settings.advanced_generation_changed")
        assert event["actor_sub"] == ADMIN.sub
        assert event["version"] == 3
        assert event["audience_mode"] == "departments"
        assert event["department_count"] == 1
        assert event["allow_detailed_analysis"] is True
        assert "hr" not in json.dumps(event) and "人事" not in json.dumps(event)

    def test_a_lost_version_is_logged_and_raised(self, section, logs):
        manager, settings, _ = section
        settings.put_if_version.side_effect = SettingsVersionConflict("lost")

        with raises_code(SettingsManagerError, ErrorCode.SETTINGS_VERSION_CONFLICT):
            manager.save_advanced_generation_setting(ADMIN, _form())

        (event,) = logs.events("settings.advanced_generation_conflict")
        assert event == {**event, "actor_sub": ADMIN.sub, "sent_version": 2}
        assert logs.events("settings.advanced_generation_changed") == []

    @pytest.mark.parametrize("enabled", [True, False])
    def test_the_section_is_offered_only_while_the_feature_is_on(
        self, section, monkeypatch, enabled
    ):
        manager, _, _ = section
        monkeypatch.setattr(config, "ENABLE_ADVANCED_GENERATION", enabled)

        assert manager.is_advanced_generation_section_offered() is enabled

    def test_the_feature_off_hides_the_section_and_its_save(self, section, monkeypatch):
        """The closed network Stage: the same 404 as a missing page."""
        manager, settings, departments = section
        monkeypatch.setattr(config, "ENABLE_ADVANCED_GENERATION", False)

        with raises_code(SettingsManagerError, ErrorCode.SETTINGS_NOT_FOUND):
            manager.advanced_generation_section(ADMIN)
        with raises_code(SettingsManagerError, ErrorCode.SETTINGS_NOT_FOUND):
            manager.save_advanced_generation_setting(ADMIN, _form())
        settings.get.assert_not_called()
        settings.put_if_version.assert_not_called()
        departments.list_all.assert_not_called()

    def test_the_feature_off_keeps_the_members_save_forbidden(
        self, section, monkeypatch
    ):
        manager, settings, _ = section
        monkeypatch.setattr(config, "ENABLE_ADVANCED_GENERATION", False)

        with raises_code(CodedError, ErrorCode.ACCESS_DENIED):
            manager.save_advanced_generation_setting(MEMBER_A, _form())
        settings.put_if_version.assert_not_called()

    def test_a_vanished_department_tells_the_cause(self, section):
        manager, settings, _ = section

        with raises_code(
            SettingsManagerError,
            ErrorCode.REQUEST_INVALID,
            reason="vanished_departments",
            count=1,
        ):
            manager.save_advanced_generation_setting(
                ADMIN, _form(department_groups=["group:sales", "group:gone"])
            )
        settings.put_if_version.assert_not_called()

    def test_bad_input_is_the_managers_validation_error(self, section):
        manager, settings, _ = section

        with raises_code(SettingsManagerError, ErrorCode.REQUEST_INVALID):
            manager.save_advanced_generation_setting(
                ADMIN, _form(department_groups=["group:gone"])
            )
        settings.put_if_version.assert_not_called()

    def test_an_unreadable_setting_is_the_managers_error(self, section):
        manager, settings, _ = section
        settings.get.side_effect = AppSettingsStoreError("down")

        with raises_code(SettingsManagerError, ErrorCode.DATABASE_OPERATION_FAILED):
            manager.advanced_generation_section(ADMIN)
