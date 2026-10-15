"""
AgentDiscussionManager の単体テスト

エージェントモードでの議論管理をテストします。
U6 からは実行する人（Principal）を受け取り、保存は DiscussionRecords を通す。
"""

import json

import pytest

from src.models.errors import AccessDeniedError, CodedError, ErrorCode
from unittest.mock import Mock, patch

from src.managers.agent_discussion_manager import (
    AgentDiscussionManager,
    AgentDiscussionManagerError,
)
from src.models.persona import Persona
from src.services.agent_service import (
    AgentConfigurationError,
    AgentInitializationError,
    PersonaAgent,
    FacilitatorAgent,
)
from tests.error_helpers import raises_code
from tests.unit.discussion_world import NO_ROLE, OWNER, World


def _persona(name="p"):
    return Persona.create_new(
        name=name,
        age=30,
        occupation="x",
        background="y",
        values=["a"],
        pain_points=["b"],
        goals=["c"],
    )


def _readable_participants() -> Mock:
    """A ParticipantReader that always finds everyone readable."""
    return Mock(can_read_all=Mock(return_value=True))


def _manager(**kwargs) -> AgentDiscussionManager:
    kwargs.setdefault("agent_service", Mock())
    kwargs.setdefault("database_service", Mock())
    kwargs.setdefault("dataset_analysis_service", Mock())
    kwargs.setdefault("binding_store", Mock())
    kwargs.setdefault("participants", _readable_participants())
    kwargs.setdefault("resource_lookup", Mock())
    kwargs.setdefault("discussion_records", Mock())
    return AgentDiscussionManager(**kwargs)


def _run_rounds(manager, personas, topic, agents, facilitator, **kwargs) -> list:
    return list(
        manager._rounds(
            OWNER,
            personas,
            topic,
            agents,
            facilitator,
            streaming=kwargs.pop("streaming", False),
            **kwargs,
        )
    )


class TestAgentInputValidationUnifiedWithClassic:
    """エージェント議論も非エージェントと同じ入力ルール（上限5・重複）を課すことの回帰。

    以前はエージェント側にペルソナ上限・重複検証が無く drift していた（#3）。
    DiscussionValidation Component への集約で両モード統一されたことを保証する。
    """

    def test_rejects_more_than_5_personas(self):
        mgr = _manager()
        personas = [_persona(f"p{i}") for i in range(6)]
        with raises_code(
            AgentDiscussionManagerError,
            ErrorCode.DISCUSSION_TOO_MANY_PERSONAS,
            max_personas=5,
        ):
            mgr._validate_discussion_input(personas, "有効なトピック", [Mock()], Mock())

    def test_rejects_duplicate_personas(self):
        mgr = _manager()
        p = _persona()
        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_PERSONA_DUPLICATED
        ):
            mgr._validate_discussion_input([p, p], "有効なトピック", [Mock()], Mock())


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

        manager = _manager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        assert manager.agent_service == mock_agent_service
        assert manager.database_service == mock_db_service


