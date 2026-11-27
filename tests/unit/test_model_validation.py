"""Unit tests for src.managers.shared.model_validation.

Tests the model selection validation logic shared by AgentDiscussionManager and
InterviewManager.
"""

import pytest
from unittest.mock import patch

from src.models.errors import CodedError, ErrorCode
from src.managers.agent_discussion_manager import AgentDiscussionManagerError
from src.managers.interview_manager import InterviewValidationError
from src.managers.shared.model_validation import (
    resolve_effective_persona_models,
    validate_document_size_for_models,
    validate_model_selection,
)
from tests.error_helpers import raises_code


class _DummyError(CodedError):
    """Domain exception for tests."""


JP_HAIKU_5_5 = "jp.anthropic.claude-haiku-5-5"
JP_HAIKU_4_5 = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
GLOBAL_SONNET_5 = "global.anthropic.claude-sonnet-5"


def _validate_discussion(model_ids):
    validate_model_selection(
        model_ids,
        AgentDiscussionManagerError,
        ErrorCode.DISCUSSION_MODEL_UNSUPPORTED,
        ErrorCode.DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED,
    )


def _validate_interview(model_ids):
    validate_model_selection(
        model_ids,
        InterviewValidationError,
        ErrorCode.INTERVIEW_MODEL_UNSUPPORTED,
        ErrorCode.INTERVIEW_MODEL_ADDITIONAL_MODELS_DISABLED,
    )


class TestValidateModelSelectionByScope:
    """Accepts only models in the scope list or the environment default."""

    @pytest.fixture
    def jp(self, monkeypatch):
        from src.config import config

        monkeypatch.setattr(config, "INFERENCE_SCOPE", "jp")
        monkeypatch.setattr(config, "AGENT_MODEL_ID", JP_HAIKU_5_5)
        monkeypatch.setattr(config, "ENABLE_ADDITIONAL_PERSONA_MODELS", False)

    @pytest.fixture
    def global_scope(self, monkeypatch):
        from src.config import config

        monkeypatch.setattr(config, "INFERENCE_SCOPE", "global")
        monkeypatch.setattr(
            config, "AGENT_MODEL_ID", "global.anthropic.claude-haiku-5-5"
        )
        monkeypatch.setattr(config, "ENABLE_ADDITIONAL_PERSONA_MODELS", False)

    def test_jp_accepts_the_jp_models(self, jp):
        _validate_discussion({"p1": JP_HAIKU_5_5, "p2": JP_HAIKU_4_5, "p3": None})
        _validate_interview({"p1": JP_HAIKU_4_5})

    def test_jp_accepts_the_agent_model_id_outside_the_list(self, jp, monkeypatch):
        from src.config import config

        monkeypatch.setattr(config, "AGENT_MODEL_ID", "custom.agent-model")
        _validate_discussion({"p1": "custom.agent-model"})

    def test_jp_rejects_a_global_model_for_discussion(self, jp):
        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_MODEL_UNSUPPORTED
        ):
            _validate_discussion({"p1": GLOBAL_SONNET_5})

    def test_jp_rejects_a_global_model_for_interview(self, jp):
        with raises_code(
            InterviewValidationError, ErrorCode.INTERVIEW_MODEL_UNSUPPORTED
        ):
            _validate_interview({"p1": GLOBAL_SONNET_5})

    def test_jp_rejects_a_mantle_model_as_unsupported(self, jp):
        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_MODEL_UNSUPPORTED
        ):
            _validate_discussion({"p1": "google.gemma-4-31b"})

    def test_global_rejects_a_jp_model(self, global_scope):
        with raises_code(
            AgentDiscussionManagerError, ErrorCode.DISCUSSION_MODEL_UNSUPPORTED
        ):
            _validate_discussion({"p1": JP_HAIKU_4_5})

    def test_global_mantle_model_while_disabled_is_the_disabled_error(
        self, global_scope
    ):
        with raises_code(
            AgentDiscussionManagerError,
            ErrorCode.DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED,
        ):
            _validate_discussion({"p1": "openai.gpt-5.6-terra"})

    def test_global_accepts_haiku_5_5(self, global_scope):
        _validate_discussion({"p1": "global.anthropic.claude-haiku-5-5"})


