"""
AgentDiscussionManager の単体テスト

エージェントモードでの議論管理をテストします。
"""

import pytest

from src.models.errors import ErrorCode
from unittest.mock import Mock, patch

from src.managers.agent_discussion_manager import (
    AgentDiscussionManager,
    AgentDiscussionManagerError,
)
from src.models.discussion import Discussion
from src.services.agent_service import (
    AgentConfigurationError,
    AgentInitializationError,
    PersonaAgent,
    FacilitatorAgent,
)


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

        mock_persona_agent = Mock(spec=PersonaAgent)
        mock_persona_agent.get_persona_id.return_value = sample_persona.id
        mock_agent_service.create_persona_agent.return_value = mock_persona_agent

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )
        # _create_agent_with_integrations 内で service_factory を直接インポートするためモック
        manager._create_agent_with_integrations = Mock(return_value=mock_persona_agent)

        personas = [sample_persona, sample_persona_2]
        system_prompts = {}

        agents = manager.create_persona_agents(personas, system_prompts)

        assert len(agents) == 2
        assert manager._create_agent_with_integrations.call_count == 2

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
        manager._create_agent_with_integrations = Mock(return_value=mock_persona_agent)

        custom_prompt = "カスタムシステムプロンプト"
        system_prompts = {sample_persona.id: custom_prompt}

        # 最低2つのペルソナが必要
        manager.create_persona_agents(
            [sample_persona, sample_persona_2], system_prompts
        )

        # エージェント作成が2回呼ばれたことを確認
        assert manager._create_agent_with_integrations.call_count == 2