class TestCreatePersonaAgents:
    """ペルソナエージェント作成のテスト"""

    def test_create_persona_agents_success(self, sample_persona, sample_persona_2):
        """ペルソナエージェント作成が成功することを確認"""
        mock_persona_agent = Mock(spec=PersonaAgent)
        manager = _manager()
        # agent 生成は Component (_agent_integration.build_agent) に委譲するためモック
        manager._agent_integration.build_agent = Mock(return_value=mock_persona_agent)

        agents = manager._create_persona_agents(
            [sample_persona, sample_persona_2], {}, principal=OWNER
        )

        assert len(agents) == 2
        assert manager._agent_integration.build_agent.call_count == 2

    def test_create_persona_agents_with_custom_prompts(
        self, sample_persona, sample_persona_2
    ):
        """カスタムプロンプトがそのまま渡ることを確認"""
        manager = _manager()
        manager._agent_integration.build_agent = Mock(
            return_value=Mock(spec=PersonaAgent)
        )

        custom_prompt = "カスタムシステムプロンプト"
        manager._create_persona_agents(
            [sample_persona, sample_persona_2],
            {sample_persona.id: custom_prompt},
            principal=OWNER,
        )

        prompts = [
            c.args[2] for c in manager._agent_integration.build_agent.call_args_list
        ]
        assert prompts[0] == custom_prompt
        assert prompts[1] != custom_prompt

    def test_execution_context_is_built_per_persona(
        self, sample_persona, sample_persona_2
    ):
        """BR6.1: 記憶を書くのは実行する人が持ち主のペルソナだけ。"""
        manager = _manager()
        manager._agent_integration.build_agent = Mock(
            return_value=Mock(spec=PersonaAgent)
        )
        mine = Persona.from_dict({**sample_persona.to_dict(), "owner_sub": OWNER.sub})
        theirs = Persona.from_dict({**sample_persona_2.to_dict(), "owner_sub": "b"})

        manager._create_persona_agents([mine, theirs], {}, principal=OWNER)

        contexts = [
            c.args[0] for c in manager._agent_integration.build_agent.call_args_list
        ]
        assert [c.write_memory for c in contexts] == [True, False]

    def test_memory_without_a_session_is_turned_off(
        self, sample_persona, sample_persona_2
    ):
        manager = _manager()
        manager._agent_integration.build_agent = Mock(
            return_value=Mock(spec=PersonaAgent)
        )
        manager._create_persona_agents(
            [sample_persona, sample_persona_2],
            {},
            enable_memory=True,
            session_id=None,
            principal=OWNER,
        )
        flags = [
            c.kwargs["enable_memory"]
            for c in manager._agent_integration.build_agent.call_args_list
        ]
        assert flags == [False, False]

    def test_empty_and_single_persona_lists_are_refused(self, sample_persona):
        manager = _manager()
        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_PERSONAS_REQUIRED
        ):
            manager._create_persona_agents([], {}, principal=OWNER)
        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_TOO_FEW_PERSONAS
        ):
            manager._create_persona_agents([sample_persona], {}, principal=OWNER)


