"""AdvancedGenerationPolicy: who may use the advanced generation.

Usable only when the environment allows it (``ENABLE_ADVANCED_GENERATION``),
a Code Interpreter is configured, the administrator opened it to the user
(administrators only while nothing is set) and the user has ``ai:execute``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from src.config import config
from src.managers.components.advanced_generation_policy import (
    AdvancedGenerationPolicy,
    Availability,
)
from src.models.authz import Department, Principal, Role
from src.models.errors import AccessDeniedError, CodedError, ErrorCode
from src.services.stores.app_settings_store import (
    ADVANCED_GENERATION_SETTING_KEY,
    AppSettingsStore,
    AppSettingsStoreError,
    SettingsVersionConflict,
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


# --- The administrator's setting ---------------------------------------------

NOW = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
DEPARTMENTS = [Department("sales", "営業部"), Department("hr", "人事部")]
NO_GROUP_MEMBER = Principal(sub="member-2", roles=frozenset({Role.MEMBER}))


def _stored(**overrides: object) -> dict:
    values: dict = {
        "setting_key": ADVANCED_GENERATION_SETTING_KEY,
        "schema_version": 1,
        "version": 3,
        "audience_mode": "departments",
        "department_groups": ["group:sales", "group:gone"],
        "allow_detailed_analysis": True,
        "updated_by": "admin-9",
        "updated_at": "2026-11-21T09:00:00+00:00",
    }
    values.update(overrides)
    return values


def _form(**overrides: object) -> dict:
    values: dict = {
        "audience_mode": "departments",
        "department_groups": ["group:sales"],
        "allow_detailed_analysis": True,
        "version": 3,
    }
    values.update(overrides)
    return values


class TestAvailabilityU6:
    @pytest.mark.parametrize(
        ("setting", "who", "usable", "detailed"),
        [
            (None, ADMIN, True, False),
            (_stored(allow_detailed_analysis=True), ADMIN, True, True),
            (_stored(audience_mode="everyone"), MEMBER, True, True),
            (_stored(allow_detailed_analysis=False), MEMBER, True, False),
            (_stored(), NO_GROUP_MEMBER, False, False),
            (_stored(audience_mode="admins_only"), MEMBER, False, False),
        ],
    )
    def test_one_read_tells_both(
        self, setting: dict | None, who: Principal, usable: bool, detailed: bool
    ) -> None:
        policy, store = _policy(setting)

        availability = policy.availability(who)

        assert (availability.usable, availability.detailed) == (usable, detailed)
        assert store.get.call_count == 1

    def test_unreadable_is_admins_only_without_detailed(self) -> None:
        policy, store = _policy(None)
        store.get.side_effect = AppSettingsStoreError("down")

        assert policy.availability(ADMIN) == Availability(usable=True, detailed=False)
        assert policy.availability(MEMBER) == Availability(usable=False, detailed=False)

    def test_in_process_never_shows_detailed(self) -> None:
        policy, _ = _policy(_stored(audience_mode="everyone"))

        assert policy.availability(ADMIN, in_process=True).detailed is False

    def test_is_available_uses_availability(self) -> None:
        policy, _ = _policy(_stored(audience_mode="everyone"))

        assert policy.is_available(MEMBER) is policy.availability(MEMBER).usable


class TestSectionView:
    def test_shows_the_values_the_version_and_missing_departments(self) -> None:
        policy, _ = _policy(_stored())

        view = policy.section_view(DEPARTMENTS)

        assert view.setting.version == 3
        assert view.setting.audience_mode == "departments"
        assert view.setting.department_groups == ("sales", "gone")
        assert view.setting.allow_detailed_analysis is True
        assert view.departments == tuple(DEPARTMENTS)
        assert view.missing_groups == ("gone",)

    def test_nothing_stored_is_admins_only_off_version_0(self) -> None:
        policy, _ = _policy(None)

        setting = policy.section_view(DEPARTMENTS).setting

        assert setting.audience_mode == "admins_only"
        assert setting.allow_detailed_analysis is False
        assert setting.version == 0 and setting.updated_by is None


class TestSaveSetting:
    def _save(self, policy: AdvancedGenerationPolicy, **form: object):
        return policy.save_setting(
            ADMIN, _form(**form), DEPARTMENTS, now=NOW, error_type=_CallerError
        )

    def test_writes_on_the_version_read(self) -> None:
        policy, store = _policy(_stored())
        store.put_if_version.return_value = 4

        view = self._save(policy)

        key, item, expected = store.put_if_version.call_args.args
        assert key == ADVANCED_GENERATION_SETTING_KEY and expected == 3
        assert item["audience_mode"] == "departments"
        assert item["department_groups"] == ["group:sales"]
        assert item["allow_detailed_analysis"] is True
        assert item["updated_by"] == ADMIN.sub
        assert item["updated_at"] == NOW.isoformat()
        assert view.setting.version == 4 and view.notice == "saved"

    def test_departments_without_one_is_admins_only(self) -> None:
        policy, store = _policy(_stored())
        store.put_if_version.return_value = 4

        view = self._save(policy, department_groups=[])

        item = store.put_if_version.call_args.args[1]
        assert item["audience_mode"] == "admins_only"
        assert "department_groups" not in item
        assert view.setting.audience_mode == "admins_only"

    def test_other_modes_keep_no_departments(self) -> None:
        policy, store = _policy(_stored())
        store.put_if_version.return_value = 4

        self._save(policy, audience_mode="everyone")

        assert "department_groups" not in store.put_if_version.call_args.args[1]

    def test_departments_are_kept_once(self) -> None:
        policy, store = _policy(_stored())
        store.put_if_version.return_value = 4

        self._save(policy, department_groups=["group:sales", "group:sales", "group:hr"])

        groups = store.put_if_version.call_args.args[1]["department_groups"]
        assert groups == ["group:sales", "group:hr"]

    @pytest.mark.parametrize(
        "form",
        [
            {"audience_mode": "nobody"},
            {"department_groups": ["group:gone"]},
            {"department_groups": [f"group:d{i}" for i in range(101)]},
            {"version": "x"},
            {"version": -1},
        ],
    )
    def test_bad_input_is_refused_without_a_write(self, form: dict) -> None:
        many = [Department(f"d{i}", f"部 {i}") for i in range(101)]
        policy, store = _policy(_stored())

        with raises_code(_CallerError, ErrorCode.REQUEST_INVALID):
            policy.save_setting(
                ADMIN,
                _form(**form),
                [*DEPARTMENTS, *many],
                now=NOW,
                error_type=_CallerError,
            )
        store.put_if_version.assert_not_called()

    def test_a_vanished_department_tells_the_cause_and_the_count(self) -> None:
        """The screen asks to take off the departments that are gone."""
        policy, store = _policy(_stored())

        with raises_code(
            _CallerError,
            ErrorCode.REQUEST_INVALID,
            field="department_groups",
            reason="vanished_departments",
            count=2,
        ) as raised:
            self._save(
                policy, department_groups=["group:sales", "group:gone", "group:old"]
            )
        # no group id reaches the context (only the count is shown)
        assert "gone" not in repr(raised.value.context)
        store.put_if_version.assert_not_called()

    def test_too_many_departments_is_not_called_vanished(self) -> None:
        many = [Department(f"d{i}", f"部 {i}") for i in range(101)]
        policy, _ = _policy(_stored())

        with raises_code(_CallerError, ErrorCode.REQUEST_INVALID) as raised:
            policy.save_setting(
                ADMIN,
                _form(department_groups=[f"group:d{i}" for i in range(101)]),
                many,
                now=NOW,
                error_type=_CallerError,
            )
        assert "reason" not in raised.value.context

    def test_a_lost_version_shows_the_latest_values(self) -> None:
        policy, store = _policy(_stored(version=5, audience_mode="everyone"))
        store.put_if_version.side_effect = SettingsVersionConflict("lost")

        with raises_code(_CallerError, ErrorCode.SETTINGS_VERSION_CONFLICT) as caught:
            self._save(policy)

        latest = caught.value.context["section"]
        assert latest.setting.version == 5
        assert latest.setting.audience_mode == "everyone"
        assert latest.notice == "conflict"
