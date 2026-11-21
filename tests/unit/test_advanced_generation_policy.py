"""AdvancedGenerationPolicy: who may use the advanced generation (BR1.1〜BR1.3).

Usable only when the environment allows it (``ENABLE_ADVANCED_GENERATION``),
a Code Interpreter is configured, the administrator opened it to the user
(administrators only while nothing is set) and the user has ``ai:execute``.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from src.config import config
from src.managers.components.advanced_generation_policy import (
    AdvancedGenerationPolicy,
)
from src.models.authz import Principal, Role
from src.models.errors import AccessDeniedError, CodedError, ErrorCode
from src.services.stores.app_settings_store import (
    ADVANCED_GENERATION_SETTING_KEY,
    AppSettingsStore,
    AppSettingsStoreError,
)
from tests.error_helpers import raises_code

pytestmark = pytest.mark.unit

ADMIN = Principal(sub="admin-1", roles=frozenset({Role.ADMIN}))
MEMBER = Principal(
    sub="member-1", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"sales"})
)
NO_ROLE = Principal(sub="nobody")


class _CallerError(CodedError):
    """Stands for the calling Manager's exception type."""


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ENABLE_ADVANCED_GENERATION", True)
    monkeypatch.setattr(config, "CODE_INTERPRETER_ID", "ci-123")


def _policy(setting: dict | None = None) -> tuple[AdvancedGenerationPolicy, Mock]:
    store = Mock(spec=AppSettingsStore)
    store.get.return_value = setting
    return AdvancedGenerationPolicy(store), store


class TestAvailability:
    def test_admin_may_use_it_while_nothing_is_set(self) -> None:
        policy, _ = _policy(None)

        assert policy.is_available(ADMIN) is True

    def test_member_may_not_while_nothing_is_set(self) -> None:
        policy, store = _policy(None)

        assert policy.is_available(MEMBER) is False
        store.get.assert_called_with(ADVANCED_GENERATION_SETTING_KEY)

    def test_everyone_opens_it_to_members(self) -> None:
        policy, _ = _policy({"audience_mode": "everyone"})

        assert policy.is_available(MEMBER) is True

    def test_departments_open_it_to_their_members_only(self) -> None:
        policy, _ = _policy(
            {"audience_mode": "departments", "department_groups": ["group:sales"]}
        )
        other = Principal(
            sub="m2", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"hr"})
        )

        assert policy.is_available(MEMBER) is True
        assert policy.is_available(other) is False

    def test_disabled_environment_closes_it_even_to_admins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "ENABLE_ADVANCED_GENERATION", False)
        policy, store = _policy({"audience_mode": "everyone"})

        assert policy.is_available(ADMIN) is False
        store.get.assert_not_called()

    def test_no_code_interpreter_closes_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "CODE_INTERPRETER_ID", "")
        policy, _ = _policy({"audience_mode": "everyone"})

        assert policy.is_available(ADMIN) is False

    def test_no_ai_execute_closes_it(self) -> None:
        policy, _ = _policy({"audience_mode": "everyone"})

        assert policy.is_available(NO_ROLE) is False

    def test_unreadable_setting_falls_back_to_admins_only(self) -> None:
        policy, store = _policy(None)
        store.get.side_effect = AppSettingsStoreError("down")

        assert policy.is_available(ADMIN) is True
        assert policy.is_available(MEMBER) is False


class TestEnsureAvailable:
    def test_refuses_with_the_callers_type(self) -> None:
        policy, _ = _policy(None)

        with raises_code(_CallerError, ErrorCode.ADVANCED_GENERATION_UNAVAILABLE):
            policy.ensure_available(MEMBER, _CallerError)

    def test_missing_ai_execute_is_the_existing_forbidden(self) -> None:
        policy, _ = _policy({"audience_mode": "everyone"})

        with pytest.raises(AccessDeniedError):
            policy.ensure_available(NO_ROLE, _CallerError)

    def test_passes_for_an_admin(self) -> None:
        policy, _ = _policy(None)

        policy.ensure_available(ADMIN, _CallerError)

    def test_upload_limit_comes_from_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_UPLOAD_MAX_BYTES", 123)
        policy, _ = _policy(None)

        assert policy.upload_limit_bytes() == 123
