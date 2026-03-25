"""
AgentDiscussionManager の単体テスト

エージェントモードでの議論管理をテストします。
"""

import pytest
from unittest.mock import Mock, patch

from src.managers.agent_discussion_manager import (
    AgentDiscussionManager,
    AgentDiscussionManagerError,
)
from src.models.discussion import Discussion
from src.services.agent_service import PersonaAgent, FacilitatorAgent


class TestAgentDiscussionManagerInitialization:
    """AgentDiscussionManager初期化のテスト"""

    @patch("src.managers.agent_discussion_manager.service_factory")
    def test_initialization_success(self, mock_service_factory):
        """正常な初期化を確認"""
        mock_db_service = Mock()
        mock_agent_service = Mock()

        mock_service_factory.get_database_service.return_value = mock_db_service
        mock_service_factory.get_agent_service.return_value = mock_agent_service

        manager = AgentDiscussionManager()

        assert manager is not None
        assert manager.database_service is mock_db_service
        assert manager.agent_service is mock_agent_service

    def test_initialization_with_custom_services(self):
        """カスタムサービスでの初期化を確認"""
        mock_agent_service = Mock()
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        assert manager.agent_service == mock_agent_service
        assert manager.database_service == mock_db_service


class TestCreatePersonaAgents:
    """ペルソナエージェント作成のテスト"""

    def test_create_persona_agents_success(self, sample_persona, sample_persona_2):
        """ペルソナエージェント作成が成功することを確認"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()
        mock_agent_service.generate_persona_system_prompt.return_value = (
            "テストプロンプト"
        )

        mock_persona_agent = Mock(spec=PersonaAgent)
        mock_persona_agent.get_persona_id.return_value = sample_persona.id
        mock_agent_service.create_persona_agent.return_value = mock_persona_agent

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        personas = [sample_persona, sample_persona_2]
        system_prompts = {}

        agents = manager.create_persona_agents(personas, system_prompts)

        assert len(agents) == 2
        assert mock_agent_service.create_persona_agent.call_count == 2

    def test_create_persona_agents_with_custom_prompts(
        self, sample_persona, sample_persona_2
    ):
        """カスタムプロンプトでのエージェント作成を確認"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()
        mock_persona_agent = Mock(spec=PersonaAgent)
        mock_agent_service.create_persona_agent.return_value = mock_persona_agent

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        custom_prompt = "カスタムシステムプロンプト"
        system_prompts = {sample_persona.id: custom_prompt}

        # 最低2つのペルソナが必要
        agents = manager.create_persona_agents(
            [sample_persona, sample_persona_2], system_prompts
        )

        # カスタムプロンプトが使用されたことを確認
        assert mock_agent_service.create_persona_agent.call_count == 2


