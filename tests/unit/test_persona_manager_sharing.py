"""PersonaManager の所有と共有・判定の単体テスト（U4）。

ResourceSharing と AccessPolicy は本物、Store はメモリ上の偽物、DatabaseService と
OwnerDirectory はモックにする。対応: BR3.7、BR3.8、BR6.1〜BR6.7、NFR1.13、NFR1.15、
NFR2.3、AC3.1.4、AC4.1.3、AC4.1.4、AC5.1.2、AC5.1.4。
"""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import Mock

import pytest

from src.managers.components.resource_sharing import ResourceSharing
from src.managers.persona_manager import (
    SEARCH_RESULT_LIMIT,
    PersonaManager,
    PersonaManagerError,
)
from src.models.authz import Principal
from src.models.errors import AccessDeniedError, CodedError, ErrorCode
from src.models.persona import Persona
from src.models.sharing import ListTab
from src.services.resource_share_store import ResourceShareStoreError
from tests.error_helpers import raises_code
from tests.unit.test_resource_sharing import (
    ADMIN,
    DEPARTMENTS,
    MEMBER_A,
    MEMBER_B,
    MEMBER_C,
    NO_ROLE,
    TABLE,
    FakeStore,
)

BASE = datetime(2026, 10, 1, 9, 0, 0)


