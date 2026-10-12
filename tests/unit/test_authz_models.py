"""Principal / Role（約束 C1）の単体テスト。"""

import dataclasses

import pytest

from src.models.authz import Principal, Role


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
