"""
Interview Manager の単体テスト
"""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime

from src.managers.interview_manager import (
    InterviewManager,
    InterviewManagerError,
    InterviewSession,
)
from src.models.persona import Persona
from src.models.message import Message


class TestInterviewSession:
    """InterviewSession のテストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化"""
        self.session = InterviewSession(
            id="test-session-1",
            participants=["persona-1", "persona-2"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
        )

    def test_interview_session_initialization(self):
        """InterviewSession の初期化テスト"""
        assert self.session.id == "test-session-1"
        assert self.session.participants == ["persona-1", "persona-2"]
        assert self.session.messages == []
        assert self.session.is_saved is False
        assert self.session.enable_memory is False  # デフォルト値

    def test_interview_session_with_memory_enabled(self):
        """長期記憶を有効にしたInterviewSession の初期化テスト"""
        session = InterviewSession(
            id="test-session-memory",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
            enable_memory=True,
        )
        assert session.enable_memory is True

    def test_add_user_message(self):
        """ユーザーメッセージ追加テスト"""
        content = "こんにちは、質問があります"
        new_session = self.session.add_user_message(content)

        # 新しいセッションが返されることを確認
        assert new_session is not self.session
        assert len(new_session.messages) == 1

        # メッセージの内容を確認
        message = new_session.messages[0]
        assert message.persona_id == "user"
        assert message.persona_name == "User"
        assert message.content == content
        assert message.message_type == "user_message"

    def test_add_user_message_preserves_enable_memory(self):
        """ユーザーメッセージ追加時にenable_memoryが保持されることを確認"""
        session = InterviewSession(
            id="test-session-memory",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
            enable_memory=True,
        )
        new_session = session.add_user_message("テストメッセージ")
        assert new_session.enable_memory is True

    def test_add_persona_response(self):
        """ペルソナ応答追加テスト"""
        persona_id = "persona-1"
        persona_name = "田中太郎"
        content = "ご質問にお答えします"

        new_session = self.session.add_persona_response(
            persona_id, persona_name, content
        )

        # 新しいセッションが返されることを確認
        assert new_session is not self.session
        assert len(new_session.messages) == 1

        # メッセージの内容を確認
        message = new_session.messages[0]
        assert message.persona_id == persona_id
        assert message.persona_name == persona_name
        assert message.content == content
        assert message.message_type == "statement"

    def test_add_persona_response_preserves_enable_memory(self):
        """ペルソナ応答追加時にenable_memoryが保持されることを確認"""
        session = InterviewSession(
            id="test-session-memory",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
            enable_memory=True,
        )
        new_session = session.add_persona_response("persona-1", "田中太郎", "応答")
        assert new_session.enable_memory is True


class TestInterviewManager:
    """InterviewManager のテストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化"""
        # テスト用ペルソナデータ
        self.test_personas = [
            Persona(
                id="persona-1",
                name="田中太郎",
                age=35,
                occupation="会社員",
                background="IT企業で働く中堅社員",
                values=["効率性", "品質"],
                pain_points=["時間不足"],
                goals=["キャリアアップ"],
                created_at=datetime.now(),
                updated_at=datetime.now(),
            ),
            Persona(
                id="persona-2",
                name="佐藤花子",
                age=28,
                occupation="デザイナー",
                background="フリーランスデザイナー",
                values=["創造性", "美しさ"],
                pain_points=["収入の不安定さ"],
                goals=["独立"],
                created_at=datetime.now(),
                updated_at=datetime.now(),
            ),
        ]

        # モックサービスを作成
        self.mock_agent_service = Mock()
        self.mock_database_service = Mock()

        # InterviewManager を初期化
        self.interview_manager = InterviewManager(
            self.mock_agent_service, self.mock_database_service
        )

    def test_interview_manager_initialization(self):
        """InterviewManager の初期化テスト"""
        assert self.interview_manager is not None
        assert self.interview_manager._active_sessions == {}
        assert self.interview_manager._session_agents == {}

    @patch("src.managers.interview_manager.uuid.uuid4")
    def test_start_interview_session_success(self, mock_uuid):
        """インタビューセッション開始成功テスト"""
        # モックの設定
        mock_uuid.return_value = "test-session-id"

        # ペルソナエージェントのモックを作成
        mock_persona_agents = [
            Mock(get_persona_id=Mock(return_value="persona-1")),
            Mock(get_persona_id=Mock(return_value="persona-2")),
        ]

        # agent_service のメソッドをモック
        self.mock_agent_service.generate_persona_system_prompt.return_value = (
            "基本プロンプト"
        )

        # create_persona_agents メソッドをモック
        self.interview_manager.create_persona_agents = Mock(
            return_value=mock_persona_agents
        )

        # インタビューセッションを開始
        session = self.interview_manager.start_interview_session(self.test_personas)

        # セッションが正しく作成されたことを確認
        assert session.id == "test-session-id"
        assert session.participants == ["persona-1", "persona-2"]
        assert session.messages == []
        assert session.is_saved is False

        # アクティブセッションに追加されたことを確認
        assert "test-session-id" in self.interview_manager._active_sessions
        assert "test-session-id" in self.interview_manager._session_agents

    def test_start_interview_session_no_personas(self):
        """ペルソナなしでのインタビューセッション開始エラーテスト"""
        with pytest.raises(InterviewManagerError) as exc_info:
            self.interview_manager.start_interview_session([])

        assert "最低1つのペルソナが必要" in str(exc_info.value)

    def test_start_interview_session_too_many_personas(self):
        """ペルソナ数過多でのインタビューセッション開始エラーテスト"""
        # 6つのペルソナを作成（上限は5つ）
        too_many_personas = [Mock(id=f"persona-{i}") for i in range(6)]

        with pytest.raises(InterviewManagerError) as exc_info:
            self.interview_manager.start_interview_session(too_many_personas)

        assert "最大5つのペルソナまで" in str(exc_info.value)

    def test_send_user_message_success(self):
        """ユーザーメッセージ送信成功テスト"""
        # セッションを準備
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
        )

        # ペルソナエージェントのモックを作成
        mock_persona_agent = Mock()
        mock_persona_agent.get_persona_id.return_value = "persona-1"
        mock_persona_agent.get_persona_name.return_value = "田中太郎"
        mock_persona_agent.respond.return_value = "こんにちは！ご質問をどうぞ。"

        # アクティブセッションに追加
        self.interview_manager._active_sessions["test-session"] = session
        self.interview_manager._session_agents["test-session"] = [mock_persona_agent]

        # ユーザーメッセージを送信
        message = "こんにちは、質問があります"
        responses = self.interview_manager.send_user_message("test-session", message)

        # 応答が正しく生成されたことを確認
        assert len(responses) == 1
        assert responses[0].persona_id == "persona-1"
        assert responses[0].persona_name == "田中太郎"
        assert responses[0].content == "こんにちは！ご質問をどうぞ。"

        # セッションが更新されたことを確認
        updated_session = self.interview_manager._active_sessions["test-session"]
        assert len(updated_session.messages) == 2  # ユーザーメッセージ + ペルソナ応答

    def test_send_user_message_session_not_found(self):
        """存在しないセッションへのメッセージ送信エラーテスト"""
        from src.managers.interview_manager import InterviewSessionNotFoundError

        with pytest.raises(InterviewSessionNotFoundError) as exc_info:
            self.interview_manager.send_user_message(
                "nonexistent-session", "test message"
            )

        assert "インタビューセッションが見つかりません" in str(exc_info.value)

    def test_send_user_message_empty_message(self):
        """空メッセージ送信エラーテスト"""
        from src.managers.interview_manager import InterviewValidationError

        # セッションを準備
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
        )
        self.interview_manager._active_sessions["test-session"] = session

        with pytest.raises(InterviewValidationError) as exc_info:
            self.interview_manager.send_user_message("test-session", "")

        assert "メッセージが指定されていません" in str(exc_info.value)

    def test_save_interview_session_success(self):
        """インタビューセッション保存成功テスト"""
        # メッセージを含むセッションを準備
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[
                Message.create_new("user", "User", "質問です", "user_message"),
                Message.create_new("persona-1", "田中太郎", "回答です", "statement"),
            ],
            created_at=datetime.now(),
            is_saved=False,
        )

        self.interview_manager._active_sessions["test-session"] = session

        # データベースサービスのモック設定
        self.mock_database_service.save_discussion.return_value = "discussion-id-123"

        # セッションを保存
        discussion_id = self.interview_manager.save_interview_session("test-session")

        # 保存が成功したことを確認
        assert discussion_id == "discussion-id-123"

        # データベースサービスが呼ばれたことを確認
        self.mock_database_service.save_discussion.assert_called_once()

        # セッションが保存済みとしてマークされたことを確認
        updated_session = self.interview_manager._active_sessions["test-session"]
        assert updated_session.is_saved is True

    def test_save_interview_session_not_found(self):
        """存在しないセッションの保存エラーテスト"""
        from src.managers.interview_manager import InterviewSessionNotFoundError

        with pytest.raises(InterviewSessionNotFoundError) as exc_info:
            self.interview_manager.save_interview_session("nonexistent-session")

        assert "インタビューセッションが見つかりません" in str(exc_info.value)

    def test_save_interview_session_no_messages(self):
        """メッセージなしセッションの保存エラーテスト"""
        # メッセージなしのセッションを準備
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
        )

        self.interview_manager._active_sessions["test-session"] = session

        with pytest.raises(InterviewManagerError) as exc_info:
            self.interview_manager.save_interview_session("test-session")

        assert "メッセージがありません" in str(exc_info.value)

    def test_get_interview_session_success(self):
        """インタビューセッション取得成功テスト"""
        # セッションを準備
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
        )

        self.interview_manager._active_sessions["test-session"] = session

        # セッションを取得
        retrieved_session = self.interview_manager.get_interview_session("test-session")

        # 正しいセッションが取得されたことを確認
        assert retrieved_session == session

    def test_get_interview_session_not_found(self):
        """存在しないセッションの取得エラーテスト"""
        from src.managers.interview_manager import InterviewSessionNotFoundError

        with pytest.raises(InterviewSessionNotFoundError) as exc_info:
            self.interview_manager.get_interview_session("nonexistent-session")

        assert "インタビューセッションが見つかりません" in str(exc_info.value)

    def test_end_interview_session_success(self):
        """インタビューセッション終了成功テスト"""
        # セッションとエージェントを準備
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
        )

        mock_persona_agent = Mock()
        mock_persona_agent.dispose = Mock()

        self.interview_manager._active_sessions["test-session"] = session
        self.interview_manager._session_agents["test-session"] = [mock_persona_agent]

        # セッションを終了
        self.interview_manager.end_interview_session("test-session")

        # エージェントのdisposeが呼ばれたことを確認
        mock_persona_agent.dispose.assert_called_once()

        # セッションが削除されたことを確認
        assert "test-session" not in self.interview_manager._active_sessions
        assert "test-session" not in self.interview_manager._session_agents

    def test_end_interview_session_not_found(self):
        """存在しないセッションの終了エラーテスト"""
        from src.managers.interview_manager import InterviewSessionNotFoundError

        with pytest.raises(InterviewSessionNotFoundError) as exc_info:
            self.interview_manager.end_interview_session("nonexistent-session")

        assert "インタビューセッションが見つかりません" in str(exc_info.value)

    def test_generate_interview_system_prompt(self):
        """インタビュー用システムプロンプト生成テスト"""
        # ベースプロンプトのモック設定
        base_prompt = "基本的なペルソナプロンプト"
        self.mock_agent_service.generate_persona_system_prompt.return_value = (
            base_prompt
        )

        # プロンプトを生成
        prompt = self.interview_manager._generate_interview_system_prompt(
            self.test_personas[0]
        )

        # ベースプロンプトが含まれていることを確認
        assert base_prompt in prompt

        # インタビュー固有の指示が含まれていることを確認
        assert "インタビュー" in prompt
        assert "ユーザー" in prompt
        assert "質問" in prompt

    def test_create_interview_prompt_first_interaction(self):
        """初回インタラクション用プロンプト生成テスト"""
        # ペルソナエージェントのモック
        mock_persona_agent = Mock()
        mock_persona_agent.get_persona_name.return_value = "田中太郎"

        user_message = "こんにちは、質問があります"
        context = []

        # プロンプトを生成
        prompt = self.interview_manager._create_interview_prompt(
            mock_persona_agent, user_message, context
        )

        # 必要な要素が含まれていることを確認
        assert user_message in prompt
        assert "田中太郎" in prompt
        assert "質問" in prompt

    def test_create_interview_prompt_with_context(self):
        """コンテキスト付きプロンプト生成テスト"""
        # ペルソナエージェントのモック
        mock_persona_agent = Mock()
        mock_persona_agent.get_persona_name.return_value = "田中太郎"

        user_message = "追加の質問です"
        context = [
            Message.create_new("user", "User", "最初の質問", "user_message"),
            Message.create_new("persona-1", "田中太郎", "最初の回答", "statement"),
        ]

        # プロンプトを生成
        prompt = self.interview_manager._create_interview_prompt(
            mock_persona_agent, user_message, context
        )

        # 必要な要素が含まれていることを確認
        assert user_message in prompt
        assert "田中太郎" in prompt
        assert "会話" in prompt
        assert "最初の質問" in prompt or "最初の回答" in prompt


