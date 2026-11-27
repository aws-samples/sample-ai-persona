"""
Unit tests for AgentDiscussionManager.

Covers discussion management in agent mode. Discussions are run by a
principal and saved through DiscussionRecords.
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
    kwargs.setdefault("dataset_store", Mock())
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
    """Agent discussions apply the same input rules as non-agent ones (max 5, no duplicates).

    The agent side used to lack the persona limit and duplicate checks and
    drifted. Both modes now share DiscussionValidation.
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
    """Tests for AgentDiscussionManager initialization."""

    @patch("src.managers.agent_discussion_manager.service_factory")
    def test_initialization_success(self, mock_service_factory):
        """Initializes normally."""
        mock_dataset_store = Mock()
        mock_agent_service = Mock()

        mock_service_factory.get_dataset_store.return_value = mock_dataset_store
        mock_service_factory.get_agent_service.return_value = mock_agent_service

        manager = AgentDiscussionManager()

        assert manager is not None
        assert manager.dataset_store is mock_dataset_store
        assert manager.agent_service is mock_agent_service

    def test_initialization_with_custom_services(self):
        """Initializes with custom services."""
        mock_agent_service = Mock()
        mock_dataset_store = Mock()

        manager = _manager(
            agent_service=mock_agent_service, dataset_store=mock_dataset_store
        )

        assert manager.agent_service == mock_agent_service
        assert manager.dataset_store == mock_dataset_store


class TestCreatePersonaAgents:
    """Tests for persona agent creation."""

    def test_create_persona_agents_success(self, sample_persona, sample_persona_2):
        """Persona agent creation succeeds."""
        mock_persona_agent = Mock(spec=PersonaAgent)
        manager = _manager()
        # Agent construction is delegated to the component (_agent_integration.build_agent), so mock it
        manager._agent_integration.build_agent = Mock(return_value=mock_persona_agent)

        agents = manager._create_persona_agents(
            [sample_persona, sample_persona_2], {}, principal=OWNER
        )

        assert len(agents) == 2
        assert manager._agent_integration.build_agent.call_count == 2

    def test_create_persona_agents_with_custom_prompts(
        self, sample_persona, sample_persona_2
    ):
        """A custom prompt is passed through unchanged."""
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
        """Memory is written only for personas owned by the acting principal."""
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
    """Validation tests for persona_models / facilitator_model."""

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
        mock_config.INFERENCE_SCOPE = "global"
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
        """If build_agent raises AgentConfigurationError, the CONFIG code propagates
        instead of being swallowed as a per-persona failure."""
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
        """If the second agent fails with a config error after the first was created, the first is released.

        Without cleanup_agents before the raise, the already-created
        persona agent was passed nowhere and leaked.
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
        """Created agents are released even when fewer than the minimum (2) were built."""
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
    """persona_agents are released when facilitator creation fails.

    If facilitator creation failed after the persona agents were built, the
    round-execution finally was never reached and persona_agents leaked.
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
    """Tests for facilitator agent creation."""

    def test_create_facilitator_agent_success(self):
        """Facilitator agent creation succeeds."""
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
    """Tests for round execution (_rounds)."""

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
        """The discussion completes and returns statements and a summary."""
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
        """Too few personas returns an error."""
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
        """Agents are released even when document validation fails.

        Document loading used to sit outside the try block, so on validation
        failure the finally clause's cleanup_agents never ran and resources
        leaked. A PDF over Gemma4's 3.5MB limit is attached to trigger the
        size-validation failure.
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
        """An empty topic returns an error."""
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
        """Agents are also released when the topic is too short (input validation failure)."""
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
    """Tests for agent resource cleanup."""

    def test_dispose_agents_success(self):
        """Agent resources are released successfully."""
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
        """Errors during agent cleanup are handled."""
        manager = _manager()
        mock_persona_agent = Mock(spec=PersonaAgent)
        mock_persona_agent.dispose.side_effect = Exception("Dispose error")
        mock_facilitator = Mock(spec=FacilitatorAgent)

        manager._cleanup_agents([mock_persona_agent], mock_facilitator)

        mock_facilitator.dispose.assert_called_once()


class TestPrepareDocumentContents:
    """Tests for document content preparation (via shared/document_loader)."""

    def test_prepare_image_content(self, tmp_path):
        """Image content preparation."""
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
        """PDF content preparation."""
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
        """Text content preparation."""
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
        """Unsupported MIME type."""
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
        """Nonexistent file."""
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
        """Preparing multiple documents."""
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
# Acting principal, re-checks while running, and memory
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
        dataset_store=Mock(),
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

    def test_insights_get_the_participants_profiles(self, u6_manager, u6_world):
        u6_manager.run_agent_discussion_full(
            OWNER, _personas(u6_world), U6_TOPIC, rounds=2
        )
        profiles = u6_manager._ai_service.extract_insights.call_args.kwargs[
            "participant_profiles"
        ]
        names = {p.name for p in _personas(u6_world)}
        assert {line.split(":")[0].removeprefix("- ") for line in profiles} == names

    def test_one_participant_check_per_statement(self, u6_manager, u6_world):
        before = u6_world.personas.calls
        u6_manager.run_agent_discussion_full(
            OWNER, _personas(u6_world), U6_TOPIC, rounds=2
        )
        # 2 personas x 2 rounds = 4 statements. No re-read right before saving
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