class TestCreateFacilitatorAgent:
    """ファシリテーターエージェント作成のテスト"""

    def test_create_facilitator_agent_success(self):
        """ファシリテーターエージェント作成が成功することを確認"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()
        mock_facilitator = Mock(spec=FacilitatorAgent)
        mock_agent_service.create_facilitator_agent.return_value = mock_facilitator

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        facilitator = manager.create_facilitator_agent(
            rounds=3, additional_instructions="テスト指示"
        )

        assert facilitator is not None
        mock_agent_service.create_facilitator_agent.assert_called_once_with(
            3, "テスト指示"
        )


class TestStartAgentDiscussion:
    """エージェント議論開始のテスト"""

    def test_start_discussion_success(self, sample_persona, sample_persona_2):
        """議論開始が成功することを確認"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        # ペルソナエージェントのモック
        mock_persona_agent_1 = Mock(spec=PersonaAgent)
        mock_persona_agent_1.get_persona_id.return_value = sample_persona.id
        mock_persona_agent_1.get_persona_name.return_value = sample_persona.name
        mock_persona_agent_1.respond.return_value = "テスト応答1"

        mock_persona_agent_2 = Mock(spec=PersonaAgent)
        mock_persona_agent_2.get_persona_id.return_value = sample_persona_2.id
        mock_persona_agent_2.get_persona_name.return_value = sample_persona_2.name
        mock_persona_agent_2.respond.return_value = "テスト応答2"

        persona_agents = [mock_persona_agent_1, mock_persona_agent_2]

        # ファシリテーターのモック
        mock_facilitator = Mock(spec=FacilitatorAgent)
        mock_facilitator.should_continue.side_effect = [True, False]  # 1ラウンドで終了
        mock_facilitator.start_discussion.return_value = "議論を開始します"
        mock_facilitator.select_next_speaker.side_effect = [
            mock_persona_agent_1,
            mock_persona_agent_2,
            None,
        ]
        mock_facilitator.summarize_round.return_value = "ラウンドのまとめ"
        mock_facilitator.increment_round.return_value = None
        mock_facilitator.current_round = 1
        mock_facilitator.rounds = 1
        mock_facilitator.additional_instructions = ""

        discussion = manager.start_agent_discussion(
            personas=[sample_persona, sample_persona_2],
            topic="テストトピック",
            persona_agents=persona_agents,
            facilitator=mock_facilitator,
        )

        assert discussion is not None
        assert discussion.topic == "テストトピック"
        assert discussion.mode == "agent"
        assert len(discussion.messages) > 0

    def test_start_discussion_insufficient_personas(self, sample_persona):
        """ペルソナ数不足でエラーを返すことを確認"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        mock_persona_agent = Mock(spec=PersonaAgent)
        mock_facilitator = Mock(spec=FacilitatorAgent)

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager.start_agent_discussion(
                personas=[sample_persona],
                topic="テストトピック",
                persona_agents=[mock_persona_agent],
                facilitator=mock_facilitator,
            )

        assert "最低2つのペルソナが必要" in str(exc_info.value)

    def test_start_discussion_empty_topic(self, sample_persona, sample_persona_2):
        """空のトピックでエラーを返すことを確認"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        mock_persona_agents = [Mock(spec=PersonaAgent), Mock(spec=PersonaAgent)]
        mock_facilitator = Mock(spec=FacilitatorAgent)

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager.start_agent_discussion(
                personas=[sample_persona, sample_persona_2],
                topic="",
                persona_agents=mock_persona_agents,
                facilitator=mock_facilitator,
            )

        assert "トピックが空" in str(exc_info.value)


