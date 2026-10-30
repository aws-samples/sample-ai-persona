"""ResourceSharing（共有の判断、約束 C7、ADR-003）の単体テスト。

Store と DepartmentDirectory は差し替え（メモリ上の偽物とモック）、AccessPolicy は本物を使う
（判定をモックにしない。unit-test-instructions.md）。対応: BR1〜BR4、NFR1.10〜NFR1.14、
NFR1.17〜NFR1.19、NFR2.1〜NFR2.4、NFR8.10〜NFR8.12、NFR9.25。
"""

import io
import json
from contextlib import redirect_stdout
from typing import Any, Iterator, Mapping, Sequence
from unittest.mock import Mock

import pytest

from src.managers.components.resource_sharing import (
    ALL_COUNT_TTL_SECONDS,
    MAX_GROUPS,
    MAX_TARGETS,
    ResourceSharing,
    ResourceSharingError,
    default_sharing_tables,
)
from src.models.authz import AccessTarget, Department, Principal, ResourceType, Role
from src.models.errors import (
    AccessDeniedError,
    CodedError,
    ErrorCode,
    LookupUnavailableError,
)
from src.models.sharing import ListTab, ResourceShareEntry, ShareChange, SharingTable
from src.services.stores.resource_share_store import (
    ResourceShareStoreError,
    ShareConditionFailed,
    ShareStartKeyInvalid,
    ShareTransactionConflict,
    decode_position,
    encode_position,
)
from tests.error_helpers import raises_code

PERSONA = ResourceType.PERSONA
TABLE = SharingTable(
    resource_type=PERSONA,
    table="Personas",
    name_attribute="name",
    not_found=ErrorCode.PERSONA_NOT_FOUND,
    delete_failed=ErrorCode.PERSONA_OPERATION_FAILED,
    cleanup_attributes=("avatar_path",),
)

DEPARTMENTS = [
    Department("sales", "営業部"),
    Department("plan", "企画部"),
    Department("hr", "人事部"),
]

MEMBER_A = Principal("sub-a", frozenset({Role.MEMBER}), frozenset({"sales"}))
MEMBER_B = Principal("sub-b", frozenset({Role.MEMBER}), frozenset({"sales", "plan"}))
MEMBER_C = Principal("sub-c", frozenset({Role.MEMBER}), frozenset())
ADMIN = Principal("sub-admin", frozenset({Role.ADMIN}), frozenset())
NO_ROLE = Principal("sub-x", frozenset(), frozenset({"sales"}))


class FakeStore:
    """In-memory stand-in for ResourceShareStore (the parts ResourceSharing uses)."""

    def __init__(self) -> None:
        self.bodies: dict[str, dict[str, Any]] = {}
        self.shares: dict[tuple[str, str], ResourceShareEntry] = {}
        self.fail_writes: list[Exception] = []
        self.before_write: list[Any] = []
        self.calls: list[str] = []

    def add(
        self,
        pid: str,
        owner: str | None,
        shared: set[str] | None = None,
        created_at: str = "2026-10-01T00:00:00",
        avatar: str | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "id": pid,
            "name": f"名前-{pid}",
            "created_at": created_at,
        }
        if owner:
            body["owner_sub"] = owner
        if shared:
            body["shared_group_ids"] = set(shared)
            for group in shared:
                entry = ResourceShareEntry(group, PERSONA, pid, created_at, owner or "")
                self.shares[(group, entry.share_key)] = entry
        if avatar:
            body["avatar_path"] = avatar
        self.bodies[pid] = body

    def get_item(self, table: str, rid: str, attributes: Sequence[str]) -> dict | None:
        self.calls.append("get_item")
        body = self.bodies.get(rid)
        return (
            None if body is None else {k: v for k, v in body.items() if k in attributes}
        )

    def _check(self, rid: str, expected: frozenset[str]) -> dict[str, Any]:
        if self.before_write:
            self.before_write.pop(0)()
        if self.fail_writes:
            raise self.fail_writes.pop(0)
        body = self.bodies.get(rid)
        if body is None or frozenset(body.get("shared_group_ids", ())) != expected:
            raise ShareConditionFailed("changed")
        return body

    def write_shares(self, table, rtype, rid, *, expected, new, created_at, owner_sub):
        self.calls.append("write_shares")
        body = self._check(rid, expected)
        if new:
            body["shared_group_ids"] = set(new)
        else:
            body.pop("shared_group_ids", None)
        for group in new - expected:
            entry = ResourceShareEntry(group, rtype, rid, created_at, owner_sub)
            self.shares[(group, entry.share_key)] = entry
        for group in expected - new:
            self.shares.pop((group, f"{rtype.value}#{created_at}#{rid}"), None)

    def delete_with_shares(self, table, rtype, rid, *, expected, created_at, cleanup):
        self.calls.append("delete_with_shares")
        body = self._check(rid, expected)
        for column, value in cleanup.items():
            if body.get(column) != value:
                raise ShareConditionFailed("cleanup changed")
        del self.bodies[rid]
        for group in expected:
            self.shares.pop((group, f"{rtype.value}#{created_at}#{rid}"), None)

    def _owned(self, owner: str) -> list[dict[str, Any]]:
        mine = [b for b in self.bodies.values() if b.get("owner_sub") == owner]
        return sorted(mine, key=lambda b: (b["created_at"], b["id"]), reverse=True)

    def query_owner(self, table, owner_sub, *, limit, start_key=None):
        self.calls.append("query_owner")
        rows = self._owned(owner_sub)
        if start_key:
            pos = next(i for i, b in enumerate(rows) if b["id"] == start_key["id"]["S"])
            rows = rows[pos + 1 :]
        page = rows[:limit]
        last = None
        # DynamoDB returns LastEvaluatedKey whenever Limit was reached, even
        # when nothing follows (R-01).
        if page and len(page) == limit:
            b = page[-1]
            last = {
                "id": {"S": b["id"]},
                "owner_sub": {"S": owner_sub},
                "created_at": {"S": b["created_at"]},
            }
        return [b["id"] for b in page], last

    def count_owner(self, table, owner_sub):
        self.calls.append("count_owner")
        return len(self._owned(owner_sub))

    def query_group(self, group_id, rtype):
        self.calls.append("query_group")
        entries = [e for (g, _), e in self.shares.items() if g == group_id]
        return sorted(entries, key=lambda e: e.share_key, reverse=True)

    def scan_page(self, table, *, limit, attributes, start_key=None):
        self.calls.append("scan_page")
        rows = sorted(self.bodies.values(), key=lambda b: b["id"])
        if start_key:
            rows = [b for b in rows if b["id"] > start_key["id"]["S"]]
        page = rows[:limit]
        last = {"id": {"S": page[-1]["id"]}} if page and len(page) == limit else None
        return [{k: v for k, v in b.items() if k in attributes} for b in page], last

    def scan_count(self, table):
        self.calls.append("scan_count")
        return len(self.bodies)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


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
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sharing(store, departments, clock) -> ResourceSharing:
    rs = ResourceSharing(store, departments, clock=clock, sleep=lambda _s: None)
    rs.register(TABLE)
    return rs