class TestModelSelectionValidation:
    """persona_models / facilitator_model のバリデーションテスト"""

    def test_create_persona_agents_unsupported_model_raises_validation(
        self, sample_persona, sample_persona_2
    ):
        manager = _manager()
        manager._agent_integration.build_agent = Mock()

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager._create_persona_agents(
                [sample_persona, sample_persona_2],
                {},
                persona_models={sample_persona.id: "unknown.model-id"},
                principal=OWNER,
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_MODEL_UNSUPPORTED

    @patch("src.managers.shared.model_validation.config")
    def test_create_persona_agents_mantle_disabled_raises_config(
        self, mock_config, sample_persona, sample_persona_2
    ):
        mock_config.ENABLE_ADDITIONAL_PERSONA_MODELS = False
        manager = _manager()
        manager._agent_integration.build_agent = Mock()

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager._create_persona_agents(
                [sample_persona, sample_persona_2],
                {},
                persona_models={sample_persona.id: "openai.gpt-5.6-terra"},
                principal=OWNER,
            )

        assert (
            exc_info.value.code is ErrorCode.DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED
        )

    def test_create_facilitator_agent_unsupported_model_raises_validation(self):
        manager = _manager()

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager._create_facilitator_agent(
                rounds=3, facilitator_model="unknown.model-id"
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_MODEL_UNSUPPORTED

    def test_create_persona_agents_config_error_not_squashed(
        self, sample_persona, sample_persona_2
    ):
        """build_agentがAgentConfigurationErrorを投げた場合、
        個別ペルソナ失敗として握り潰さずCONFIGコードのまま伝播する。"""
        manager = _manager()
        manager._agent_integration.build_agent = Mock(
            side_effect=AgentConfigurationError(
                "mantle disabled", code=ErrorCode.AGENT_MODEL_ADDITIONAL_MODELS_DISABLED
            )
        )

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager._create_persona_agents(
                [sample_persona, sample_persona_2], {}, principal=OWNER
            )

        assert (
            exc_info.value.code is ErrorCode.DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED
        )

    def test_create_persona_agents_partial_failure_disposes_created_agents(
        self, sample_persona, sample_persona_2
    ):
        """1体目作成成功後、2体目が設定エラーで失敗した場合も1体目を解放すること。

        Issue #107再レビュー: raise前にcleanup_agentsを呼ばないと、既に作成済みの
        persona_agentがどこにも渡らずリークしていた。
        """
        manager = _manager()
        mock_agent_1 = Mock(spec=PersonaAgent)

        manager._agent_integration.build_agent = Mock(
            side_effect=[
                mock_agent_1,
                AgentConfigurationError(
                    "mantle disabled",
                    code=ErrorCode.AGENT_MODEL_ADDITIONAL_MODELS_DISABLED,
                ),
            ]
        )

        with pytest.raises(AgentDiscussionManagerError):
            manager._create_persona_agents(
                [sample_persona, sample_persona_2], {}, principal=OWNER
            )

        mock_agent_1.dispose.assert_called_once()

    def test_create_persona_agents_below_minimum_disposes_created_agents(
        self, sample_persona, sample_persona_2
    ):
        """作成できたエージェントが最低数(2)未満の場合も、作成済みのagentを解放すること。"""
        manager = _manager()
        mock_agent_1 = Mock(spec=PersonaAgent)

        manager._agent_integration.build_agent = Mock(
            side_effect=[
                mock_agent_1,
                AgentInitializationError("boom"),
            ]
        )

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager._create_persona_agents(
                [sample_persona, sample_persona_2], {}, principal=OWNER
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_AGENT_SETUP_FAILED
        mock_agent_1.dispose.assert_called_once()


class TestRunAgentDiscussionFacilitatorFailureCleanup:
    """facilitator作成失敗時にpersona_agentsが解放されることのテスト

    Issue #107再レビュー: ペルソナのエージェント作成後にファシリテータの作成が
    失敗すると、ラウンド実行のfinallyに到達せずpersona_agentsがリークしていた。
    """

    def _prepared(self):
        manager = _manager()
        agents = [Mock(spec=PersonaAgent), Mock(spec=PersonaAgent)]
        manager._create_persona_agents = Mock(return_value=agents)
        manager._create_facilitator_agent = Mock(
            side_effect=AgentDiscussionManagerError(
                "rounds too many", code=ErrorCode.DISCUSSION_ROUNDS_TOO_MANY
            )
        )
        return manager, agents

    def test_run_agent_discussion_full_disposes_personas_on_facilitator_failure(
        self, sample_persona, sample_persona_2
    ):
        manager, agents = self._prepared()

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager.run_agent_discussion_full(
                OWNER, [sample_persona, sample_persona_2], "テストトピック", rounds=99
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_ROUNDS_TOO_MANY
        for agent in agents:
            agent.dispose.assert_called_once()

    def test_run_agent_discussion_streaming_disposes_personas_on_facilitator_failure(
        self, sample_persona, sample_persona_2
    ):
        manager, agents = self._prepared()

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            list(
                manager.run_agent_discussion_streaming(
                    OWNER,
                    [sample_persona, sample_persona_2],
                    "テストトピック",
                    rounds=99,
                )
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_ROUNDS_TOO_MANY
        for agent in agents:
            agent.dispose.assert_called_once()

    def test_invalid_memory_mode_is_refused_before_agents(
        self, sample_persona, sample_persona_2
    ):
        manager, _ = self._prepared()
        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_MEMORY_MODE_INVALID
        ):
            manager.run_agent_discussion_full(
                OWNER,
                [sample_persona, sample_persona_2],
                "テストトピック",
                memory_mode="everything",
            )
        manager._create_persona_agents.assert_not_called()


class TestCreateFacilitatorAgent:
    """ファシリテーターエージェント作成のテスト"""

    def test_create_facilitator_agent_success(self):
        """ファシリテーターエージェント作成が成功することを確認"""
        mock_agent_service = Mock()
        mock_facilitator = Mock(spec=FacilitatorAgent)
        mock_agent_service.create_facilitator_agent.return_value = mock_facilitator

        manager = _manager(agent_service=mock_agent_service)

        facilitator = manager._create_facilitator_agent(
            rounds=3, additional_instructions="テスト指示"
        )

        assert facilitator is mock_facilitator
        mock_agent_service.create_facilitator_agent.assert_called_once_with(
            3, "テスト指示", model_id=None
        )

    @pytest.mark.parametrize(
        "rounds, code",
        [
            (0, ErrorCode.DISCUSSION_ROUNDS_TOO_FEW),
            (11, ErrorCode.DISCUSSION_ROUNDS_TOO_MANY),
        ],
    )
    def test_rounds_are_bounded(self, rounds, code):
        with raises_code(AgentDiscussionManagerError, code):
            _manager()._create_facilitator_agent(rounds=rounds)

    def test_service_failure_becomes_setup_failed(self):
        agent_service = Mock()
        agent_service.create_facilitator_agent.side_effect = RuntimeError("boom")
        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_AGENT_SETUP_FAILED
        ):
            _manager(agent_service=agent_service)._create_facilitator_agent(rounds=2)


class TestRounds:
    """ラウンド実行（_rounds）のテスト"""

    def _agents(self, *personas):
        agents = []
        for i, persona in enumerate(personas, 1):
            agent = Mock(spec=PersonaAgent)
            agent.get_persona_id.return_value = persona.id
            agent.get_persona_name.return_value = persona.name
            agent.respond.return_value = f"テスト応答{i}"
            agents.append(agent)
        return agents

    def _facilitator(self, rounds=1):
        facilitator = Mock(spec=FacilitatorAgent)
        facilitator.invoke.return_value = "ラウンドのまとめ"
        facilitator.rounds = rounds
        facilitator.additional_instructions = ""
        return facilitator

    def test_rounds_complete_with_statements_and_summary(
        self, sample_persona, sample_persona_2
    ):
        """議論が完了し、発言と要約が返ることを確認"""
        manager = _manager()
        events = _run_rounds(
            manager,
            [sample_persona, sample_persona_2],
            "テストトピック",
            self._agents(sample_persona, sample_persona_2),
            self._facilitator(),
        )

        kind, messages = events[-1]
        assert kind == "complete"
        assert [m.message_type for m in messages] == [
            "statement",
            "statement",
            "summary",
        ]

    def test_insufficient_personas(self, sample_persona):
        """ペルソナ数不足でエラーを返すことを確認"""
        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_TOO_FEW_PERSONAS
        ):
            _run_rounds(
                _manager(),
                [sample_persona],
                "テストトピック",
                [Mock(spec=PersonaAgent)],
                Mock(spec=FacilitatorAgent),
            )

    def test_document_validation_failure_disposes_agents(
        self, sample_persona, sample_persona_2
    ):
        """ドキュメント検証が失敗した場合もagentが解放されること。

        Issue #107レビュー: ドキュメント読み込みがtryブロックの外にあったため、
        検証失敗時にfinally節のcleanup_agentsが実行されずリソースがリークしていた。
        Gemma4（3.5MB上限）に上限超過のPDFを添付してサイズ検証を失敗させる。
        """
        from datetime import datetime, timezone

        from src.models.uploaded_file import UploadedFile

        files = Mock()
        files.attachable_file.return_value = UploadedFile.create_new(
            owner_sub=OWNER.sub,
            file_type="discussion_document",
            original_filename="test.pdf",
            file_size=5 * 1024 * 1024,
            file_hash="h",
            mime_type="application/pdf",
            now=datetime.now(timezone.utc),
        ).with_path("path/to/test.pdf")
        manager = _manager(file_attachments=files)
        agents = [Mock(spec=PersonaAgent), Mock(spec=PersonaAgent)]
        facilitator = Mock(spec=FacilitatorAgent)

        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE
        ):
            _run_rounds(
                manager,
                [sample_persona, sample_persona_2],
                "テストトピック",
                agents,
                facilitator,
                document_ids=["doc-1"],
                persona_models={sample_persona.id: "google.gemma-4-31b"},
            )

        for agent in agents:
            agent.dispose.assert_called_once()
        facilitator.dispose.assert_called_once()

    def test_someone_elses_document_is_not_found(
        self, sample_persona, sample_persona_2
    ):
        files = Mock()
        files.attachable_file.side_effect = CodedError(
            "no", code=ErrorCode.FILE_NOT_FOUND
        )
        with raises_code(AgentDiscussionManagerError, ErrorCode.FILE_NOT_FOUND):
            _run_rounds(
                _manager(file_attachments=files),
                [sample_persona, sample_persona_2],
                "テストトピック",
                [Mock(spec=PersonaAgent), Mock(spec=PersonaAgent)],
                Mock(spec=FacilitatorAgent),
                document_ids=["doc-1"],
            )

    def test_empty_topic(self, sample_persona, sample_persona_2):
        """空のトピックでエラーを返すことを確認"""
        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_TOPIC_REQUIRED
        ):
            _run_rounds(
                _manager(),
                [sample_persona, sample_persona_2],
                "",
                [Mock(spec=PersonaAgent), Mock(spec=PersonaAgent)],
                Mock(spec=FacilitatorAgent),
            )

    @pytest.mark.parametrize("streaming", [False, True])
    def test_topic_too_short_disposes_agents(
        self, streaming, sample_persona, sample_persona_2
    ):
        """トピックが短すぎる（入力検証失敗）場合もagentが解放されること（#107）。"""
        agents = [Mock(spec=PersonaAgent), Mock(spec=PersonaAgent)]
        facilitator = Mock(spec=FacilitatorAgent)

        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_TOPIC_TOO_SHORT
        ):
            _run_rounds(
                _manager(),
                [sample_persona, sample_persona_2],
                "短い",
                agents,
                facilitator,
                streaming=streaming,
            )

        for agent in agents:
            agent.dispose.assert_called_once()
        facilitator.dispose.assert_called_once()

    def test_unexpected_error_becomes_operation_failed(
        self, sample_persona, sample_persona_2
    ):
        facilitator = self._facilitator()
        facilitator.start_discussion.side_effect = RuntimeError("boom")
        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_OPERATION_FAILED
        ):
            _run_rounds(
                _manager(),
                [sample_persona, sample_persona_2],
                "テストトピック",
                self._agents(sample_persona, sample_persona_2),
                facilitator,
            )


