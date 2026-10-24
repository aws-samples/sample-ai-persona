"""DiscussionManager（簡易議論の実行・一覧・詳細・削除・インサイト）の単体テスト（U6）。

BR1.4、BR2.1〜BR2.6、BR2.8、BR5.1、BR7.1、NFR1.43〜NFR1.47、NFR2.15〜NFR2.17、
AC2.1.2、AC2.1.3、AC4.1.1〜AC4.1.3、AC4.1.5、AC4.2.1、AC4.2.5、AC4.4.1、AC4.4.2、
AC5.1.1〜AC5.1.3。

DiscussionRecords・ParticipantReader・ResourceSharing・AccessPolicy は本物を使い、
テーブルは tests/unit/discussion_world.py の偽物、AI はモック。
"""

import json
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from src.managers.discussion_manager import DiscussionManager, DiscussionManagerError
from src.models.errors import AccessDeniedError, CodedError, ErrorCode
from src.models.insight_category import InsightCategory
from src.models.message import Message
from src.models.uploaded_file import UploadedFile
from tests.error_helpers import raises_code
from tests.unit.discussion_world import (
    ADMIN,
    NO_ROLE,
    OWNER,
    PEER,
    STRANGER,
    World,
)

pytestmark = pytest.mark.unit

TOPIC = "新商品のマーケティングについて"


def _messages(world: World) -> list[Message]:
    p1, p2 = world.persona_bodies["p1"], world.persona_bodies["p2"]
    return [
        Message.create_new(
            p1.id, p1.name, "顧客のニーズを深く理解することが大切です。" * 3
        ),
        Message.create_new(
            p2.id, p2.name, "品質と価格の釣り合いも考えたいところです。" * 3
        ),
    ]


def _insights(count: int = 3) -> list[dict]:
    return [
        {
            "category": "顧客",
            "description": f"十分に長いインサイトの説明 {i}",
            "confidence_score": 0.8,
        }
        for i in range(count)
    ]


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def ai(world) -> Mock:
    ai = Mock()
    ai.facilitate_discussion.side_effect = lambda *a, **k: _messages(world)
    ai.facilitate_discussion_streaming.side_effect = lambda *a, **k: iter(
        _messages(world)
    )
    ai.extract_insights.return_value = _insights()
    return ai


@pytest.fixture
def manager(world, ai) -> DiscussionManager:
    return DiscussionManager(
        ai_service=ai,
        file_attachments=world.files,
        discussion_records=world.records,
        participants=world.participants,
        resource_sharing=world.sharing,
        owners=world.owners,
        s3_service=Mock(),
    )


def _participants(world: World) -> list:
    return [world.persona_bodies["p1"], world.persona_bodies["p2"]]


def _events(lines) -> list[dict]:
    return [json.loads(line[len("data: ") :]) for line in lines]


def _run(manager, world, principal=OWNER, **kwargs):
    return manager.run_classic_discussion(
        principal, _participants(world), kwargs.pop("topic", TOPIC), **kwargs
    )


# =============================================================================
# 実行（BR2.8、BR7.1、BR2.6、BR1.1）
# =============================================================================


