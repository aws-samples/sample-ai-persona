"""Principal / Role / Permission / AccessTarget / Department（約束 C1）の単体テスト。"""

import dataclasses

import pytest

from src.models.authz import (
    ROLE_PERMISSIONS,
    AccessTarget,
    Department,
    Permission,
    Principal,
    ResourceType,
    Role,
    permissions_of,
)


class TestRole:
    def test_values_are_admin_and_member(self):
        assert {role.value for role in Role} == {"admin", "member"}

    def test_unknown_value_is_rejected(self):
        with pytest.raises(ValueError):
            Role("owner")


class TestPrincipal:
    def test_cannot_be_changed(self):
        principal = Principal(sub="user-1", roles=frozenset({Role.MEMBER}))
        with pytest.raises(dataclasses.FrozenInstanceError):
            principal.sub = "user-2"  # type: ignore[misc]

    def test_roles_and_groups_default_to_empty_sets(self):
        principal = Principal(sub="user-1")
        assert principal.roles == frozenset()
        assert principal.group_ids == frozenset()
        assert principal.display_name is None
        assert principal.email is None

    def test_equal_principals_are_interchangeable(self):
        first = Principal(
            sub="user-1",
            roles=frozenset({Role.ADMIN}),
            group_ids=frozenset({"sales"}),
            display_name="山田",
        )
        second = Principal(
            sub="user-1",
            roles=frozenset({Role.ADMIN}),
            group_ids=frozenset({"sales"}),
            display_name="山田",
        )
        assert first == second
        assert len({first, second}) == 1


class TestPermission:
    """ロールから権限への対応表（BR2.1、約束 C1）。"""

    def test_six_permission_values(self):
        assert {p.value for p in Permission} == {
            "ai:execute",
            "resource:create",
            "resource:read_all",
            "resource:manage_all",
            "share:any_group",
            "settings:manage",
        }

    def test_admin_has_every_permission(self):
        assert permissions_of({Role.ADMIN}) == frozenset(Permission)

    def test_member_can_execute_ai_and_create(self):
        assert permissions_of({Role.MEMBER}) == frozenset(
            {Permission.AI_EXECUTE, Permission.RESOURCE_CREATE}
        )

    def test_several_roles_are_the_union(self):
        assert permissions_of({Role.ADMIN, Role.MEMBER}) == frozenset(Permission)

    def test_no_role_means_no_permission(self):
        assert permissions_of(frozenset()) == frozenset()

    def test_unknown_role_names_add_nothing(self):
        """コードにない role: のグループだけなら、権限は空（BR2.9、NFR1.7）。"""
        assert permissions_of({"owner"}) == frozenset()  # type: ignore[arg-type]

    def test_the_table_lists_only_known_roles(self):
        assert set(ROLE_PERMISSIONS) == set(Role)

    def test_the_table_cannot_be_changed_at_run_time(self):
        with pytest.raises(TypeError):
            ROLE_PERMISSIONS[Role.MEMBER] = frozenset(Permission)  # type: ignore[index]


class TestAccessTarget:
    """判定の対象（約束 C1、C3）。判定に要る項目だけを持つ。"""

    def test_cannot_be_changed(self):
        target = AccessTarget(ResourceType.PERSONA, "p-1", owner_sub="user-1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            target.owner_sub = "user-2"  # type: ignore[misc]

    def test_defaults(self):
        target = AccessTarget(ResourceType.PERSONA, "p-1")
        assert target.owner_sub is None
        assert target.shared_group_ids == frozenset()
        assert target.display_name is None
        assert target.exists is True

    def test_missing_target_does_not_exist(self):
        target = AccessTarget.missing(ResourceType.DISCUSSION, "d-1")
        assert target.exists is False
        assert target.resource_type is ResourceType.DISCUSSION
        assert target.resource_id == "d-1"
        assert target.owner_sub is None

    def test_has_only_the_decision_fields(self):
        names = {f.name for f in dataclasses.fields(AccessTarget)}
        assert names == {
            "resource_type",
            "resource_id",
            "owner_sub",
            "shared_group_ids",
            "display_name",
            "exists",
        }

    def test_resource_types(self):
        assert {t.value for t in ResourceType} == {
            "persona",
            "discussion",
            "survey_template",
            "survey",
            "knowledge_base",
            "dataset",
            "custom_persona_data",
        }


class TestDepartment:
    def test_cannot_be_changed(self):
        department = Department(group_id="sales", display_name="営業部")
        with pytest.raises(dataclasses.FrozenInstanceError):
            department.display_name = "開発部"  # type: ignore[misc]