class TestDisposeAgents:
    """エージェントリソース解放のテスト"""

    def test_dispose_agents_success(self):
        """エージェントリソース解放が成功することを確認"""
        manager = _manager()
        mock_persona_agent_1 = Mock(spec=PersonaAgent)
        mock_persona_agent_2 = Mock(spec=PersonaAgent)
        mock_facilitator = Mock(spec=FacilitatorAgent)

        manager._cleanup_agents(
            [mock_persona_agent_1, mock_persona_agent_2], mock_facilitator
        )

        mock_persona_agent_1.dispose.assert_called_once()
        mock_persona_agent_2.dispose.assert_called_once()
        mock_facilitator.dispose.assert_called_once()

    def test_dispose_agents_handles_errors(self):
        """エージェント解放中のエラーが適切に処理されることを確認"""
        manager = _manager()
        mock_persona_agent = Mock(spec=PersonaAgent)
        mock_persona_agent.dispose.side_effect = Exception("Dispose error")
        mock_facilitator = Mock(spec=FacilitatorAgent)

        manager._cleanup_agents([mock_persona_agent], mock_facilitator)

        mock_facilitator.dispose.assert_called_once()


class TestPrepareDocumentContents:
    """ドキュメントコンテンツ準備のテスト（shared/document_loader経由）"""

    def test_prepare_image_content(self, tmp_path):
        """画像コンテンツの準備テスト"""
        from src.managers.shared.document_loader import prepare_document_contents

        image_path = tmp_path / "test_image.png"
        image_path.write_bytes(b"fake_png_data")

        documents_metadata = [
            {
                "file_path": str(image_path),
                "mime_type": "image/png",
                "filename": "test_image.png",
            }
        ]

        result = prepare_document_contents(documents_metadata)

        assert len(result) == 1
        assert "image" in result[0]
        assert result[0]["image"]["format"] == "png"
        assert result[0]["image"]["source"]["bytes"] == b"fake_png_data"

    def test_prepare_pdf_content(self, tmp_path):
        """PDFコンテンツの準備テスト"""
        from src.managers.shared.document_loader import prepare_document_contents

        pdf_path = tmp_path / "test_document.pdf"
        pdf_path.write_bytes(b"fake_pdf_data")

        documents_metadata = [
            {
                "file_path": str(pdf_path),
                "mime_type": "application/pdf",
                "filename": "test_document.pdf",
            }
        ]

        result = prepare_document_contents(documents_metadata)

        assert len(result) == 1
        assert "document" in result[0]
        assert result[0]["document"]["format"] == "pdf"
        assert result[0]["document"]["source"]["bytes"] == b"fake_pdf_data"

    def test_prepare_text_content(self, tmp_path):
        """テキストコンテンツの準備テスト"""
        from src.managers.shared.document_loader import prepare_document_contents

        txt_path = tmp_path / "test_document.txt"
        txt_path.write_bytes(b"fake_text_data")

        documents_metadata = [
            {
                "file_path": str(txt_path),
                "mime_type": "text/plain",
                "filename": "test_document.txt",
            }
        ]

        result = prepare_document_contents(documents_metadata)

        assert len(result) == 1
        assert "document" in result[0]
        assert result[0]["document"]["format"] == "txt"

    def test_prepare_unsupported_mime_type(self, tmp_path):
        """サポートされていないMIMEタイプのテスト"""
        from src.managers.shared.document_loader import prepare_document_contents

        file_path = tmp_path / "test_file.xyz"
        file_path.write_bytes(b"fake_data")

        documents_metadata = [
            {
                "file_path": str(file_path),
                "mime_type": "application/unknown",
                "filename": "test_file.xyz",
            }
        ]

        result = prepare_document_contents(documents_metadata)

        assert len(result) == 0

    def test_prepare_missing_file(self):
        """存在しないファイルのテスト"""
        from src.managers.shared.document_loader import prepare_document_contents

        documents_metadata = [
            {
                "file_path": "/nonexistent/path/file.png",
                "mime_type": "image/png",
                "filename": "file.png",
            }
        ]

        result = prepare_document_contents(documents_metadata)

        assert len(result) == 0

    def test_prepare_multiple_documents(self, tmp_path):
        """複数ドキュメントの準備テスト"""
        from src.managers.shared.document_loader import prepare_document_contents

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

        result = prepare_document_contents(documents_metadata)

        assert len(result) == 2
        assert "image" in result[0]
        assert "document" in result[1]


