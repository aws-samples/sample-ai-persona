"""
インタビュールーター（interview.py）のテスト

U6: セッションはアプリの外（moto の InterviewSessionStore）に置き、持ち主だけが
使える。Router は利用者と添付の ID を Manager に渡すだけ。BR3.1〜BR3.8、BR7.2、
NFR1.48、NFR1.54、NFR1.56、NFR1.60、AC2.4.1〜AC2.4.3。
"""

import json
from contextlib import ExitStack
from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock, patch

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.managers.interview_manager import InterviewManager  # noqa: E402
from src.managers.persona_manager import PersonaManager  # noqa: E402
from src.models.errors import ErrorCode  # noqa: E402
from src.models.interview_session import InterviewMessage  # noqa: E402
from src.services.interview_session_store import InterviewSessionStore  # noqa: E402
from tests.api.u6_world import ME, ApiWorld, _agent  # noqa: E402
from tests.conftest import MEMBER_B  # noqa: E402
from tests.dialog_helpers import dialog_markup  # noqa: E402

REGION = "ap-northeast-1"
SESSIONS = "InterviewSessionsApi"
MESSAGES = "InterviewMessagesApi"


def _tables(client: Any) -> None:
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


class InterviewWorld:
    def __init__(self, ddb: Any) -> None:
        self.api = ApiWorld()
        self.store = InterviewSessionStore(
            region=REGION, sessions_table=SESSIONS, messages_table=MESSAGES, client=ddb
        )
        self.agent_service = Mock()
        self.agent_service.create_persona_agent.side_effect = _agent
        w = self.api.world
        self.manager = InterviewManager(
            agent_service=self.agent_service,
            database_service=Mock(),
            dataset_analysis_service=Mock(),
            binding_store=Mock(list_for_persona=Mock(return_value=[])),
            session_store=self.store,
            discussion_records=w.records,
            participants=w.participants,
            file_attachments=w.files,
            resource_lookup=w.lookup,
        )
        self.personas = PersonaManager(
            database_service=Mock(), participants=w.participants
        )
        self.files = Mock()

    def start(self, *persona_ids: str):
        return self.manager.start_interview_session(
            ME.principal(), [self.api.world.persona_bodies[p] for p in persona_ids]
        )