class TestResolveEffectivePersonaModels:
    """Tests for filling in config.AGENT_MODEL_ID for personas with no selection.

    config.AGENT_MODEL_ID may be set to Gemma4 or similar by the environment, so personas
    with no persona_models selection (treated as None) must also be validated as models
    that are actually called.
    """

    def test_unselected_persona_falls_back_to_agent_model_id(self):
        with patch("src.managers.shared.model_validation.config") as mock_config:
            mock_config.AGENT_MODEL_ID = "google.gemma-4-31b"
            resolved = resolve_effective_persona_models(["persona-1"], None)

        assert resolved == {"persona-1": "google.gemma-4-31b"}

    def test_selected_persona_keeps_explicit_choice(self):
        with patch("src.managers.shared.model_validation.config") as mock_config:
            mock_config.AGENT_MODEL_ID = "google.gemma-4-31b"
            resolved = resolve_effective_persona_models(
                ["persona-1"], {"persona-1": "openai.gpt-5.6-terra"}
            )

        assert resolved == {"persona-1": "openai.gpt-5.6-terra"}

    def test_gemma4_as_env_default_still_enforces_size_limit(self):
        """If config.AGENT_MODEL_ID is Gemma4, the limit applies even with no persona_models selection."""
        documents_total_size = 4 * 1024 * 1024  # 4MB > 3.5MB limit

        with patch("src.managers.shared.model_validation.config") as mock_config:
            mock_config.AGENT_MODEL_ID = "google.gemma-4-31b"
            effective = resolve_effective_persona_models(["persona-1"], None)

        with pytest.raises(_DummyError) as exc_info:
            validate_document_size_for_models(
                documents_total_size,
                effective,
                _DummyError,
                ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE,
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE


class TestValidateDocumentSizeForModels:
    """Tests for the total document size check against max_request_bytes of Gemma4 and similar."""

    def test_gemma4_over_limit_raises_capacity_error(self):
        with pytest.raises(_DummyError) as exc_info:
            validate_document_size_for_models(
                4 * 1024 * 1024,  # 4MB > 3.5MB limit
                {"persona-1": "google.gemma-4-31b"},
                _DummyError,
                ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE,
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE

    def test_gemma4_under_limit_passes(self):
        validate_document_size_for_models(
            1 * 1024 * 1024,  # 1MB < 3.5MB limit
            {"persona-1": "google.gemma-4-31b"},
            _DummyError,
            ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE,
        )  # verify no exception is raised

    def test_no_persona_models_skips_validation(self):
        validate_document_size_for_models(
            100 * 1024 * 1024,  # 100MB
            None,
            _DummyError,
            ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE,
        )

    def test_stored_gemma_in_jp_is_not_limited_by_gemma(self, monkeypatch):
        """In jp, Gemma is remapped to the default, so 3.5 MB does not reject."""
        from src.config import config

        monkeypatch.setattr(config, "INFERENCE_SCOPE", "jp")
        monkeypatch.setattr(config, "AGENT_MODEL_ID", "jp.anthropic.claude-haiku-5-5")
        validate_document_size_for_models(
            4 * 1024 * 1024,
            {"persona-1": "google.gemma-4-31b"},
            _DummyError,
            ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE,
        )

    def test_gemma_in_global_is_still_limited(self, monkeypatch):
        from src.config import config

        monkeypatch.setattr(config, "INFERENCE_SCOPE", "global")
        with raises_code(_DummyError, ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE):
            validate_document_size_for_models(
                4 * 1024 * 1024,
                {"persona-1": "google.gemma-4-31b"},
                _DummyError,
                ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE,
            )

    def test_claude_model_has_no_limit(self):
        validate_document_size_for_models(
            100 * 1024 * 1024,  # 100MB
            {"persona-1": "global.anthropic.claude-haiku-4-5-20251001-v1:0"},
            _DummyError,
            ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE,
        )