# =============================================================================
# U6: 実行する人・実行中の確かめ直し・記憶（BR2.8、BR6.1、BR6.2、BR7.1、NFR1.46、
# NFR1.47、NFR1.50、NFR2.17、AC4.2.1、AC4.2.2、AC4.2.5）
# =============================================================================

U6_TOPIC = "新商品のマーケティングについて"


def _agent_for(persona, created: list) -> Mock:
    agent = Mock(spec=PersonaAgent)
    agent.get_persona_id.return_value = persona.id
    agent.get_persona_name.return_value = persona.name
    agent.respond.return_value = f"{persona.name}の発言です"
    agent.respond_streaming.side_effect = lambda *a, **k: iter(["発言", "です"])
    created.append(agent)
    return agent


@pytest.fixture
def u6_world() -> World:
    return World()


@pytest.fixture
def u6_manager(u6_world):
    created: list = []
    agent_service = Mock()
    agent_service.create_persona_agent.side_effect = lambda **kw: _agent_for(
        kw["persona"], created
    )
    agent_service.get_kb_tools.return_value = ([], None)
    facilitator = Mock(spec=FacilitatorAgent)
    facilitator.rounds = 2
    facilitator.additional_instructions = ""
    facilitator.start_discussion.return_value = "始めます"
    facilitator.invoke.return_value = "ラウンドのまとめ"
    facilitator.invoke_streaming.side_effect = lambda *a, **k: iter(["まとめ"])
    agent_service.create_facilitator_agent.return_value = facilitator
    ai = Mock()
    ai.extract_insights.return_value = [
        {
            "category": "c",
            "description": "十分に長いインサイトです",
            "confidence_score": 0.7,
        }
    ]
    manager = AgentDiscussionManager(
        agent_service=agent_service,
        database_service=Mock(),
        dataset_analysis_service=Mock(),
        file_attachments=u6_world.files,
        binding_store=Mock(list_for_persona=Mock(return_value=[])),
        discussion_records=u6_world.records,
        participants=u6_world.participants,
        resource_lookup=u6_world.lookup,
        ai_service=ai,
    )
    manager.created_agents = created  # type: ignore[attr-defined]
    return manager