class TestInterviewManagerMultimodal:
    """InterviewManagerのマルチモーダル機能テストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化"""
        # モックサービスを作成
        self.mock_agent_service = Mock()
        self.mock_db_service = Mock()
        self.mock_db_service.initialize_database.return_value = None

        # InterviewManagerを初期化
        self.interview_manager = InterviewManager(
            agent_service=self.mock_agent_service, database_service=self.mock_db_service
        )

        # テスト用ペルソナ
        self.test_persona = Persona(
            id="persona-1",
            name="田中太郎",
            age=35,
            occupation="会社員",
            background="IT企業で働く中堅社員",
            values=["効率性"],
            pain_points=["時間不足"],
            goals=["キャリアアップ"],
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )

    def test_send_user_message_with_documents(self):
        """ドキュメント付きメッセージ送信テスト"""
        # セッションを準備
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
        )

        # モックペルソナエージェント
        mock_persona_agent = Mock()
        mock_persona_agent.get_persona_id.return_value = "persona-1"
        mock_persona_agent.get_persona_name.return_value = "田中太郎"
        mock_persona_agent.respond.return_value = "画像を見て回答します"
        mock_persona_agent.set_document_contents = Mock()

        self.interview_manager._active_sessions["test-session"] = session
        self.interview_manager._session_agents["test-session"] = [mock_persona_agent]

        # ドキュメントコンテンツを準備
        document_contents = [
            {"image": {"format": "png", "source": {"bytes": b"fake_image_data"}}}
        ]

        # メッセージを送信
        responses = self.interview_manager.send_user_message(
            "test-session",
            "この画像について教えてください",
            document_contents=document_contents,
        )

        # ドキュメントコンテンツがエージェントに設定されたことを確認
        mock_persona_agent.set_document_contents.assert_called_once()

        # 応答が返されたことを確認
        assert len(responses) == 1
        assert responses[0].content == "画像を見て回答します"

    def test_send_user_message_without_documents(self):
        """ドキュメントなしメッセージ送信テスト"""
        # セッションを準備
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
        )

        # モックペルソナエージェント
        mock_persona_agent = Mock()
        mock_persona_agent.get_persona_id.return_value = "persona-1"
        mock_persona_agent.get_persona_name.return_value = "田中太郎"
        mock_persona_agent.respond.return_value = "通常の回答です"
        mock_persona_agent.set_document_contents = Mock()

        self.interview_manager._active_sessions["test-session"] = session
        self.interview_manager._session_agents["test-session"] = [mock_persona_agent]

        # メッセージを送信（ドキュメントなし）
        responses = self.interview_manager.send_user_message(
            "test-session", "質問があります"
        )

        # ドキュメントコンテンツが設定されていないことを確認
        mock_persona_agent.set_document_contents.assert_not_called()

        # 応答が返されたことを確認
        assert len(responses) == 1
        assert responses[0].content == "通常の回答です"

    def test_send_user_message_with_multiple_documents(self):
        """複数ドキュメント付きメッセージ送信テスト"""
        # セッションを準備
        session = InterviewSession(
            id="test-session",
            participants=["persona-1", "persona-2"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
        )

        # モックペルソナエージェント（2人）
        mock_persona_agent_1 = Mock()
        mock_persona_agent_1.get_persona_id.return_value = "persona-1"
        mock_persona_agent_1.get_persona_name.return_value = "田中太郎"
        mock_persona_agent_1.respond.return_value = "田中の回答"
        mock_persona_agent_1.set_document_contents = Mock()

        mock_persona_agent_2 = Mock()
        mock_persona_agent_2.get_persona_id.return_value = "persona-2"
        mock_persona_agent_2.get_persona_name.return_value = "佐藤花子"
        mock_persona_agent_2.respond.return_value = "佐藤の回答"
        mock_persona_agent_2.set_document_contents = Mock()

        self.interview_manager._active_sessions["test-session"] = session
        self.interview_manager._session_agents["test-session"] = [
            mock_persona_agent_1,
            mock_persona_agent_2,
        ]

        # 複数ドキュメントコンテンツを準備
        document_contents = [
            {"image": {"format": "png", "source": {"bytes": b"fake_image_1"}}},
            {
                "document": {
                    "name": "test_doc",
                    "format": "pdf",
                    "source": {"bytes": b"fake_pdf"},
                }
            },
        ]

        # メッセージを送信
        responses = self.interview_manager.send_user_message(
            "test-session",
            "これらのファイルについて教えてください",
            document_contents=document_contents,
        )

        # 両方のエージェントにドキュメントコンテンツが設定されたことを確認
        mock_persona_agent_1.set_document_contents.assert_called_once()
        mock_persona_agent_2.set_document_contents.assert_called_once()

        # 両方から応答が返されたことを確認
        assert len(responses) == 2


class TestInterviewSessionDocuments:
    """InterviewSessionのドキュメント追跡機能テストクラス"""

    def test_session_with_documents_field(self):
        """ドキュメントフィールド付きセッション初期化テスト"""
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
            enable_memory=False,
            documents=None,
        )

        assert session.documents is None

    def test_add_document_to_session(self):
        """セッションへのドキュメント追加テスト"""
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
        )

        doc_metadata = {
            "filename": "test.png",
            "mime_type": "image/png",
            "file_size": 1024,
            "uploaded_at": "2025-01-27T10:00:00",
        }

        new_session = session.add_document(doc_metadata)

        # 元のセッションは変更されていない
        assert session.documents is None

        # 新しいセッションにドキュメントが追加されている
        assert new_session.documents is not None
        assert len(new_session.documents) == 1
        assert new_session.documents[0]["filename"] == "test.png"

    def test_add_multiple_documents(self):
        """複数ドキュメント追加テスト"""
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
        )

        doc1 = {"filename": "image.png", "mime_type": "image/png", "file_size": 1024}
        doc2 = {
            "filename": "document.pdf",
            "mime_type": "application/pdf",
            "file_size": 2048,
        }

        session = session.add_document(doc1)
        session = session.add_document(doc2)

        assert len(session.documents) == 2
        assert session.documents[0]["filename"] == "image.png"
        assert session.documents[1]["filename"] == "document.pdf"

    def test_add_user_message_preserves_documents(self):
        """ユーザーメッセージ追加時にドキュメントが保持されるテスト"""
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
            documents=[{"filename": "test.png", "mime_type": "image/png"}],
        )

        new_session = session.add_user_message("テストメッセージ")

        assert new_session.documents is not None
        assert len(new_session.documents) == 1
        assert new_session.documents[0]["filename"] == "test.png"

    def test_add_persona_response_preserves_documents(self):
        """ペルソナ応答追加時にドキュメントが保持されるテスト"""
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
            documents=[{"filename": "test.pdf", "mime_type": "application/pdf"}],
        )

        new_session = session.add_persona_response("persona-1", "田中太郎", "回答です")

        assert new_session.documents is not None
        assert len(new_session.documents) == 1
        assert new_session.documents[0]["filename"] == "test.pdf"


