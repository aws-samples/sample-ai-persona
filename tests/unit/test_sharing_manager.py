"""SharingManager（共有の入口の業務）の単体テスト。

Component（ResourceSharing）は本物、Store はメモリ上の偽物、DepartmentDirectory は
モックにする（unit-test-instructions.md）。対応: BR1.4、BR1.12、BR2.1、BR6.5、
BR7.1、BR7.5、WF1、WF2。
"""

from unittest.mock import Mock

import pytest

from src.managers.components.resource_sharing import ResourceSharing
from src.managers.components.sharing_runtime import (
    SharingParts,
    build_sharing_parts,
    reset_shared_parts,
    shared_parts,
)
from src.managers.sharing_manager import (
    VANISHED_LABEL,
    SharingManager,
    SharingManagerError,
)
from src.models.authz import ResourceType
from src.models.errors import (
    AccessDeniedError,
    CodedError,
    ErrorCode,
    LookupUnavailableError,
)
from tests.error_helpers import raises_code
from tests.unit.test_resource_sharing import (
    ADMIN,
    DEPARTMENTS,
    MEMBER_A,
    MEMBER_B,
    MEMBER_C,
    TABLE,
    FakeStore,
)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def departments() -> Mock:
    directory = Mock()
    directory.list_all.return_value = list(DEPARTMENTS)
    directory.get_many.side_effect = lambda ids: {
        d.group_id: d for d in DEPARTMENTS if d.group_id in ids
    }
    return directory


@pytest.fixture
def manager(store, departments) -> SharingManager:
    sharing = ResourceSharing(store, departments, sleep=lambda _s: None)
    sharing.register(TABLE)
    return SharingManager(resource_sharing=sharing)


class TestResourceType:
    def test_registered_type_is_accepted(self, manager):
        assert manager.resource_type_of("persona") is ResourceType.PERSONA

    @pytest.mark.parametrize("value", ["discussion", "nope", ""])
    def test_unregistered_or_unknown_type_is_not_found(self, manager, value):
        with raises_code(CodedError, ErrorCode.HTTP_ERROR):
            manager.resource_type_of(value)


class TestDialog:
    def test_single_dialog_shows_choices_and_state(self, manager, store):
        store.add("p1", "sub-b", {"sales", "hr"})
        view = manager.dialog(MEMBER_B, "persona", "p1")
        assert [d.group_id for d in view.choices] == ["sales", "plan"]
        assert view.selected == {"sales"} and not view.is_bulk
        assert view.show_scope_note  # BR7.5
        assert view.show_persona_notice  # BR6.5

    def test_single_dialog_of_a_shared_only_target_is_forbidden(self, manager, store):
        store.add("p1", "sub-a", {"sales"})
        with pytest.raises(AccessDeniedError):
            manager.dialog(MEMBER_B, "persona", "p1")

    def test_malformed_id_is_the_same_not_found(self, manager, store):
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.dialog(MEMBER_A, "persona", "../etc")
        assert store.calls == []

    def test_bulk_dialog_counts_per_department_without_names(self, manager, store):
        store.add("p1", "sub-b", {"sales"})
        store.add("p2", "sub-b", {"sales", "plan"})
        store.add("p3", "sub-a")  # 読めない
        view = manager.bulk_dialog(MEMBER_B, "persona", ["p1", "p2", "p3", "p1"])
        assert view.is_bulk
        assert view.mixed == {"plan": (1, 2), "sales": (2, 2)}
        assert view.unmanageable_count == 1
        assert view.target_ids == ("p1", "p2")

    def test_bulk_dialog_counts_malformed_ids_as_unmanageable(self, manager, store):
        store.add("p1", "sub-b")
        view = manager.bulk_dialog(MEMBER_B, "persona", ["p1", "bad id!"])
        assert view.unmanageable_count == 1

    def test_bulk_dialog_of_only_malformed_ids_is_not_found(self, manager):
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.bulk_dialog(MEMBER_B, "persona", ["bad id!"])

    def test_bulk_dialog_limits(self, manager):
        with raises_code(CodedError, ErrorCode.SHARE_NO_TARGET):
            manager.bulk_dialog(MEMBER_B, "persona", [])
        with raises_code(CodedError, ErrorCode.SHARE_TOO_MANY_TARGETS):
            manager.bulk_dialog(MEMBER_B, "persona", [f"p{i}" for i in range(101)])

    def test_admin_sees_vanished_departments_without_scope_note(self, manager, store):
        store.add("p1", "sub-a", {"old-dept"})
        view = manager.dialog(ADMIN, "persona", "p1")
        assert view.vanished == ("old-dept",) and "old-dept" in view.selected
        assert not view.show_scope_note
        assert VANISHED_LABEL == "（存在しないグループ）"

    def test_member_without_department_cannot_save(self, manager, store):
        store.add("p1", "sub-c")
        view = manager.dialog(MEMBER_C, "persona", "p1")
        assert view.no_department and view.choices == ()

    def test_department_list_unavailable_is_retryable(
        self, manager, store, departments
    ):
        store.add("p1", "sub-a")
        departments.list_all.side_effect = LookupUnavailableError("down")
        with raises_code(CodedError, ErrorCode.LOOKUP_UNAVAILABLE):
            manager.dialog(MEMBER_A, "persona", "p1")


