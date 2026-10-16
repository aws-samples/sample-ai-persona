"""DiscussionRecords（議論の保存・読み出し・削除の部品、ADR-006）の単体テスト。

BR1.1〜BR1.7、BR2.4〜BR2.7、BR4.4、BR5.1、BR5.2、BR7.3、BR9.1、NFR1.44、NFR1.45、
NFR1.53、NFR1.57、NFR1.58、NFR8.18、AC2.1.1、AC2.2.4、AC4.1.2、AC4.3.1、AC4.3.2。

Store は手元の偽物（失敗を差し込める）、AccessPolicy・ResourceSharing・ResourceLookup
は本物を使う（判定をモックにしない）。資料の紐付けは FileAttachments のモック。
"""

from dataclasses import replace
from datetime import datetime
from typing import Any, Sequence
from unittest.mock import Mock

import pytest

from src.managers.components.discussion_records import (
    CLEANUP_ATTEMPTS,
    DiscussionRecords,
)
from src.managers.components.participant_reader import ParticipantReader
from src.managers.components.resource_lookup import build_resource_lookup
from src.managers.components.resource_sharing import (
    ResourceSharing,
    default_sharing_tables,
)
from src.models.persona import Persona
from src.models.authz import AccessTarget, Principal, ResourceType, Role
from src.models.discussion import Discussion, DocumentRef
from src.models.discussion_report import DiscussionReport
from src.models.errors import AccessDeniedError, CodedError, ErrorCode
from src.models.insight import Insight
from src.models.message import Message
from src.models.uploaded_file import AttachResult
from src.services.discussion_store import (
    MAX_BODY_BYTES,
    DiscussionConditionFailed,
    DiscussionStore,
    DiscussionStoreError,
)
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture

pytestmark = pytest.mark.unit

DISCUSSION = ResourceType.DISCUSSION
PERSONA = ResourceType.PERSONA
OWNER = Principal(
    sub="a", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"sales"})
)
PEER = Principal(
    sub="b", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"sales"})
)
STRANGER = Principal(
    sub="c", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"plan"})
)
ADMIN = Principal(sub="admin", roles=frozenset({Role.ADMIN}))
NOW = datetime(2026, 10, 15, 9, 0)