def _target(pid: str) -> AccessTarget:
    return AccessTarget(PERSONA, pid)


def _change(add: set[str] = frozenset(), remove: set[str] = frozenset()) -> ShareChange:
    return ShareChange(add=frozenset(add), remove=frozenset(remove))


class LogCapture:
    """The one-line JSON logs written by ``log_event`` (stdout, via capsys)."""

    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._capsys = capsys
        self._records: list[dict[str, Any]] = []

    def records(self) -> list[dict[str, Any]]:
        out = self._capsys.readouterr().out
        for line in out.splitlines():
            if line.startswith("{"):
                self._records.append(json.loads(line))
        return self._records


@pytest.fixture
def logs(capsys: pytest.CaptureFixture[str]) -> Iterator[LogCapture]:
    yield LogCapture(capsys)


def _events(capture: LogCapture, name: str) -> list[dict[str, Any]]:
    return [r for r in capture.records() if r["event"] == name]


# =============================================================================
# 登録
# =============================================================================


class TestRegistration:
    def test_unregistered_type_is_a_caller_bug(self, store, departments):
        rs = ResourceSharing(store, departments)
        assert not rs.is_registered(PERSONA)
        with pytest.raises(ValueError):
            rs.update_shares(MEMBER_A, _target("p1"), _change({"sales"}))

    def test_registering_twice_is_an_error(self, sharing):
        with pytest.raises(ValueError):
            sharing.register(TABLE)

    def test_default_tables_register_personas_and_discussions(self):
        tables = default_sharing_tables()
        assert [t.resource_type for t in tables] == [
            PERSONA,
            ResourceType.DISCUSSION,
            ResourceType.SURVEY_TEMPLATE,
            ResourceType.SURVEY,
        ]
        persona = tables[0]
        assert persona.name_attribute == "name"
        assert persona.not_found is ErrorCode.PERSONA_NOT_FOUND
        assert persona.cleanup_attributes == ("avatar_path",)

    def test_discussion_table_registration(self):
        """U6（logical-components.md「共有するもの」）。"""
        discussion = default_sharing_tables()[1]
        assert discussion.table.endswith("Discussions")
        assert discussion.name_attribute == "topic"
        assert discussion.not_found is ErrorCode.DISCUSSION_NOT_FOUND
        assert discussion.delete_failed is ErrorCode.DISCUSSION_OPERATION_FAILED
        assert discussion.cleanup_attributes == (
            "documents",
            "message_count",
            "report_count",
        )

    def test_survey_template_and_survey_registration(self):
        """U7 BR6.3（security-design.md 1.5）。"""
        tables = {t.resource_type: t for t in default_sharing_tables()}
        template = tables[ResourceType.SURVEY_TEMPLATE]
        assert template.table.endswith("SurveyTemplates")
        assert template.name_attribute == "name"
        assert template.not_found is ErrorCode.SURVEY_TEMPLATE_NOT_FOUND
        assert template.delete_failed is ErrorCode.SURVEY_OPERATION_FAILED
        assert template.cleanup_attributes == ("images",)
        survey = tables[ResourceType.SURVEY]
        assert survey.table.endswith("Surveys")
        assert survey.name_attribute == "name"
        assert survey.not_found is ErrorCode.SURVEY_NOT_FOUND
        assert survey.delete_failed is ErrorCode.SURVEY_OPERATION_FAILED
        # status と execution_arn: 実行中の削除で実行を止める（BR7.1）
        assert survey.cleanup_attributes == (
            "s3_result_path",
            "status",
            "execution_arn",
        )

    def test_ownerless_survey_cannot_be_shared_alone_or_in_bulk(
        self, store, departments
    ):
        """U7 BR1.6: 所有者のない古い結果は共有できない（一括では失敗に数える）。"""
        rs = ResourceSharing(store, departments)
        for table in default_sharing_tables():
            rs.register(
                SharingTable(
                    resource_type=table.resource_type,
                    table=TABLE.table,
                    name_attribute="name",
                    not_found=table.not_found,
                    delete_failed=table.delete_failed,
                )
            )
        store.add("legacy", None)
        store.add("mine", "sub-admin")
        target = AccessTarget(ResourceType.SURVEY, "legacy")
        with raises_code(ResourceSharingError, ErrorCode.SHARE_OWNER_MISSING):
            rs.update_shares(ADMIN, target, _change({"sales"}))
        result = rs.bulk_update_shares(
            ADMIN,
            [target, AccessTarget(ResourceType.SURVEY, "mine")],
            _change({"sales"}),
        )
        assert result.updated == ["mine"]
        assert [f.code for f in result.failed] == [ErrorCode.SHARE_OWNER_MISSING]


