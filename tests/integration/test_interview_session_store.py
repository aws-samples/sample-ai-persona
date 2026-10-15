"""InterviewSessionStore（外に置いたインタビュー）の結合テスト。moto を使う。

BR3.1〜BR3.5、BR3.7、BR3.8、NFR1.59〜NFR1.61、NFR9.31、NFR9.32。送信は
(a) 利用者の発言とセッションの更新、(b) ペルソナ 1 体の応答とセッションの更新を
それぞれ 1 つのトランザクションで書き、条件が合わなければ「衝突」の例外にする。
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from src.models.discussion import DocumentRef  # noqa: E402
from src.models.interview_session import (  # noqa: E402
    InterviewMessage,
    InterviewSession,
)
from src.services.interview_session_store import (  # noqa: E402
    InterviewConflict,
    InterviewSessionStore,
    InterviewSessionStoreError,
)

REGION = "ap-northeast-1"
SESSIONS = "InterviewSessionsTest"
MESSAGES = "InterviewMessagesTest"
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.integration


def create_tables(client: Any) -> None:
    """The two interview tables as CDK makes them."""
    client.create_table(
        TableName=SESSIONS,
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=MESSAGES,
        KeySchema=[
            {"AttributeName": "session_id", "KeyType": "HASH"},
            {"AttributeName": "seq", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "session_id", "AttributeType": "S"},
            {"AttributeName": "seq", "AttributeType": "N"},
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


def make_store(client: Any) -> InterviewSessionStore:
    return InterviewSessionStore(
        region=REGION, sessions_table=SESSIONS, messages_table=MESSAGES, client=client
    )


@pytest.fixture
def store(client) -> InterviewSessionStore:
    return make_store(client)


def _session() -> InterviewSession:
    return InterviewSession.create_new(
        owner_sub="user-a",
        persona_ids=["p1", "p2"],
        persona_names={"p1": "佐藤", "p2": "鈴木"},
        memory_mode="full",
        now=NOW,
    )


def _user(session_id: str, seq: int, now: datetime = NOW) -> InterviewMessage:
    return InterviewMessage.create_new(
        session_id=session_id,
        seq=seq,
        speaker="user",
        persona_id="user",
        persona_name_at_time="あなた",
        content=f"質問 {seq}",
        now=now,
    )


def _reply(session_id: str, seq: int, persona: str = "p1") -> InterviewMessage:
    return InterviewMessage.create_new(
        session_id=session_id,
        seq=seq,
        speaker="persona",
        persona_id=persona,
        persona_name_at_time="佐藤",
        content=f"答え {seq}",
        now=NOW,
    )


@pytest.fixture
def created(store) -> InterviewSession:
    session = _session()
    store.create(session)
    return session


# =============================================================================
# 作成と読み出し（BR3.1、BR3.5）
# =============================================================================


class TestCreateAndRead:
    def test_session_is_written_with_its_ttl(self, store, client, created):
        item = client.get_item(TableName=SESSIONS, Key={"id": {"S": created.id}})[
            "Item"
        ]
        assert int(item["expires_at"]["N"]) == created.expires_at
        assert store.get(created.id) == created

    def test_the_same_id_cannot_be_created_twice(self, store, created):
        with pytest.raises(InterviewSessionStoreError):
            store.create(created)

    def test_missing_session_is_none(self, store):
        assert store.get("nope") is None

    def test_extend_moves_the_last_action_and_the_expiry(self, store, created):
        later = NOW + timedelta(hours=3)
        store.extend(created.touched(later))
        read = store.get(created.id)
        assert read.last_active_at == later
        assert read.expires_at == created.touched(later).expires_at

    def test_end_makes_the_session_unusable(self, store, created):
        store.end(created.id)
        assert store.get(created.id) is None


# =============================================================================
# (a) 利用者の発言（BR3.3、NFR9.32）
# =============================================================================


class TestAppendUserMessage:
    def test_writes_the_message_and_marks_the_reply(self, store, created):
        updated = store.append_user_message(
            created, _user(created.id, 1), token="t1", now=NOW
        )
        assert updated.last_seq == 1
        assert updated.reply_token == "t1"
        read = store.get(created.id)
        assert read.last_seq == 1 and read.reply_token == "t1"
        assert [m.seq for m in store.query_messages(created.id, up_to=1)] == [1]

    def test_a_stale_last_seq_is_a_conflict(self, store, created):
        store.append_user_message(created, _user(created.id, 1), token="t1", now=NOW)
        store.release_reply(created.id, "t1")
        with pytest.raises(InterviewConflict):
            # created still says last_seq=0
            store.append_user_message(
                created, _user(created.id, 1), token="t2", now=NOW
            )

    def test_a_live_reply_mark_is_a_conflict(self, store, created):
        first = store.append_user_message(
            created, _user(created.id, 1), token="t1", now=NOW
        )
        with pytest.raises(InterviewConflict):
            store.append_user_message(
                first,
                _user(created.id, 2),
                token="t2",
                now=NOW + timedelta(minutes=4),
            )

    def test_a_mark_older_than_five_minutes_is_taken_over(self, store, created):
        first = store.append_user_message(
            created, _user(created.id, 1), token="t1", now=NOW
        )
        later = NOW + timedelta(minutes=5, seconds=1)
        second = store.append_user_message(
            first, _user(created.id, 2, later), token="t2", now=later
        )
        assert second.reply_token == "t2" and second.last_seq == 2

    def test_attachments_are_added_to_the_session(self, store, created):
        doc = DocumentRef("f1", "a.png", "image/png", 3, "2026")
        session = replace(created, document_ids=["f1"], documents=[doc])
        store.append_user_message(session, _user(created.id, 1), token="t", now=NOW)
        read = store.get(created.id)
        assert read.document_ids == ["f1"] and read.documents == [doc]


# =============================================================================
# (b) ペルソナの応答（BR3.3、NFR1.59、機能設計への追加）
# =============================================================================


class TestAppendReply:
    def _started(self, store, session) -> InterviewSession:
        return store.append_user_message(
            session, _user(session.id, 1), token="t1", now=NOW
        )

    def test_each_reply_moves_last_seq_and_renews_the_mark(self, store, created):
        started = self._started(store, created)
        later = NOW + timedelta(minutes=4)
        store.append_reply(
            started.id,
            _reply(started.id, 2),
            expected_seq=1,
            token="t1",
            now=later,
            last=False,
        )
        read = store.get(started.id)
        assert read.last_seq == 2
        assert read.reply_token == "t1"
        assert read.reply_started_at == later

    def test_the_last_reply_removes_the_mark(self, store, created):
        started = self._started(store, created)
        store.append_reply(
            started.id,
            _reply(started.id, 2),
            expected_seq=1,
            token="t1",
            now=NOW,
            last=True,
        )
        read = store.get(started.id)
        assert read.reply_token is None and read.reply_started_at is None
        assert [m.seq for m in store.query_messages(started.id, up_to=2)] == [1, 2]

    def test_a_taken_mark_is_a_conflict_and_nothing_is_written(self, store, created):
        started = self._started(store, created)
        later = NOW + timedelta(minutes=6)
        store.append_user_message(
            started, _user(started.id, 2, later), token="t2", now=later
        )
        with pytest.raises(InterviewConflict):
            store.append_reply(
                started.id,
                _reply(started.id, 2),
                expected_seq=1,
                token="t1",
                now=later,
                last=True,
            )
        messages = store.query_messages(started.id, up_to=2)
        assert [m.speaker for m in messages] == ["user", "user"]

    def test_renew_and_release_need_the_own_token(self, store, created):
        started = self._started(store, created)
        assert not store.renew_reply(started.id, "other", NOW)
        assert store.renew_reply(started.id, "t1", NOW + timedelta(minutes=1))
        assert not store.release_reply(started.id, "other")
        assert store.release_reply(started.id, "t1")
        assert store.get(started.id).reply_token is None


# =============================================================================
# 発言の読み出しと保存（BR3.4、BR3.7、BR3.8、NFR1.60）
# =============================================================================


class TestMessagesAndSave:
    def test_messages_page_through_in_order(self, store, created):
        session = created
        for seq in range(1, 61):
            if seq % 2:
                session = store.append_user_message(
                    session, _user(created.id, seq), token=f"t{seq}", now=NOW
                )
            else:
                store.append_reply(
                    created.id,
                    _reply(created.id, seq),
                    expected_seq=seq - 1,
                    token=f"t{seq - 1}",
                    now=NOW,
                    last=True,
                )
                session = replace(
                    session, last_seq=seq, reply_token=None, reply_started_at=None
                )
        read = store.query_messages(created.id, up_to=60)
        assert [m.seq for m in read] == list(range(1, 61))

    def test_mark_saved_records_the_discussion(self, store, created):
        store.mark_saved(created.id, "d1", now=NOW)
        assert store.get(created.id).saved_discussion_id == "d1"

    def test_save_during_a_reply_is_a_conflict(self, store, created):
        store.append_user_message(created, _user(created.id, 1), token="t", now=NOW)
        with pytest.raises(InterviewConflict):
            store.mark_saved(created.id, "d1", now=NOW + timedelta(minutes=1))
        assert store.get(created.id).saved_discussion_id is None

    def test_save_after_a_stale_mark_is_allowed(self, store, created):
        store.append_user_message(created, _user(created.id, 1), token="t", now=NOW)
        store.mark_saved(created.id, "d1", now=NOW + timedelta(minutes=6))
        assert store.get(created.id).saved_discussion_id == "d1"


# =============================================================================
# 失敗の分類
# =============================================================================


class _Failing:
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


class TestFailures:
    def test_transaction_conflict_is_a_conflict(self, client, created):
        store = make_store(_Failing(client, ["None", "TransactionConflict"]))
        with pytest.raises(InterviewConflict):
            store.append_user_message(created, _user(created.id, 1), token="t", now=NOW)

    def test_other_failures_are_store_errors(self, client, created):
        store = make_store(_Failing(client, ["ThrottlingError"]))
        with pytest.raises(InterviewSessionStoreError) as info:
            store.append_user_message(created, _user(created.id, 1), token="t", now=NOW)
        assert not isinstance(info.value, InterviewConflict)

    def test_unreachable_table_is_a_store_error(self, client):
        store = make_store(client)
        store.sessions_table = "Nope"
        with pytest.raises(InterviewSessionStoreError):
            store.get("x")
