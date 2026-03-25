"""
インタビュールーター（interview.py）のテスト

インタビューセッション作成、メッセージ送信、保存、SSEストリーミングをテストします。
"""

from unittest.mock import Mock, patch
from datetime import datetime

from src.models.message import Message
from src.managers.interview_manager import (
    InterviewSession,
    InterviewSessionNotFoundError,
)


class TestCreateInterviewSessionEndpoint:
    """インタビューセッション作成エンドポイントのテスト"""

    @patch("web.routers.interview.get_interview_manager")
    @patch("web.routers.interview.get_persona_manager")
    def test_create_session_success(
        self, mock_get_persona, mock_get_interview, client, sample_persona
    ):
        """セッション作成が成功することを確認"""
        # ペルソナマネージャーのモック
        mock_persona_manager = Mock()
        mock_persona_manager.get_persona.return_value = sample_persona
        mock_get_persona.return_value = mock_persona_manager

        # インタビューマネージャーのモック
        mock_session = InterviewSession(
            id="test-session-id",
            participants=[sample_persona.id],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
        )
        mock_interview_manager = Mock()
        mock_interview_manager.start_interview_session.return_value = mock_session
        mock_get_interview.return_value = mock_interview_manager

        response = client.post(
            "/interview/create", data={"persona_ids": [sample_persona.id]}
        )

        assert response.status_code == 200
        data = response.json()
        assert "session_id" in data
        assert data["session_id"] == "test-session-id"

    @patch("web.routers.interview.get_persona_manager")
    def test_create_session_no_personas(self, mock_get_manager, client):
        """ペルソナなしでエラーを返すことを確認"""
        # FastAPIのForm(...)は空リストを許可するが、ルーター内で400を返す
        # 空のpersona_idsパラメータを送信
        response = client.post(
            "/interview/create",
            data={},  # persona_idsを送信しない
        )

        # FastAPIはForm(...)で必須パラメータがない場合422を返す
        assert response.status_code == 422

    @patch("web.routers.interview.get_persona_manager")
    def test_create_session_too_many_personas(self, mock_get_manager, client):
        """ペルソナ数過多でエラーを返すことを確認"""
        mock_manager = Mock()
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/interview/create",
            data={"persona_ids": ["p1", "p2", "p3", "p4", "p5", "p6"]},
        )

        assert response.status_code == 400
        data = response.json()
        assert "最大5つのペルソナまで" in data["error"]

    @patch("web.routers.interview.get_persona_manager")
    def test_create_session_persona_not_found(self, mock_get_manager, client):
        """存在しないペルソナでエラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.get_persona.return_value = None
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/interview/create", data={"persona_ids": ["non-existent-id"]}
        )

        assert response.status_code == 404
        data = response.json()
        assert data["error_type"] == "persona_not_found"

    @patch("web.routers.interview.get_interview_manager")
    @patch("web.routers.interview.get_persona_manager")
    def test_create_session_with_memory_enabled(
        self, mock_get_persona, mock_get_interview, client, sample_persona
    ):
        """長期記憶を有効にしたセッション作成が成功することを確認"""
        # ペルソナマネージャーのモック
        mock_persona_manager = Mock()
        mock_persona_manager.get_persona.return_value = sample_persona
        mock_get_persona.return_value = mock_persona_manager

        # インタビューマネージャーのモック
        mock_session = InterviewSession(
            id="test-session-id",
            participants=[sample_persona.id],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
            enable_memory=True,
        )
        mock_interview_manager = Mock()
        mock_interview_manager.start_interview_session.return_value = mock_session
        mock_get_interview.return_value = mock_interview_manager

        response = client.post(
            "/interview/create",
            data={"persona_ids": [sample_persona.id], "enable_memory": "true"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "session_id" in data
        assert data["session_id"] == "test-session-id"
        assert data["enable_memory"] is True

        # start_interview_sessionがenable_memory=Trueで呼ばれたことを確認
        mock_interview_manager.start_interview_session.assert_called_once()
        call_kwargs = mock_interview_manager.start_interview_session.call_args
        assert call_kwargs.kwargs.get("enable_memory") is True


class TestSendMessageEndpoint:
    """メッセージ送信エンドポイントのテスト"""

    @patch("web.routers.interview.get_interview_manager")
    def test_send_message_success(self, mock_get_manager, client):
        """メッセージ送信が成功することを確認"""
        mock_response = Message.create_new(
            persona_id="persona-1",
            persona_name="田中花子",
            content="ご質問にお答えします。",
            message_type="statement",
        )

        mock_manager = Mock()
        mock_manager.send_user_message.return_value = [mock_response]
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/interview/test-session/message",
            data={"message": "こんにちは、質問があります"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "responses" in data
        assert len(data["responses"]) == 1
        assert data["responses"][0]["persona_name"] == "田中花子"

    @patch("web.routers.interview.get_interview_manager")
    def test_send_message_empty(self, mock_get_manager, client):
        """空メッセージでエラーを返すことを確認"""
        # FastAPIのForm(...)は空文字列を許可するが、ルーター内で400を返す
        # ただし、空文字列の場合FastAPIが422を返す場合がある
        response = client.post("/interview/test-session/message", data={"message": ""})

        # FastAPIは空文字列を422で拒否する場合がある
        assert response.status_code in [400, 422]

    @patch("web.routers.interview.get_interview_manager")
    def test_send_message_whitespace_only(self, mock_get_manager, client):
        """空白のみのメッセージでエラーを返すことを確認"""
        response = client.post(
            "/interview/test-session/message", data={"message": "   "}
        )

        assert response.status_code == 400
        data = response.json()
        assert data["error_type"] == "validation_error"

    @patch("web.routers.interview.get_interview_manager")
    def test_send_message_too_long(self, mock_get_manager, client):
        """長すぎるメッセージでエラーを返すことを確認"""
        long_message = "あ" * 2001

        response = client.post(
            "/interview/test-session/message", data={"message": long_message}
        )

        assert response.status_code == 400
        data = response.json()
        assert "長すぎます" in data["error"]

    @patch("web.routers.interview.get_interview_manager")
    def test_send_message_session_not_found(self, mock_get_manager, client):
        """存在しないセッションでエラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.send_user_message.side_effect = InterviewSessionNotFoundError(
            "セッションが見つかりません"
        )
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/interview/non-existent-session/message",
            data={"message": "テストメッセージ"},
        )

        assert response.status_code == 404
        data = response.json()
        assert data["error_type"] == "session_not_found"