def _personas(world):
    return [world.persona_bodies["p1"], world.persona_bodies["p2"]]


class TestU6AgentDiscussion:
    def test_ai_execute_is_required(self, u6_manager, u6_world):
        with pytest.raises(AccessDeniedError):
            u6_manager.run_agent_discussion_full(
                NO_ROLE, _personas(u6_world), U6_TOPIC, rounds=2
            )
        u6_manager.agent_service.create_persona_agent.assert_not_called()

    def test_memory_session_is_the_saved_discussion_and_peers_only_retrieve(
        self, u6_manager, u6_world
    ):
        discussion = u6_manager.run_agent_discussion_full(
            OWNER,
            _personas(u6_world),
            U6_TOPIC,
            rounds=2,
            enable_memory=True,
            memory_mode="full",
        )
        calls = {
            c.kwargs["persona"].id: c.kwargs
            for c in u6_manager.agent_service.create_persona_agent.call_args_list
        }
        assert {c["session_id"] for c in calls.values()} == {discussion.id}
        assert calls["p1"]["memory_mode"] == "full"
        assert calls["p2"]["memory_mode"] == "retrieve_only"
        assert discussion.id in u6_world.store.bodies
        assert u6_world.store.bodies[discussion.id]["owner_sub"] == OWNER.sub
        assert u6_world.store.bodies[discussion.id]["mode"] == "agent"

    def test_one_participant_check_per_statement(self, u6_manager, u6_world):
        before = u6_world.personas.calls
        u6_manager.run_agent_discussion_full(
            OWNER, _personas(u6_world), U6_TOPIC, rounds=2
        )
        # 2 体 × 2 ラウンド = 4 発言。保存の直前には読み直さない
        assert u6_world.personas.calls - before == 4

    def test_unshared_mid_run_stops_before_the_next_statement(
        self, u6_manager, u6_world
    ):
        def respond(prompt, context=None):
            u6_world.unshare("p2")
            return "最初の発言"

        def make(**kw):
            agent = _agent_for(kw["persona"], u6_manager.created_agents)
            agent.respond.side_effect = respond
            return agent

        u6_manager.agent_service.create_persona_agent.side_effect = make
        with pytest.raises(CodedError) as info:
            u6_manager.run_agent_discussion_full(
                OWNER, _personas(u6_world), U6_TOPIC, rounds=2
            )
        assert info.value.code is ErrorCode.PERSONA_NOT_FOUND
        assert u6_world.store.bodies == {}
        spoken = sum(a.respond.call_count for a in u6_manager.created_agents)
        assert spoken == 1
        assert all(a.dispose.called for a in u6_manager.created_agents)

    def test_streaming_sends_stopped_and_saves_nothing(self, u6_manager, u6_world):
        def make(**kw):
            agent = _agent_for(kw["persona"], u6_manager.created_agents)

            def streaming(*_a, **_k):
                u6_world.unshare("p2")
                yield "発言"

            agent.respond_streaming.side_effect = streaming
            return agent

        u6_manager.agent_service.create_persona_agent.side_effect = make
        lines = list(
            u6_manager.run_agent_discussion_streaming(
                OWNER, _personas(u6_world), U6_TOPIC, rounds=2
            )
        )
        events = [json.loads(line[len("data: ") :]) for line in lines]
        assert events[-1]["type"] == "stopped"
        assert u6_world.store.bodies == {}

    def test_streaming_completes_with_the_saved_id(self, u6_manager, u6_world):
        lines = list(
            u6_manager.run_agent_discussion_streaming(
                OWNER, _personas(u6_world), U6_TOPIC, rounds=2
            )
        )
        events = [json.loads(line[len("data: ") :]) for line in lines]
        kinds = {e["type"] for e in events}
        assert {"message_start", "message_delta", "message_end"} <= kinds
        complete = events[-1]
        assert complete["type"] == "complete"
        assert complete["discussion_id"] in u6_world.store.bodies