class TestRun:
    def test_saved_discussion_is_owned_by_the_runner(self, manager, world):
        discussion = _run(manager, world)
        body = world.store.bodies[discussion.id]
        assert body["owner_sub"] == OWNER.sub
        assert body["message_count"] == 2
        assert body["participant_names"] == {"p1": "佐藤", "p2": "鈴木"}
        assert len(body["insights"]) == 3

    def test_ai_execute_is_required_first(self, manager, world, ai):
        with pytest.raises(AccessDeniedError):
            _run(manager, world, principal=NO_ROLE)
        ai.facilitate_discussion.assert_not_called()

    def test_input_is_still_validated(self, manager, world):
        with raises_code(DiscussionManagerError, ErrorCode.DISCUSSION_TOPIC_TOO_SHORT):
            _run(manager, world, topic="短い")

    def test_participants_are_checked_again_before_saving(self, manager, world, ai):
        def unshare_while_running(*_a, **_k):
            world.unshare("p2")
            return _messages(world)

        ai.facilitate_discussion.side_effect = unshare_while_running
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            _run(manager, world)
        assert world.store.bodies == {}

    def test_insights_are_cut_at_100(self, manager, world, ai):
        ai.extract_insights.return_value = _insights(130)
        discussion = _run(manager, world)
        assert len(world.store.bodies[discussion.id]["insights"]) == 100

    def test_categories_are_kept_in_the_config(self, manager, world):
        categories = [InsightCategory(name="価格", description="価格への反応")]
        discussion = _run(manager, world, categories=categories)
        config = world.store.bodies[discussion.id]["agent_config"]
        assert config["insight_categories"][0]["name"] == "価格"

    def test_documents_are_read_as_the_users_and_attached(self, manager, world):
        world.files.attachable_file.return_value = UploadedFile(
            id="11111111-1111-1111-1111-111111111111",
            owner_sub=OWNER.sub,
            file_type="discussion_document",
            s3_key="uploads/a/discussion_document/x_資料.pdf",
            original_filename="資料.pdf",
            file_size=10,
            file_hash="h",
            mime_type="application/pdf",
            uploaded_at=datetime(2026, 10, 15),
            file_path="s3://bucket/uploads/a/x",
        )
        discussion = _run(
            manager, world, document_ids=["11111111-1111-1111-1111-111111111111"]
        )
        documents = world.store.bodies[discussion.id]["documents"]
        assert documents[0]["filename"] == "資料.pdf"
        assert "file_path" not in documents[0]
        world.files.attach_all.assert_called_once()

    def test_someone_elses_document_stops_before_the_ai(self, manager, world, ai):
        world.files.attachable_file.side_effect = CodedError(
            "no", code=ErrorCode.FILE_NOT_FOUND
        )
        with raises_code(CodedError, ErrorCode.FILE_NOT_FOUND):
            _run(manager, world, document_ids=["11111111-1111-1111-1111-111111111111"])
        ai.facilitate_discussion.assert_not_called()

    def test_local_document_must_exist(self, manager, world):
        world.files.attachable_file.return_value = UploadedFile(
            id="11111111-1111-1111-1111-111111111111",
            owner_sub=OWNER.sub,
            file_type="discussion_document",
            s3_key="k",
            original_filename="a.pdf",
            file_size=10,
            file_hash="h",
            mime_type="application/pdf",
            uploaded_at=datetime(2026, 10, 15),
            file_path="/nonexistent/a.pdf",
        )
        with raises_code(CodedError, ErrorCode.DISCUSSION_DOCUMENT_NOT_FOUND):
            _run(manager, world, document_ids=["11111111-1111-1111-1111-111111111111"])

    def test_total_document_size_is_limited(self, manager, world):
        world.files.attachable_file.return_value = UploadedFile(
            id="11111111-1111-1111-1111-111111111111",
            owner_sub=OWNER.sub,
            file_type="discussion_document",
            s3_key="k",
            original_filename="a.pdf",
            file_size=33 * 1024 * 1024,
            file_hash="h",
            mime_type="application/pdf",
            uploaded_at=datetime(2026, 10, 15),
            file_path="/tmp/a.pdf",
        )
        with patch("pathlib.Path.exists", return_value=True):
            with raises_code(CodedError, ErrorCode.DISCUSSION_DOCUMENTS_TOO_LARGE):
                _run(
                    manager,
                    world,
                    document_ids=["11111111-1111-1111-1111-111111111111"],
                )


class TestRunStreaming:
    def _stream(self, manager, world, principal=OWNER):
        return list(
            manager.run_classic_discussion_streaming(
                principal, _participants(world), TOPIC
            )
        )

    def test_messages_then_complete(self, manager, world):
        events = _events(self._stream(manager, world))
        assert [e["type"] for e in events] == ["message", "message", "complete"]
        assert events[-1]["discussion_id"] in world.store.bodies

    def test_unreadable_participant_stops_without_saving(self, manager, world, ai):
        def unshare_while_running(*_a, **_k):
            yield from _messages(world)
            world.unshare("p2")

        ai.facilitate_discussion_streaming.side_effect = unshare_while_running
        events = _events(self._stream(manager, world))
        assert events[-1]["type"] == "stopped"
        assert world.store.bodies == {}

    def test_ai_execute_is_required(self, manager, world):
        with pytest.raises(AccessDeniedError):
            self._stream(manager, world, principal=NO_ROLE)


# =============================================================================
# 一覧（BR2.1〜BR2.3、NFR1.43、NFR2.15）
# =============================================================================


def _seed(manager, world, principal=OWNER, mode="classic", topic=TOPIC):
    """A saved discussion of ``principal`` (written through DiscussionRecords)."""
    from src.models.discussion import Discussion

    discussion = Discussion.create_new(
        topic=topic,
        participants=["p1", "p2"],
        mode=mode,
        participant_names={"p1": "佐藤", "p2": "鈴木"},
    )
    return world.records.save_new(principal, discussion, _messages(world))


