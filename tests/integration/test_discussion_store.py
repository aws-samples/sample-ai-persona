"""DiscussionStore（議論の本体・発言・レポートの読み書き）の結合テスト。moto を使う。

BR1.2〜BR1.6、BR2.7、BR3.8、BR4.4、NFR1.57、NFR1.58、NFR1.62、NFR2.15、NFR2.16、
NFR2.19、NFR9.34。条件・トランザクションの失敗は自前の例外に分ける。moto で起こせない
TransactionConflict は CancellationReasons を組み立てたスタブの client で確かめる。
"""

from datetime import datetime, timezone
from typing import Any

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from src.models.discussion import Discussion  # noqa: E402
from src.models.discussion_report import DiscussionReport  # noqa: E402
from src.models.insight import Insight  # noqa: E402
from src.models.message import DiscussionMessage, Message  # noqa: E402
from src.services.discussion_store import (  # noqa: E402
    MAX_BODY_BYTES,
    DiscussionConditionFailed,
    DiscussionConflict,
    DiscussionStore,
    DiscussionStoreError,
)

REGION = "ap-northeast-1"
DISCUSSIONS = "DiscussionsTest"
MESSAGES = "DiscussionMessagesTest"
REPORTS = "DiscussionReportsTest"
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.integration


def create_tables(client: Any) -> None:
    """The three discussion tables as CDK makes them (OwnerIndex on Discussions)."""
    client.create_table(
        TableName=DISCUSSIONS,
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
            {"AttributeName": "owner_sub", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "OwnerIndex",
                "KeySchema": [
                    {"AttributeName": "owner_sub", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=MESSAGES,
        KeySchema=[
            {"AttributeName": "discussion_id", "KeyType": "HASH"},
            {"AttributeName": "seq", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "discussion_id", "AttributeType": "S"},
            {"AttributeName": "seq", "AttributeType": "N"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=REPORTS,
        KeySchema=[
            {"AttributeName": "discussion_id", "KeyType": "HASH"},
            {"AttributeName": "report_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "discussion_id", "AttributeType": "S"},
            {"AttributeName": "report_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def client(monkeypatch) -> Any:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        c = boto3.client("dynamodb", region_name=REGION)
        create_tables(c)
        yield c


def make_store(client: Any, **kwargs: Any) -> DiscussionStore:
    return DiscussionStore(
        region=REGION,
        discussions_table=DISCUSSIONS,
        messages_table=MESSAGES,
        reports_table=REPORTS,
        client=client,
        sleep=lambda _s: None,
        **kwargs,
    )


@pytest.fixture
def store(client) -> DiscussionStore:
    return make_store(client)


def _discussion(**overrides: Any) -> Discussion:
    from dataclasses import replace

    base = Discussion.create_new(
        topic="新商品",
        participants=["p1", "p2"],
        owner_sub="user-a",
        participant_names={"p1": "佐藤", "p2": "鈴木"},
        now=NOW,
    )
    return replace(base, **overrides)


def _messages(discussion_id: str, count: int) -> list[DiscussionMessage]:
    return [
        DiscussionMessage.from_message(
            discussion_id, i, Message.create_new("p1", "佐藤", f"発言 {i}")
        )
        for i in range(1, count + 1)
    ]


class _Failing:
    """A client whose transaction fails with the given cancellation reasons."""

    def __init__(self, real: Any, reasons: list[str]) -> None:
        self._real = real
        self._reasons = reasons

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def transact_write_items(self, **_kwargs: Any) -> Any:
        raise ClientError(
            {
                "Error": {"Code": "TransactionCanceledException", "Message": "x"},
                "CancellationReasons": [{"Code": r} for r in self._reasons],
            },
            "TransactWriteItems",
        )


# =============================================================================
# 本体の大きさ（BR2.7、NFR2.19）
# =============================================================================


class TestBodySize:
    def test_the_limit_is_350kb(self):
        assert MAX_BODY_BYTES == 350 * 1024

    def test_size_counts_names_and_values(self):
        small = DiscussionStore.item_size({"id": "a"})
        assert small == len("id") + len("a")

    def test_size_grows_with_the_body(self):
        d = _discussion()
        big = d.with_insights(
            [Insight.create_new("c", "x" * 1000, [], 0.5) for _ in range(10)]
        )
        assert DiscussionStore.item_size(big.to_dict()) > DiscussionStore.item_size(
            d.to_dict()
        )

    def test_numbers_and_nested_values_are_measured(self):
        size = DiscussionStore.item_size(
            {"n": 12, "m": {"k": "v"}, "l": [1.5, True], "ss": {"a", "b"}}
        )
        assert size > 0


# =============================================================================
# 本体の書き込みと読み出し（BR1.2、BR1.3）
# =============================================================================


class TestPutNew:
    def test_writes_the_body_and_the_callers_items_together(self, store, client):
        d = _discussion()
        extra = [
            {
                "Put": {
                    "TableName": REPORTS,
                    "Item": {
                        "discussion_id": {"S": "marker"},
                        "report_id": {"S": "r"},
                    },
                }
            }
        ]
        store.put_new(d.to_dict(), extra_items=extra)
        assert store.get(d.id)["topic"] == "新商品"
        marker = client.get_item(
            TableName=REPORTS,
            Key={"discussion_id": {"S": "marker"}, "report_id": {"S": "r"}},
        )
        assert "Item" in marker

    def test_an_existing_id_is_refused(self, store):
        d = _discussion()
        store.put_new(d.to_dict())
        with pytest.raises(DiscussionConditionFailed):
            store.put_new(d.to_dict())

    def test_get_returns_plain_values(self, store):
        d = _discussion(message_count=3)
        store.put_new(d.to_dict())
        body = store.get(d.id)
        assert body["message_count"] == 3
        assert isinstance(body["message_count"], int)
        assert Discussion.from_dict(body) == d

    def test_nested_agent_config_round_trips(self, store):
        """Issue #107: ペルソナごとのモデル（入れ子の dict）が往復する。"""
        config = {
            "rounds": 3,
            "persona_models": {"p1": "openai.gpt", "p2": "google.gemma"},
            "facilitator_model": "global.anthropic.claude",
        }
        d = _discussion(agent_config=config, mode="agent")
        store.put_new(d.to_dict())
        assert Discussion.from_dict(store.get(d.id)).agent_config == config

    def test_get_of_a_missing_id_is_none(self, store):
        assert store.get("nope") is None


class TestGetMany:
    def test_reads_only_the_given_attributes(self, store):
        d = _discussion(insights=[Insight.create_new("c", "説明です", [], 0.5)])
        store.put_new(d.to_dict())
        found = store.get_many([d.id, "missing"], ["id", "topic", "mode"])
        assert set(found) == {d.id}
        assert set(found[d.id]) == {"id", "topic", "mode"}

    def test_unprocessed_keys_are_read_again(self, client):
        store = make_store(client)
        d = _discussion()
        store.put_new(d.to_dict())
        calls = {"n": 0}
        real = client.batch_get_item

        def flaky(**kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                keys = kwargs["RequestItems"][DISCUSSIONS]["Keys"]
                return {
                    "Responses": {DISCUSSIONS: []},
                    "UnprocessedKeys": {DISCUSSIONS: {"Keys": keys}},
                }
            return real(**kwargs)

        client.batch_get_item = flaky
        assert set(store.get_many([d.id], ["id"])) == {d.id}
        assert calls["n"] == 2

    def test_keys_left_unprocessed_raise(self, client):
        store = make_store(client)

        def stuck(**kwargs: Any) -> Any:
            keys = kwargs["RequestItems"][DISCUSSIONS]["Keys"]
            return {"Responses": {}, "UnprocessedKeys": {DISCUSSIONS: {"Keys": keys}}}

        client.batch_get_item = stuck
        with pytest.raises(DiscussionStoreError):
            store.get_many(["a"], ["id"])

    def test_more_than_100_ids_are_split(self, store):
        ids = []
        for i in range(105):
            d = Discussion.create_new(topic=f"t{i}", participants=["p1"], now=NOW)
            store.put_new(d.to_dict())
            ids.append(d.id)
        assert len(store.get_many(ids, ["id"])) == 105


# =============================================================================
# 発言（BR1.2 (2)、BR1.3、NFR9.34）
# =============================================================================


class TestMessages:
    def test_messages_are_written_and_read_in_order(self, store):
        store.write_messages(list(reversed(_messages("d1", 30))))
        read = store.query_messages("d1")
        assert [m.seq for m in read] == list(range(1, 31))

    def test_read_stops_at_the_count(self, store):
        store.write_messages(_messages("d1", 5))
        assert [m.seq for m in store.query_messages("d1", up_to=3)] == [1, 2, 3]

    def test_a_thousand_messages_page_through(self, store):
        """NFR9.34: 長い議論も保存して開ける（Query のページを送る）。"""
        long_text = "長い発言" * 100
        messages = [
            DiscussionMessage.from_message(
                "d1", i, Message.create_new("p1", "佐藤", long_text)
            )
            for i in range(1, 1001)
        ]
        store.write_messages(messages)
        assert len(store.query_messages("d1")) == 1000

    def test_unprocessed_items_are_sent_again(self, client):
        store = make_store(client)
        calls = {"n": 0}
        real = client.batch_write_item

        def flaky(**kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                return {"UnprocessedItems": kwargs["RequestItems"]}
            return real(**kwargs)

        client.batch_write_item = flaky
        store.write_messages(_messages("d1", 2))
        assert len(store.query_messages("d1")) == 2

    def test_items_left_unprocessed_raise_after_three_resends(self, client):
        sleeps: list[float] = []
        store = make_store(client)
        store._sleep = sleeps.append  # type: ignore[assignment]

        def stuck(**kwargs: Any) -> Any:
            return {"UnprocessedItems": kwargs["RequestItems"]}

        client.batch_write_item = stuck
        with pytest.raises(DiscussionStoreError):
            store.write_messages(_messages("d1", 1))
        assert len(sleeps) == 3

    def test_delete_messages_removes_every_message(self, store):
        store.write_messages(_messages("d1", 40))
        store.write_messages(_messages("d2", 1))
        assert store.delete_messages("d1") == 40
        assert store.query_messages("d1") == []
        assert len(store.query_messages("d2")) == 1


# =============================================================================
# 部分更新（BR1.4、BR3.8、NFR1.62）
# =============================================================================


class TestPartialUpdates:
    def test_update_insights_keeps_the_other_columns(self, store):
        d = _discussion(report_count=2, message_count=5)
        store.put_new(d.to_dict())
        insights = [Insight.create_new("c", "新しい説明", [], 0.4).to_dict()]
        store.update_insights(d.id, insights, NOW.isoformat())
        body = store.get(d.id)
        assert body["insights"][0]["description"] == "新しい説明"
        assert body["report_count"] == 2
        assert body["message_count"] == 5
        assert body["updated_at"] == NOW.isoformat()

    def test_update_insights_needs_the_body(self, store):
        with pytest.raises(DiscussionConditionFailed):
            store.update_insights("missing", [], NOW.isoformat())

    def test_message_count_moves_only_from_the_expected_value(self, store):
        d = _discussion(message_count=4)
        store.put_new(d.to_dict())
        store.advance_message_count(d.id, expected=4, new=6, updated_at="u")
        assert store.get(d.id)["message_count"] == 6
        with pytest.raises(DiscussionConditionFailed):
            store.advance_message_count(d.id, expected=4, new=8, updated_at="u")

    def test_advance_can_add_documents(self, store):
        d = _discussion(message_count=1)
        store.put_new(d.to_dict())
        docs = [
            {
                "id": "f1",
                "filename": "a.pdf",
                "mime_type": "application/pdf",
                "file_size": 1,
                "uploaded_at": "2026",
            }
        ]
        store.advance_message_count(
            d.id, expected=1, new=2, updated_at="u", documents=docs
        )
        assert store.get(d.id)["documents"] == docs


# =============================================================================
# レポート（BR4.4、NFR1.62）
# =============================================================================


def _report(discussion_id: str, content: str = "内容") -> DiscussionReport:
    return DiscussionReport.create_new(discussion_id, "summary", content, now=NOW)


class TestReports:
    def test_save_writes_the_report_and_counts_it(self, store):
        d = _discussion()
        store.put_new(d.to_dict())
        report = _report(d.id)
        store.put_report(report, max_reports=3, updated_at="u")
        assert store.get(d.id)["report_count"] == 1
        assert store.query_reports(d.id) == [report]

    def test_the_fourth_report_is_refused(self, store):
        d = _discussion(report_count=3)
        store.put_new(d.to_dict())
        with pytest.raises(DiscussionConditionFailed):
            store.put_report(_report(d.id), max_reports=3, updated_at="u")
        assert store.query_reports(d.id) == []

    def test_two_saves_on_two_reports_let_one_through(self, store):
        """NFR1.62: 同時の保存でも 3 件を超えない（条件は本体の件数）。"""
        d = _discussion(report_count=2)
        store.put_new(d.to_dict())
        store.put_report(_report(d.id, "a"), max_reports=3, updated_at="u")
        with pytest.raises(DiscussionConditionFailed):
            store.put_report(_report(d.id, "b"), max_reports=3, updated_at="u")
        assert store.get(d.id)["report_count"] == 3
        assert len(store.query_reports(d.id)) == 1

    def test_a_report_needs_its_discussion(self, store):
        with pytest.raises(DiscussionConditionFailed):
            store.put_report(_report("missing"), max_reports=3, updated_at="u")

    def test_update_changes_the_content_and_the_body_time(self, store):
        d = _discussion()
        store.put_new(d.to_dict())
        report = _report(d.id)
        store.put_report(report, max_reports=3, updated_at="u1")
        store.update_report(report.with_content("新しい", now=NOW), updated_at="u2")
        assert store.query_reports(d.id)[0].content == "新しい"
        assert store.get(d.id)["updated_at"] == "u2"

    def test_update_of_a_missing_report_is_refused(self, store):
        d = _discussion()
        store.put_new(d.to_dict())
        with pytest.raises(DiscussionConditionFailed):
            store.update_report(_report(d.id), updated_at="u")

    def test_delete_removes_one_and_lowers_the_count(self, store):
        d = _discussion()
        store.put_new(d.to_dict())
        report = _report(d.id)
        store.put_report(report, max_reports=3, updated_at="u")
        store.delete_report(d.id, report.report_id, updated_at="u")
        assert store.get(d.id)["report_count"] == 0
        assert store.query_reports(d.id) == []
        with pytest.raises(DiscussionConditionFailed):
            store.delete_report(d.id, report.report_id, updated_at="u")

    def test_delete_reports_removes_all(self, store):
        d = _discussion()
        store.put_new(d.to_dict())
        store.put_report(_report(d.id), max_reports=3, updated_at="u")
        store.put_report(_report(d.id), max_reports=3, updated_at="u")
        assert store.delete_reports(d.id) == 2
        assert store.query_reports(d.id) == []


# =============================================================================
# 失敗の分類
# =============================================================================


class TestFailureClassification:
    def test_conflict_is_told_apart(self, client):
        store = make_store(_Failing(client, ["None", "TransactionConflict"]))
        with pytest.raises(DiscussionConflict):
            store.put_new(_discussion().to_dict())

    def test_condition_failure_is_told_apart(self, client):
        store = make_store(_Failing(client, ["ConditionalCheckFailed"]))
        with pytest.raises(DiscussionConditionFailed) as info:
            store.put_new(_discussion().to_dict())
        assert info.value.failed_indexes == (0,)

    def test_other_cancellations_are_store_errors(self, client):
        store = make_store(_Failing(client, ["ThrottlingError"]))
        with pytest.raises(DiscussionStoreError) as info:
            store.put_new(_discussion().to_dict())
        assert not isinstance(
            info.value, (DiscussionConflict, DiscussionConditionFailed)
        )

    def test_unreachable_table_is_a_store_error(self, client):
        store = make_store(client)
        store.discussions_table = "NoSuchTable"
        with pytest.raises(DiscussionStoreError):
            store.get("x")


# =============================================================================
# 運用のスクリプト向けの読み（BR1.6）
# =============================================================================


class TestOrphanScans:
    def test_partitions_carry_the_newest_write_time(self, client):
        """発言は書いた時刻（written_at）を持ち、後始末は 1 時間以内のものを飛ばす。"""
        store = make_store(client, clock=lambda: 1_800_000_000.0)
        store.write_messages(_messages("d1", 2))
        (summary,) = store.message_partitions().values()
        assert summary.count == 2
        assert summary.newest == 1_800_000_000.0
        d = _discussion()
        store.put_new(d.to_dict())
        store.put_report(_report(d.id), max_reports=3, updated_at="u")
        report = store.report_partitions()[d.id]
        assert report.count == 1 and report.newest is not None

    def test_existing_tells_which_bodies_exist(self, store):
        d = _discussion()
        store.put_new(d.to_dict())
        assert store.existing([d.id, "gone"]) == {d.id}