# =============================================================================
# 共有先の変更（BR1）
# =============================================================================


class TestUpdateAuthorization:
    def test_owner_adds_a_department(self, sharing, store):
        store.add("p1", "sub-a")
        assert sharing.update_shares(MEMBER_A, _target("p1"), _change({"sales"})) == {
            "sales"
        }
        assert store.bodies["p1"]["shared_group_ids"] == {"sales"}

    def test_unreadable_target_is_not_found_and_logged(self, sharing, store, logs):
        store.add("p1", "sub-b")
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            sharing.update_shares(MEMBER_A, _target("p1"), _change({"sales"}))
        assert "write_shares" not in store.calls

    def test_missing_target_is_the_same_not_found(self, sharing, store):
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            sharing.update_shares(MEMBER_A, _target("nope"), _change({"sales"}))

    def test_shared_only_target_is_forbidden(self, sharing, store, logs):
        store.add("p1", "sub-b", {"sales"})
        with pytest.raises(AccessDeniedError):
            sharing.update_shares(MEMBER_A, _target("p1"), _change(remove={"sales"}))
        assert "write_shares" not in store.calls

    def test_denial_writes_authz_denied(self, store, departments):
        rs = ResourceSharing(store, departments, sleep=lambda _s: None)
        rs.register(TABLE)
        store.add("p1", "sub-b", {"sales"})
        buffer = io.StringIO()
        with redirect_stdout(buffer), pytest.raises(AccessDeniedError):
            rs.update_shares(MEMBER_A, _target("p1"), _change(remove={"sales"}))
        events = [json.loads(line)["event"] for line in buffer.getvalue().splitlines()]
        assert events.count("authz.denied") == 1

    def test_admin_manages_someone_elses(self, sharing, store):
        store.add("p1", "sub-a")
        assert sharing.update_shares(ADMIN, _target("p1"), _change({"hr"})) == {"hr"}
        assert store.bodies["p1"]["owner_sub"] == "sub-a"  # BR6.6

    def test_no_role_cannot_share_own(self, sharing, store):
        store.add("p1", "sub-x")
        with raises_code(CodedError, ErrorCode.SHARE_NO_DEPARTMENT):
            sharing.update_shares(NO_ROLE, _target("p1"), _change({"sales"}))


class TestUpdateScope:
    def test_out_of_scope_add_is_rejected(self, sharing, store):
        store.add("p1", "sub-a")
        with raises_code(ResourceSharingError, ErrorCode.SHARE_SCOPE_INVALID):
            sharing.update_shares(MEMBER_A, _target("p1"), _change({"plan"}))
        assert "write_shares" not in store.calls

    def test_out_of_scope_remove_is_rejected(self, sharing, store):
        store.add("p1", "sub-a", {"hr"})
        with raises_code(ResourceSharingError, ErrorCode.SHARE_SCOPE_INVALID):
            sharing.update_shares(MEMBER_A, _target("p1"), _change(remove={"hr"}))

    def test_out_of_scope_shares_stay(self, sharing, store):
        store.add("p1", "sub-a", {"hr", "sales"})
        new = sharing.update_shares(MEMBER_A, _target("p1"), _change(remove={"sales"}))
        assert new == {"hr"}

    def test_no_department_cannot_save(self, sharing, store):
        store.add("p1", "sub-c")
        with raises_code(ResourceSharingError, ErrorCode.SHARE_NO_DEPARTMENT):
            sharing.update_shares(MEMBER_C, _target("p1"), _change({"sales"}))

    def test_admin_cannot_add_a_vanished_department(self, sharing, store):
        store.add("p1", "sub-a")
        with raises_code(ResourceSharingError, ErrorCode.SHARE_SCOPE_INVALID):
            sharing.update_shares(ADMIN, _target("p1"), _change({"old-dept"}))

    def test_admin_removes_a_vanished_department(self, sharing, store):
        store.add("p1", "sub-a", {"old-dept", "sales"})
        new = sharing.update_shares(ADMIN, _target("p1"), _change(remove={"old-dept"}))
        assert new == {"sales"}

    def test_member_cannot_remove_a_vanished_department(self, sharing, store):
        store.add("p1", "sub-a", {"old-dept"})
        with raises_code(ResourceSharingError, ErrorCode.SHARE_SCOPE_INVALID):
            sharing.update_shares(MEMBER_A, _target("p1"), _change(remove={"old-dept"}))

    def test_departments_unavailable_is_retryable(self, sharing, store, departments):
        store.add("p1", "sub-a")
        departments.list_all.side_effect = LookupUnavailableError("down")
        with pytest.raises(LookupUnavailableError):
            sharing.update_shares(MEMBER_A, _target("p1"), _change({"sales"}))