class World:
    """FakeStore bodies and the matching Persona objects."""

    def __init__(self) -> None:
        self.store = FakeStore()
        self.personas: dict[str, Persona] = {}
        self.db = Mock()
        self.db.get_persona.side_effect = lambda pid, consistent=False: self._get(pid)
        self.db.get_personas_by_ids.side_effect = lambda ids: {
            i: p for i in ids if (p := self._get(i)) is not None
        }
        self.db.get_all_personas.side_effect = lambda **_kw: (
            [p for i in sorted(self.store.bodies) if (p := self._get(i))],
            None,
        )
        self.db.save_persona.side_effect = lambda p: p.id
        self.db.update_persona.return_value = True

    def _get(self, pid: str) -> Persona | None:
        body = self.store.bodies.get(pid)
        if body is None:
            return None
        return replace(
            self.personas[pid],
            owner_sub=body.get("owner_sub"),
            shared_group_ids=frozenset(body.get("shared_group_ids", ())),
            avatar_path=body.get("avatar_path"),
        )

    def add(
        self,
        pid: str,
        owner: str | None,
        shared: set[str] | None = None,
        *,
        minutes: int = 0,
        name: str | None = None,
        avatar: str | None = None,
        background: str = "背景",
    ) -> None:
        created = BASE + timedelta(minutes=minutes)
        self.store.add(pid, owner, shared, created.isoformat(), avatar)
        persona = Persona.create_new(
            name=name or f"名前-{pid}",
            age=30,
            occupation="職業",
            background=background,
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        self.personas[pid] = replace(persona, id=pid, created_at=created)


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def owners() -> Mock:
    directory = Mock()
    directory.labels.side_effect = lambda subs: {s: f"氏名-{s}" for s in subs}
    return directory


@pytest.fixture
def s3() -> Mock:
    return Mock()


@pytest.fixture
def manager(world, owners, s3) -> PersonaManager:
    departments = Mock()
    departments.list_all.return_value = list(DEPARTMENTS)
    departments.get_many.side_effect = lambda ids: {
        d.group_id: d for d in DEPARTMENTS if d.group_id in ids
    }
    sharing = ResourceSharing(world.store, departments, sleep=lambda _s: None)
    sharing.register(TABLE)
    return PersonaManager(
        database_service=world.db,
        s3_service=s3,
        resource_sharing=sharing,
        owners=owners,
    )


def _ids(listing: Any) -> list[str]:
    return [p.id for p in listing.items]


# =============================================================================
# 保存（BR6.1）
# =============================================================================


class TestSave:
    def test_saved_persona_is_owned_by_the_saver(self, manager, world, world_persona):
        manager.save_persona(MEMBER_A, world_persona)
        (saved,) = world.db.save_persona.call_args.args
        assert saved.owner_sub == MEMBER_A.sub
        assert saved.shared_group_ids == frozenset()

    def test_user_without_create_permission_cannot_save(
        self, manager, world, world_persona
    ):
        with pytest.raises(AccessDeniedError):
            manager.save_persona(NO_ROLE, world_persona)
        world.db.save_persona.assert_not_called()


@pytest.fixture
def world_persona() -> Persona:
    return Persona.create_new(
        name="新しい人",
        age=40,
        occupation="職業",
        background="背景",
        values=["v"],
        pain_points=["p"],
        goals=["g"],
    )


# =============================================================================
# タブ別の一覧と件数（BR3、BR3.7、BR6.3、BR6.4）
# =============================================================================


class TestList:
    def test_mine_is_newest_first_and_read_in_one_batch(self, manager, world):
        world.add("old", MEMBER_A.sub, minutes=0)
        world.add("new", MEMBER_A.sub, minutes=5)
        world.add("other", MEMBER_B.sub, minutes=9)

        listing = manager.list_personas(MEMBER_A, "mine", None)

        assert listing.tab is ListTab.MINE
        assert _ids(listing) == ["new", "old"]
        world.db.get_personas_by_ids.assert_called_once()
        assert listing.counts == {"mine": 2, "shared": 0}
        assert not listing.show_all
        assert listing.manageable == {"new", "old"}

    def test_member_asking_for_all_gets_mine(self, manager, world):
        world.add("p1", MEMBER_A.sub)
        world.add("p2", MEMBER_B.sub)
        listing = manager.list_personas(MEMBER_A, "all", None)
        assert listing.tab is ListTab.MINE and _ids(listing) == ["p1"]
        assert "scan_page" not in world.store.calls

    def test_unknown_tab_is_mine(self, manager, world):
        assert manager.list_personas(MEMBER_A, "bogus", None).tab is ListTab.MINE

    def test_shared_rows_have_owner_and_source_departments(
        self, manager, world, owners
    ):
        world.add("p1", MEMBER_A.sub, {"sales", "hr"})
        listing = manager.list_personas(MEMBER_B, "shared", None)

        assert _ids(listing) == ["p1"]
        assert listing.owners == {"p1": f"氏名-{MEMBER_A.sub}"}
        assert listing.shared_with == {"p1": ["営業部"]}  # 共有元（自分の所属）
        assert listing.manageable == frozenset()  # BR6.4

    def test_row_unshared_after_the_index_was_read_is_hidden(self, manager, world):
        world.add("p1", MEMBER_A.sub, {"sales"})
        world.store.bodies["p1"].pop("shared_group_ids")  # 本体だけ外れた
        listing = manager.list_personas(MEMBER_B, "shared", None)
        assert listing.items == []
        assert listing.counts["shared"] == 1  # 件数は索引のまま

    def test_own_rows_show_where_they_are_shared(self, manager, world):
        world.add("p1", MEMBER_A.sub, {"plan", "old-dept"})
        listing = manager.list_personas(MEMBER_A, "mine", None)
        assert sorted(listing.shared_with["p1"]) == ["企画部", "（存在しないグループ）"]

    def test_admin_all_scans_and_marks_unknown_owners(self, manager, world):
        world.add("p1", MEMBER_A.sub)
        world.add("legacy", None)
        listing = manager.list_personas(ADMIN, "all", None)

        assert listing.tab is ListTab.ALL and listing.show_all
        assert set(_ids(listing)) == {"p1", "legacy"}
        assert listing.owners["legacy"] == "所有者不明"
        assert listing.manageable == {"p1", "legacy"}
        assert set(listing.counts) == {"mine", "shared", "all"}

    def test_owner_copy_unavailable_shows_dash(self, manager, world, owners):
        owners.labels.side_effect = None
        owners.labels.return_value = None
        world.add("p1", MEMBER_A.sub, {"sales"})
        listing = manager.list_personas(MEMBER_B, "shared", None)
        assert listing.owners == {"p1": None}

    def test_count_failure_is_none_and_the_list_is_still_shown(self, manager, world):
        world.add("p1", MEMBER_A.sub)
        world.store.count_owner = Mock(side_effect=CodedError("down"))
        listing = manager.list_personas(MEMBER_A, "mine", None)
        assert _ids(listing) == ["p1"] and listing.counts["mine"] is None

    def test_next_page_has_no_counts(self, manager, world):
        for i in range(25):
            world.add(f"p{i:02d}", MEMBER_A.sub, minutes=i)
        first = manager.list_personas(MEMBER_A, "mine", None)
        assert first.next_cursor and len(first.items) == 20
        second = manager.list_personas(MEMBER_A, "mine", first.next_cursor)
        assert len(second.items) == 5 and second.next_cursor is None
        assert second.counts == {}

    def test_shared_first_page_reads_the_departments_once(self, manager, world):
        """R-06: the shared count reuses the page's read (one read per request)."""
        for i in range(23):
            world.add(f"p{i:02d}", MEMBER_A.sub, {"sales", "plan"}, minutes=i)
        world.store.calls.clear()

        listing = manager.list_personas(MEMBER_B, "shared", None)

        assert world.store.calls.count("query_group") == len(MEMBER_B.group_ids)
        assert listing.counts["shared"] == 23

    def test_member_without_department(self, manager, world):
        listing = manager.list_personas(MEMBER_C, "shared", None)
        assert listing.no_department and listing.items == []

    def test_index_failure_is_retryable(self, manager, world):
        world.store.query_owner = Mock(side_effect=ResourceShareStoreError("down"))
        with raises_code(CodedError, ErrorCode.LOOKUP_UNAVAILABLE):
            manager.list_personas(MEMBER_A, "mine", None)

    def test_body_read_failure_is_a_persona_error(self, manager, world):
        world.add("p1", MEMBER_A.sub)
        world.db.get_personas_by_ids.side_effect = RuntimeError("down")
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_OPERATION_FAILED):
            manager.list_personas(MEMBER_A, "mine", None)