class TestInterviewManagerDocumentMetadata:
    """InterviewManagerのドキュメントメタデータ機能テストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化"""
        self.mock_agent_service = Mock()
        self.mock_db_service = Mock()
        self.mock_db_service.initialize_database.return_value = None

        self.interview_manager = InterviewManager(
            agent_service=self.mock_agent_service, database_service=self.mock_db_service
        )

    def test_send_user_message_with_document_metadata(self):
        """ドキュメントメタデータ付きメッセージ送信テスト"""
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
        )

        mock_persona_agent = Mock()
        mock_persona_agent.get_persona_id.return_value = "persona-1"
        mock_persona_agent.get_persona_name.return_value = "田中太郎"
        mock_persona_agent.respond.return_value = "回答です"
        mock_persona_agent.set_document_contents = Mock()

        self.interview_manager._active_sessions["test-session"] = session
        self.interview_manager._session_agents["test-session"] = [mock_persona_agent]

        document_contents = [
            {"image": {"format": "png", "source": {"bytes": b"fake_image"}}}
        ]
        document_metadata = [
            {
                "filename": "test.png",
                "mime_type": "image/png",
                "file_size": 1024,
                "uploaded_at": "2025-01-27T10:00:00",
            }
        ]

        self.interview_manager.send_user_message(
            "test-session",
            "画像について教えてください",
            document_contents=document_contents,
            document_metadata=document_metadata,
        )

        # セッションにドキュメントメタデータが追加されていることを確認
        updated_session = self.interview_manager._active_sessions["test-session"]
        assert updated_session.documents is not None
        assert len(updated_session.documents) == 1
        assert updated_session.documents[0]["filename"] == "test.png"

    def test_send_user_message_avoids_duplicate_documents(self):
        """重複ドキュメントが追加されないテスト"""
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[],
            created_at=datetime.now(),
            is_saved=False,
            documents=[
                {"filename": "test.png", "mime_type": "image/png", "file_size": 1024}
            ],
        )

        mock_persona_agent = Mock()
        mock_persona_agent.get_persona_id.return_value = "persona-1"
        mock_persona_agent.get_persona_name.return_value = "田中太郎"
        mock_persona_agent.respond.return_value = "回答です"
        mock_persona_agent.set_document_contents = Mock()

        self.interview_manager._active_sessions["test-session"] = session
        self.interview_manager._session_agents["test-session"] = [mock_persona_agent]

        # 同じファイル名とサイズのドキュメントを再度送信
        document_metadata = [
            {"filename": "test.png", "mime_type": "image/png", "file_size": 1024}
        ]

        self.interview_manager.send_user_message(
            "test-session", "同じ画像について", document_metadata=document_metadata
        )

        # 重複が追加されていないことを確認
        updated_session = self.interview_manager._active_sessions["test-session"]
        assert len(updated_session.documents) == 1

    def test_create_discussion_from_session_includes_documents(self):
        """セッションからDiscussion作成時にドキュメントが含まれるテスト"""
        session = InterviewSession(
            id="test-session",
            participants=["persona-1"],
            messages=[
                Message.create_new("user", "User", "質問", "user_message"),
                Message.create_new("persona-1", "田中太郎", "回答", "statement"),
            ],
            created_at=datetime.now(),
            is_saved=False,
            documents=[
                {"filename": "test.png", "mime_type": "image/png", "file_size": 1024},
                {
                    "filename": "doc.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 2048,
                },
            ],
        )

        discussion = self.interview_manager._create_discussion_from_session(
            session, "テストセッション"
        )

        assert discussion.documents is not None
        assert len(discussion.documents) == 2
        assert discussion.documents[0]["filename"] == "test.png"
        assert discussion.documents[1]["filename"] == "doc.pdf"