class TestGetMessagesEndpoint:
    """メッセージ履歴取得エンドポイントのテスト"""

    @patch("web.routers.interview.get_interview_manager")
    def test_get_messages_success(self, mock_get_manager, client):
        """メッセージ履歴取得が成功することを確認"""
        mock_session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[
                Message.create_new("user", "User", "質問です", "user_message"),
                Message.create_new("persona-1", "田中花子", "回答です", "statement"),
            ],
            created_at=datetime.now(),
            is_saved=False,
        )

        mock_manager = Mock()
        mock_manager.get_interview_session.return_value = mock_session
        mock_get_manager.return_value = mock_manager

        response = client.get("/interview/test-session/messages")

        assert response.status_code == 200
        data = response.json()
        assert "messages" in data
        assert len(data["messages"]) == 2

    @patch("web.routers.interview.get_interview_manager")
    def test_get_messages_session_not_found(self, mock_get_manager, client):
        """存在しないセッションでエラーを返すことを確認"""
        from src.managers.interview_manager import InterviewManagerError

        mock_manager = Mock()
        mock_manager.get_interview_session.side_effect = InterviewManagerError(
            "セッションが見つかりません"
        )
        mock_get_manager.return_value = mock_manager

        response = client.get("/interview/non-existent-session/messages")

        assert response.status_code == 404