class TestSearch:
    def test_search_stays_inside_the_tab(self, manager, world):
        world.add("p1", MEMBER_A.sub, name="田中")
        world.add("p2", MEMBER_B.sub, name="田中")  # 他人のもの
        listing = manager.search_personas(MEMBER_A, "mine", "田中")
        assert _ids(listing) == ["p1"] and not listing.search_truncated

    def test_search_matches_occupation_and_background(self, manager, world):
        world.add("p1", MEMBER_A.sub, background="東京の会社員")
        assert _ids(manager.search_personas(MEMBER_A, "mine", "東京")) == ["p1"]

    def test_search_reads_the_newest_500_and_marks_truncation(self, manager, world):
        for i in range(501):
            world.add(f"p{i:03d}", MEMBER_A.sub, minutes=i)
        listing = manager.search_personas(MEMBER_A, "mine", "名前")
        assert listing.search_truncated
        assert len(listing.items) == SEARCH_RESULT_LIMIT
        assert listing.items[0].id == "p500"

    def test_shared_search_hides_unreadable_bodies(self, manager, world):
        world.add("p1", MEMBER_A.sub, {"sales"})
        world.add("p2", MEMBER_A.sub, {"sales"})
        world.store.bodies["p2"].pop("shared_group_ids")
        assert _ids(manager.search_personas(MEMBER_B, "shared", "名前")) == ["p1"]

    def test_shared_search_reads_the_departments_once_and_counts_only_its_tab(
        self, manager, world
    ):
        """R-06: the search reuses its own read for the count of the tab."""
        for i in range(3):
            world.add(f"p{i}", MEMBER_A.sub, {"sales", "plan"}, minutes=i)
        world.store.calls.clear()

        listing = manager.search_personas(MEMBER_B, "shared", "名前")

        assert world.store.calls.count("query_group") == len(MEMBER_B.group_ids)
        assert "count_owner" not in world.store.calls
        assert listing.counts == {"shared": 3}

    def test_mine_search_does_not_read_the_department_index(self, manager, world):
        """R-06: no count of the other tabs is fetched while searching."""
        world.add("p1", MEMBER_A.sub, {"sales"})
        world.add("p2", MEMBER_B.sub, {"sales"})
        world.store.calls.clear()

        listing = manager.search_personas(MEMBER_A, "mine", "名前")

        assert "query_group" not in world.store.calls
        assert "count_owner" not in world.store.calls
        assert listing.counts == {"mine": 1}

    def test_truncated_search_has_no_count(self, manager, world):
        for i in range(501):
            world.add(f"p{i:03d}", MEMBER_A.sub, minutes=i)
        listing = manager.search_personas(MEMBER_A, "mine", "名前")
        assert listing.counts == {"mine": None}

    def test_admin_all_search_counts_from_its_scan(self, manager, world):
        world.add("p1", MEMBER_A.sub)
        world.add("p2", MEMBER_B.sub)
        world.store.calls.clear()
        listing = manager.search_personas(ADMIN, "all", "名前")
        assert listing.counts == {"all": 2}
        assert "scan_count" not in world.store.calls
        assert "query_group" not in world.store.calls

    def test_admin_all_search_scans(self, manager, world):
        world.add("p1", MEMBER_A.sub)
        world.add("p2", MEMBER_B.sub)
        listing = manager.search_personas(ADMIN, "all", "名前")
        assert set(_ids(listing)) == {"p1", "p2"}
        world.db.get_all_personas.assert_called_once_with(search_all=True)


# =============================================================================
# 詳細・編集・更新・削除（BR6.2、BR6.4、BR6.6、BR6.7）
# =============================================================================


