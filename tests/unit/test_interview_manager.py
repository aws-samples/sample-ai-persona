"""InterviewManager の単体テスト（U6: アプリの外に置いたセッション）。

BR3.1〜BR3.8、BR6.1、BR7.2、NFR1.48〜NFR1.50、NFR1.54、NFR1.59〜NFR1.61、
NFR2.18、NFR8.19、NFR8.20、NFR9.31〜NFR9.33、AC2.4.1〜AC2.4.3、AC4.2.2、AC4.2.5。

``InterviewSessionStore`` は moto の上の本物、``DiscussionRecords``・
``ParticipantReader``・``AccessPolicy`` も本物（tests/unit/discussion_world.py）、
AI はモック。時計は差し替える。
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock, patch

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.managers.interview_manager import (  # noqa: E402
    InterviewManager,
    InterviewManagerError,
    InterviewValidationError,
)
from src.models.errors import AccessDeniedError, CodedError, ErrorCode  # noqa: E402
from src.models.interview_session import InterviewMessage  # noqa: E402
from src.models.uploaded_file import UploadedFile  # noqa: E402
from src.services.agent_service import (  # noqa: E402
    AgentCommunicationError,
    PersonaAgent,
)
from src.services.stores.interview_session_store import (  # noqa: E402
    InterviewSessionStore,
    InterviewSessionStoreError,
)
from tests.error_helpers import raises_code  # noqa: E402
from tests.unit.discussion_world import (  # noqa: E402
    ADMIN,
    NO_ROLE,
    OWNER,
    PEER,
    World,
    persona,
)

pytestmark = pytest.mark.unit

REGION = "ap-northeast-1"
SESSIONS = "InterviewSessionsUnit"
MESSAGES = "InterviewMessagesUnit"
START = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
FILE_1 = "11111111-1111-4111-8111-111111111111"
FILE_2 = "22222222-2222-4222-8222-222222222222"


@dataclass
class Clock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now = self.now + timedelta(**kwargs)


def _create_tables(client: Any) -> None:
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
        _create_tables(c)
        yield c


@pytest.fixture
def store(client) -> InterviewSessionStore:
    return InterviewSessionStore(
        region=REGION, sessions_table=SESSIONS, messages_table=MESSAGES, client=client
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def world() -> World:
    return World()


class Agents:
    """Records every agent the AgentService mock makes."""

    def __init__(self) -> None:
        self.made: list[Mock] = []
        self.calls: list[dict[str, Any]] = []
        self.respond: Any = None

    def make(self, **kw: Any) -> Mock:
        p = kw["persona"]
        agent = Mock(spec=PersonaAgent)
        agent.get_persona_id.return_value = p.id
        agent.get_persona_name.return_value = p.name
        agent.respond.side_effect = self.respond or (
            lambda prompt, context=None: f"{p.name}の回答です"
        )
        agent.respond_streaming.side_effect = lambda prompt, context=None: iter(
            [f"{p.name}の", "回答です"]
        )
        self.made.append(agent)
        self.calls.append(kw)
        return agent


@pytest.fixture
def agents() -> Agents:
    return Agents()


def _manager(store, world, clock, agents, **kwargs) -> InterviewManager:
    agent_service = Mock()
    agent_service.create_persona_agent.side_effect = agents.make
    return InterviewManager(
        agent_service=agent_service,
        dataset_store=Mock(),
        dataset_analysis_service=Mock(build_binding_tools=Mock(return_value=([], []))),
        binding_store=Mock(list_for_persona=Mock(return_value=[])),
        session_store=store,
        discussion_records=world.records,
        participants=world.participants,
        file_attachments=world.files,
        resource_lookup=world.lookup,
        s3_service=None,
        clock=clock,
        **kwargs,
    )


@pytest.fixture
def manager(store, world, clock, agents) -> InterviewManager:
    return _manager(store, world, clock, agents)


def _personas(world: World, *ids: str) -> list:
    return [world.persona_bodies[i] for i in ids or ("p1", "p2")]


def _start(manager, world, principal=OWNER, **kwargs):
    return manager.start_interview_session(principal, _personas(world), **kwargs)


def _upload(tmp_path, file_id=FILE_1, name="資料.txt", size=None) -> UploadedFile:
    path = tmp_path / f"{file_id}.txt"
    path.write_bytes(b"hello")
    uploaded = UploadedFile.create_new(
        owner_sub=OWNER.sub,
        file_type="discussion_document",
        original_filename=name,
        file_size=size or 5,
        file_hash="h",
        mime_type="text/plain",
        now=START,
        file_id=file_id,
    )
    return uploaded.with_path(str(path))


def _events(capsys, name: str) -> list[dict]:
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("event") == name]


# =============================================================================
# 開始（BR3.1、BR6.1）
# =============================================================================


class TestStart:
    def test_session_is_owned_by_the_starter(self, manager, world, store):
        session = _start(manager, world)
        stored = store.get(session.id)
        assert stored.owner_sub == OWNER.sub
        assert stored.last_seq == 0
        assert stored.persona_names == {"p1": "佐藤", "p2": "鈴木"}

    def test_ai_execute_is_required(self, manager, world, agents, client):
        with pytest.raises(AccessDeniedError):
            _start(manager, world, principal=NO_ROLE)
        assert agents.made == []
        assert client.scan(TableName=SESSIONS)["Count"] == 0

    def test_only_owned_personas_write_memory(self, manager, world, agents):
        session = _start(manager, world, enable_memory=True, memory_mode="full")
        modes = {c["persona"].id: c["memory_mode"] for c in agents.calls}
        assert modes == {"p1": "full", "p2": "retrieve_only"}
        assert {c["session_id"] for c in agents.calls} == {session.id}

    def test_memory_off_is_disabled(self, manager, world, store):
        session = _start(manager, world, enable_memory=False)
        assert store.get(session.id).memory_mode == "disabled"

    def test_no_personas(self, manager):
        with raises_code(
            InterviewValidationError, ErrorCode.INTERVIEW_PERSONAS_REQUIRED
        ):
            manager.start_interview_session(OWNER, [])

    def test_too_many_personas(self, manager):
        many = [persona(f"x{i}", "a") for i in range(6)]
        with raises_code(
            InterviewValidationError, ErrorCode.INTERVIEW_TOO_MANY_PERSONAS
        ):
            manager.start_interview_session(OWNER, many)

    def test_invalid_memory_mode(self, manager, world):
        with raises_code(
            InterviewValidationError, ErrorCode.INTERVIEW_MEMORY_MODE_INVALID
        ):
            _start(manager, world, enable_memory=True, memory_mode="everything")

    def test_unsupported_model(self, manager, world):
        with raises_code(
            InterviewValidationError, ErrorCode.INTERVIEW_MODEL_UNSUPPORTED
        ):
            _start(manager, world, persona_models={"p1": "unknown.model-id"})

    @patch("src.managers.shared.model_validation.config")
    def test_additional_models_disabled(self, mock_config, manager, world):
        mock_config.ENABLE_ADDITIONAL_PERSONA_MODELS = False
        mock_config.INFERENCE_SCOPE = "global"
        with raises_code(
            InterviewValidationError,
            ErrorCode.INTERVIEW_MODEL_ADDITIONAL_MODELS_DISABLED,
        ):
            _start(manager, world, persona_models={"p1": "openai.gpt-5.6-terra"})

    def test_persona_models_are_kept(self, manager, world, store):
        session = _start(
            manager, world, persona_models={"p1": "global.anthropic.claude-sonnet-5"}
        )
        assert store.get(session.id).persona_models == {
            "p1": "global.anthropic.claude-sonnet-5"
        }


# =============================================================================
# 持ち主と期限（BR3.2、BR3.5）
# =============================================================================


class TestOwnerAndExpiry:
    @pytest.mark.parametrize("principal", [PEER, ADMIN])
    def test_others_see_nothing(self, manager, world, principal):
        session = _start(manager, world)
        for call in (
            lambda: manager.send_user_message(principal, session.id, "こんにちは"),
            lambda: manager.get_interview_session(principal, session.id),
            lambda: manager.get_session_status(principal, session.id),
            lambda: manager.save_interview_session(principal, session.id, "名前"),
            lambda: manager.end_interview_session(principal, session.id),
        ):
            with raises_code(CodedError, ErrorCode.INTERVIEW_SESSION_NOT_FOUND):
                call()

    def test_missing_session(self, manager):
        with raises_code(CodedError, ErrorCode.INTERVIEW_SESSION_NOT_FOUND):
            manager.get_interview_session(OWNER, "nonexistent")

    def test_idle_for_8_hours_expires(self, manager, world, clock):
        session = _start(manager, world)
        clock.advance(hours=8)
        with raises_code(CodedError, ErrorCode.INTERVIEW_SESSION_NOT_FOUND):
            manager.send_user_message(OWNER, session.id, "こんにちは")

    def test_seven_days_is_the_longest(self, manager, world, clock):
        session = _start(manager, world)
        for _ in range(23):
            clock.advance(hours=7)
            manager.send_user_message(OWNER, session.id, "続けます")
        # 最後の操作から 7 時間（8 時間未満）でも、始めてから 7 日で無効
        clock.advance(hours=7)
        with raises_code(CodedError, ErrorCode.INTERVIEW_SESSION_NOT_FOUND):
            manager.get_interview_session(OWNER, session.id)

    def test_sending_extends_the_session(self, manager, world, clock, store):
        session = _start(manager, world)
        clock.advance(hours=7)
        manager.send_user_message(OWNER, session.id, "こんにちは")
        clock.advance(hours=7)
        assert manager.get_interview_session(OWNER, session.id).id == session.id


# =============================================================================
# 拒否のログ（NFR8.22、BR2.13。Build and Test の Loop-back 1）
# =============================================================================


def _denied(capsys) -> list[dict]:
    return _events(capsys, "authz.denied")


class TestDenialLog:
    """持ち主の照合で断ったら、U2 の ``authz.denied`` を 1 行書く。

    理由は U2 の ``ensure_readable`` と同じ ``not_found``（応答が NOT_FOUND のため）。
    インタビューのセッションは ``ResourceType`` に無いので、``resource_type`` は
    U2 の ``require`` が種類なしで断ったときと同じく書かない。存在しない・期限切れも、
    U2 で存在しない ID を ``ensure_readable`` が断ったときと同じく 1 行書く。
    """

    @pytest.mark.parametrize("principal", [PEER, ADMIN])
    @pytest.mark.parametrize("operation", ["send", "get", "end"])
    def test_someone_elses_session_logs_one_denial(
        self, manager, world, capsys, principal, operation
    ):
        session = _start(manager, world)
        capsys.readouterr()
        call = {
            "send": lambda: manager.send_user_message(principal, session.id, "やあ"),
            "get": lambda: manager.get_interview_session(principal, session.id),
            "end": lambda: manager.end_interview_session(principal, session.id),
        }[operation]
        with raises_code(CodedError, ErrorCode.INTERVIEW_SESSION_NOT_FOUND):
            call()
        out = capsys.readouterr().out
        lines = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
        (line,) = [x for x in lines if x.get("event") == "authz.denied"]
        assert line["reason"] == "not_found"
        # 種類なし（log_event は None の項目を書かない。U2 の require と同じ）
        assert line.get("resource_type") is None
        assert line["sub"] == principal.sub
        # セッションの ID はログに出さない（NFR8.6）
        assert session.id not in out

    def test_the_owner_logs_no_denial(self, manager, world, capsys):
        session = _start(manager, world)
        manager.send_user_message(OWNER, session.id, "こんにちは")
        manager.get_interview_session(OWNER, session.id)
        manager.get_session_status(OWNER, session.id)
        manager.end_interview_session(OWNER, session.id)
        assert _denied(capsys) == []

    def test_a_missing_session_logs_one_denial(self, manager, capsys):
        with raises_code(CodedError, ErrorCode.INTERVIEW_SESSION_NOT_FOUND):
            manager.send_user_message(OWNER, "nonexistent", "こんにちは")
        (line,) = _denied(capsys)
        assert (line["reason"], line["sub"]) == ("not_found", OWNER.sub)

    def test_an_expired_session_logs_one_denial(self, manager, world, clock, capsys):
        session = _start(manager, world)
        capsys.readouterr()
        clock.advance(hours=8)
        with raises_code(CodedError, ErrorCode.INTERVIEW_SESSION_NOT_FOUND):
            manager.get_interview_session(OWNER, session.id)
        (line,) = _denied(capsys)
        assert (line["reason"], line["sub"]) == ("not_found", OWNER.sub)


# =============================================================================
# 送信（BR3.3、BR7.2、NFR1.59〜NFR1.61）
# =============================================================================


class TestSend:
    def test_messages_are_saved_in_order(self, manager, world, store):
        session = _start(manager, world)
        reply = manager.send_user_message(OWNER, session.id, "好きな食べ物は？")
        assert [m.persona_id for m in reply.responses] == ["p1", "p2"]
        stored = store.get(session.id)
        assert stored.last_seq == 3 and stored.reply_token is None
        messages = store.query_messages(session.id, up_to=3)
        assert [m.speaker for m in messages] == ["user", "persona", "persona"]
        assert messages[0].content == "好きな食べ物は？"

    def test_unreadable_participant_stops_before_writing(
        self, manager, world, store, agents
    ):
        session = _start(manager, world)
        world.unshare("p2")
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.send_user_message(OWNER, session.id, "こんにちは")
        assert store.get(session.id).last_seq == 0
        assert all(not a.respond.called for a in agents.made)

    def test_empty_and_long_messages(self, manager, world):
        session = _start(manager, world)
        with raises_code(
            InterviewValidationError, ErrorCode.INTERVIEW_MESSAGE_REQUIRED
        ):
            manager.send_user_message(OWNER, session.id, "   ")
        with raises_code(
            InterviewValidationError, ErrorCode.INTERVIEW_MESSAGE_TOO_LONG
        ):
            manager.send_user_message(OWNER, session.id, "あ" * 2001)

    def _hold_mark(self, store, session_id, now):
        session = store.get(session_id)
        message = InterviewMessage.create_new(
            session_id=session_id,
            seq=session.last_seq + 1,
            speaker="user",
            persona_id="user",
            persona_name_at_time="ユーザー",
            content="別の送信",
            now=now,
        )
        store.append_user_message(session, message, token="other", now=now)

    def test_a_reply_in_progress_refuses_send_and_save(
        self, manager, world, store, clock, capsys
    ):
        session = _start(manager, world)
        self._hold_mark(store, session.id, clock())
        with raises_code(CodedError, ErrorCode.CONCURRENT_UPDATE):
            manager.send_user_message(OWNER, session.id, "こんにちは")
        with raises_code(CodedError, ErrorCode.CONCURRENT_UPDATE):
            manager.save_interview_session(OWNER, session.id, "名前")
        (event, *_) = _events(capsys, "interview.message_conflict")
        assert event["interview_session_id"] == session.id

    def test_a_stale_mark_is_ignored(self, manager, world, store, clock):
        session = _start(manager, world)
        self._hold_mark(store, session.id, clock())
        clock.advance(minutes=6)
        reply = manager.send_user_message(OWNER, session.id, "こんにちは")
        assert len(reply.responses) == 2

    def test_all_agents_failing_releases_the_mark(self, store, world, clock, agents):
        def boom(prompt, context=None):
            raise AgentCommunicationError("down")

        agents.respond = boom
        manager = _manager(store, world, clock, agents)
        session = _start(manager, world)
        with raises_code(CodedError, ErrorCode.INTERVIEW_AGENT_UNAVAILABLE):
            manager.send_user_message(OWNER, session.id, "こんにちは")
        stored = store.get(session.id)
        assert stored.reply_token is None and stored.last_seq == 1
        # すぐ次の送信と保存の判定が通る（印が残っていない）
        agents.respond = None
        for agent in agents.made:
            agent.respond.side_effect = lambda prompt, context=None: "回答です"
        assert manager.send_user_message(OWNER, session.id, "もう一度").responses

    def test_one_agent_failing_keeps_the_others(self, store, world, clock, agents):
        def p2_fails(prompt, context=None):
            raise AgentCommunicationError("down")

        manager = _manager(store, world, clock, agents)
        session = _start(manager, world)
        agents.made[1].respond.side_effect = p2_fails
        reply = manager.send_user_message(OWNER, session.id, "こんにちは")
        assert [m.persona_id for m in reply.responses] == ["p1"]
        stored = store.get(session.id)
        assert stored.last_seq == 2 and stored.reply_token is None

    def test_closing_the_stream_releases_the_mark(self, manager, world, store):
        session = _start(manager, world)
        stream = manager.send_user_message_streaming(OWNER, session.id, "こんにちは")
        next(stream)
        stream.close()
        assert store.get(session.id).reply_token is None
        assert manager.send_user_message(OWNER, session.id, "もう一度").responses

    def test_streaming_sends_each_reply(self, manager, world, store):
        session = _start(manager, world)
        events = list(
            manager.send_user_message_streaming(OWNER, session.id, "こんにちは")
        )
        kinds = [kind for kind, _ in events]
        assert kinds.count("message_end") == 2
        assert kinds[0] == "message_start"
        assert store.get(session.id).last_seq == 3

    def test_a_mark_taken_over_saves_no_reply(
        self, store, world, clock, agents, client, capsys
    ):
        manager = _manager(store, world, clock, agents)
        session = _start(manager, world)

        def taken_over(prompt, context=None):
            client.update_item(
                TableName=SESSIONS,
                Key={"id": {"S": session.id}},
                UpdateExpression="SET reply_token = :t",
                ExpressionAttributeValues={":t": {"S": "someone-else"}},
            )
            return "回答"

        agents.made[0].respond.side_effect = taken_over
        with raises_code(CodedError, ErrorCode.CONCURRENT_UPDATE):
            manager.send_user_message(OWNER, session.id, "こんにちは")
        assert store.get(session.id).last_seq == 1
        assert all(a.dispose.called for a in agents.made[:2])
        assert _events(capsys, "interview.message_conflict")

    def test_later_personas_see_earlier_replies_of_the_same_send(
        self, store, world, clock, agents
    ):
        """BR3.4「今の会話の進め方と同じく」: 2 体目の prompt に 1 体目の今回の応答が入る。"""
        manager = _manager(store, world, clock, agents)
        session = _start(manager, world)
        manager.send_user_message(OWNER, session.id, "質問1")
        manager.send_user_message(OWNER, session.id, "質問2")

        (_, second_p1_prompt) = [
            c.args[0] for c in agents.made[0].respond.call_args_list
        ]
        (_, second_p2_prompt) = [
            c.args[0] for c in agents.made[1].respond.call_args_list
        ]
        # 1 体目の今回の応答（2 回目の送信の 佐藤の回答）は 2 体目にだけ見える
        assert second_p2_prompt.count("佐藤: 佐藤の回答です") == 2
        assert second_p1_prompt.count("佐藤: 佐藤の回答です") == 1
        # これまでの会話は最新の 5 件まで（質問1・佐藤・鈴木・質問2・佐藤）
        context = second_p2_prompt.split("\n\n")[0].splitlines()[1:]
        assert len(context) == 5
        assert context[-1] == "佐藤: 佐藤の回答です"

    def test_an_overlapping_send_keeps_the_agents_in_use(
        self, store, world, clock, agents, monkeypatch
    ):
        """同じ台で送信が重なっても、応答中の送信のエージェントを捨てない。"""
        manager = _manager(store, world, clock, agents)
        session = _start(manager, world)
        stale = store.get(session.id)
        real_get = store.get
        overlapping: list = []

        def stale_once(session_id):
            # 後の送信は、先の送信が (a) を書く前のセッションを読んだとする
            monkeypatch.setattr(store, "get", real_get)
            return stale

        def first_reply(prompt, context=None):
            monkeypatch.setattr(store, "get", stale_once)
            with raises_code(CodedError, ErrorCode.CONCURRENT_UPDATE):
                manager.send_user_message(OWNER, session.id, "割り込み")
            overlapping.append(True)
            return "佐藤の回答です"

        agents.made[0].respond.side_effect = first_reply
        reply = manager.send_user_message(OWNER, session.id, "こんにちは")

        assert overlapping == [True]
        assert [m.persona_id for m in reply.responses] == ["p1", "p2"]
        assert len(agents.made) == 2
        assert not any(a.dispose.called for a in agents.made)
        assert store.get(session.id).last_seq == 3

    def test_mark_is_renewed_after_each_reply(self, store, world, clock, agents):
        manager = _manager(store, world, clock, agents)
        session = _start(manager, world)
        seen: list = []

        def slow(prompt, context=None):
            clock.advance(minutes=4)
            seen.append(store.get(session.id).reply_started_at)
            return "回答"

        for agent in agents.made:
            agent.respond.side_effect = slow
        manager.send_user_message(OWNER, session.id, "こんにちは")
        # 2 体目の応答の時点で、印は 1 体目の応答のあとに更新されている
        assert seen[0] == START
        assert seen[1] == START + timedelta(minutes=4)


# =============================================================================
# 作り直し（BR3.4、NFR9.31〜NFR9.33）
# =============================================================================


class TestRebuild:
    def test_two_tasks_carry_one_conversation(
        self, store, world, clock, agents, capsys
    ):
        first = _manager(store, world, clock, agents)
        second = _manager(store, world, clock, agents)
        session = _start(first, world)
        first.send_user_message(OWNER, session.id, "質問1")
        second.send_user_message(OWNER, session.id, "質問2")
        first.send_user_message(OWNER, session.id, "質問3")

        messages = store.query_messages(session.id, up_to=9)
        assert [m.seq for m in messages] == list(range(1, 10))
        assert [m.content for m in messages if m.speaker == "user"] == [
            "質問1",
            "質問2",
            "質問3",
        ]
        rebuilt = _events(capsys, "interview.agents_rebuilt")
        assert [e["reason"] for e in rebuilt] == ["missing", "seq_mismatch"]

    def test_history_is_the_user_and_the_persona_itself(
        self, store, world, clock, agents
    ):
        first = _manager(store, world, clock, agents)
        second = _manager(store, world, clock, agents)
        session = _start(first, world)
        first.send_user_message(OWNER, session.id, "質問1")
        agents.calls.clear()
        second.send_user_message(OWNER, session.id, "質問2")

        history = {c["persona"].id: c["initial_messages"] for c in agents.calls}
        assert [m["role"] for m in history["p1"]] == ["user", "assistant"]
        assert history["p1"][0]["content"] == [{"text": "質問1"}]
        assert history["p1"][1]["content"] == [{"text": "佐藤の回答です"}]
        assert "鈴木の回答です" not in json.dumps(history["p1"], ensure_ascii=False)

    def test_a_gap_stops_the_send(self, store, world, clock, agents, client):
        first = _manager(store, world, clock, agents)
        session = _start(first, world)
        first.send_user_message(OWNER, session.id, "質問1")
        client.delete_item(
            TableName=MESSAGES,
            Key={"session_id": {"S": session.id}, "seq": {"N": "2"}},
        )
        second = _manager(store, world, clock, agents)
        with raises_code(CodedError, ErrorCode.INTERVIEW_SESSION_OPERATION_FAILED):
            second.send_user_message(OWNER, session.id, "質問2")
        assert store.get(session.id).last_seq == 3

    def test_local_agents_are_dropped_after_8_idle_hours(
        self, manager, world, clock, agents
    ):
        _start(manager, world)
        assert manager.get_active_sessions_count() == 1
        clock.advance(hours=8, minutes=1)
        _start(manager, world)
        assert manager.get_active_sessions_count() == 1
        assert all(a.dispose.called for a in agents.made[:2])

    def test_local_agents_belong_to_the_owner(self, manager, world, store):
        session = _start(manager, world)
        with raises_code(CodedError, ErrorCode.INTERVIEW_SESSION_NOT_FOUND):
            manager.send_user_message(PEER, session.id, "横取り")
        assert store.get(session.id).last_seq == 0


# =============================================================================
# 添付（BR3.6、NFR1.54）
# =============================================================================


class TestAttachments:
    def test_files_are_kept_by_id(self, manager, world, store, tmp_path, agents):
        world.files.attachable_file.side_effect = lambda p, i: _upload(tmp_path, i)
        session = _start(manager, world)
        manager.send_user_message(OWNER, session.id, "見てください", [FILE_1])
        stored = store.get(session.id)
        assert stored.document_ids == [FILE_1]
        assert stored.documents[0].filename == "資料.txt"
        (first, *_) = store.query_messages(session.id, up_to=1)
        assert first.document_ids == (FILE_1,)
        agents.made[0].set_document_contents.assert_called()

    def test_an_unreadable_new_file_stops_the_send(self, manager, world, store):
        world.files.attachable_file.side_effect = CodedError(
            "no", code=ErrorCode.FILE_NOT_FOUND
        )
        session = _start(manager, world)
        with raises_code(CodedError, ErrorCode.FILE_NOT_FOUND):
            manager.send_user_message(OWNER, session.id, "見て", [FILE_1])
        assert store.get(session.id).last_seq == 0

    def test_rebuild_drops_unreadable_files_and_names_them(
        self, store, world, clock, agents, tmp_path
    ):
        world.files.attachable_file.side_effect = lambda p, i: _upload(tmp_path, i)
        first = _manager(store, world, clock, agents)
        session = _start(first, world)
        first.send_user_message(OWNER, session.id, "見て", [FILE_1])

        world.files.attachable_file.side_effect = CodedError(
            "expired", code=ErrorCode.FILE_NOT_FOUND
        )
        world.files.readable_file.side_effect = CodedError(
            "expired", code=ErrorCode.FILE_NOT_FOUND
        )
        second = _manager(store, world, clock, agents)
        reply = second.send_user_message(OWNER, session.id, "続き")
        assert reply.excluded_files == ["資料.txt"]

    def test_saved_files_are_read_through_the_discussion(
        self, store, world, clock, agents, tmp_path
    ):
        world.files.attachable_file.side_effect = lambda p, i: _upload(tmp_path, i)
        first = _manager(store, world, clock, agents)
        session = _start(first, world)
        first.send_user_message(OWNER, session.id, "見て", [FILE_1])
        first.save_interview_session(OWNER, session.id, "保存名")

        world.files.attachable_file.side_effect = CodedError(
            "attached", code=ErrorCode.FILE_NOT_FOUND
        )
        world.files.readable_file.side_effect = lambda p, i: _upload(tmp_path, i)
        second = _manager(store, world, clock, agents)
        reply = second.send_user_message(OWNER, session.id, "続き")
        assert reply.excluded_files == []
        world.files.readable_file.assert_called_with(OWNER, FILE_1)

    def test_size_is_checked_for_the_models(self, manager, world, tmp_path):
        from src.config import config

        world.files.attachable_file.side_effect = lambda p, i: _upload(
            tmp_path, i, size=5 * 1024 * 1024
        )
        with patch.object(config, "ENABLE_ADDITIONAL_PERSONA_MODELS", True):
            session = _start(
                manager, world, persona_models={"p1": "google.gemma-4-31b"}
            )
        with raises_code(
            InterviewValidationError, ErrorCode.INTERVIEW_MODEL_INPUT_TOO_LARGE
        ):
            manager.send_user_message(OWNER, session.id, "見て", [FILE_1])


# =============================================================================
# 保存（BR3.7、BR3.8）
# =============================================================================


class TestSave:
    def test_first_save_makes_an_interview_discussion(self, manager, world, store):
        session = _start(manager, world)
        manager.send_user_message(OWNER, session.id, "質問1")
        result = manager.save_interview_session(OWNER, session.id, "保存名")

        body = world.store.bodies[result.discussion_id]
        assert body["mode"] == "interview" and body["topic"] == "保存名"
        assert body["message_count"] == 3
        assert body["owner_sub"] == OWNER.sub
        assert body.get("insights", []) == []
        assert store.get(session.id).saved_discussion_id == result.discussion_id

    def test_saving_again_appends_to_the_same_discussion(self, manager, world, store):
        session = _start(manager, world)
        manager.send_user_message(OWNER, session.id, "質問1")
        first = manager.save_interview_session(OWNER, session.id, "保存名")
        manager.send_user_message(OWNER, session.id, "質問2")
        again = manager.save_interview_session(OWNER, session.id, "保存名")

        assert again.discussion_id == first.discussion_id
        assert world.store.bodies[first.discussion_id]["message_count"] == 6
        assert len(world.store.bodies) == 1

    def test_retry_after_the_id_was_not_recorded_reuses_the_discussion(
        self, manager, world, store, tmp_path
    ):
        """初回の保存で議論の ID の記録だけ失敗しても、再保存は同じ議論に戻る。"""
        world.files.attachable_file.side_effect = lambda p, i: _upload(tmp_path, i)
        session = _start(manager, world)
        manager.send_user_message(OWNER, session.id, "見て", [FILE_1])

        real_mark_saved = store.mark_saved
        with patch.object(
            store, "mark_saved", side_effect=InterviewSessionStoreError("down")
        ):
            with raises_code(CodedError, ErrorCode.INTERVIEW_SAVE_FAILED):
                manager.save_interview_session(OWNER, session.id, "保存名")
        assert len(world.store.bodies) == 1
        (first_id,) = world.store.bodies

        # 添付は最初の議論に紐付いた（紐付け前としては読めない）
        world.files.attachable_file.side_effect = CodedError(
            "attached", code=ErrorCode.FILE_NOT_FOUND
        )
        world.files.readable_file.side_effect = lambda p, i: _upload(tmp_path, i)
        with patch.object(store, "mark_saved", side_effect=real_mark_saved):
            result = manager.save_interview_session(OWNER, session.id, "保存名")

        assert result.discussion_id == first_id
        assert result.excluded_files == []
        assert len(world.store.bodies) == 1
        assert world.store.bodies[first_id]["message_count"] == 3
        assert store.get(session.id).saved_discussion_id == first_id

    def test_a_second_save_during_the_first_does_not_lose_messages(
        self, manager, world, store
    ):
        """同じインタビューを同時に保存しても、成立した議論の発言は消えない。"""
        session = _start(manager, world)
        manager.send_user_message(OWNER, session.id, "質問1")

        real_save_new = world.records.save_new
        second: dict[str, Any] = {}

        def save_new_with_a_second_request(principal, discussion, messages):
            # 1 つ目の要求が保存している間に、2 つ目の要求が届く（入れ子は 1 回だけ）
            if "started" not in second:
                second["started"] = True
                try:
                    manager.save_interview_session(OWNER, session.id, "保存名")
                except CodedError as e:
                    second["error"] = e
            return real_save_new(principal, discussion, messages)

        with patch.object(
            world.records, "save_new", side_effect=save_new_with_a_second_request
        ):
            result = manager.save_interview_session(OWNER, session.id, "保存名")

        assert second["error"].code is ErrorCode.CONCURRENT_UPDATE
        assert len(world.store.bodies) == 1
        assert world.store.bodies[result.discussion_id]["message_count"] == 3
        assert len(world.store.messages[result.discussion_id]) == 3
        assert store.get(session.id).saved_discussion_id == result.discussion_id

    def test_a_save_after_the_first_finished_joins_it(self, manager, world, store):
        session = _start(manager, world)
        manager.send_user_message(OWNER, session.id, "質問1")
        first = manager.save_interview_session(OWNER, session.id, "保存名")
        again = manager.save_interview_session(OWNER, session.id, "保存名")
        assert again.discussion_id == first.discussion_id
        assert len(world.store.messages[first.discussion_id]) == 3

    def test_a_failed_first_save_releases_the_mark(self, manager, world, store):
        session = _start(manager, world)
        manager.send_user_message(OWNER, session.id, "質問1")
        with patch.object(
            world.records,
            "save_new",
            side_effect=CodedError("down", code=ErrorCode.DISCUSSION_OPERATION_FAILED),
        ):
            with raises_code(CodedError, ErrorCode.DISCUSSION_OPERATION_FAILED):
                manager.save_interview_session(OWNER, session.id, "保存名")
        result = manager.save_interview_session(OWNER, session.id, "保存名")
        assert world.store.bodies[result.discussion_id]["message_count"] == 3

    def test_unusable_files_are_left_out_and_named(self, manager, world, tmp_path):
        uploads = {
            i: _upload(tmp_path, i, name=f"{i[:2]}.txt") for i in (FILE_1, FILE_2)
        }
        world.files.attachable_file.side_effect = lambda p, i: uploads[i]
        session = _start(manager, world)
        manager.send_user_message(OWNER, session.id, "見て", [FILE_1, FILE_2])

        def only_first(p, i):
            if i == FILE_2:
                raise CodedError("expired", code=ErrorCode.FILE_NOT_FOUND)
            return uploads[i]

        world.files.attachable_file.side_effect = only_first
        world.files.readable_file.side_effect = CodedError(
            "no", code=ErrorCode.FILE_NOT_FOUND
        )
        result = manager.save_interview_session(OWNER, session.id, "保存名")
        assert result.excluded_files == ["22.txt"]
        world.files.attach_all.assert_called_once_with(
            OWNER, [FILE_1], "discussion", result.discussion_id
        )

    def test_unreadable_participant_saves_nothing(self, manager, world):
        session = _start(manager, world)
        manager.send_user_message(OWNER, session.id, "質問1")
        world.unshare("p2")
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.save_interview_session(OWNER, session.id, "保存名")
        assert world.store.bodies == {}

    def test_an_empty_session_cannot_be_saved(self, manager, world):
        session = _start(manager, world)
        with raises_code(
            InterviewManagerError, ErrorCode.INTERVIEW_SAVE_PRECONDITION_NOT_MET
        ):
            manager.save_interview_session(OWNER, session.id, "保存名")


# =============================================================================
# 参照・状態・終了
# =============================================================================


class TestReadAndEnd:
    def test_view_has_the_messages(self, manager, world):
        session = _start(manager, world)
        manager.send_user_message(OWNER, session.id, "質問1")
        view = manager.get_interview_session(OWNER, session.id)
        assert [m.message_type for m in view.messages] == [
            "user_message",
            "statement",
            "statement",
        ]
        assert not view.is_saved

    def test_status(self, manager, world):
        session = _start(manager, world, enable_dataset=True)
        manager.send_user_message(OWNER, session.id, "質問1")
        status = manager.get_session_status(OWNER, session.id)
        assert status["message_count"] == 3
        assert status["has_user_messages"] and status["has_persona_responses"]
        assert status["enable_dataset"] is True
        assert status["agents_active"] is True

    def test_end_removes_the_session(self, manager, world, agents):
        session = _start(manager, world)
        agents.made[0].dispose.side_effect = RuntimeError("dispose error")
        manager.end_interview_session(OWNER, session.id)
        assert agents.made[1].dispose.called
        with raises_code(CodedError, ErrorCode.INTERVIEW_SESSION_NOT_FOUND):
            manager.get_interview_session(OWNER, session.id)
        assert manager.get_active_sessions_count() == 0

    def test_no_manual_cleanup(self, manager):
        assert not hasattr(manager, "cleanup_inactive_sessions")


# =============================================================================
# 文面
# =============================================================================


class TestPrompts:
    def test_system_prompt(self, manager, world):
        prompt = manager._generate_interview_system_prompt(world.persona_bodies["p1"])
        assert "佐藤" in prompt

    def test_first_interaction(self, manager):
        agent = Mock(spec=PersonaAgent)
        agent.get_persona_name.return_value = "佐藤"
        prompt = manager._create_interview_prompt(agent, "質問", [])
        assert "ユーザーからの質問: 「質問」" in prompt

    def test_with_context(self, manager):
        from src.models.message import Message

        agent = Mock(spec=PersonaAgent)
        agent.get_persona_name.return_value = "佐藤"
        context = [
            Message.create_new("user", "ユーザー", "前の質問", "user_message"),
            Message.create_new("p2", "鈴木", "鈴木の答え"),
        ]
        prompt = manager._create_interview_prompt(agent, "新しい質問", context)
        assert "ユーザー: 前の質問" in prompt and "鈴木: 鈴木の答え" in prompt