class TestList:
    def test_mine_shared_and_counts(self, manager, world):
        mine = _seed(manager, world)
        theirs = _seed(manager, world, principal=PEER)
        world.share(theirs.id, "sales")
        _seed(manager, world, principal=STRANGER)
        listing = manager.list_discussions(OWNER, "mine", None)
        assert [d.id for d in listing.items] == [mine.id]
        assert listing.counts == {"mine": 1, "shared": 1}
        shared = manager.list_discussions(OWNER, "shared", None)
        assert [d.id for d in shared.items] == [theirs.id]
        assert shared.owners[theirs.id] == f"利用者{PEER.sub}"
        assert theirs.id not in shared.manageable

    def test_member_asking_for_all_gets_mine_without_a_scan(self, manager, world):
        _seed(manager, world, principal=STRANGER)
        listing = manager.list_discussions(OWNER, "all", None)
        assert listing.tab.value == "mine" and listing.items == []
        assert "scan_page" not in world.share_store.calls

    def test_admin_sees_all(self, manager, world):
        a = _seed(manager, world)
        b = _seed(manager, world, principal=STRANGER)
        listing = manager.list_discussions(ADMIN, "all", None)
        assert {d.id for d in listing.items} == {a.id, b.id}
        assert listing.show_all

    def test_mode_filter(self, manager, world):
        _seed(manager, world, mode="classic")
        interview = _seed(manager, world, mode="interview")
        listing = manager.list_discussions(OWNER, "mine", None, mode="interview")
        assert [d.id for d in listing.items] == [interview.id]

    def test_mode_filter_reaches_older_pages(self, manager, world):
        """新しい 1 ページぶんがすべて別の種類でも、古いインタビューに届く。"""
        interview = _seed(manager, world, mode="interview")
        for _ in range(25):
            _seed(manager, world, mode="classic")
        listing = manager.list_discussions(OWNER, "mine", None, mode="interview")
        assert [d.id for d in listing.items] == [interview.id]
        assert listing.next_cursor is None

    def test_mode_filter_keeps_a_cursor_while_older_items_remain(self, manager, world):
        """読み取りの上限に達したら、空でも「もっと見る」で続きを読める。"""
        from src.managers import discussion_manager as dm

        interview = _seed(manager, world, mode="interview")
        for _ in range(45):
            _seed(manager, world, mode="classic")
        with patch.object(dm, "MODE_FILTER_MAX_PAGES", 2):
            first = manager.list_discussions(OWNER, "mine", None, mode="interview")
            assert first.items == [] and first.next_cursor is not None
            rest = manager.list_discussions(
                OWNER, "mine", first.next_cursor, mode="interview"
            )
        assert [d.id for d in rest.items] == [interview.id]

    def test_unreadable_rows_are_dropped_on_reread(self, manager, world):
        theirs = _seed(manager, world, principal=PEER)
        world.share(theirs.id, "sales")
        listing_ids = manager.list_discussions(OWNER, "shared", None)
        assert listing_ids.items
        world.store.bodies[theirs.id]["shared_group_ids"] = []
        assert manager.list_discussions(OWNER, "shared", None).items == []

    def test_search_inside_the_tab(self, manager, world):
        hit = _seed(manager, world, topic="価格の見直しについて")
        _seed(manager, world, topic="広告の出し方について")
        result = manager.list_discussions(OWNER, "mine", None, search="価格")
        assert [d.id for d in result.items] == [hit.id]
        assert result.counts == {"mine": 2}

    def test_api_list_is_mine_and_shared(self, manager, world):
        mine = _seed(manager, world)
        theirs = _seed(manager, world, principal=PEER)
        world.share(theirs.id, "sales")
        _seed(manager, world, principal=STRANGER)
        found = manager.list_accessible_discussions(OWNER)
        assert {d.id for d in found} == {mine.id, theirs.id}


# =============================================================================
# 詳細・エクスポート・資料・削除・インサイト（BR2.4、BR2.5、BR5.1、NFR1.44、NFR1.45）
# =============================================================================