class TestSaveInterviewSessionEndpoint:
    """インタビューセッション保存エンドポイントのテスト"""

    @patch("web.routers.interview.get_interview_manager")
    def test_save_session_success(self, mock_get_manager, client):
        """セッション保存が成功することを確認"""
        mock_manager = Mock()
        mock_manager.get_session_status.return_value = {
            "is_saved": False,
            "message_count": 2,
            "has_user_messages": True,
            "has_persona_responses": True,
        }
        mock_manager.save_interview_session.return_value = "discussion-id-123"
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/interview/test-session/save", data={"session_name": "テストセッション"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "discussion_id" in data
        assert data["discussion_id"] == "discussion-id-123"

    @patch("web.routers.interview.get_interview_manager")
    def test_save_session_already_saved(self, mock_get_manager, client):
        """既に保存済みのセッションで適切なレスポンスを返すことを確認"""
        mock_manager = Mock()
        mock_manager.get_session_status.return_value = {
            "is_saved": True,
            "message_count": 2,
        }
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/interview/test-session/save", data={"session_name": "テストセッション"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["already_saved"] is True

    @patch("web.routers.interview.get_interview_manager")
    def test_save_session_no_messages(self, mock_get_manager, client):
        """メッセージなしでエラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.get_session_status.return_value = {
            "is_saved": False,
            "message_count": 0,
        }
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/interview/test-session/save", data={"session_name": "テストセッション"}
        )

        assert response.status_code == 400
        data = response.json()
        assert "メッセージがありません" in data["error"]

    @patch("web.routers.interview.get_interview_manager")
    def test_save_session_empty_name(self, mock_get_manager, client):
        """空のセッション名でエラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.get_session_status.return_value = {
            "is_saved": False,
            "message_count": 2,
            "has_user_messages": True,
            "has_persona_responses": True,
        }
        mock_get_manager.return_value = mock_manager

        # 空文字列のセッション名を送信
        response = client.post(
            "/interview/test-session/save", data={"session_name": ""}
        )

        # FastAPIは空文字列を422で拒否する場合がある
        assert response.status_code in [400, 422]


class TestEndInterviewSessionEndpoint:
    """インタビューセッション終了エンドポイントのテスト"""

    @patch("web.routers.interview.get_interview_manager")
    def test_end_session_success(self, mock_get_manager, client):
        """セッション終了が成功することを確認"""
        mock_manager = Mock()
        mock_manager.end_interview_session.return_value = None
        mock_get_manager.return_value = mock_manager

        response = client.delete("/interview/test-session")

        assert response.status_code == 200
        data = response.json()
        assert "終了されました" in data["message"]

    @patch("web.routers.interview.get_interview_manager")
    def test_end_session_not_found(self, mock_get_manager, client):
        """存在しないセッションでエラーを返すことを確認"""
        from src.managers.interview_manager import InterviewManagerError

        mock_manager = Mock()
        mock_manager.end_interview_session.side_effect = InterviewManagerError(
            "セッションが見つかりません"
        )
        mock_get_manager.return_value = mock_manager

        response = client.delete("/interview/non-existent-session")

        assert response.status_code == 400


class TestInterviewChatPage:
    """インタビューチャットページのテスト"""

    @patch("web.routers.interview.get_persona_manager")
    @patch("web.routers.interview.get_interview_manager")
    def test_chat_page_loads(
        self, mock_get_interview, mock_get_persona, client, sample_persona
    ):
        """チャットページが正常に読み込まれることを確認"""
        mock_session = InterviewSession(
            id="test-session",
            participants=[sample_persona.id],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
        )

        mock_interview_manager = Mock()
        mock_interview_manager.get_interview_session.return_value = mock_session
        mock_get_interview.return_value = mock_interview_manager

        mock_persona_manager = Mock()
        mock_persona_manager.get_persona.return_value = sample_persona
        mock_get_persona.return_value = mock_persona_manager

        response = client.get("/interview/chat/test-session")

        assert response.status_code == 200

    @patch("web.routers.interview.get_interview_manager")
    def test_chat_page_session_not_found(self, mock_get_manager, client):
        """存在しないセッションで404エラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.get_interview_session.side_effect = InterviewSessionNotFoundError(
            "セッションが見つかりません"
        )
        mock_get_manager.return_value = mock_manager

        response = client.get("/interview/chat/non-existent-session")

        assert response.status_code == 404


class TestInterviewStatsEndpoint:
    """インタビュー統計エンドポイントのテスト"""

    @patch("web.routers.interview.get_interview_manager")
    def test_get_stats_success(self, mock_get_manager, client):
        """統計取得が成功することを確認"""
        mock_manager = Mock()
        mock_manager.get_active_sessions_count.return_value = 5
        mock_get_manager.return_value = mock_manager

        response = client.get("/interview/stats")

        assert response.status_code == 200
        data = response.json()
        assert "active_sessions" in data
        assert data["active_sessions"] == 5


class TestCleanupEndpoint:
    """セッションクリーンアップエンドポイントのテスト"""

    @patch("web.routers.interview.get_interview_manager")
    def test_cleanup_success(self, mock_get_manager, client):
        """クリーンアップが成功することを確認"""
        mock_manager = Mock()
        mock_manager.cleanup_inactive_sessions.return_value = 3
        mock_get_manager.return_value = mock_manager

        response = client.post("/interview/cleanup")

        assert response.status_code == 200
        data = response.json()
        assert data["cleaned_sessions"] == 3