@pytest.fixture
def iw(client, monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    ME.login(client)
    with moto.mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        _tables(ddb)
        world = InterviewWorld(ddb)
        with ExitStack() as stack:
            for target, value in [
                ("web.routers.interview.get_interview_manager", world.manager),
                ("web.routers.interview.get_persona_manager", world.personas),
                ("web.routers.interview.get_file_manager", world.files),
            ]:
                stack.enter_context(patch(target, return_value=value))
            yield world


def _sse(response) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


# =============================================================================
# 開始（BR3.1）
# =============================================================================


class TestCreate:
    def test_owner_starts_a_session(self, iw, client):
        response = client.post("/interview/create", data={"persona_ids": ["p1", "p2"]})
        assert response.status_code == 200
        data = response.json()
        stored = iw.store.get(data["session_id"])
        assert stored.owner_sub == ME.sub
        assert [p["id"] for p in data["participants"]] == ["p1", "p2"]

    def test_unreadable_persona_is_not_found(self, iw, client):
        response = client.post("/interview/create", data={"persona_ids": ["p1", "p3"]})
        assert response.status_code == 404
        assert response.json()["error_type"] == "persona_not_found"
        iw.agent_service.create_persona_agent.assert_not_called()

    def test_too_many_personas(self, iw, client):
        from src.models.authz import AccessTarget, ResourceType
        from tests.unit.discussion_world import persona

        world = iw.api.world
        ids = [f"x{i}" for i in range(6)]
        for pid in ids:
            world.personas.targets[pid] = AccessTarget(
                ResourceType.PERSONA, pid, owner_sub=ME.sub
            )
            world.persona_bodies[pid] = persona(pid, ME.sub)
        response = client.post("/interview/create", data={"persona_ids": ids})
        assert response.status_code == 400
        assert response.json()["error_type"] == "validation_error"

    def test_invalid_memory_mode(self, iw, client):
        response = client.post(
            "/interview/create",
            data={"persona_ids": ["p1"], "enable_memory": "true", "memory_mode": "x"},
        )
        assert response.status_code == 400
        assert response.json()["error_type"] == "validation_error"

    def test_persona_models_are_passed(self, iw, client):
        response = client.post(
            "/interview/create",
            data={
                "persona_ids": ["p1"],
                "persona_models[p1]": "global.anthropic.claude-sonnet-5",
            },
        )
        assert response.status_code == 200
        stored = iw.store.get(response.json()["session_id"])
        assert stored.persona_models == {"p1": "global.anthropic.claude-sonnet-5"}

    def test_role_is_required(self, iw, client):
        from tests.conftest import NO_ROLE

        NO_ROLE.login(client)
        response = client.post("/interview/create", data={"persona_ids": ["p1"]})
        # ロールのない利用者は AuthMiddleware が止める（U2 BR2.8）
        assert response.status_code == 403
        iw.agent_service.create_persona_agent.assert_not_called()


# =============================================================================
# 持ち主だけ（BR3.2）
# =============================================================================


class TestOwnerOnly:
    def test_others_get_not_found_everywhere(self, iw, client):
        session = iw.start("p1")
        MEMBER_B.login(client)
        for method, path, data in [
            ("get", f"/interview/chat/{session.id}", None),
            ("get", f"/interview/{session.id}/messages", None),
            ("get", f"/interview/{session.id}/status", None),
            ("post", f"/interview/{session.id}/message", {"message": "こんにちは"}),
            ("post", f"/interview/{session.id}/save", {"session_name": "名前"}),
            ("delete", f"/interview/{session.id}", None),
        ]:
            response = (
                getattr(client, method)(path, data=data)
                if data
                else getattr(client, method)(path)
            )
            assert response.status_code == 404, path
        assert iw.store.get(session.id).last_seq == 0

    def test_others_stream_gets_the_stop_event(self, iw, client):
        session = iw.start("p1")
        MEMBER_B.login(client)
        response = client.post(
            f"/interview/{session.id}/message/stream", data={"message": "横取り"}
        )
        events = _sse(response)
        assert events[-1]["type"] == "error"
        assert events[-1]["error_type"] == "conversation_stopped"
        assert iw.store.get(session.id).last_seq == 0

    def test_manual_cleanup_is_gone(self, iw, client):
        assert client.post("/interview/cleanup").status_code in (404, 405)


# =============================================================================
# 送信（BR3.3、BR3.6、BR7.2、NFR1.54、NFR1.60）
# =============================================================================


class TestSend:
    def test_send_and_read_back(self, iw, client):
        session = iw.start("p1", "p2")
        response = client.post(
            f"/interview/{session.id}/message", data={"message": "こんにちは"}
        )
        assert response.status_code == 200
        data = response.json()
        assert [r["persona_id"] for r in data["responses"]] == ["p1", "p2"]
        assert data["excluded_files"] == []

        messages = client.get(f"/interview/{session.id}/messages").json()["messages"]
        assert [m["message_type"] for m in messages] == [
            "user_message",
            "statement",
            "statement",
        ]

    def test_stream_sends_each_reply_and_completes(self, iw, client):
        session = iw.start("p1", "p2")
        response = client.post(
            f"/interview/{session.id}/message/stream", data={"message": "こんにちは"}
        )
        kinds = [e["type"] for e in _sse(response)]
        assert kinds.count("message_end") == 2
        assert kinds[-1] == "complete"
        assert iw.store.get(session.id).last_seq == 3

    def test_empty_message(self, iw, client):
        session = iw.start("p1")
        response = client.post(
            f"/interview/{session.id}/message", data={"message": "   "}
        )
        assert response.status_code == 400

    def test_unreadable_participant_stops_the_conversation(self, iw, client):
        session = iw.start("p1", "p2")
        iw.api.world.unshare("p2")
        response = client.post(
            f"/interview/{session.id}/message", data={"message": "こんにちは"}
        )
        assert response.status_code == 404
        assert response.json()["error_type"] == "conversation_stopped"
        assert iw.store.get(session.id).last_seq == 0

    def test_a_reply_in_progress_is_a_concurrent_update(self, iw, client):
        session = iw.start("p1")
        stored = iw.store.get(session.id)
        now = datetime.now(timezone.utc)
        iw.store.append_user_message(
            stored,
            InterviewMessage.create_new(
                session_id=session.id,
                seq=1,
                speaker="user",
                persona_id="user",
                persona_name_at_time="ユーザー",
                content="別の送信",
                now=now,
            ),
            token="other",
            now=now,
        )
        sent = client.post(
            f"/interview/{session.id}/message", data={"message": "こんにちは"}
        )
        assert sent.status_code == 503
        assert sent.json()["error_type"] == "concurrent_update"

        saved = client.post(
            f"/interview/{session.id}/save", data={"session_name": "名前"}
        )
        assert saved.status_code == 503
        assert saved.json()["error_type"] == "concurrent_update"

    def test_attachments_are_uploaded_first_and_passed_by_id(
        self, iw, client, tmp_path
    ):
        from src.managers.file_manager import FileMetadata
        from src.models.uploaded_file import UploadedFile

        path = tmp_path / "memo.txt"
        path.write_bytes(b"hello")
        uploaded = UploadedFile.create_new(
            owner_sub=ME.sub,
            file_type="discussion_document",
            original_filename="memo.txt",
            file_size=5,
            file_hash="h",
            mime_type="text/plain",
            now=datetime.now(timezone.utc),
            file_id="11111111-1111-4111-8111-111111111111",
        ).with_path(str(path))
        iw.files.upload_interview_attachment.return_value = FileMetadata(
            file_id=uploaded.id,
            original_filename="memo.txt",
            saved_filename="x",
            file_path=str(path),
            file_size=5,
            file_hash="h",
            mime_type="text/plain",
            uploaded_at=uploaded.uploaded_at,
        )
        iw.api.world.files.attachable_file.side_effect = lambda p, i: uploaded
        session = iw.start("p1")

        response = client.post(
            f"/interview/{session.id}/message/stream",
            data={"message": "見てください"},
            files=[("files", ("memo.txt", b"hello", "text/plain"))],
        )

        assert _sse(response)[-1]["type"] == "complete"
        principal, content, filename, mime = (
            iw.files.upload_interview_attachment.call_args.args
        )
        assert (principal.sub, content, filename, mime) == (
            ME.sub,
            b"hello",
            "memo.txt",
            "text/plain",
        )
        assert iw.store.get(session.id).document_ids == [uploaded.id]

    def test_an_upload_error_is_shown_before_sending(self, iw, client):
        from src.managers.file_manager import FileUploadError

        iw.files.upload_interview_attachment.side_effect = FileUploadError(
            "unsupported (ClientError) s3://secret",
            code=ErrorCode.FILE_MIME_UNSUPPORTED,
        )
        session = iw.start("p1")
        response = client.post(
            f"/interview/{session.id}/message/stream",
            data={"message": "見て"},
            files=[("files", ("a.zip", b"PK", "application/zip"))],
        )
        events = _sse(response)
        assert events[-1]["type"] == "error"
        assert events[-1]["error_type"] == "validation_error"
        assert "secret" not in response.text
        assert iw.store.get(session.id).last_seq == 0


# =============================================================================
# 保存・終了・状態（BR3.7、BR3.8）
# =============================================================================


class TestSaveAndEnd:
    def test_save_twice_writes_to_one_discussion(self, iw, client):
        session = iw.start("p1", "p2")
        client.post(f"/interview/{session.id}/message", data={"message": "質問1"})
        first = client.post(
            f"/interview/{session.id}/save", data={"session_name": "保存名"}
        )
        assert first.status_code == 200
        discussion_id = first.json()["discussion_id"]
        assert first.json()["excluded_files"] == []

        client.post(f"/interview/{session.id}/message", data={"message": "質問2"})
        again = client.post(
            f"/interview/{session.id}/save", data={"session_name": "保存名"}
        )
        assert again.json()["discussion_id"] == discussion_id
        body = iw.api.world.store.bodies[discussion_id]
        assert body["mode"] == "interview" and body["message_count"] == 6

    def test_nothing_to_save(self, iw, client):
        session = iw.start("p1")
        response = client.post(
            f"/interview/{session.id}/save", data={"session_name": "保存名"}
        )
        assert response.status_code == 400

    def test_name_is_required_and_bounded(self, iw, client):
        session = iw.start("p1")
        assert (
            client.post(
                f"/interview/{session.id}/save", data={"session_name": "  "}
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/interview/{session.id}/save", data={"session_name": "あ" * 101}
            ).status_code
            == 400
        )

    def test_end_makes_the_session_unusable(self, iw, client):
        session = iw.start("p1")
        assert client.delete(f"/interview/{session.id}").status_code == 200
        assert client.get(f"/interview/{session.id}/status").status_code == 404

    def test_status(self, iw, client):
        session = iw.start("p1")
        status = client.get(f"/interview/{session.id}/status").json()["session_status"]
        assert status["session_id"] == session.id
        assert status["message_count"] == 0

    def test_stats_are_this_tasks_agents(self, iw, client):
        iw.start("p1")
        assert client.get("/interview/stats").json()["active_sessions"] == 1


# =============================================================================
# チャット画面
# =============================================================================


class TestChatPage:
    def test_chat_page_loads_with_the_messages(self, iw, client):
        session = iw.start("p1")
        client.post(f"/interview/{session.id}/message", data={"message": "質問1"})
        html = client.get(f"/interview/chat/{session.id}").text
        assert "質問1" in html
        assert "佐藤" in html

    def test_missing_session_is_not_found(self, iw, client):
        assert client.get("/interview/chat/nonexistent").status_code == 404

    def test_unreadable_participant_shows_the_stop_panel(self, iw, client):
        session = iw.start("p1", "p2")
        iw.api.world.unshare("p2")
        html = client.get(f"/interview/chat/{session.id}").text
        assert "data-conversation-stopped" in html
        assert "このインタビューは続けられません" in html

    def test_save_name_dialog_keeps_e2e_selectors_and_fetch(self, iw, client):
        session = iw.start("p1")
        html = client.get(f"/interview/chat/{session.id}").text

        dialog_markup(html, "save-name-dialog")
        for selector in (
            'id="save-session-btn"',
            'id="session-name-input"',
            'id="confirm-save-btn"',
        ):
            assert selector in html
        assert 'data-dialog-close="confirm"' in html
        assert "openDialog('save-name-dialog')" in html
        assert "fetch(`/interview/${sessionId}/save`" in html
        assert "'X-Requested-With': 'XMLHttpRequest'" in html