class TestDetail:
    def test_reader_gets_the_view(self, manager, world):
        discussion = _seed(manager, world)
        view = manager.get_discussion(OWNER, discussion.id)
        assert len(view.messages) == 2 and view.manageable

    def test_stranger_gets_not_found(self, manager, world):
        discussion = _seed(manager, world)
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            manager.get_discussion(STRANGER, discussion.id)

    def test_export_is_markdown_for_readers(self, manager, world):
        discussion = _seed(manager, world)
        world.share(discussion.id, "sales")
        filename, text = manager.export_discussion(PEER, discussion.id)
        assert filename.endswith(".md") and TOPIC in text
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            manager.export_discussion(STRANGER, discussion.id)

    def test_document_urls_go_through_file_attachments(self, manager, world):
        discussion = _seed(manager, world)
        world.store.bodies[discussion.id]["documents"] = [
            {
                "id": "f1",
                "filename": "a.png",
                "mime_type": "image/png",
                "file_size": 1,
                "uploaded_at": "2026",
            },
            {
                "id": "f2",
                "filename": "b.png",
                "mime_type": "image/png",
                "file_size": 1,
                "uploaded_at": "2026",
            },
        ]
        uploaded = Mock(file_path="s3://bucket/k")

        def readable(principal, file_id):
            if file_id == "f2":
                raise CodedError("no", code=ErrorCode.FILE_NOT_FOUND)
            return uploaded

        world.files.readable_file.side_effect = readable
        manager._s3.generate_presigned_url.return_value = "https://signed"
        view = manager.get_discussion(OWNER, discussion.id)
        urls = manager.get_document_urls(OWNER, view)
        assert urls == {"f1": "https://signed"}


class TestDelete:
    def test_owner_deletes(self, manager, world):
        discussion = _seed(manager, world)
        assert manager.delete_discussion(OWNER, discussion.id)
        assert discussion.id not in world.store.bodies

    def test_shared_reader_is_denied(self, manager, world):
        discussion = _seed(manager, world)
        world.share(discussion.id, "sales")
        with pytest.raises(AccessDeniedError):
            manager.delete_discussion(PEER, discussion.id)

    def test_admin_deletes_anyones(self, manager, world):
        discussion = _seed(manager, world, principal=STRANGER)
        assert manager.delete_discussion(ADMIN, discussion.id)


class TestRegenerateInsights:
    def test_owner_regenerates_and_keeps_reports_and_documents(
        self, manager, world, ai
    ):
        discussion = _seed(manager, world)
        world.records.save_report(OWNER, discussion.id, "summary", "内容", None)
        ai.extract_insights.return_value = _insights(5)
        insights = manager.regenerate_insights(OWNER, discussion.id)
        assert len(insights) == 5
        body = world.store.bodies[discussion.id]
        assert len(body["insights"]) == 5 and body["report_count"] == 1

    def test_shared_reader_is_denied_before_the_ai(self, manager, world, ai):
        discussion = _seed(manager, world)
        world.share(discussion.id, "sales")
        ai.extract_insights.reset_mock()
        with pytest.raises(AccessDeniedError):
            manager.regenerate_insights(PEER, discussion.id)
        ai.extract_insights.assert_not_called()

    def test_needs_ai_execute(self, manager, world):
        discussion = _seed(manager, world)
        with pytest.raises(AccessDeniedError):
            manager.regenerate_insights(NO_ROLE, discussion.id)

    def test_no_insights_is_a_failure(self, manager, world, ai):
        discussion = _seed(manager, world)
        ai.extract_insights.return_value = []
        with raises_code(
            DiscussionManagerError, ErrorCode.DISCUSSION_INSIGHT_GENERATION_FAILED
        ):
            manager.regenerate_insights(OWNER, discussion.id)

    def test_insight_job_is_refused_before_it_is_queued(self, manager, world):
        discussion = _seed(manager, world)
        manager.check_insights_allowed(OWNER, discussion.id)
        world.share(discussion.id, "sales")
        with pytest.raises(AccessDeniedError):
            manager.check_insights_allowed(PEER, discussion.id)
        with raises_code(CodedError, ErrorCode.DISCUSSION_NOT_FOUND):
            manager.check_insights_allowed(STRANGER, discussion.id)
        with pytest.raises(AccessDeniedError):
            manager.check_insights_allowed(NO_ROLE, discussion.id)

    def test_a_legacy_discussion_is_refused_before_the_job(self, manager, world):
        """v1.0.0 より前の議論（読むだけ）は、ジョブを積む前に断る。"""
        world.store.bodies["old"] = {
            "id": "old",
            "topic": "古い議論",
            "participants": [],
            "insights": [],
            "created_at": "2026-01-01T00:00:00",
            "messages": [],
        }
        assert manager._discussion_records.can_manage(ADMIN, "old")
        with raises_code(CodedError, ErrorCode.DISCUSSION_OPERATION_FAILED):
            manager.check_insights_allowed(ADMIN, "old")

    def test_default_categories_need_no_user(self, manager):
        assert manager.get_default_categories()