class FakeStore:
    """In-memory stand-in for DiscussionStore with failure injection."""

    def __init__(self) -> None:
        self.bodies: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, dict[int, Any]] = {}
        self.reports: dict[str, dict[str, DiscussionReport]] = {}
        self.fail: dict[str, list[Exception]] = {}
        self.calls: list[str] = []

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        queue = self.fail.get(name)
        if queue:
            raise queue.pop(0)

    item_size = staticmethod(DiscussionStore.item_size)

    def put_new(self, body, extra_items=()):
        self._maybe_fail("put_new")
        if body["id"] in self.bodies:
            raise DiscussionConditionFailed("exists")
        self.bodies[body["id"]] = dict(body)

    def get(self, discussion_id):
        self._maybe_fail("get")
        body = self.bodies.get(discussion_id)
        return dict(body) if body else None

    def get_many(self, ids, attributes):
        self._maybe_fail("get_many")
        return {
            i: {k: v for k, v in self.bodies[i].items() if k in attributes}
            for i in ids
            if i in self.bodies
        }

    def write_messages(self, messages):
        self._maybe_fail("write_messages")
        for m in messages:
            self.messages.setdefault(m.discussion_id, {})[m.seq] = m

    def query_messages(self, discussion_id, *, up_to=None):
        self._maybe_fail("query_messages")
        rows = self.messages.get(discussion_id, {})
        return [rows[s] for s in sorted(rows) if up_to is None or s <= up_to]

    def delete_messages(self, discussion_id):
        self._maybe_fail("delete_messages")
        return len(self.messages.pop(discussion_id, {}))

    def query_reports(self, discussion_id):
        self._maybe_fail("query_reports")
        return sorted(
            self.reports.get(discussion_id, {}).values(), key=lambda r: r.created_at
        )

    def delete_reports(self, discussion_id):
        self._maybe_fail("delete_reports")
        return len(self.reports.pop(discussion_id, {}))

    def update_insights(self, discussion_id, insights, updated_at):
        self._maybe_fail("update_insights")
        if discussion_id not in self.bodies:
            raise DiscussionConditionFailed("gone")
        self.bodies[discussion_id]["insights"] = insights
        self.bodies[discussion_id]["updated_at"] = updated_at

    def advance_message_count(
        self, discussion_id, *, expected, new, updated_at, documents=None
    ):
        self._maybe_fail("advance_message_count")
        body = self.bodies.get(discussion_id)
        if body is None or body.get("message_count") != expected:
            raise DiscussionConditionFailed("moved")
        body["message_count"] = new
        body["updated_at"] = updated_at
        if documents is not None:
            body["documents"] = documents

    def put_report(self, report, *, max_reports, updated_at):
        self._maybe_fail("put_report")
        body = self.bodies.get(report.discussion_id)
        if body is None or body.get("report_count", 0) >= max_reports:
            raise DiscussionConditionFailed("limit", (1,))
        body["report_count"] = body.get("report_count", 0) + 1
        self.reports.setdefault(report.discussion_id, {})[report.report_id] = report

    def update_report(self, report, *, updated_at):
        self._maybe_fail("update_report")
        rows = self.reports.get(report.discussion_id, {})
        if report.report_id not in rows or report.discussion_id not in self.bodies:
            raise DiscussionConditionFailed("gone", (0,))
        rows[report.report_id] = report

    def delete_report(self, discussion_id, report_id, *, updated_at):
        self._maybe_fail("delete_report")
        rows = self.reports.get(discussion_id, {})
        if report_id not in rows:
            raise DiscussionConditionFailed("gone", (0,))
        del rows[report_id]
        self.bodies[discussion_id]["report_count"] -= 1


