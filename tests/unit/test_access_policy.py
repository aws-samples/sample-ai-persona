"""AccessPolicy（判定、約束 C2）の関数ごとの単体テスト。

BR2.1〜BR2.7、BR2.13、NFR1.4、NFR8.6、NFR9.13（外部を呼ばない）。全体の表は
`test_access_policy_matrix.py` が持つ。
"""

import json

import pytest

from src.managers.components.access_policy import AccessPolicy
from src.models.authz import AccessTarget, Permission, Principal, ResourceType, Role
from src.models.errors import AccessDeniedError, CodedError, ErrorCode
from tests.error_helpers import raises_code

MEMBER = Principal(
    sub="user-a", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"sales"})
)
OTHER = Principal(
    sub="user-b", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"dev"})
)
ADMIN = Principal(sub="admin", roles=frozenset({Role.ADMIN}))


def _persona(**overrides: object) -> AccessTarget:
    values: dict = {
        "resource_type": ResourceType.PERSONA,
        "resource_id": "p-secret-1",
        "owner_sub": "user-a",
    }
    values.update(overrides)
    return AccessTarget(**values)


def _denied_lines(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    return [line for line in lines if line.get("event") == "authz.denied"]


class TestRequire:
    def test_passes_with_the_permission(self):
        AccessPolicy.require(MEMBER, Permission.AI_EXECUTE)

    def test_missing_permission_is_403(self):
        with raises_code(AccessDeniedError, ErrorCode.ACCESS_DENIED):
            AccessPolicy.require(MEMBER, Permission.SETTINGS_MANAGE)

    def test_admin_only_operation_hides_itself_as_404(self):
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            AccessPolicy.require(
                MEMBER,
                Permission.RESOURCE_READ_ALL,
                hidden_as=ErrorCode.PERSONA_NOT_FOUND,
            )

    def test_empty_sub_is_denied_even_with_a_role(self):
        with raises_code(AccessDeniedError, ErrorCode.ACCESS_DENIED):
            AccessPolicy.require(
                Principal(sub="", roles=frozenset({Role.ADMIN})), Permission.AI_EXECUTE
            )


class TestCanRead:
    def test_owner_reads(self):
        assert AccessPolicy.can_read(MEMBER, _persona())

    def test_shared_department_reads(self):
        target = _persona(owner_sub="user-b", shared_group_ids=frozenset({"sales"}))
        assert AccessPolicy.can_read(MEMBER, target)

    def test_unrelated_user_does_not_read(self):
        assert not AccessPolicy.can_read(OTHER, _persona())

    def test_read_all_reads_anything(self):
        assert AccessPolicy.can_read(ADMIN, _persona(owner_sub=None))

    def test_deleted_is_never_readable(self):
        assert not AccessPolicy.can_read(ADMIN, _persona(exists=False))
        assert not AccessPolicy.can_read(MEMBER, _persona(exists=False))


class TestCanManage:
    def test_owner_manages(self):
        assert AccessPolicy.can_manage(MEMBER, _persona())

    def test_shared_department_does_not_manage(self):
        target = _persona(owner_sub="user-b", shared_group_ids=frozenset({"sales"}))
        assert not AccessPolicy.can_manage(MEMBER, target)

    def test_ownerless_is_managed_only_with_manage_all(self):
        target = _persona(owner_sub=None, shared_group_ids=frozenset({"sales"}))
        assert not AccessPolicy.can_manage(MEMBER, target)
        assert AccessPolicy.can_manage(ADMIN, target)

    @pytest.mark.parametrize("owner", [None, ""])
    def test_empty_owner_never_matches_an_empty_sub(self, owner):
        nobody = Principal(sub="", roles=frozenset({Role.MEMBER}))
        assert not AccessPolicy.can_manage(nobody, _persona(owner_sub=owner))
        assert not AccessPolicy.can_read(nobody, _persona(owner_sub=owner))


class TestShareableGroups:
    def test_member_can_share_with_own_departments_only(self):
        principal = Principal(
            sub="user-a",
            roles=frozenset({Role.MEMBER}),
            group_ids=frozenset({"sales", "gone"}),
        )
        all_groups = frozenset({"sales", "dev", "hr"})
        assert AccessPolicy.shareable_groups(principal, all_groups) == {"sales"}

    def test_any_group_permission_shares_with_all(self):
        all_groups = frozenset({"sales", "dev"})
        assert AccessPolicy.shareable_groups(ADMIN, all_groups) == all_groups


class TestCanUseAsset:
    def _kb(self, groups: set[str], **overrides: object) -> AccessTarget:
        values: dict = {
            "resource_type": ResourceType.KNOWLEDGE_BASE,
            "resource_id": "kb-1",
            "owner_sub": None,
            "shared_group_ids": frozenset(groups),
        }
        values.update(overrides)
        return AccessTarget(**values)

    def test_in_scope_department_uses(self):
        assert AccessPolicy.can_use_asset(MEMBER, self._kb({"sales"}))

    def test_out_of_scope_department_does_not(self):
        assert not AccessPolicy.can_use_asset(MEMBER, self._kb({"hr"}))

    def test_owner_is_not_looked_at(self):
        assert not AccessPolicy.can_use_asset(
            MEMBER, self._kb({"hr"}, owner_sub="user-a")
        )

    def test_settings_manager_uses_any(self):
        assert AccessPolicy.can_use_asset(ADMIN, self._kb(set()))


class TestEnsure:
    def test_readable_passes(self):
        AccessPolicy.ensure_readable(MEMBER, _persona(), ErrorCode.PERSONA_NOT_FOUND)

    def test_unreadable_raises_the_given_not_found(self):
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            AccessPolicy.ensure_readable(
                OTHER,
                _persona(resource_type=ResourceType.DISCUSSION),
                ErrorCode.DISCUSSION_NOT_FOUND,
            )

    def test_unreadable_looks_like_missing(self):
        """読めないと存在しないで、同じコード・同じ型（NFR1.4）。"""
        errors = []
        for target in (_persona(), _persona(exists=False)):
            with pytest.raises(CodedError) as info:
                AccessPolicy.ensure_readable(OTHER, target, ErrorCode.PERSONA_NOT_FOUND)
            errors.append(info.value)
        assert type(errors[0]) is type(errors[1])
        assert errors[0].code is errors[1].code is ErrorCode.PERSONA_NOT_FOUND

    def test_manageable_checks_reading_first(self):
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            AccessPolicy.ensure_manageable(
                OTHER, _persona(), ErrorCode.PERSONA_NOT_FOUND
            )

    def test_readable_but_not_manageable_is_403(self):
        target = _persona(owner_sub="user-b", shared_group_ids=frozenset({"sales"}))
        with raises_code(AccessDeniedError, ErrorCode.ACCESS_DENIED):
            AccessPolicy.ensure_manageable(MEMBER, target, ErrorCode.PERSONA_NOT_FOUND)

    def test_owner_manages(self):
        AccessPolicy.ensure_manageable(MEMBER, _persona(), ErrorCode.PERSONA_NOT_FOUND)


class TestDeniedLog:
    """authz.denied を理由ごとに 1 行出し、ID を含めない（BR2.13、NFR8.6）。"""

    def test_not_found(self, capsys):
        with pytest.raises(CodedError):
            AccessPolicy.ensure_readable(OTHER, _persona(), ErrorCode.PERSONA_NOT_FOUND)
        (line,) = _denied_lines(capsys)
        assert line["reason"] == "not_found"
        assert line["resource_type"] == "persona"
        assert line["sub"] == "user-b"

    def test_denied_on_manage(self, capsys):
        target = _persona(owner_sub="user-b", shared_group_ids=frozenset({"sales"}))
        with pytest.raises(AccessDeniedError):
            AccessPolicy.ensure_manageable(MEMBER, target, ErrorCode.PERSONA_NOT_FOUND)
        (line,) = _denied_lines(capsys)
        assert line["reason"] == "denied"

    def test_denied_on_permission(self, capsys):
        with pytest.raises(AccessDeniedError):
            AccessPolicy.require(MEMBER, Permission.SETTINGS_MANAGE)
        (line,) = _denied_lines(capsys)
        assert line["reason"] == "denied"
        assert line["sub"] == "user-a"

    def test_hidden(self, capsys):
        with pytest.raises(CodedError):
            AccessPolicy.require(
                MEMBER,
                Permission.SETTINGS_MANAGE,
                hidden_as=ErrorCode.PERSONA_NOT_FOUND,
            )
        (line,) = _denied_lines(capsys)
        assert line["reason"] == "hidden"

    def test_no_id_in_the_log(self, capsys):
        with pytest.raises(CodedError):
            AccessPolicy.ensure_readable(OTHER, _persona(), ErrorCode.PERSONA_NOT_FOUND)
        assert "p-secret-1" not in capsys.readouterr().out

    def test_allowed_calls_log_nothing(self, capsys):
        AccessPolicy.ensure_readable(MEMBER, _persona(), ErrorCode.PERSONA_NOT_FOUND)
        AccessPolicy.require(MEMBER, Permission.AI_EXECUTE)
        assert _denied_lines(capsys) == []
