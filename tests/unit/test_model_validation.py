"""
src.managers.shared.model_validation の単体テスト

AgentDiscussionManager, InterviewManager の両方から共通利用される
モデル選択バリデーションロジックをテストします。
"""

import pytest
from unittest.mock import patch

from src.models.errors import CodedError, ErrorCode
from src.managers.shared.model_validation import (
    resolve_effective_persona_models,
    validate_document_size_for_models,
)


class _DummyError(CodedError):
    """テスト用のドメイン例外。"""


class TestResolveEffectivePersonaModels:
    """未選択ペルソナへのconfig.AGENT_MODEL_ID補完のテスト

    Issue #107再レビュー: config.AGENT_MODEL_IDが環境でGemma4等に設定されうるため、
    persona_models未選択（None扱い）のペルソナも実際に呼び出されるモデルとして
    検証対象に含める必要がある。
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
        """config.AGENT_MODEL_IDがGemma4の場合、persona_models未選択でも上限が効くこと。"""
        documents_total_size = 4 * 1024 * 1024  # 4MB > 3.5MB上限

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
    """Gemma4等のmax_request_bytesに対するドキュメント合計サイズ検証のテスト"""

    def test_gemma4_over_limit_raises_capacity_error(self):
        with pytest.raises(_DummyError) as exc_info:
            validate_document_size_for_models(
                4 * 1024 * 1024,  # 4MB > 3.5MB上限
                {"persona-1": "google.gemma-4-31b"},
                _DummyError,
                ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE,
            )

        assert exc_info.value.code is ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE

    def test_gemma4_under_limit_passes(self):
        validate_document_size_for_models(
            1 * 1024 * 1024,  # 1MB < 3.5MB上限
            {"persona-1": "google.gemma-4-31b"},
            _DummyError,
            ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE,
        )  # 例外が発生しないことを確認

    def test_no_persona_models_skips_validation(self):
        validate_document_size_for_models(
            100 * 1024 * 1024,  # 100MB
            None,
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