class TestUpdateWrites:
    def test_same_result_writes_nothing(self, sharing, store, logs):
        store.add("p1", "sub-a", {"sales"})
        assert sharing.update_shares(MEMBER_A, _target("p1"), _change({"sales"})) == {
            "sales"
        }
        assert "write_shares" not in store.calls

    def test_owner_missing_cannot_be_shared(self, sharing, store):
        store.add("legacy", None)
        with raises_code(ResourceSharingError, ErrorCode.SHARE_OWNER_MISSING):
            sharing.update_shares(ADMIN, _target("legacy"), _change({"sales"}))

    def test_index_copies_created_at_and_owner(self, sharing, store):
        store.add("p1", "sub-a", created_at="2026-09-01T10:00:00")
        sharing.update_shares(ADMIN, _target("p1"), _change({"sales"}))
        (entry,) = store.shares.values()
        assert entry.created_at == "2026-09-01T10:00:00"
        assert entry.owner_sub == "sub-a"

    def test_more_than_99_departments_is_capacity(self, store, departments):
        many = [Department(f"g{i}", f"部署{i}") for i in range(120)]
        departments.list_all.return_value = many
        rs = ResourceSharing(store, departments, sleep=lambda _s: None)
        rs.register(TABLE)
        store.add("p1", "sub-a", {f"g{i}" for i in range(50)})
        with raises_code(
            ResourceSharingError, ErrorCode.SHARE_TOO_MANY_GROUPS, max_groups=MAX_GROUPS
        ):
            rs.update_shares(
                ADMIN, _target("p1"), _change({f"g{i}" for i in range(50, 100)})
            )

    def test_more_than_99_changes_is_capacity(self, store, departments):
        many = [Department(f"g{i}", f"部署{i}") for i in range(120)]
        departments.list_all.return_value = many
        rs = ResourceSharing(store, departments, sleep=lambda _s: None)
        rs.register(TABLE)
        store.add("p1", "sub-a", {f"g{i}" for i in range(60)})
        change = _change(
            {f"g{i}" for i in range(60, 110)}, {f"g{i}" for i in range(60)}
        )
        with raises_code(ResourceSharingError, ErrorCode.SHARE_TOO_MANY_GROUPS):
            rs.update_shares(ADMIN, _target("p1"), change)

    def test_changed_event_has_ids_but_no_names(self, sharing, store, logs):
        store.add("p1", "sub-a", {"sales"})
        sharing.update_shares(ADMIN, _target("p1"), _change({"hr"}, {"sales"}))
        sharing.update_shares(ADMIN, _target("p1"), _change({"hr"}))  # 変わらない
        (event,) = _events(logs, "sharing.changed")
        assert event["actor_sub"] == "sub-admin"
        assert event["resource_type"] == "persona"
        assert event["resource_id"] == "p1"
        assert event["added"] == ["hr"] and event["removed"] == ["sales"]
        assert event["bulk"] is False
        assert "名前-p1" not in json.dumps(event, ensure_ascii=False)


class TestUpdateRetries:
    def test_condition_failure_rereads_and_rejudges(self, sharing, store, logs):
        store.add("p1", "sub-a")

        def someone_else_adds_hr() -> None:
            store.bodies["p1"]["shared_group_ids"] = {"hr"}

        store.before_write.append(someone_else_adds_hr)

        new = sharing.update_shares(ADMIN, _target("p1"), _change({"sales"}))

        assert new == {"hr", "sales"}
        assert store.calls.count("get_item") == 2
        (retry,) = _events(logs, "sharing.retry")
        assert retry["attempt"] == 1 and retry["resource_id"] == "p1"

    def test_conflict_waits_then_retries(self, store, departments):
        sleeps: list[float] = []
        rs = ResourceSharing(
            store, departments, sleep=sleeps.append, jitter=lambda: 0.5
        )
        rs.register(TABLE)
        store.add("p1", "sub-a")
        store.fail_writes.append(ShareTransactionConflict("busy"))
        assert rs.update_shares(MEMBER_A, _target("p1"), _change({"sales"})) == {
            "sales"
        }
        assert sleeps == [0.05]

    def test_third_failure_is_retryable_error(self, sharing, store, logs):
        store.add("p1", "sub-a")
        store.fail_writes += [ShareConditionFailed("x")] * 3
        with raises_code(ResourceSharingError, ErrorCode.SHARE_UPDATE_FAILED):
            sharing.update_shares(MEMBER_A, _target("p1"), _change({"sales"}))
        assert store.calls.count("write_shares") == 3
        assert len(_events(logs, "sharing.retry")) == 2
        (failed,) = _events(logs, "sharing.update_failed")
        assert failed["resource_id"] == "p1"

    def test_access_lost_between_attempts(self, sharing, store):
        store.add("p1", "sub-a", {"sales"})

        def owner_changes() -> None:
            store.bodies["p1"]["owner_sub"] = "sub-b"

        store.before_write.append(owner_changes)
        store.fail_writes.append(ShareConditionFailed("x"))
        with pytest.raises(AccessDeniedError):
            sharing.update_shares(
                MEMBER_A,
                _target("p1"),
                _change(remove={"sales"}),
            )

    def test_store_failure_is_retryable_error(self, sharing, store):
        store.add("p1", "sub-a")
        store.fail_writes.append(ResourceShareStoreError("boom"))
        with raises_code(ResourceSharingError, ErrorCode.SHARE_UPDATE_FAILED):
            sharing.update_shares(MEMBER_A, _target("p1"), _change({"sales"}))