class FakeShareStore:
    """What ResourceSharing needs, reading the same bodies as FakeStore."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store

    def get_item(self, table: str, rid: str, attributes: Sequence[str]):
        body = self.store.bodies.get(rid)
        return (
            None if body is None else {k: v for k, v in body.items() if k in attributes}
        )

    def delete_with_shares(self, table, rtype, rid, *, expected, created_at, cleanup):
        del self.store.bodies[rid]


class PersonaReader:
    def __init__(self) -> None:
        self.targets = {
            "p1": AccessTarget(PERSONA, "p1", owner_sub="a"),
            "p2": AccessTarget(
                PERSONA, "p2", owner_sub="b", shared_group_ids=frozenset({"sales"})
            ),
        }

    def read(self, ids, deadline):
        return {i: self.targets[i] for i in ids if i in self.targets}


class DiscussionReader:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    def read(self, ids, deadline):
        found = {}
        for i in ids:
            body = self.store.bodies.get(i)
            if body:
                found[i] = AccessTarget(
                    DISCUSSION,
                    i,
                    owner_sub=body.get("owner_sub"),
                    shared_group_ids=frozenset(body.get("shared_group_ids") or ()),
                )
        return found


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def files() -> Mock:
    files = Mock()
    files.attach_all.side_effect = lambda p, ids, pt, pid: AttachResult(
        parent_type=pt, parent_id=pid, original_expires_at={i: 1 for i in ids}
    )
    files.delete_attached.return_value = []
    return files


@pytest.fixture
def records(store, files) -> DiscussionRecords:
    sharing = ResourceSharing(FakeShareStore(store), Mock(), sleep=lambda _s: None)
    for table in default_sharing_tables():
        sharing.register(table)
    lookup = build_resource_lookup(
        [(PERSONA, PersonaReader()), (DISCUSSION, DiscussionReader(store))],
        sample_rate=0.0,
    )
    bodies = Mock()
    # 参加者のカード（BR7.3）: 読めるペルソナの本体だけを返す
    bodies.get_personas_by_ids.side_effect = lambda ids: {
        i: _participant_persona(i) for i in ids if i in ("p1", "p2")
    }
    participants = ParticipantReader(lookup, bodies)
    records = DiscussionRecords(
        store,
        sharing,
        files,
        participants,
        clock=lambda: NOW,
        sleep=lambda _s: None,
    )
    records.persona_bodies = bodies  # type: ignore[attr-defined]
    return records


def _participant_persona(pid: str) -> Persona:
    return Persona(
        id=pid,
        name=f"本体{pid}",
        age=41,
        occupation="会社員",
        background="背景",
        values=["価値"],
        pain_points=[],
        goals=[],
        created_at=NOW,
        updated_at=NOW,
        owner_sub="owner",
    )


@pytest.fixture
def logs(capsys) -> LogCapture:
    return LogCapture(capsys)


def _discussion(**overrides: Any) -> Discussion:
    base = Discussion.create_new(
        topic="新商品",
        participants=["p1", "p2", "p3"],
        participant_names={"p1": "佐藤", "p2": "鈴木", "p3": "田中"},
        now=NOW,
    )
    return replace(base, **overrides)


def _messages(count: int = 3) -> list[Message]:
    return [Message.create_new("p1", "佐藤", f"発言 {i}") for i in range(count)]


def _doc(file_id: str = "f1") -> DocumentRef:
    return DocumentRef(file_id, "資料.pdf", "application/pdf", 10, "2026-10-15")


def _saved(records: DiscussionRecords, **overrides: Any) -> Discussion:
    return records.save_new(OWNER, _discussion(**overrides), _messages())


def _share(store: FakeStore, discussion_id: str, *groups: str) -> None:
    store.bodies[discussion_id]["shared_group_ids"] = list(groups)


# =============================================================================
# 保存（BR1.1、BR1.2、BR2.7、NFR1.57）
# =============================================================================


class TestSaveNew:
    def test_saver_owns_it_unshared_with_its_counts(self, records, store):
        saved = records.save_new(OWNER, _discussion(), _messages(3))
        body = store.bodies[saved.id]
        assert body["owner_sub"] == "a"
        assert "shared_group_ids" not in body
        assert body["message_count"] == 3
        assert [m.seq for m in store.query_messages(saved.id)] == [1, 2, 3]

    def test_order_is_attach_messages_then_body(self, records, store, files):
        records.save_new(OWNER, _discussion(documents=[_doc()]), _messages())
        files.attach_all.assert_called_once()
        assert store.calls.index("write_messages") < store.calls.index("put_new")

    def test_too_large_is_refused_before_anything_is_written(
        self, records, store, files
    ):
        huge = [Insight.create_new("c", "x" * 2000, [], 0.5) for _ in range(200)]
        with raises_code(CodedError, ErrorCode.DISCUSSION_TOO_LARGE):
            records.save_new(
                OWNER, _discussion(insights=huge, documents=[_doc()]), _messages()
            )
        files.attach_all.assert_not_called()
        assert store.messages == {} and store.bodies == {}

    def test_exactly_the_limit_is_written(self, records, store):
        discussion = _discussion()
        base = DiscussionStore.item_size(
            replace(discussion, owner_sub="a", message_count=1).to_dict()
        )
        padding = MAX_BODY_BYTES - base - len("agent_config") - 3 - len("k") - 1
        discussion = replace(discussion, agent_config={"k": "x" * padding})
        size = DiscussionStore.item_size(
            replace(discussion, owner_sub="a", message_count=1).to_dict()
        )
        assert size == MAX_BODY_BYTES
        records.save_new(OWNER, discussion, _messages(1))
        assert discussion.id in store.bodies

    def test_body_failure_detaches_and_removes_the_messages(
        self, records, store, files
    ):
        store.fail["put_new"] = [DiscussionStoreError("down")]
        with raises_code(CodedError, ErrorCode.DISCUSSION_OPERATION_FAILED):
            records.save_new(OWNER, _discussion(documents=[_doc()]), _messages())
        files.detach.assert_called_once()
        assert store.messages == {} and store.bodies == {}

    def test_an_existing_body_keeps_its_messages(self, records, store):
        """同じ ID の議論が先に保存されていたら、巻き戻しでその発言を消さない。"""
        discussion = _discussion()
        records.save_new(OWNER, discussion, _messages())
        with raises_code(CodedError, ErrorCode.DISCUSSION_OPERATION_FAILED):
            records.save_new(OWNER, discussion, _messages())
        assert len(store.messages[discussion.id]) == 3
        assert store.bodies[discussion.id]["message_count"] == 3

    def test_messages_left_behind_are_logged(self, records, store, logs):
        store.fail["put_new"] = [DiscussionStoreError("down")]
        store.fail["delete_messages"] = [DiscussionStoreError("x")] * CLEANUP_ATTEMPTS
        with raises_code(CodedError, ErrorCode.DISCUSSION_OPERATION_FAILED):
            records.save_new(OWNER, _discussion(), _messages())
        failed = logs.events("discussion.cleanup_failed")
        assert [f["step"] for f in failed] == ["rollback_messages"]
        assert failed[0]["error_type"] == "DiscussionStoreError"
        assert "発言" not in str(failed)

    def test_message_write_failure_stops_before_the_body(self, records, store, files):
        store.fail["write_messages"] = [DiscussionStoreError("down")]
        with raises_code(CodedError, ErrorCode.DISCUSSION_OPERATION_FAILED):
            records.save_new(OWNER, _discussion(documents=[_doc()]), _messages())
        assert store.bodies == {}
        files.detach.assert_called_once()

    def test_attach_refusal_keeps_its_code(self, records, store, files):
        files.attach_all.side_effect = CodedError("x", code=ErrorCode.FILE_NOT_FOUND)
        with raises_code(CodedError, ErrorCode.FILE_NOT_FOUND):
            records.save_new(OWNER, _discussion(documents=[_doc()]), _messages())
        assert store.messages == {}

    def test_without_a_user_nothing_is_saved(self, records, store):
        with raises_code(CodedError, ErrorCode.DISCUSSION_OPERATION_FAILED):
            records.save_new(Principal(sub=""), _discussion(), _messages())
        assert store.bodies == {}


# =============================================================================
# 読み出し（BR1.3、BR1.7、BR2.4、BR7.3、NFR1.44）
# =============================================================================


class TestGet:
    def test_owner_reads_messages_reports_and_participants(self, records, store):
        saved = _saved(records)
        view = records.get(OWNER, saved.id)
        assert [m.content for m in view.messages] == ["発言 0", "発言 1", "発言 2"]
        assert view.manageable
        assert [
            (p.persona_id, p.display_name, p.status) for p in view.participants
        ] == [
            ("p1", "佐藤", "readable"),
            ("p2", "鈴木", "readable"),
            ("p3", "田中", "deleted"),
        ]

    def test_readable_participants_carry_their_bodies(self, records, store):
        saved = _saved(records)
        view = records.get(OWNER, saved.id)
        cards = {p.persona_id: p.persona for p in view.participants}
        assert cards["p1"] is not None and cards["p1"].occupation == "会社員"
        assert cards["p2"] is not None
        assert cards["p3"] is None
        records.persona_bodies.get_personas_by_ids.assert_called_once_with(["p1", "p2"])

    def test_unreadable_participants_bodies_are_not_read(self, records, store):
        saved = _saved(records)
        _share(store, saved.id, "sales")
        view = records.get(PEER, saved.id)
        readable = [p.persona_id for p in view.participants if p.status == "readable"]
        assert "p1" not in readable
        for p in view.participants:
            assert (p.persona is not None) == (p.status == "readable")
        (ids,), _ = records.persona_bodies.get_personas_by_ids.call_args
        assert ids == readable

    def test_shared_reader_sees_it_but_cannot_manage(self, records, store):
        saved = _saved(records)
        _share(store, saved.id, "sales")
        view = records.get(PEER, saved.id)
        assert not view.manageable
        statuses = {p.persona_id: p.status for p in view.participants}
        assert statuses["p1"] == "no_access"

    def test_stranger_gets_not_found_and_no_messages_are_read(self, records, store):
        saved = _saved(records)
        store.calls.clear()
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            records.get(STRANGER, saved.id)
        assert "query_messages" not in store.calls

    def test_missing_is_the_same_not_found(self, records):
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            records.get(OWNER, "missing")

    def test_store_failure_is_503(self, records, store):
        from src.models.errors import LookupUnavailableError

        store.fail["get"] = [DiscussionStoreError("down")]
        with pytest.raises(LookupUnavailableError):
            records.get(OWNER, "x")

    def test_only_message_count_messages_are_read(self, records, store):
        saved = _saved(records)
        store.write_messages(
            [
                replace(store.messages[saved.id][1], seq=99),
            ]
        )
        assert len(records.get(OWNER, saved.id).messages) == 3

    def test_legacy_discussion_is_admin_only_and_shows_its_inner_messages(
        self, records, store
    ):
        store.bodies["old"] = {
            "id": "old",
            "topic": "古い",
            "participants": ["p1"],
            "insights": [],
            "created_at": "2026-01-01T00:00:00",
            "messages": [
                {
                    "persona_id": "p1",
                    "persona_name": "佐藤",
                    "content": "昔の発言",
                    "timestamp": "2026-01-01T00:00:00",
                    "message_type": "statement",
                    "round_number": None,
                }
            ],
            "reports": [
                {
                    "id": "r1",
                    "template_type": "summary",
                    "content": "まとめ",
                    "created_at": "2026-01-01T00:00:00",
                }
            ],
        }
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            records.get(OWNER, "old")
        view = records.get(ADMIN, "old")
        assert [m.content for m in view.messages] == ["昔の発言"]
        assert view.reports[0].report_id == "r1"
        assert view.participants[0].display_name == "佐藤"


# =============================================================================
# 部分更新（BR1.4、BR2.5、BR2.6）
# =============================================================================


class TestUpdateInsights:
    def test_only_insights_change(self, records, store):
        saved = _saved(records, documents=[_doc()])
        store.bodies[saved.id]["report_count"] = 2
        records.update_insights(
            OWNER, saved.id, [Insight.create_new("c", "説明", [], 0.3)]
        )
        body = store.bodies[saved.id]
        assert body["insights"][0]["description"] == "説明"
        assert body["report_count"] == 2
        assert body["documents"]

    def test_insights_are_cut_at_100(self, records, store):
        saved = _saved(records)
        many = [Insight.create_new("c", f"説明 {i}", [], 0.3) for i in range(150)]
        updated = records.update_insights(OWNER, saved.id, many)
        assert len(updated.insights) == 100
        assert len(store.bodies[saved.id]["insights"]) == 100

    def test_shared_reader_is_denied(self, records, store):
        saved = _saved(records)
        _share(store, saved.id, "sales")
        with pytest.raises(AccessDeniedError):
            records.update_insights(PEER, saved.id, [])

    def test_stranger_is_not_found(self, records):
        saved = _saved(records)
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            records.update_insights(STRANGER, saved.id, [])

    def test_legacy_shape_is_refused(self, records, store):
        store.bodies["old"] = {
            "id": "old",
            "topic": "古い",
            "participants": [],
            "insights": [],
            "created_at": "2026-01-01T00:00:00",
            "messages": [],
        }
        with raises_code(CodedError, ErrorCode.DISCUSSION_OPERATION_FAILED):
            records.update_insights(ADMIN, "old", [])

    def test_vanished_body_is_not_found(self, records, store):
        saved = _saved(records)
        store.fail["update_insights"] = [DiscussionConditionFailed("gone")]
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            records.update_insights(OWNER, saved.id, [])


# =============================================================================
# 削除と後始末（BR1.5、BR1.6、NFR1.58、NFR8.18）
# =============================================================================


class TestDelete:
    def test_body_then_messages_reports_and_files(self, records, store, files):
        saved = _saved(records, documents=[_doc()])
        records.save_report(OWNER, saved.id, "summary", "内容", None)
        assert records.delete(OWNER, saved.id) is True
        assert saved.id not in store.bodies
        assert saved.id not in store.messages and saved.id not in store.reports
        files.delete_attached.assert_called_once_with("discussion", saved.id, ["f1"])

    def test_shared_reader_cannot_delete(self, records, store):
        saved = _saved(records)
        _share(store, saved.id, "sales")
        with pytest.raises(AccessDeniedError):
            records.delete(PEER, saved.id)
        assert saved.id in store.bodies

    def test_stranger_cannot_delete(self, records, store):
        saved = _saved(records)
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            records.delete(STRANGER, saved.id)

    def test_missing_is_not_found(self, records):
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            records.delete(OWNER, "missing")

    def test_cleanup_is_retried_three_times_then_logged_per_step(
        self, records, store, files, logs
    ):
        saved = _saved(records, documents=[_doc()])
        store.fail["delete_messages"] = [DiscussionStoreError("x")] * 3
        store.fail["delete_reports"] = [DiscussionStoreError("x")]
        files.delete_attached.return_value = ["f1"]
        assert records.delete(OWNER, saved.id) is True
        failed = logs.events("discussion.cleanup_failed")
        assert {f["step"] for f in failed} == {"messages", "files"}
        assert all(f["discussion_id"] == saved.id for f in failed)
        assert files.delete_attached.call_count == CLEANUP_ATTEMPTS

    def test_admin_deletes_a_legacy_discussion(self, records, store):
        store.bodies["old"] = {
            "id": "old",
            "topic": "古い",
            "participants": [],
            "insights": [],
            "created_at": "2026-01-01T00:00:00",
            "messages": [],
        }
        assert records.delete(ADMIN, "old") is True
        assert "old" not in store.bodies


# =============================================================================
# 書き足し（BR3.8）
# =============================================================================


class TestAppendMessages:
    def test_messages_and_new_files_are_added(self, records, store, files):
        saved = _saved(records, documents=[_doc("f1")])
        files.attach_all.reset_mock()
        updated = records.append_messages(
            OWNER, saved.id, _messages(2), [_doc("f1"), _doc("f2")]
        )
        assert updated.message_count == 5
        assert [m.seq for m in store.query_messages(saved.id)] == [1, 2, 3, 4, 5]
        files.attach_all.assert_called_once_with(OWNER, ["f2"], "discussion", saved.id)
        assert [d["id"] for d in store.bodies[saved.id]["documents"]] == ["f1", "f2"]

    def test_whole_conversation_skips_what_is_saved(self, records, store):
        saved = _saved(records)
        count = store.bodies[saved.id]["message_count"]
        updated = records.append_messages(
            OWNER, saved.id, _messages(count + 2), [], skip_saved=True
        )
        assert updated.message_count == count + 2
        assert len(store.query_messages(saved.id)) == count + 2

    def test_moved_count_is_a_failure_to_retry(self, records, store, files):
        saved = _saved(records)
        store.fail["advance_message_count"] = [DiscussionConditionFailed("moved")]
        with raises_code(CodedError, ErrorCode.DISCUSSION_OPERATION_FAILED):
            records.append_messages(OWNER, saved.id, _messages(1), [_doc("f9")])
        files.detach.assert_called_once()

    def test_deleted_discussion_is_not_found(self, records, store):
        saved = _saved(records)
        del store.bodies[saved.id]
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            records.append_messages(OWNER, saved.id, _messages(1), [])

    def test_shared_reader_cannot_append(self, records, store):
        saved = _saved(records)
        _share(store, saved.id, "sales")
        with pytest.raises(AccessDeniedError):
            records.append_messages(PEER, saved.id, _messages(1), [])


# =============================================================================
# レポート（BR4.2、BR4.4）
# =============================================================================


class TestReports:
    def test_owner_saves_with_a_server_id(self, records, store):
        saved = _saved(records)
        report = records.save_report(OWNER, saved.id, "summary", "内容", None)
        assert report.report_id
        assert store.bodies[saved.id]["report_count"] == 1

    def test_fourth_report_is_the_limit(self, records, store):
        saved = _saved(records)
        for _ in range(3):
            records.save_report(OWNER, saved.id, "summary", "内容", None)
        with raises_code(CodedError, ErrorCode.REPORT_LIMIT_REACHED, max_reports=3):
            records.save_report(OWNER, saved.id, "summary", "内容", None)

    def test_race_on_the_last_slot_is_the_limit(self, records, store):
        saved = _saved(records)
        store.fail["put_report"] = [DiscussionConditionFailed("limit", (1,))]
        with raises_code(CodedError, ErrorCode.REPORT_LIMIT_REACHED):
            records.save_report(OWNER, saved.id, "summary", "内容", None)

    def test_shared_reader_cannot_save(self, records, store):
        saved = _saved(records)
        _share(store, saved.id, "sales")
        with pytest.raises(AccessDeniedError):
            records.save_report(PEER, saved.id, "summary", "内容", None)

    def test_update_and_delete(self, records, store):
        saved = _saved(records)
        report = records.save_report(OWNER, saved.id, "custom", "古い", "指示")
        records.update_report(OWNER, saved.id, report.report_id, "新しい")
        assert records.get_report(OWNER, saved.id, report.report_id).content == "新しい"
        records.delete_report(OWNER, saved.id, report.report_id)
        with raises_code(CodedError, ErrorCode.REPORT_NOT_FOUND):
            records.get_report(OWNER, saved.id, report.report_id)

    def test_missing_report_is_report_not_found(self, records):
        saved = _saved(records)
        with raises_code(CodedError, ErrorCode.REPORT_NOT_FOUND):
            records.update_report(OWNER, saved.id, "nope", "x")
        with raises_code(CodedError, ErrorCode.REPORT_NOT_FOUND):
            records.delete_report(OWNER, saved.id, "nope")

    def test_report_of_an_unreadable_discussion_is_discussion_not_found(self, records):
        saved = _saved(records)
        report = records.save_report(OWNER, saved.id, "summary", "内容", None)
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            records.get_report(STRANGER, saved.id, report.report_id)


# =============================================================================
# 一覧の本体（NFR2.15）とエクスポート（BR9.1）
# =============================================================================


class TestListBodies:
    def test_rereads_and_drops_unreadable(self, records, store):
        mine = _saved(records)
        other = records.save_new(STRANGER, _discussion(), _messages(1))
        found = records.list_bodies(OWNER, [mine.id, other.id, "gone"])
        assert [d.id for d in found] == [mine.id]
        assert store.calls.count("get_many") == 1

    def test_mode_filter(self, records):
        classic = _saved(records)
        agent = records.save_new(OWNER, _discussion(mode="agent"), _messages(1))
        found = records.list_bodies(OWNER, [classic.id, agent.id], mode="agent")
        assert [d.id for d in found] == [agent.id]


class TestExport:
    def test_markdown_has_the_parts_without_owner_or_shares(self, records, store):
        saved = records.save_new(
            OWNER,
            _discussion(
                topic="テーマ *強調* と\n改行",
                documents=[_doc()],
                insights=[Insight.create_new("顧客", "大事な気づき", [], 0.8)],
            ),
            _messages(2),
        )
        _share(store, saved.id, "sales")
        records.save_report(OWNER, saved.id, "summary", "# レポート本文", None)
        filename, text = records.export_markdown(OWNER, saved.id)
        assert filename.endswith(".md")
        assert "テーマ \\*強調\\* と 改行" in text
        assert "発言 0" in text and "大事な気づき" in text and "レポート本文" in text
        assert "資料.pdf" in text
        assert "〔削除済み〕" in text  # p3
        assert "sales" not in text and OWNER.sub not in text.split("\n")[0]
        assert "owner_sub" not in text

    def test_export_needs_read(self, records):
        saved = _saved(records)
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            records.export_markdown(STRANGER, saved.id)