class TestModelSelectionValidation:
    """persona_models / facilitator_model のバリデーションテスト"""

    def _make_manager(self):
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None
        mock_agent_service = Mock()
        return AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        ), mock_agent_service

    def test_create_persona_agents_unsupported_model_raises_validation(
        self, sample_persona, sample_persona_2
    ):
        manager, _ = self._make_manager()
        manager._create_agent_with_integrations = Mock()

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager.create_persona_agents(
                [sample_persona, sample_persona_2],
                {},
                persona_models={sample_persona.id: "unknown.model-id"},
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_MODEL_UNSUPPORTED

    @patch("src.managers.agent_discussion_manager.config")
    def test_create_persona_agents_mantle_disabled_raises_config(
        self, mock_config, sample_persona, sample_persona_2
    ):
        mock_config.ENABLE_ADDITIONAL_PERSONA_MODELS = False
        manager, _ = self._make_manager()
        manager._create_agent_with_integrations = Mock()

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager.create_persona_agents(
                [sample_persona, sample_persona_2],
                {},
                persona_models={sample_persona.id: "openai.gpt-5.6-terra"},
            )

        assert (
            exc_info.value.code is ErrorCode.DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED
        )

    def test_create_facilitator_agent_unsupported_model_raises_validation(self):
        manager, _ = self._make_manager()

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager.create_facilitator_agent(
                rounds=3, facilitator_model="unknown.model-id"
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_MODEL_UNSUPPORTED

    def test_create_persona_agents_config_error_not_squashed(
        self, sample_persona, sample_persona_2
    ):
        """_create_agent_with_integrationsがAgentConfigurationErrorを投げた場合、
        個別ペルソナ失敗として握り潰さずCONFIGコードのまま伝播する。"""
        manager, _ = self._make_manager()
        manager._create_agent_with_integrations = Mock(
            side_effect=AgentConfigurationError(
                "mantle disabled", code=ErrorCode.AGENT_MODEL_ADDITIONAL_MODELS_DISABLED
            )
        )

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager.create_persona_agents([sample_persona, sample_persona_2], {})

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
        manager, _ = self._make_manager()
        mock_agent_1 = Mock(spec=PersonaAgent)

        manager._create_agent_with_integrations = Mock(
            side_effect=[
                mock_agent_1,
                AgentConfigurationError(
                    "mantle disabled",
                    code=ErrorCode.AGENT_MODEL_ADDITIONAL_MODELS_DISABLED,
                ),
            ]
        )

        with pytest.raises(AgentDiscussionManagerError):
            manager.create_persona_agents([sample_persona, sample_persona_2], {})

        mock_agent_1.dispose.assert_called_once()

    def test_create_persona_agents_below_minimum_disposes_created_agents(
        self, sample_persona, sample_persona_2
    ):
        """作成できたエージェントが最低数(2)未満の場合も、作成済みのagentを解放すること。"""
        manager, _ = self._make_manager()
        mock_agent_1 = Mock(spec=PersonaAgent)

        manager._create_agent_with_integrations = Mock(
            side_effect=[
                mock_agent_1,
                AgentInitializationError("boom"),
            ]
        )

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager.create_persona_agents([sample_persona, sample_persona_2], {})

        assert exc_info.value.code is ErrorCode.DISCUSSION_AGENT_SETUP_FAILED
        mock_agent_1.dispose.assert_called_once()


class TestRunAgentDiscussionFacilitatorFailureCleanup:
    """facilitator作成失敗時にpersona_agentsが解放されることのテスト

    Issue #107再レビュー: create_persona_agents成功後にcreate_facilitator_agentが
    失敗すると、start_agent_discussion(_streaming)のfinallyに到達せず
    persona_agentsがリークしていた。
    """

    def _make_manager(self):
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None
        mock_agent_service = Mock()
        return AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

    def test_run_agent_discussion_full_disposes_personas_on_facilitator_failure(
        self, sample_persona, sample_persona_2
    ):
        manager = self._make_manager()
        mock_persona_agent_1 = Mock(spec=PersonaAgent)
        mock_persona_agent_2 = Mock(spec=PersonaAgent)
        manager.create_persona_agents = Mock(
            return_value=[mock_persona_agent_1, mock_persona_agent_2]
        )
        manager.create_facilitator_agent = Mock(
            side_effect=AgentDiscussionManagerError(
                "rounds too many", code=ErrorCode.DISCUSSION_ROUNDS_TOO_MANY
            )
        )

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager.run_agent_discussion_full(
                personas=[sample_persona, sample_persona_2],
                topic="テストトピック",
                rounds=99,
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_ROUNDS_TOO_MANY
        mock_persona_agent_1.dispose.assert_called_once()
        mock_persona_agent_2.dispose.assert_called_once()

    def test_run_agent_discussion_streaming_disposes_personas_on_facilitator_failure(
        self, sample_persona, sample_persona_2
    ):
        manager = self._make_manager()
        mock_persona_agent_1 = Mock(spec=PersonaAgent)
        mock_persona_agent_2 = Mock(spec=PersonaAgent)
        manager.create_persona_agents = Mock(
            return_value=[mock_persona_agent_1, mock_persona_agent_2]
        )
        manager.create_facilitator_agent = Mock(
            side_effect=AgentDiscussionManagerError(
                "rounds too many", code=ErrorCode.DISCUSSION_ROUNDS_TOO_MANY
            )
        )

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            list(
                manager.run_agent_discussion_streaming(
                    personas=[sample_persona, sample_persona_2],
                    topic="テストトピック",
                    rounds=99,
                )
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_ROUNDS_TOO_MANY
        mock_persona_agent_1.dispose.assert_called_once()
        mock_persona_agent_2.dispose.assert_called_once()


class TestValidateDocumentSupportForModels:
    """Mantle経由モデル（GPT/Gemma）のドキュメント種別対応検証のテスト

    画像（input_image）はfilenameという概念を持たずMantle側も問題なく受理するため許可、
    document（PDF等。input_file）はStrands SDKがfilenameを送信しない実装漏れにより
    Mantle側で"Unsupported file type"エラーとなるため拒否する（AWS実機検証で確認済み）。
    """

    def _make_manager(self):
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None
        mock_agent_service = Mock()
        return AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

    def test_mantle_model_with_pdf_raises_capacity_error(self):
        manager = self._make_manager()
        documents_metadata = [{"mime_type": "application/pdf"}]

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager._validate_document_support_for_models(
                documents_metadata, {"persona-1": "openai.gpt-5.6-terra"}
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_MODEL_DOCUMENT_UNSUPPORTED

    def test_gemma4_with_pdf_raises_capacity_error(self):
        manager = self._make_manager()
        documents_metadata = [{"mime_type": "application/pdf"}]

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager._validate_document_support_for_models(
                documents_metadata, {"persona-1": "google.gemma-4-31b"}
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_MODEL_DOCUMENT_UNSUPPORTED

    def test_mantle_model_with_image_passes(self):
        """画像はfilenameを持たないパラメータ形式のためMantle経由でも許可される。"""
        manager = self._make_manager()
        documents_metadata = [{"mime_type": "image/png"}]

        manager._validate_document_support_for_models(
            documents_metadata, {"persona-1": "openai.gpt-5.6-terra"}
        )  # 例外が発生しないことを確認

    def test_mantle_model_with_image_and_pdf_raises_capacity_error(self):
        """画像とPDFが混在する場合はPDFが原因で拒否される。"""
        manager = self._make_manager()
        documents_metadata = [
            {"mime_type": "image/png"},
            {"mime_type": "application/pdf"},
        ]

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager._validate_document_support_for_models(
                documents_metadata, {"persona-1": "openai.gpt-5.6-terra"}
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_MODEL_DOCUMENT_UNSUPPORTED

    def test_claude_model_with_pdf_passes(self):
        manager = self._make_manager()
        documents_metadata = [{"mime_type": "application/pdf"}]

        manager._validate_document_support_for_models(
            documents_metadata,
            {"persona-1": "global.anthropic.claude-haiku-4-5-20251001-v1:0"},
        )  # 例外が発生しないことを確認

    def test_no_persona_models_skips_validation(self):
        manager = self._make_manager()
        documents_metadata = [{"mime_type": "application/pdf"}]

        manager._validate_document_support_for_models(documents_metadata, None)


class TestResolveEffectivePersonaModels:
    """未選択ペルソナへのconfig.AGENT_MODEL_ID補完のテスト

    Issue #107再レビュー: config.AGENT_MODEL_IDが環境でGemma4等に設定されうるため、
    persona_models未選択（None扱い）のペルソナも実際に呼び出されるモデルとして
    検証対象に含める必要がある。
    """

    def _make_manager(self):
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None
        mock_agent_service = Mock()
        return AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

    def test_unselected_persona_falls_back_to_agent_model_id(self):
        manager = self._make_manager()
        agent1 = Mock(spec=PersonaAgent)
        agent1.get_persona_id.return_value = "persona-1"

        with patch("src.managers.agent_discussion_manager.config") as mock_config:
            mock_config.AGENT_MODEL_ID = "google.gemma-4-31b"
            resolved = manager._resolve_effective_persona_models([agent1], None)

        assert resolved == {"persona-1": "google.gemma-4-31b"}

    def test_selected_persona_keeps_explicit_choice(self):
        manager = self._make_manager()
        agent1 = Mock(spec=PersonaAgent)
        agent1.get_persona_id.return_value = "persona-1"

        with patch("src.managers.agent_discussion_manager.config") as mock_config:
            mock_config.AGENT_MODEL_ID = "google.gemma-4-31b"
            resolved = manager._resolve_effective_persona_models(
                [agent1], {"persona-1": "openai.gpt-5.6-terra"}
            )

        assert resolved == {"persona-1": "openai.gpt-5.6-terra"}

    def test_gemma4_as_env_default_still_enforces_size_limit(self):
        """config.AGENT_MODEL_IDがGemma4の場合、persona_models未選択でも上限が効くこと。"""
        manager = self._make_manager()
        agent1 = Mock(spec=PersonaAgent)
        agent1.get_persona_id.return_value = "persona-1"
        documents_metadata = [{"file_size": 4 * 1024 * 1024}]  # 4MB > 3.5MB上限

        with patch("src.managers.agent_discussion_manager.config") as mock_config:
            mock_config.AGENT_MODEL_ID = "google.gemma-4-31b"
            effective = manager._resolve_effective_persona_models([agent1], None)

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager._validate_document_size_for_models(documents_metadata, effective)

        assert exc_info.value.code is ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE


class TestValidateDocumentSizeForModels:
    """Gemma4等のmax_request_bytesに対するドキュメント合計サイズ検証のテスト"""

    def _make_manager(self):
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None
        mock_agent_service = Mock()
        return AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

    def test_gemma4_over_limit_raises_capacity_error(self):
        manager = self._make_manager()
        documents_metadata = [{"file_size": 4 * 1024 * 1024}]  # 4MB > 3.5MB上限

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager._validate_document_size_for_models(
                documents_metadata, {"persona-1": "google.gemma-4-31b"}
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE

    def test_gemma4_under_limit_passes(self):
        manager = self._make_manager()
        documents_metadata = [{"file_size": 1 * 1024 * 1024}]  # 1MB < 3.5MB上限

        manager._validate_document_size_for_models(
            documents_metadata, {"persona-1": "google.gemma-4-31b"}
        )  # 例外が発生しないことを確認

    def test_no_persona_models_skips_validation(self):
        manager = self._make_manager()
        documents_metadata = [{"file_size": 100 * 1024 * 1024}]  # 100MB

        manager._validate_document_size_for_models(documents_metadata, None)

    def test_claude_model_has_no_limit(self):
        manager = self._make_manager()
        documents_metadata = [{"file_size": 100 * 1024 * 1024}]  # 100MB

        manager._validate_document_size_for_models(
            documents_metadata,
            {"persona-1": "global.anthropic.claude-haiku-4-5-20251001-v1:0"},
        )


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
            3, "テスト指示", model_id=None
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
        mock_facilitator.start_discussion.return_value = "議論を開始します"
        mock_facilitator.invoke.return_value = "ラウンドのまとめ"
        mock_facilitator.rounds = 1
        mock_facilitator.additional_instructions = ""
        mock_facilitator.clear_conversation_history = Mock()

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

        assert exc_info.value.code is ErrorCode.DISCUSSION_TOO_FEW_PERSONAS

    def test_start_discussion_document_validation_failure_disposes_agents(
        self, sample_persona, sample_persona_2
    ):
        """Mantle系モデル+PDF添付でドキュメント検証が失敗した場合もagentが解放されること。

        Issue #107レビュー: ドキュメント読み込みがtryブロックの外にあったため、
        検証失敗時にfinally節のcleanup_agentsが実行されずリソースがリークしていた。
        """
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None
        mock_db_service.get_uploaded_file_info.return_value = {
            "id": "doc-1",
            "filename": "test.pdf",
            "file_path": "path/to/test.pdf",
            "file_size": 100,
            "mime_type": "application/pdf",
            "uploaded_at": None,
        }

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        mock_persona_agent_1 = Mock(spec=PersonaAgent)
        mock_persona_agent_2 = Mock(spec=PersonaAgent)
        persona_agents = [mock_persona_agent_1, mock_persona_agent_2]

        mock_facilitator = Mock(spec=FacilitatorAgent)

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager.start_agent_discussion(
                personas=[sample_persona, sample_persona_2],
                topic="テストトピック",
                persona_agents=persona_agents,
                facilitator=mock_facilitator,
                document_ids=["doc-1"],
                persona_models={sample_persona.id: "openai.gpt-5.6-terra"},
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_MODEL_DOCUMENT_UNSUPPORTED
        mock_persona_agent_1.dispose.assert_called_once()
        mock_persona_agent_2.dispose.assert_called_once()
        mock_facilitator.dispose.assert_called_once()

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

        assert exc_info.value.code is ErrorCode.DISCUSSION_TOPIC_REQUIRED

    def test_start_discussion_topic_too_short_disposes_agents(
        self, sample_persona, sample_persona_2
    ):
        """トピックが短すぎる（入力検証失敗）場合もagentが解放されること。

        Issue #107再レビュー: _validate_discussion_inputがtry/finallyの外にあり、
        トピック長等の通常の入力検証エラーではcleanup_agentsが実行されずagentが
        リークしていた。
        """
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        mock_persona_agent_1 = Mock(spec=PersonaAgent)
        mock_persona_agent_2 = Mock(spec=PersonaAgent)
        mock_facilitator = Mock(spec=FacilitatorAgent)

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            manager.start_agent_discussion(
                personas=[sample_persona, sample_persona_2],
                topic="短い",  # 5文字未満
                persona_agents=[mock_persona_agent_1, mock_persona_agent_2],
                facilitator=mock_facilitator,
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_TOPIC_TOO_SHORT
        mock_persona_agent_1.dispose.assert_called_once()
        mock_persona_agent_2.dispose.assert_called_once()
        mock_facilitator.dispose.assert_called_once()

    def test_start_discussion_streaming_topic_too_short_disposes_agents(
        self, sample_persona, sample_persona_2
    ):
        """ストリーミング版でも入力検証失敗時にagentが解放されること。"""
        mock_db_service = Mock()
        mock_db_service.initialize_database.return_value = None

        mock_agent_service = Mock()

        manager = AgentDiscussionManager(
            agent_service=mock_agent_service, database_service=mock_db_service
        )

        mock_persona_agent_1 = Mock(spec=PersonaAgent)
        mock_persona_agent_2 = Mock(spec=PersonaAgent)
        mock_facilitator = Mock(spec=FacilitatorAgent)

        with pytest.raises(AgentDiscussionManagerError) as exc_info:
            list(
                manager.start_agent_discussion_streaming(
                    personas=[sample_persona, sample_persona_2],
                    topic="短い",
                    persona_agents=[mock_persona_agent_1, mock_persona_agent_2],
                    facilitator=mock_facilitator,
                )
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_TOPIC_TOO_SHORT
        mock_persona_agent_1.dispose.assert_called_once()
        mock_persona_agent_2.dispose.assert_called_once()
        mock_facilitator.dispose.assert_called_once()


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

        assert exc_info.value.code is ErrorCode.DISCUSSION_OPERATION_FAILED


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

        manager.cleanup_agents(persona_agents, mock_facilitator)

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
        manager.cleanup_agents([mock_persona_agent], mock_facilitator)

        # ファシリテーターのdisposeも呼ばれることを確認
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
