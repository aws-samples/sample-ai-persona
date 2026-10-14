"""共有の型（src/models/sharing.py、約束 C7）と Persona の所有・共有の列（BR6.7、約束 C8）のテスト。

entities.md の ListTab・ShareChange・ListPage・BulkShareResult・BulkShareFailure・
SharingTable・OwnerSummary を確かめる。
"""

from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest

from src.models.authz import AccessTarget, ResourceType
from src.models.errors import ErrorCode
from src.models.persona import Persona
from src.models.sharing import (
    BulkShareFailure,
    BulkShareResult,
    ListPage,
    ListTab,
    OwnerSummary,
    ShareChange,
    SharingTable,
    parse_share_key,
    share_key,
)

PERSONA = ResourceType.PERSONA


def _persona(**overrides: object) -> Persona:
    base = Persona.create_new(
        name="佐藤 太郎",
        age=35,
        occupation="会社員",
        background="東京で働いている",
        values=["効率"],
        pain_points=["時間"],
        goals=["成長"],
    )
    data = base.to_dict()
    data.update(overrides)
    return Persona.from_dict(data)


class TestListTab:
    def test_three_values(self):
        assert [t.value for t in ListTab] == ["mine", "shared", "all"]

    def test_parse_unknown_is_none(self):
        assert ListTab.parse("shared") is ListTab.SHARED
        assert ListTab.parse("everything") is None
        assert ListTab.parse(None) is None


class TestShareChange:
    def test_defaults_are_empty_frozensets(self):
        change = ShareChange()
        assert change.add == frozenset() and change.remove == frozenset()
        assert change.is_empty

    def test_sets_are_frozen(self):
        change = ShareChange(add=frozenset({"sales"}), remove=frozenset({"hr"}))
        assert isinstance(change.add, frozenset)
        assert not change.is_empty
        with pytest.raises(FrozenInstanceError):
            change.add = frozenset()  # type: ignore[misc]

    def test_overlap_cannot_be_built(self):
        with pytest.raises(ValueError):
            ShareChange(add=frozenset({"sales"}), remove=frozenset({"sales"}))

    def test_plain_sets_are_rejected(self):
        with pytest.raises(TypeError):
            ShareChange(add={"sales"})  # type: ignore[arg-type]


class TestValueTypesAreFrozen:
    def test_list_page(self):
        page = ListPage(items=[AccessTarget(PERSONA, "p1")], next_cursor="c", total=3)
        with pytest.raises(FrozenInstanceError):
            page.total = 4  # type: ignore[misc]
        assert ListPage(items=[]).next_cursor is None
        assert ListPage(items=[]).total is None

    def test_bulk_result_and_failure(self):
        failure = BulkShareFailure("p2", ErrorCode.PERSONA_NOT_FOUND)
        assert failure.display_name is None
        result = BulkShareResult(updated=["p1"], failed=[failure])
        with pytest.raises(FrozenInstanceError):
            result.updated = []  # type: ignore[misc]

    def test_sharing_table_cleanup_defaults_to_empty(self):
        table = SharingTable(
            resource_type=PERSONA,
            table="Personas",
            name_attribute="name",
            not_found=ErrorCode.PERSONA_NOT_FOUND,
            delete_failed=ErrorCode.PERSONA_OPERATION_FAILED,
        )
        assert table.cleanup_attributes == ()
        with pytest.raises(FrozenInstanceError):
            table.table = "x"  # type: ignore[misc]

    def test_owner_summary(self):
        owner = OwnerSummary(sub="s1", display_name="中村 由美", enabled=True)
        with pytest.raises(FrozenInstanceError):
            owner.enabled = False  # type: ignore[misc]


class TestShareKey:
    def test_round_trip(self):
        key = share_key(PERSONA, "2026-10-01T09:00:00", "p1")
        assert key == "persona#2026-10-01T09:00:00#p1"
        assert parse_share_key(key) == (PERSONA, "2026-10-01T09:00:00", "p1")

    @pytest.mark.parametrize(
        "bad",
        ["", "persona", "persona#x", "unknown#2026#p1", "persona##p1", "persona#a#"],
    )
    def test_malformed_keys_are_none(self, bad):
        assert parse_share_key(bad) is None


class TestPersonaOwnerColumns:
    def test_from_dict_reads_owner_and_shares(self):
        persona = _persona(owner_sub="sub-a", shared_group_ids=["sales", "plan"])
        assert persona.owner_sub == "sub-a"
        assert persona.shared_group_ids == frozenset({"sales", "plan"})

    def test_defaults_when_absent(self):
        persona = _persona()
        assert persona.owner_sub is None
        assert persona.shared_group_ids == frozenset()

    def test_to_dict_omits_none_and_empty(self):
        data = _persona().to_dict()
        assert "owner_sub" not in data
        assert "shared_group_ids" not in data

    def test_to_dict_writes_sorted_list(self):
        data = _persona(owner_sub="s", shared_group_ids=["plan", "hr"]).to_dict()
        assert data["owner_sub"] == "s"
        assert data["shared_group_ids"] == ["hr", "plan"]

    def test_update_keeps_owner_and_shares(self):
        persona = _persona(owner_sub="s", shared_group_ids=["sales"])
        updated = persona.update(name="新しい名前")
        assert updated.owner_sub == "s"
        assert updated.shared_group_ids == frozenset({"sales"})

    def test_owned_by_returns_a_new_instance(self):
        persona = _persona()
        owned = persona.owned_by("sub-x")
        assert owned.owner_sub == "sub-x"
        assert persona.owner_sub is None
        assert owned.created_at == persona.created_at
        assert isinstance(owned.created_at, datetime)