# =============================================================================
# 入力から ShareChange を作る（BR1.12、security-design.md §2）
# =============================================================================


class TestBuildChange:
    def test_remove_is_initial_minus_selected_and_unchanged(self, sharing):
        change = sharing.build_change(
            MEMBER_B, selected=["sales"], unchanged=[], initial=["sales", "plan"]
        )
        assert change == _change({"sales"}, {"plan"})

    def test_departments_not_initially_shared_are_not_removed(self, sharing):
        change = sharing.build_change(MEMBER_B, selected=[], unchanged=[], initial=[])
        assert change.is_empty

    def test_unchanged_departments_are_kept(self, sharing):
        change = sharing.build_change(
            MEMBER_B, selected=[], unchanged=["plan"], initial=["plan", "sales"]
        )
        assert change == _change(set(), {"sales"})

    def test_out_of_scope_initial_is_dropped(self, sharing):
        change = sharing.build_change(
            MEMBER_A, selected=[], unchanged=[], initial=["sales", "hr"]
        )
        assert change == _change(set(), {"sales"})

    def test_out_of_scope_selected_rejects_everything(self, sharing):
        with raises_code(ResourceSharingError, ErrorCode.SHARE_SCOPE_INVALID):
            sharing.build_change(MEMBER_A, selected=["hr"], unchanged=[], initial=[])

    def test_out_of_scope_unchanged_rejects_everything(self, sharing):
        with raises_code(ResourceSharingError, ErrorCode.SHARE_SCOPE_INVALID):
            sharing.build_change(MEMBER_A, selected=[], unchanged=["hr"], initial=[])

    def test_admin_may_remove_vanished(self, sharing):
        change = sharing.build_change(
            ADMIN, selected=[], unchanged=[], initial=["old-dept"]
        )
        assert change == _change(set(), {"old-dept"})

    def test_member_vanished_initial_is_dropped(self, sharing):
        change = sharing.build_change(
            MEMBER_A, selected=[], unchanged=[], initial=["old-dept"]
        )
        assert change.is_empty

    def test_no_department_member(self, sharing):
        with raises_code(ResourceSharingError, ErrorCode.SHARE_NO_DEPARTMENT):
            sharing.build_change(MEMBER_C, selected=[], unchanged=[], initial=[])


# =============================================================================
# 一括共有（BR2）
# =============================================================================


class TestBulk:
    def test_counts_are_checked_once_before_writing(self, sharing, store):
        with raises_code(ResourceSharingError, ErrorCode.SHARE_NO_TARGET):
            sharing.bulk_update_shares(MEMBER_A, [], _change({"sales"}))
        targets = [_target(f"p{i}") for i in range(MAX_TARGETS + 1)]
        with raises_code(
            ResourceSharingError, ErrorCode.SHARE_TOO_MANY_TARGETS, max_targets=100
        ):
            sharing.bulk_update_shares(MEMBER_A, targets, _change({"sales"}))
        assert store.calls == []

    def test_duplicates_are_merged_before_counting(self, sharing, store):
        store.add("p1", "sub-a")
        targets = [_target("p1")] * (MAX_TARGETS + 5)
        result = sharing.bulk_update_shares(MEMBER_A, targets, _change({"sales"}))
        assert result.updated == ["p1"]

    def test_scope_is_checked_once_for_everything(self, sharing, store):
        store.add("p1", "sub-a")
        with raises_code(ResourceSharingError, ErrorCode.SHARE_SCOPE_INVALID):
            sharing.bulk_update_shares(MEMBER_A, [_target("p1")], _change({"hr"}))
        assert "write_shares" not in store.calls

    def test_each_target_is_written_and_failures_reported(self, sharing, store, logs):
        store.add("p1", "sub-a")
        store.add("p2", "sub-b", {"sales"})  # 共有されているだけ
        store.add("p3", "sub-b")  # 読めない
        store.add("p4", "sub-a", {"plan"})

        result = sharing.bulk_update_shares(
            MEMBER_A,
            [
                _target("p1"),
                _target("p2"),
                _target("p3"),
                _target("missing"),
                _target("p4"),
            ],
            _change({"sales"}),
        )

        assert result.updated == ["p1", "p4"]
        failures = {f.resource_id: f for f in result.failed}
        assert failures["p2"].code is ErrorCode.ACCESS_DENIED
        assert failures["p2"].display_name == "名前-p2"
        assert failures["p3"].code is ErrorCode.PERSONA_NOT_FOUND
        assert failures["p3"].display_name is None
        assert failures["missing"].display_name is None
        assert store.bodies["p4"]["shared_group_ids"] == {
            "plan",
            "sales",
        }  # 中間は今のまま
        assert all(e["bulk"] for e in _events(logs, "sharing.changed"))
        assert len(_events(logs, "sharing.changed")) == 2

    def test_unchanged_target_counts_as_updated(self, sharing, store):
        store.add("p1", "sub-a", {"sales"})
        result = sharing.bulk_update_shares(
            MEMBER_A, [_target("p1")], _change({"sales"})
        )
        assert result.updated == ["p1"] and result.failed == []