class TestDetail:
    def test_shared_member_reads_but_cannot_manage(self, manager, world):
        world.add("p1", MEMBER_A.sub, {"sales"})
        detail = manager.get_persona_detail(MEMBER_B, "p1")
        assert detail.persona.id == "p1" and not detail.manageable
        assert detail.owner_label == f"氏名-{MEMBER_A.sub}"
        world.db.get_persona.assert_called_with("p1", consistent=True)

    def test_unreadable_and_missing_are_the_same_not_found(self, manager, world):
        world.add("p1", MEMBER_A.sub)
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.get_persona_detail(MEMBER_B, "p1")
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.get_persona_detail(MEMBER_B, "nope")

    def test_admin_reads_and_manages_anything(self, manager, world):
        world.add("p1", MEMBER_A.sub)
        assert manager.get_persona_detail(ADMIN, "p1").manageable

    def test_edit_form_needs_manage(self, manager, world):
        world.add("p1", MEMBER_A.sub, {"sales"})
        with pytest.raises(AccessDeniedError):
            manager.get_persona_for_edit(MEMBER_B, "p1")
        assert manager.get_persona_for_edit(MEMBER_A, "p1").id == "p1"

    def test_describe_gives_the_detail_rows_of_an_updated_persona(self, manager, world):
        """R-07: the edit response gets the same rows as the detail page."""
        world.add("p1", MEMBER_A.sub, {"sales", "plan"})
        updated = manager.update_persona(MEMBER_A, "p1", name="新しい名前")
        world.db.get_persona.reset_mock()

        detail = manager.describe_persona(MEMBER_A, updated)

        assert detail.persona.name == "新しい名前" and detail.manageable
        assert detail.owner_label == f"氏名-{MEMBER_A.sub}"
        assert detail.shared_with == ["企画部", "営業部"]
        world.db.get_persona.assert_not_called()  # 読み直さない

    def test_describe_refuses_an_unreadable_persona(self, manager, world, owners):
        world.add("p1", MEMBER_A.sub)
        persona = world._get("p1")
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.describe_persona(MEMBER_B, persona)
        owners.labels.assert_not_called()  # 所有者の名前を引かない

    def test_existing_get_persona_is_unchanged(self, manager, world):
        world.add("p1", MEMBER_A.sub)
        assert manager.get_persona("p1").id == "p1"


class TestUpdate:
    def test_shared_member_is_forbidden_and_nothing_is_written(self, manager, world):
        world.add("p1", MEMBER_A.sub, {"sales"})
        with pytest.raises(AccessDeniedError):
            manager.update_persona(MEMBER_B, "p1", name="新しい名前")
        world.db.update_persona.assert_not_called()

    def test_unrelated_member_gets_not_found(self, manager, world):
        world.add("p1", MEMBER_A.sub)
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.update_persona(MEMBER_B, "p1", name="新しい名前")

    def test_admin_update_keeps_the_owner_and_shares(self, manager, world):
        world.add("p1", MEMBER_A.sub, {"sales"})
        updated = manager.update_persona(ADMIN, "p1", name="新しい名前")
        (written,) = world.db.update_persona.call_args.args
        assert written.name == "新しい名前"
        assert written.owner_sub == MEMBER_A.sub == updated.owner_sub
        assert written.shared_group_ids == {"sales"}

    def test_persona_deleted_while_editing_is_not_found(self, manager, world):
        world.add("p1", MEMBER_A.sub)
        world.db.update_persona.return_value = False
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_NOT_FOUND):
            manager.update_persona(MEMBER_A, "p1", name="新しい名前")


class TestDelete:
    def test_delete_removes_shares_and_the_returned_avatar(self, manager, world, s3):
        world.add("p1", MEMBER_A.sub, {"sales"}, avatar="persona_avatars/p1/a.webp")
        assert manager.delete_persona(MEMBER_A, "p1") is True
        assert "p1" not in world.store.bodies and not world.store.shares
        s3.delete_file.assert_called_once_with("persona_avatars/p1/a.webp")

    def test_shared_member_cannot_delete(self, manager, world, s3):
        world.add("p1", MEMBER_A.sub, {"sales"})
        with pytest.raises(AccessDeniedError):
            manager.delete_persona(MEMBER_B, "p1")
        assert "p1" in world.store.bodies

    def test_missing_is_the_same_not_found_as_unreadable(self, manager, world):
        world.add("p1", MEMBER_A.sub)
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.delete_persona(MEMBER_B, "p1")
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.delete_persona(MEMBER_A, "gone")

    def test_admin_deletes_someone_elses(self, manager, world):
        world.add("p1", MEMBER_A.sub)
        assert manager.delete_persona(ADMIN, "p1") is True


class TestAccessibleForApi:
    def test_only_own_and_shared(self, manager, world):
        world.add("mine", MEMBER_B.sub, name="田中")
        world.add("shared", MEMBER_A.sub, {"plan"})
        world.add("other", MEMBER_A.sub)
        personas = manager.list_accessible_personas(MEMBER_B)
        assert {p.id for p in personas} == {"mine", "shared"}
        assert [p.id for p in manager.list_accessible_personas(MEMBER_B, "田中")] == [
            "mine"
        ]

    def test_principal_is_required(self, manager):
        with pytest.raises(TypeError):
            manager.list_accessible_personas()  # type: ignore[call-arg]


def test_blank_principal_reads_nothing(manager, world):
    world.add("p1", "")
    nobody = Principal("")
    with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
        manager.get_persona_detail(nobody, "p1")
