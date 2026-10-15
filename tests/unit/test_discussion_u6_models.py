"""U6 の型の単体テスト（entities.md、BR1.7、BR2.6、BR2.7、BR3.5、BR6.1、BR8.3）。

議論の本体は発言・レポートを持たず、所有者・共有先・件数を持つ。インタビューの
セッションはアプリの外に置く形（持ち主、番号、応答中の印、期限）になる。
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.models.authz import (
    AccessTarget,
    ExecutionContext,
    Principal,
    ResourceType,
    Role,
)
from src.models.discussion import (
    MAX_INSIGHTS,
    Discussion,
    DiscussionView,
    DocumentRef,
    ParticipantView,
    legacy_messages,
    legacy_reports,
)
from src.models.discussion_report import DiscussionReport
from src.models.insight import Insight
from src.models.interview_session import (
    REPLY_TIMEOUT_SECONDS,
    InterviewMessage,
    InterviewSession,
)
from src.models.job import Job, JobStatus
from src.models.message import DiscussionMessage, Message

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)


def _insight(n: int = 0) -> Insight:
    return Insight.create_new("顧客", f"インサイトの説明 {n} です", [], 0.8)


def _discussion(**overrides: object) -> Discussion:
    base = Discussion.create_new(
        topic="新商品について",
        participants=["p1", "p2"],
        mode="classic",
        owner_sub="user-a",
        participant_names={"p1": "佐藤", "p2": "鈴木"},
        now=_NOW,
    )
    from dataclasses import replace

    return replace(base, **overrides)  # type: ignore[arg-type]


# =============================================================================
# Discussion
# =============================================================================


class TestDiscussionShape:
    def test_has_no_messages_or_reports(self):
        d = _discussion()
        assert not hasattr(d, "messages")
        assert not hasattr(d, "reports")

    def test_new_discussion_is_owned_and_unshared(self):
        d = _discussion()
        assert d.owner_sub == "user-a"
        assert d.shared_group_ids == frozenset()
        assert d.message_count == 0
        assert d.report_count == 0
        assert d.participant_names == {"p1": "佐藤", "p2": "鈴木"}

    def test_create_new_accepts_a_preset_id(self):
        d = Discussion.create_new(
            topic="t", participants=["p1"], mode="agent", discussion_id="fixed-id"
        )
        assert d.id == "fixed-id"

    def test_unknown_mode_is_refused(self):
        with pytest.raises(ValueError):
            Discussion.create_new(topic="t", participants=["p1"], mode="other")

    def test_with_insights_returns_a_new_instance(self):
        d = _discussion()
        updated = d.with_insights([_insight()], now=_NOW)
        assert updated is not d
        assert d.insights == []
        assert len(updated.insights) == 1
        assert updated.updated_at == _NOW

    def test_max_insights_is_100(self):
        assert MAX_INSIGHTS == 100


class TestDiscussionToDict:
    def test_none_fields_are_left_out(self):
        data = _discussion().to_dict()
        for name in ("agent_config", "updated_at", "shared_group_ids"):
            assert name not in data
        assert all(v is not None for v in data.values())

    def test_no_messages_or_reports_are_written(self):
        data = _discussion().to_dict()
        assert "messages" not in data
        assert "reports" not in data

    def test_counts_and_owner_are_written(self):
        data = _discussion(message_count=12, report_count=2).to_dict()
        assert data["message_count"] == 12
        assert data["report_count"] == 2
        assert data["owner_sub"] == "user-a"
        assert data["participant_names"] == {"p1": "佐藤", "p2": "鈴木"}

    def test_shares_are_a_sorted_list(self):
        data = _discussion(shared_group_ids=frozenset({"b", "a"})).to_dict()
        assert data["shared_group_ids"] == ["a", "b"]

    def test_documents_have_no_file_path(self):
        doc = DocumentRef(
            id="f1",
            filename="a.pdf",
            mime_type="application/pdf",
            file_size=10,
            uploaded_at="2026-10-15T09:00:00",
        )
        data = _discussion(documents=[doc]).to_dict()
        assert data["documents"] == [doc.to_dict()]
        assert "file_path" not in data["documents"][0]

    def test_round_trip(self):
        d = _discussion(
            insights=[_insight()],
            message_count=3,
            report_count=1,
            shared_group_ids=frozenset({"sales"}),
            updated_at=_NOW,
            agent_config={"rounds": 3},
        )
        assert Discussion.from_dict(d.to_dict()) == d


class TestDiscussionFromLegacyItems:
    """v1.0.0 より前の議論（中に発言・レポートを持ち、所有者がない、BR1.7）。"""

    _LEGACY = {
        "id": "old-1",
        "topic": "古い議論",
        "participants": ["p1", "p2"],
        "messages": [
            {
                "persona_id": "p1",
                "persona_name": "佐藤",
                "content": "こんにちは",
                "timestamp": "2026-01-01T10:00:00",
                "message_type": "statement",
                "round_number": None,
            }
        ],
        "insights": [],
        "created_at": "2026-01-01T10:00:00",
        "mode": "classic",
        "type": "discussion",
        "reports": [
            {
                "id": "r1",
                "template_type": "summary",
                "content": "まとめ",
                "created_at": "2026-01-01T11:00:00",
            }
        ],
        "documents": [
            {
                "id": "f1",
                "filename": "a.pdf",
                "file_path": "s3://bucket/a.pdf",
                "file_size": 10,
                "mime_type": "application/pdf",
                "uploaded_at": "2026-01-01T09:00:00",
            }
        ],
    }

    def test_reads_the_old_shape(self):
        d = Discussion.from_dict(dict(self._LEGACY))
        assert d.owner_sub is None
        assert d.is_legacy is True
        assert d.message_count is None
        assert d.report_count == 1
        assert d.documents and d.documents[0].id == "f1"

    def test_legacy_messages_and_reports_are_read_for_display(self):
        messages = legacy_messages(self._LEGACY)
        reports = legacy_reports(self._LEGACY, "old-1")
        assert [m.content for m in messages] == ["こんにちは"]
        assert reports[0].report_id == "r1"
        assert reports[0].discussion_id == "old-1"

    def test_participant_names_fall_back_to_the_message_names(self):
        d = Discussion.from_dict(dict(self._LEGACY))
        assert d.participant_names == {"p1": "佐藤"}

    def test_new_item_is_not_legacy(self):
        assert _discussion().is_legacy is False

    def test_missing_legacy_lists_read_as_empty(self):
        assert legacy_messages({"id": "x"}) == []
        assert legacy_reports({"id": "x"}, "x") == []


# =============================================================================
# DiscussionMessage / DiscussionReport / DocumentRef / ParticipantView
# =============================================================================


class TestDiscussionMessage:
    def test_from_message_keeps_the_name_at_the_time(self):
        message = Message.create_new("p1", "佐藤", "発言", round_number=2)
        stored = DiscussionMessage.from_message("d1", 1, message)
        assert stored.discussion_id == "d1"
        assert stored.seq == 1
        assert stored.persona_name_at_time == "佐藤"
        assert stored.round_number == 2
        back = stored.to_message()
        assert back.persona_name == "佐藤"
        assert back.content == "発言"

    def test_seq_starts_at_one(self):
        message = Message.create_new("p1", "佐藤", "発言")
        with pytest.raises(ValueError):
            DiscussionMessage.from_message("d1", 0, message)

    def test_unknown_message_type_is_refused(self):
        message = Message.create_new("p1", "佐藤", "発言", message_type="shout")
        with pytest.raises(ValueError):
            DiscussionMessage.from_message("d1", 1, message)

    def test_round_trip(self):
        message = Message.create_new("user", "あなた", "質問", "user_message")
        stored = DiscussionMessage.from_message("d1", 3, message)
        assert DiscussionMessage.from_dict(stored.to_dict()) == stored


class TestDiscussionReport:
    def test_server_assigns_the_id(self):
        a = DiscussionReport.create_new("d1", "summary", "内容", now=_NOW)
        b = DiscussionReport.create_new("d1", "summary", "内容", now=_NOW)
        assert a.report_id != b.report_id
        assert a.id == a.report_id

    def test_unknown_template_is_refused(self):
        with pytest.raises(ValueError):
            DiscussionReport.create_new("d1", "poem", "内容")

    def test_with_content_sets_updated_at(self):
        report = DiscussionReport.create_new("d1", "custom", "古い", "指示", now=_NOW)
        later = _NOW + timedelta(minutes=1)
        updated = report.with_content("新しい", now=later)
        assert updated.content == "新しい"
        assert updated.updated_at == later
        assert report.content == "古い"

    def test_round_trip_and_no_none(self):
        report = DiscussionReport.create_new("d1", "data_driven", "内容", now=_NOW)
        data = report.to_dict()
        assert "custom_prompt" not in data and "updated_at" not in data
        assert DiscussionReport.from_dict(data) == report


class TestDocumentRef:
    def test_from_dict_drops_the_s3_key(self):
        ref = DocumentRef.from_dict(
            {
                "id": "f1",
                "filename": "a.png",
                "file_path": "s3://b/k",
                "file_size": 3,
                "mime_type": "image/png",
                "uploaded_at": "2026-10-15T09:00:00",
            }
        )
        assert not hasattr(ref, "file_path")
        assert ref.to_dict()["filename"] == "a.png"

    def test_negative_size_is_refused(self):
        with pytest.raises(ValueError):
            DocumentRef("f1", "a.png", "image/png", -1, "2026-10-15")


class TestParticipantView:
    @pytest.mark.parametrize(
        ("status", "linkable"),
        [("readable", True), ("no_access", False), ("deleted", False)],
    )
    def test_only_readable_is_a_link(self, status, linkable):
        view = ParticipantView("p1", "佐藤", status)
        assert view.linkable is linkable

    def test_unknown_status_is_refused(self):
        with pytest.raises(ValueError):
            ParticipantView("p1", "佐藤", "hidden")

    def test_view_bundles_the_parts(self):
        d = _discussion()
        view = DiscussionView(
            discussion=d,
            messages=[],
            reports=[],
            participants=[ParticipantView("p1", "佐藤", "readable")],
            manageable=True,
        )
        assert view.discussion is d and view.manageable


# =============================================================================
# InterviewSession / InterviewMessage
# =============================================================================


def _session(**overrides: object) -> InterviewSession:
    session = InterviewSession.create_new(
        owner_sub="user-a",
        persona_ids=["p1", "p2"],
        persona_names={"p1": "佐藤", "p2": "鈴木"},
        memory_mode="full",
        now=_NOW,
    )
    from dataclasses import replace

    return replace(session, **overrides)  # type: ignore[arg-type]


class TestInterviewSession:
    def test_new_session_belongs_to_its_starter(self):
        s = _session()
        assert s.owner_sub == "user-a"
        assert s.last_seq == 0
        assert s.reply_token is None
        assert s.saved_discussion_id is None
        assert not hasattr(s, "messages")
        assert not hasattr(s, "is_saved")

    def test_expires_eight_hours_after_the_last_action(self):
        s = _session()
        assert s.expires_at == int((_NOW + timedelta(hours=8)).timestamp())
        assert not s.is_expired(_NOW + timedelta(hours=7, minutes=59))
        assert s.is_expired(_NOW + timedelta(hours=8))

    def test_seven_days_is_the_hard_limit(self):
        s = _session()
        late = _NOW + timedelta(days=6, hours=23)
        touched = s.touched(late)
        assert touched.expires_at == int((_NOW + timedelta(days=7)).timestamp())
        assert touched.is_expired(_NOW + timedelta(days=7))
        assert not touched.is_expired(_NOW + timedelta(days=6, hours=23, minutes=30))

    def test_reply_mark_is_stale_after_five_minutes(self):
        s = _session(reply_token="t", reply_started_at=_NOW)
        assert REPLY_TIMEOUT_SECONDS == 300
        assert s.reply_in_progress(_NOW + timedelta(minutes=4, seconds=59))
        assert not s.reply_in_progress(_NOW + timedelta(minutes=5))
        assert not _session().reply_in_progress(_NOW)

    def test_unknown_memory_mode_is_refused(self):
        with pytest.raises(ValueError):
            InterviewSession.create_new(
                owner_sub="u",
                persona_ids=["p1"],
                persona_names={"p1": "佐藤"},
                memory_mode="forever",
                now=_NOW,
            )

    def test_round_trip_and_no_none(self):
        s = _session(
            reply_token="t",
            reply_started_at=_NOW,
            saved_discussion_id="d1",
            document_ids=["f1"],
            persona_models={"p1": "m"},
        )
        data = s.to_dict()
        assert all(v is not None for v in data.values())
        assert InterviewSession.from_dict(data) == s
        assert "reply_token" not in _session().to_dict()


class TestInterviewMessage:
    def test_expires_eight_days_after_it_was_written(self):
        m = InterviewMessage.create_new(
            session_id="s1",
            seq=1,
            speaker="user",
            persona_id="user",
            persona_name_at_time="あなた",
            content="質問",
            now=_NOW,
        )
        assert m.expires_at == int((_NOW + timedelta(days=8)).timestamp())

    def test_speaker_and_seq_are_checked(self):
        with pytest.raises(ValueError):
            InterviewMessage.create_new(
                session_id="s1",
                seq=0,
                speaker="user",
                persona_id="user",
                persona_name_at_time="あなた",
                content="質問",
                now=_NOW,
            )
        with pytest.raises(ValueError):
            InterviewMessage.create_new(
                session_id="s1",
                seq=1,
                speaker="robot",
                persona_id="x",
                persona_name_at_time="x",
                content="x",
                now=_NOW,
            )

    def test_to_message_marks_the_user(self):
        m = InterviewMessage.create_new(
            session_id="s1",
            seq=2,
            speaker="persona",
            persona_id="p1",
            persona_name_at_time="佐藤",
            content="答え",
            now=_NOW,
            document_ids=("f1",),
        )
        assert m.to_message().message_type == "statement"
        assert InterviewMessage.from_dict(m.to_dict()) == m


# =============================================================================
# ExecutionContext（約束 C1）と Job.error_code（BR8.3）
# =============================================================================


_MEMBER = Principal(sub="user-a", roles=frozenset({Role.MEMBER}))
_ADMIN = Principal(sub="admin-1", roles=frozenset({Role.ADMIN}))


def _persona(owner: str | None) -> AccessTarget:
    return AccessTarget(ResourceType.PERSONA, "p1", owner_sub=owner)


class TestExecutionContext:
    def test_owner_writes_memory(self):
        context = ExecutionContext.for_persona(_MEMBER, _persona("user-a"))
        assert context.write_memory is True
        assert context.principal is _MEMBER

    def test_admin_on_someone_elses_persona_does_not(self):
        assert not ExecutionContext.for_persona(_ADMIN, _persona("user-a")).write_memory

    def test_ownerless_persona_does_not(self):
        assert not ExecutionContext.for_persona(_MEMBER, _persona(None)).write_memory

    def test_empty_sub_does_not(self):
        nobody = Principal(sub="")
        assert not ExecutionContext.for_persona(nobody, _persona("")).write_memory


class TestJobErrorCode:
    def test_failed_job_keeps_only_a_code(self):
        job = Job(
            id="j1",
            status=JobStatus.FAILED,
            created_at=datetime(2026, 10, 15),
            error_code="discussion_operation_failed",
        )
        data = job.to_dict()
        assert data["error_code"] == "discussion_operation_failed"
        assert "error" not in data
        assert Job.from_dict(data) == job

    def test_old_error_text_is_dropped(self):
        job = Job.from_dict(
            {
                "id": "j1",
                "status": "failed",
                "created_at": "2026-10-15T00:00:00",
                "updated_at": "2026-10-15T00:00:00",
                "error": "botocore said s3://secret/path",
            }
        )
        assert job.error_code == "unknown"
        assert not hasattr(job, "error")