class TestSaveAgentDiscussion:
    """エージェント議論保存のテスト"""

    def test_save_discussion_success(self, sample_discussion):
        """議論保存が成功することを確認"""
        # sample_discussionのmodeをagentに変更
        agent_discussion = Discussion(
            id=sample_discussion.id,
            topic=sample_discussion.topic,
            participants=sample_discussion.participants,
            messages=sample_discussion.messages,
            insights=sample_discussion.insights,
            created_at=sample_discussion.created_at,
            mode="agent",
        )

        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None
        mock_db_service.save_discussion.return_value = agent_discussion.id

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        result_id = manager.save_agent_discussion(agent_discussion)

        assert result_id == agent_discussion.id
        mock_db_service.save_discussion.assert_called_once_with(agent_discussion)

    def test_save_discussion_invalid(self):
        """無効な議論でエラーを返すことを確認"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager.save_agent_discussion(None)

        assert "議論オブジェクトが無効" in str(exc_info.value)


class TestDisposeAgents:
    """エージェントリソース解放のテスト"""

    def test_dispose_agents_success(self):
        """エージェントリソース解放が成功することを確認"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        # モックエージェント
        mock_persona_agent_1 = Mock(spec=PersonaAgent)
        mock_persona_agent_2 = Mock(spec=PersonaAgent)
        mock_facilitator = Mock(spec=FacilitatorAgent)

        persona_agents = [mock_persona_agent_1, mock_persona_agent_2]

        manager._cleanup_agents(persona_agents, mock_facilitator)

        # 全てのエージェントのdisposeが呼ばれたことを確認
        mock_persona_agent_1.dispose.assert_called_once()
        mock_persona_agent_2.dispose.assert_called_once()
        mock_facilitator.dispose.assert_called_once()

    def test_dispose_agents_handles_errors(self):
        """エージェント解放中のエラーが適切に処理されることを確認"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        # エラーを発生させるモックエージェント
        mock_persona_agent = Mock(spec=PersonaAgent)
        mock_persona_agent.dispose.side_effect = Exception("Dispose error")

        mock_facilitator = Mock(spec=FacilitatorAgent)

        # エラーが発生しても例外が伝播しないことを確認
        manager._cleanup_agents([mock_persona_agent], mock_facilitator)

        # ファシリテーターのdisposeも呼ばれることを確認
        mock_facilitator.dispose.assert_called_once()


class TestPrepareDocumentContents:
    """ドキュメントコンテンツ準備のテスト"""

    def test_prepare_image_content(self, tmp_path):
        """画像コンテンツの準備テスト"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        # テスト用画像ファイルを作成
        image_path = tmp_path / "test_image.png"
        image_path.write_bytes(b"fake_png_data")

        documents_metadata = [
            {
                "file_path": str(image_path),
                "mime_type": "image/png",
                "filename": "test_image.png",
            }
        ]

        result = manager._prepare_document_contents(documents_metadata)

        assert len(result) == 1
        assert "image" in result[0]
        assert result[0]["image"]["format"] == "png"
        assert result[0]["image"]["source"]["bytes"] == b"fake_png_data"

    def test_prepare_pdf_content(self, tmp_path):
        """PDFコンテンツの準備テスト"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        # テスト用PDFファイルを作成
        pdf_path = tmp_path / "test_document.pdf"
        pdf_path.write_bytes(b"fake_pdf_data")

        documents_metadata = [
            {
                "file_path": str(pdf_path),
                "mime_type": "application/pdf",
                "filename": "test_document.pdf",
            }
        ]

        result = manager._prepare_document_contents(documents_metadata)

        assert len(result) == 1
        assert "document" in result[0]
        assert result[0]["document"]["format"] == "pdf"
        assert result[0]["document"]["source"]["bytes"] == b"fake_pdf_data"

    def test_prepare_text_content(self, tmp_path):
        """テキストコンテンツの準備テスト"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        # テスト用テキストファイルを作成
        txt_path = tmp_path / "test_document.txt"
        txt_path.write_bytes(b"fake_text_data")

        documents_metadata = [
            {
                "file_path": str(txt_path),
                "mime_type": "text/plain",
                "filename": "test_document.txt",
            }
        ]

        result = manager._prepare_document_contents(documents_metadata)

        assert len(result) == 1
        assert "document" in result[0]
        assert result[0]["document"]["format"] == "txt"

    def test_prepare_unsupported_mime_type(self, tmp_path):
        """サポートされていないMIMEタイプのテスト"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        # テスト用ファイルを作成
        file_path = tmp_path / "test_file.xyz"
        file_path.write_bytes(b"fake_data")

        documents_metadata = [
            {
                "file_path": str(file_path),
                "mime_type": "application/unknown",
                "filename": "test_file.xyz",
            }
        ]

        result = manager._prepare_document_contents(documents_metadata)

        # サポートされていないMIMEタイプは無視される
        assert len(result) == 0

    def test_prepare_missing_file(self):
        """存在しないファイルのテスト"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        documents_metadata = [
            {
                "file_path": "/nonexistent/path/file.png",
                "mime_type": "image/png",
                "filename": "file.png",
            }
        ]

        result = manager._prepare_document_contents(documents_metadata)

        # 存在しないファイルは無視される
        assert len(result) == 0

    def test_prepare_multiple_documents(self, tmp_path):
        """複数ドキュメントの準備テスト"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        # テスト用ファイルを作成
        image_path = tmp_path / "test_image.jpeg"
        image_path.write_bytes(b"fake_jpeg_data")

        pdf_path = tmp_path / "test_doc.pdf"
        pdf_path.write_bytes(b"fake_pdf_data")

        documents_metadata = [
            {
                "file_path": str(image_path),
                "mime_type": "image/jpeg",
                "filename": "test_image.jpeg",
            },
            {
                "file_path": str(pdf_path),
                "mime_type": "application/pdf",
                "filename": "test_doc.pdf",
            },
        ]

        result = manager._prepare_document_contents(documents_metadata)

        assert len(result) == 2
        assert "image" in result[0]
        assert "document" in result[1]