# =============================================================================
# 一覧と件数（BR3）
# =============================================================================


class TestTabs:
    def test_member_all_becomes_mine(self, sharing):
        assert sharing.effective_tab(MEMBER_A, ListTab.ALL) is ListTab.MINE
        assert sharing.effective_tab(ADMIN, ListTab.ALL) is ListTab.ALL

    def test_mine_is_newest_first_from_owner_index(self, sharing, store):
        for i in range(25):
            store.add(f"p{i:02d}", "sub-a", created_at=f"2026-10-01T00:00:{i:02d}")
        store.add("other", "sub-b")

        page = sharing.list_page(MEMBER_A, PERSONA, ListTab.MINE, None)

        assert [t.resource_id for t in page.items][:2] == ["p24", "p23"]
        assert len(page.items) == 20
        assert page.total == 25
        assert page.next_cursor
        rest = sharing.list_page(MEMBER_A, PERSONA, ListTab.MINE, page.next_cursor)
        assert [t.resource_id for t in rest.items] == [
            f"p{i:02d}" for i in range(4, -1, -1)
        ]
        assert rest.next_cursor is None

    @pytest.mark.parametrize("owned", [20, 40])
    def test_mine_exact_multiple_of_the_page_has_no_empty_next_page(
        self, sharing, store, owned
    ):
        """R-01: a full last page must not offer "もっと見る" to an empty page."""
        for i in range(owned):
            store.add(f"p{i:02d}", "sub-a", created_at=f"2026-10-01T00:00:{i:02d}")

        pages = [sharing.list_page(MEMBER_A, PERSONA, ListTab.MINE, None)]
        while pages[-1].next_cursor and len(pages) < 5:
            pages.append(
                sharing.list_page(
                    MEMBER_A, PERSONA, ListTab.MINE, pages[-1].next_cursor
                )
            )

        assert [len(p.items) for p in pages] == [20] * (owned // 20)
        assert pages[-1].next_cursor is None

    @pytest.mark.parametrize("bodies", [20, 40])
    def test_all_exact_multiple_of_the_page_has_no_empty_next_page(
        self, sharing, store, bodies
    ):
        """R-01: the same boundary for the admin ``all`` scan."""
        for i in range(bodies):
            store.add(f"p{i:02d}", "sub-a")

        pages = [sharing.list_page(ADMIN, PERSONA, ListTab.ALL, None)]
        while pages[-1].next_cursor and len(pages) < 5:
            pages.append(
                sharing.list_page(ADMIN, PERSONA, ListTab.ALL, pages[-1].next_cursor)
            )

        assert [len(p.items) for p in pages] == [20] * (bodies // 20)
        assert pages[-1].next_cursor is None

    def test_mine_with_21_still_offers_the_next_page(self, sharing, store):
        for i in range(21):
            store.add(f"p{i:02d}", "sub-a", created_at=f"2026-10-01T00:00:{i:02d}")
        page = sharing.list_page(MEMBER_A, PERSONA, ListTab.MINE, None)
        assert len(page.items) == 20 and page.next_cursor
        rest = sharing.list_page(MEMBER_A, PERSONA, ListTab.MINE, page.next_cursor)
        assert [t.resource_id for t in rest.items] == ["p00"]
        assert rest.next_cursor is None

    def test_shared_page_reads_each_department_once(self, sharing, store):
        """R-06: the shared total comes from the page's own read."""
        for i in range(23):
            store.add(
                f"p{i:02d}", "sub-x", {"sales", "plan"}, f"2026-10-01T00:00:{i:02d}"
            )
        store.calls.clear()

        page = sharing.list_page(MEMBER_B, PERSONA, ListTab.SHARED, None)

        assert page.total == 23
        assert store.calls.count("query_group") == len(MEMBER_B.group_ids)

    def test_shared_merges_departments_and_skips_own(self, sharing, store):
        store.add("p1", "sub-a", {"sales", "plan"}, created_at="2026-10-01T00:00:01")
        store.add("p2", "sub-x", {"plan"}, created_at="2026-10-01T00:00:02")
        store.add("mine", "sub-b", {"sales"}, created_at="2026-10-01T00:00:03")
        store.add("hr-only", "sub-x", {"hr"})

        page = sharing.list_page(MEMBER_B, PERSONA, ListTab.SHARED, None)

        assert [t.resource_id for t in page.items] == ["p2", "p1"]
        assert page.total == 2
        assert sharing.count(MEMBER_B, PERSONA, ListTab.SHARED) == 2
        assert page.next_cursor is None

    def test_shared_pages_by_share_key(self, sharing, store):
        for i in range(23):
            store.add(
                f"p{i:02d}", "sub-x", {"sales"}, created_at=f"2026-10-01T00:00:{i:02d}"
            )
        page = sharing.list_page(MEMBER_A, PERSONA, ListTab.SHARED, None)
        rest = sharing.list_page(MEMBER_A, PERSONA, ListTab.SHARED, page.next_cursor)
        assert len(page.items) == 20 and len(rest.items) == 3
        assert rest.items[-1].resource_id == "p00"

    def test_no_department_shared_is_empty(self, sharing, store):
        store.add("p1", "sub-x", {"sales"})
        page = sharing.list_page(MEMBER_C, PERSONA, ListTab.SHARED, None)
        assert page.items == [] and page.total == 0
        assert "query_group" not in store.calls

    def test_member_never_scans(self, sharing, store):
        store.add("p1", "sub-a")
        sharing.list_page(MEMBER_A, PERSONA, ListTab.ALL, None)
        sharing.count(MEMBER_A, PERSONA, ListTab.ALL)
        sharing.collect_ids(MEMBER_A, PERSONA, ListTab.SHARED)
        assert "scan_page" not in store.calls and "scan_count" not in store.calls

    def test_admin_all_scans_in_pages(self, sharing, store):
        for i in range(22):
            store.add(f"p{i:02d}", "sub-a" if i % 2 else None)
        page = sharing.list_page(ADMIN, PERSONA, ListTab.ALL, None)
        assert len(page.items) == 20 and page.total == 22
        rest = sharing.list_page(ADMIN, PERSONA, ListTab.ALL, page.next_cursor)
        assert len(rest.items) == 2 and rest.next_cursor is None

    def test_all_count_is_kept_for_60_seconds(self, sharing, store, clock):
        store.add("p1", "sub-a")
        assert sharing.count(ADMIN, PERSONA, ListTab.ALL) == 1
        store.add("p2", "sub-a")
        clock.now += ALL_COUNT_TTL_SECONDS - 1
        assert sharing.count(ADMIN, PERSONA, ListTab.ALL) == 1
        assert store.calls.count("scan_count") == 1
        clock.now += 2
        assert sharing.count(ADMIN, PERSONA, ListTab.ALL) == 2

    def test_count_failure_is_none(self, sharing, store):
        store.count_owner = Mock(side_effect=ResourceShareStoreError("down"))  # type: ignore[method-assign]
        assert sharing.count(MEMBER_A, PERSONA, ListTab.MINE) is None

    def test_list_failure_is_retryable(self, sharing, store):
        store.query_owner = Mock(side_effect=ResourceShareStoreError("down"))  # type: ignore[method-assign]
        with pytest.raises(LookupUnavailableError):
            sharing.list_page(MEMBER_A, PERSONA, ListTab.MINE, None)


class TestCursor:
    def test_unreadable_cursor_starts_over(self, sharing, store):
        store.add("p1", "sub-a")
        page = sharing.list_page(MEMBER_A, PERSONA, ListTab.MINE, "garbage")
        assert [t.resource_id for t in page.items] == ["p1"]

    def test_cursor_of_another_tab_starts_over(self, sharing, store):
        for i in range(21):
            store.add(
                f"p{i:02d}", "sub-a", {"sales"}, created_at=f"2026-10-01T00:00:{i:02d}"
            )
        mine = sharing.list_page(MEMBER_A, PERSONA, ListTab.MINE, None)
        page = sharing.list_page(MEMBER_B, PERSONA, ListTab.SHARED, mine.next_cursor)
        assert page.items[0].resource_id == "p20"

    def test_someone_elses_owner_position_starts_over(self, sharing, store):
        for i in range(21):
            store.add(f"p{i:02d}", "sub-a", created_at=f"2026-10-01T00:00:{i:02d}")
        cursor = sharing.list_page(MEMBER_A, PERSONA, ListTab.MINE, None).next_cursor
        payload = decode_position(cursor)
        payload["p"]["owner_sub"] = {"S": "sub-b"}
        page = sharing.list_page(
            MEMBER_A, PERSONA, ListTab.MINE, encode_position(payload)
        )
        assert page.items[0].resource_id == "p20"

    def test_malformed_share_key_starts_over(self, sharing, store):
        store.add("p1", "sub-x", {"sales"})
        cursor = encode_position({"t": "shared", "r": "persona", "p": "discussion#x#y"})
        page = sharing.list_page(MEMBER_A, PERSONA, ListTab.SHARED, cursor)
        assert [t.resource_id for t in page.items] == ["p1"]

    def test_refused_start_key_starts_over(self, sharing, store):
        store.add("p1", "sub-a")
        real = store.query_owner
        calls: list[Any] = []

        def query(table, owner, *, limit, start_key=None):
            calls.append(start_key)
            if start_key:
                raise ShareStartKeyInvalid("bad")
            return real(table, owner, limit=limit)

        store.query_owner = query  # type: ignore[method-assign]
        cursor = encode_position(
            {
                "t": "mine",
                "r": "persona",
                "p": {
                    "id": {"S": "zz"},
                    "owner_sub": {"S": "sub-a"},
                    "created_at": {"S": "t"},
                },
            }
        )
        page = sharing.list_page(MEMBER_A, PERSONA, ListTab.MINE, cursor)
        assert [t.resource_id for t in page.items] == ["p1"]
        assert calls[-1] is None


class TestCollectIds:
    def test_newest_500_and_truncation(self, sharing, store):
        for i in range(505):
            store.add(
                f"p{i:03d}",
                "sub-a",
                created_at=f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}",
            )
        ids, truncated = sharing.collect_ids(MEMBER_A, PERSONA, ListTab.MINE)
        assert len(ids) == 500 and truncated
        assert ids[0] == "p504"

    def test_shared_without_truncation(self, sharing, store):
        store.add("p1", "sub-x", {"sales"})
        assert sharing.collect_ids(MEMBER_A, PERSONA, ListTab.SHARED) == (["p1"], False)


# =============================================================================
# 削除（BR4）
# =============================================================================


class TestDelete:
    def test_deletes_body_and_shares_and_returns_the_body(self, sharing, store):
        store.add("p1", "sub-a", {"sales"}, avatar="s3://a.webp")
        body = sharing.delete_with_shares(MEMBER_A, PERSONA, "p1")
        assert body["avatar_path"] == "s3://a.webp"
        assert "p1" not in store.bodies and store.shares == {}

    def test_missing_body_is_nothing_to_delete(self, sharing, store):
        assert sharing.delete_with_shares(MEMBER_A, PERSONA, "nope") is None
        assert "delete_with_shares" not in store.calls

    def test_judges_on_the_strongly_read_body(self, sharing, store):
        store.add("p1", "sub-b", {"sales"})
        with pytest.raises(AccessDeniedError):
            sharing.delete_with_shares(MEMBER_A, PERSONA, "p1")
        store.add("p2", "sub-b")
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            sharing.delete_with_shares(MEMBER_A, PERSONA, "p2")
        assert "delete_with_shares" not in store.calls

    def test_replaced_avatar_is_reread(self, sharing, store):
        store.add("p1", "sub-a", avatar="s3://old.webp")

        def replace_avatar() -> None:
            store.bodies["p1"]["avatar_path"] = "s3://new.webp"

        store.before_write.append(replace_avatar)
        body = sharing.delete_with_shares(MEMBER_A, PERSONA, "p1")
        assert body["avatar_path"] == "s3://new.webp"

    def test_three_failures_use_the_types_code(self, sharing, store):
        store.add("p1", "sub-a")
        store.fail_writes += [ShareConditionFailed("x")] * 3
        with raises_code(ResourceSharingError, ErrorCode.PERSONA_OPERATION_FAILED):
            sharing.delete_with_shares(MEMBER_A, PERSONA, "p1")


# =============================================================================
# ダイアログの中身（BR7.1、BR1.4、BR7.5、BR6.5）
# =============================================================================


class TestDialogState:
    def test_single_dialog(self, sharing, store):
        store.add("p1", "sub-b", {"sales", "hr"})
        view = sharing.dialog_state(MEMBER_B, PERSONA, ["p1"], bulk=False)
        assert [d.group_id for d in view.choices] == ["sales", "plan"]
        assert view.selected == {"sales"}
        assert view.mixed is None
        assert view.show_scope_note and view.show_persona_notice
        assert view.target_ids == ("p1",) and view.target_label == "名前-p1"

    def test_single_dialog_of_unmanageable_target(self, sharing, store):
        store.add("p1", "sub-a", {"sales"})
        with pytest.raises(AccessDeniedError):
            sharing.dialog_state(MEMBER_B, PERSONA, ["p1"], bulk=False)
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            sharing.dialog_state(MEMBER_B, PERSONA, ["nope"], bulk=False)

    def test_bulk_dialog_counts_per_department(self, sharing, store):
        store.add("p1", "sub-b", {"sales"})
        store.add("p2", "sub-b", {"sales", "plan"})
        store.add("p3", "sub-a")  # 読めない
        view = sharing.dialog_state(MEMBER_B, PERSONA, ["p1", "p2", "p3"], bulk=True)
        assert view.target_ids == ("p1", "p2")
        assert view.mixed == {"plan": (1, 2), "sales": (2, 2)}
        assert view.unmanageable_count == 1
        assert view.target_label == "名前-p1 ほか 1 件"

    def test_admin_sees_vanished_departments(self, sharing, store):
        store.add("p1", "sub-a", {"old-dept"})
        view = sharing.dialog_state(ADMIN, PERSONA, ["p1"], bulk=False)
        assert view.vanished == ("old-dept",)
        assert "old-dept" in view.selected
        assert not view.show_scope_note
        # 部署の一覧の順（DepartmentDirectory は表示名の順に返す）のまま
        assert [d.group_id for d in view.choices] == ["sales", "plan", "hr"]

    def test_member_without_department(self, sharing, store):
        store.add("p1", "sub-c")
        view = sharing.dialog_state(MEMBER_C, PERSONA, ["p1"], bulk=False)
        assert view.no_department and view.choices == ()

    def test_departments_unavailable_is_retryable(self, sharing, store, departments):
        store.add("p1", "sub-a")
        departments.list_all.side_effect = LookupUnavailableError("down")
        with pytest.raises(LookupUnavailableError):
            sharing.dialog_state(MEMBER_A, PERSONA, ["p1"], bulk=False)


class TestDepartmentNames:
    def test_unknown_departments_are_marked(self, sharing):
        assert sharing.department_names(["plan", "old-dept"]) == [
            "企画部",
            "（存在しないグループ）",
        ]

    def test_unavailable_directory_falls_back_to_ids(self, sharing, departments):
        departments.get_many.side_effect = LookupUnavailableError("down")
        assert sharing.department_names(["plan"]) == ["plan"]


def test_readable_targets_filter_uses_bodies(sharing, store):
    """BR3.7: 索引から得た ID を本体で確かめ直す口（Manager が使う）。"""
    store.add("p1", "sub-a", {"sales"})
    store.add("p2", "sub-x")
    bodies: Mapping[str, Mapping[str, Any]] = {
        "p1": store.bodies["p1"],
        "p2": store.bodies["p2"],
    }
    assert sharing.can_read_body(MEMBER_B, PERSONA, bodies["p1"])
    assert not sharing.can_read_body(MEMBER_B, PERSONA, bodies["p2"])
    assert sharing.can_manage_body(MEMBER_A, PERSONA, bodies["p1"])
    assert not sharing.can_manage_body(MEMBER_B, PERSONA, bodies["p1"])