class TestSave:
    def test_single_save_builds_the_change_from_the_picker(self, manager, store):
        store.add("p1", "sub-b", {"sales", "hr"})
        # 開いたときに sales（選択）。plan を足し sales を外す。hr は範囲外で残る
        result = manager.save(
            MEMBER_B,
            "persona",
            "p1",
            selected=["plan"],
            unchanged=[],
            initial=["sales"],
        )
        assert result.updated == ("p1",) and result.is_complete
        assert result.total == 1
        assert store.bodies["p1"]["shared_group_ids"] == {"plan", "hr"}

    def test_department_added_after_opening_is_not_removed(self, manager, store):
        store.add("p1", "sub-b", {"plan"})  # 開いた後に別の人が plan を足した
        manager.save(
            MEMBER_B, "persona", "p1", selected=["sales"], unchanged=[], initial=[]
        )
        assert store.bodies["p1"]["shared_group_ids"] == {"plan", "sales"}

    def test_out_of_scope_value_rejects_without_writing(self, manager, store):
        store.add("p1", "sub-a")
        with raises_code(CodedError, ErrorCode.SHARE_SCOPE_INVALID):
            manager.save(
                MEMBER_A, "persona", "p1", selected=["hr"], unchanged=[], initial=[]
            )
        assert "write_shares" not in store.calls

    def test_malformed_id_save_is_not_found(self, manager):
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.save(
                MEMBER_A, "persona", "", selected=["sales"], unchanged=[], initial=[]
            )

    def test_bulk_save_reports_failures(self, manager, store):
        store.add("p1", "sub-a")
        store.add("p2", "sub-b")  # 読めない
        result = manager.bulk_save(
            MEMBER_A,
            "persona",
            ["p1", "p2", "p1", "bad id!"],
            selected=["sales"],
            unchanged=[],
            initial=[],
        )
        assert result.total == 3
        assert result.updated == ("p1",)
        assert [f.resource_id for f in result.failed] == ["p2", "bad id!"]
        assert result.unnamed_failure_count == 2
        assert result.named_failures == ()
        assert result.failed_ids == ("p2", "bad id!")
        assert not result.is_complete

    def test_bulk_save_names_readable_failures(self, manager, store):
        store.add("p1", "sub-b", {"sales"})  # 共有されているだけ（名前は読める）
        result = manager.bulk_save(
            MEMBER_A, "persona", ["p1"], selected=["sales"], unchanged=[], initial=[]
        )
        assert [f.display_name for f in result.named_failures] == ["名前-p1"]
        assert result.unnamed_failure_count == 0

    def test_bulk_save_limits_are_checked_first(self, manager, store):
        with raises_code(CodedError, ErrorCode.SHARE_NO_TARGET):
            manager.bulk_save(
                MEMBER_A, "persona", [], selected=["sales"], unchanged=[], initial=[]
            )
        with raises_code(CodedError, ErrorCode.SHARE_TOO_MANY_TARGETS):
            manager.bulk_save(
                MEMBER_A,
                "persona",
                [f"p{i}" for i in range(101)],
                selected=["sales"],
                unchanged=[],
                initial=[],
            )
        assert store.calls == []

    def test_bulk_save_of_only_malformed_ids_checks_scope_then_fails_all(
        self, manager, store
    ):
        with raises_code(CodedError, ErrorCode.SHARE_SCOPE_INVALID):
            manager.bulk_save(
                MEMBER_A, "persona", ["bad!"], selected=["hr"], unchanged=[], initial=[]
            )
        result = manager.bulk_save(
            MEMBER_A, "persona", ["bad!"], selected=["sales"], unchanged=[], initial=[]
        )
        assert result.updated == () and result.failed_ids == ("bad!",)

    def test_unexpected_error_becomes_a_manager_error(self, store, departments):
        sharing = Mock()
        sharing.tables.return_value = (TABLE,)
        sharing.build_change.side_effect = RuntimeError("bug")
        manager = SharingManager(resource_sharing=sharing)
        with raises_code(SharingManagerError, ErrorCode.SHARE_UPDATE_FAILED):
            manager.save(
                MEMBER_A, "persona", "p1", selected=["sales"], unchanged=[], initial=[]
            )


class TestSharedParts:
    def test_parts_are_built_once_per_task(self):
        reset_shared_parts()
        built: list[SharingParts] = []

        def build() -> SharingParts:
            parts = SharingParts(sharing=Mock(), owners=Mock())
            built.append(parts)
            return parts

        try:
            first = shared_parts(build)
            assert shared_parts(build) is first
            assert len(built) == 1
        finally:
            reset_shared_parts()

    def test_build_registers_the_default_tables(self):
        parts = build_sharing_parts(Mock(), Mock(), Mock())
        assert parts.sharing.is_registered(ResourceType.PERSONA)
        assert parts.owners.label(None) == "所有者不明"
