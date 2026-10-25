"""本体のない発言・レポートと、親のないファイルを見つけるスクリプトの結合テスト（moto）。

BR1.6、NFR1.58。議論の削除は本体と共有を先に消し、発言・レポート・資料の後始末を
3 回までやり直す。それでも残ったものをこのスクリプトが見つける。既定は件数を
出すだけで何も消さない。--apply で消す。
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from scripts.cleanup_discussion_orphans import (  # noqa: E402
    apply_plan,
    main,
    plan_cleanup,
)
from src.models.discussion import Discussion  # noqa: E402
from src.models.discussion_report import DiscussionReport  # noqa: E402
from src.models.message import DiscussionMessage, Message  # noqa: E402
from src.models.uploaded_file import UploadedFile  # noqa: E402
from src.services.stores.uploaded_file_store import UploadedFileStore  # noqa: E402
from tests.integration.test_discussion_store import (  # noqa: E402
    REGION,
    create_tables,
    make_store,
)

FILES = "UploadedFilesCleanupTest"
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
#: The store writes messages at NOW (``written_at``); the script runs two hours later.
WRITTEN = NOW.timestamp()
LATER = WRITTEN + 2 * 3600
#: Thirty minutes after the writes: still inside the one-hour grace.
SOON = WRITTEN + 30 * 60


@pytest.fixture
def ddb(monkeypatch) -> Any:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        create_tables(client)
        client.create_table(
            TableName=FILES,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


@pytest.fixture
def stores(ddb) -> tuple[Any, UploadedFileStore]:
    return make_store(ddb, clock=lambda: WRITTEN), UploadedFileStore(
        region=REGION, table_name=FILES, client=ddb
    )


def _discussion(store: Any, *, body: bool = True, reports: int = 1) -> str:
    discussion = Discussion.create_new(
        topic="後始末のテスト", participants=["p1", "p2"], mode="classic"
    )
    if body:
        store.put_new(discussion.to_dict())
    store.write_messages(
        [
            DiscussionMessage.from_message(
                discussion.id, seq, Message.create_new("p1", "佐藤", "発言")
            )
            for seq in (1, 2)
        ]
    )
    for i in range(reports):
        report = DiscussionReport.create_new(discussion.id, "summary", "内容", now=NOW)
        if body:
            store.put_report(report, max_reports=3, updated_at=NOW.isoformat())
            continue
        # 本体が先に消えた議論のレポート（put_report は本体の件数を条件にする）
        store.client.put_item(
            TableName=store.reports_table,
            Item={
                "discussion_id": {"S": discussion.id},
                "report_id": {"S": f"r-orphan-{i}"},
                "template_type": {"S": "summary"},
                "content": {"S": "内容"},
                "created_at": {"S": NOW.isoformat()},
            },
        )
    return discussion.id


def _file(
    files: UploadedFileStore, parent_id: str, *, uploaded_at: datetime = NOW
) -> UploadedFile:
    uploaded = (
        UploadedFile.create_new(
            owner_sub="a",
            file_type="discussion_document",
            original_filename="資料.pdf",
            file_size=1,
            file_hash="h",
            mime_type="application/pdf",
            now=uploaded_at,
        )
        .with_path("s3://bucket/uploads/a/discussion_document/x.pdf")
        .attached_to("discussion", parent_id)
    )
    files.put(uploaded)
    return uploaded


def _attach_now(files: UploadedFileStore, parent_id: str, when: float) -> str:
    """An old upload attached at ``when`` through the real attach transaction."""
    uploaded = UploadedFile.create_new(
        owner_sub="a",
        file_type="discussion_document",
        original_filename="古い資料.pdf",
        file_size=1,
        file_hash="h",
        mime_type="application/pdf",
        now=NOW - timedelta(hours=20),
    ).with_path("s3://bucket/uploads/a/discussion_document/y.pdf")
    files.put(uploaded)
    files.transact_attach("a", [uploaded.id], "discussion", parent_id, int(when))
    return uploaded.id


class TestPlan:
    def test_orphans_are_found_and_live_ones_are_not(self, stores):
        store, files = stores
        live = _discussion(store)
        gone = _discussion(store, body=False)
        live_file = _file(files, live)
        orphan_file = _file(files, gone)

        plan = plan_cleanup(store, files, now=LATER)

        assert plan.messages == {gone: 2}
        assert plan.reports == {gone: 1}
        assert [f.id for f in plan.files] == [orphan_file.id]
        assert live_file.id not in [f.id for f in plan.files]
        assert plan.recent == []
        assert not plan.is_clean

    def test_nothing_to_clean(self, stores):
        store, files = stores
        _discussion(store)
        assert plan_cleanup(store, files, now=LATER).is_clean

    def test_a_discussion_being_saved_is_left_alone(self, stores):
        """保存の途中（発言と資料は書いた、本体はまだ）の議論は 1 時間は対象にしない。"""
        store, files = stores
        saving = _discussion(store, body=False, reports=0)
        _file(files, saving)

        plan = plan_cleanup(store, files, now=SOON)

        assert plan.messages == {} and plan.files == []
        assert plan.recent == [saving]
        assert plan.is_clean

    def test_a_file_attached_just_now_keeps_its_discussion(self, stores):
        """発言を書く前（資料の紐付けだけ済んだ）: 古いアップロードでも紐付けの時刻で見る。"""
        store, files = stores
        saving = "d-attach-only"
        file_id = _attach_now(files, saving, SOON)

        plan = plan_cleanup(store, files, now=SOON + 60)

        assert plan.files == [] and plan.recent == [saving]
        assert files.get(file_id) is not None
        # 1 時間を過ぎても本体がなければ、そのときは対象になる
        later = plan_cleanup(store, files, now=SOON + 2 * 3600)
        assert [f.id for f in later.files] == [file_id]

    def test_old_messages_written_recently_are_left_alone(self, ddb):
        """インタビューの保存は古い発言を移す。発言の時刻ではなく書いた時刻で見る。"""
        store = make_store(ddb, clock=lambda: LATER)
        files = UploadedFileStore(region=REGION, table_name=FILES, client=ddb)
        saving = _discussion(store, body=False, reports=0)  # created_at は NOW

        plan = plan_cleanup(store, files, now=LATER + 60)

        assert plan.messages == {} and plan.recent == [saving]


class TestRun:
    def test_default_only_counts(self, stores, capsys):
        store, files = stores
        gone = _discussion(store, body=False)
        _file(files, gone)

        assert main([], store=store, files=files, objects=None, now=LATER) == 0

        out = capsys.readouterr().out
        assert "本体のない発言: 1 件の議論（2 件）" in out
        assert "何も消していません" in out
        assert len(store.query_messages(gone)) == 2
        assert files.scan_attached("discussion")

    def test_apply_deletes_the_orphans(self, stores, capsys):
        store, files = stores
        live = _discussion(store)
        gone = _discussion(store, body=False)
        orphan = _file(files, gone)
        objects = Mock()

        assert (
            main(["--apply"], store=store, files=files, objects=objects, now=LATER) == 0
        )

        assert store.query_messages(gone) == []
        assert store.query_reports(gone) == []
        assert len(store.query_messages(live)) == 2
        assert files.get(orphan.id) is None
        objects.delete_file.assert_called_once_with(orphan.file_path)
        assert "消しました" in capsys.readouterr().out

    def test_clean_tables_say_so(self, stores, capsys):
        store, files = stores
        _discussion(store)
        assert main(["--apply"], store=store, files=files, objects=None, now=LATER) == 0
        assert "残っているものはありません" in capsys.readouterr().out

    def test_recent_ones_are_reported_as_skipped(self, stores, capsys):
        store, files = stores
        _discussion(store, body=False, reports=0)
        assert main([], store=store, files=files, objects=None, now=SOON) == 0
        assert "書かれてから 1 時間たっていない議論: 1 件" in capsys.readouterr().out

    def test_apply_without_s3_keeps_going(self, stores):
        store, files = stores
        gone = _discussion(store, body=False)
        orphan = _file(files, gone)

        plan = plan_cleanup(store, files, now=LATER)
        apply_plan(store, files, None, plan)

        assert files.get(orphan.id) is None

    def test_a_body_written_after_the_plan_is_kept(self, stores):
        """消す直前に本体を確かめ直す: 計画のあとで保存が終わった議論は消さない。"""
        store, files = stores
        discussion = Discussion.create_new(
            topic="保存が終わる議論", participants=["p1", "p2"], mode="classic"
        )
        store.write_messages(
            [
                DiscussionMessage.from_message(
                    discussion.id, 1, Message.create_new("p1", "佐藤", "発言")
                )
            ]
        )
        attached = _file(files, discussion.id)
        objects = Mock()
        plan = plan_cleanup(store, files, now=LATER)
        assert plan.messages == {discussion.id: 1}

        store.put_new(discussion.to_dict())  # 保存が終わった
        kept = apply_plan(store, files, objects, plan)

        assert kept == [discussion.id]
        assert len(store.query_messages(discussion.id)) == 1
        assert files.get(attached.id) is not None
        objects.delete_file.assert_not_called()
