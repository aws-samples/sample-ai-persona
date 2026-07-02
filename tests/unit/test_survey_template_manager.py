"""SurveyTemplateManager のユニットテスト"""

from unittest.mock import Mock

import pytest

from src.managers.survey_template_manager import (
    SurveyTemplateManager,
    SurveyTemplateManagerError,
    SurveyTemplateValidationError,
)
from src.models.survey_template import Question, SurveyTemplate


@pytest.fixture
def mock_db() -> Mock:
    return Mock()


@pytest.fixture
def mock_ai() -> Mock:
    return Mock()


@pytest.fixture
def mgr(mock_db: Mock, mock_ai: Mock) -> SurveyTemplateManager:
    return SurveyTemplateManager(database_service=mock_db, ai_service=mock_ai)


@pytest.fixture
def sample_questions() -> list:
    return [
        Question.create_multiple_choice("Q1", ["A", "B"]),
        Question.create_free_text("Q2"),
    ]


class TestCreateTemplate:
    def test_success(
        self, mgr: SurveyTemplateManager, mock_db: Mock, sample_questions: list
    ) -> None:
        result = mgr.create_template("テスト", sample_questions)
        assert isinstance(result, SurveyTemplate)
        assert result.name == "テスト"
        mock_db.save_survey_template.assert_called_once()

    def test_empty_name_raises(
        self, mgr: SurveyTemplateManager, sample_questions: list
    ) -> None:
        with pytest.raises(SurveyTemplateValidationError):
            mgr.create_template("  ", sample_questions)

    def test_no_questions_raises(self, mgr: SurveyTemplateManager) -> None:
        with pytest.raises(SurveyTemplateValidationError):
            mgr.create_template("名前", [])

    def test_invalid_multiple_choice_raises(self, mgr: SurveyTemplateManager) -> None:
        bad_q = [Question.create_multiple_choice("Q", ["一つだけ"])]
        with pytest.raises(SurveyTemplateValidationError):
            mgr.create_template("名前", bad_q)


class TestUpdateTemplate:
    def test_success(
        self, mgr: SurveyTemplateManager, mock_db: Mock, sample_questions: list
    ) -> None:
        mock_db.get_survey_template.return_value = SurveyTemplate.create_new(
            name="旧名", questions=sample_questions
        )
        result = mgr.update_template("tid", "新名", sample_questions)
        assert result.name == "新名"
        mock_db.update_survey_template.assert_called_once()

    def test_not_found_raises(
        self, mgr: SurveyTemplateManager, mock_db: Mock, sample_questions: list
    ) -> None:
        mock_db.get_survey_template.return_value = None
        with pytest.raises(SurveyTemplateManagerError):
            mgr.update_template("missing", "名前", sample_questions)


class TestDeleteTemplate:
    def test_success(self, mgr: SurveyTemplateManager, mock_db: Mock) -> None:
        mock_db.delete_survey_template.return_value = True
        assert mgr.delete_template("tid") is True


class TestGenerateAIChatResponse:
    def test_returns_reply(self, mgr: SurveyTemplateManager, mock_ai: Mock) -> None:
        mock_ai.chat_for_survey.return_value = "ヒアリング回答"
        reply = mgr.generate_ai_chat_response([{"role": "user", "content": "開始"}])
        assert reply == "ヒアリング回答"
        mock_ai.chat_for_survey.assert_called_once()

    def test_no_ai_raises(self, mock_db: Mock) -> None:
        mgr = SurveyTemplateManager(database_service=mock_db, ai_service=Mock())
        mgr.ai_service = None  # type: ignore[assignment]
        with pytest.raises(SurveyTemplateManagerError):
            mgr.generate_ai_chat_response([{"role": "user", "content": "hi"}])

    def test_empty_messages_raises(self, mgr: SurveyTemplateManager) -> None:
        with pytest.raises(SurveyTemplateValidationError):
            mgr.generate_ai_chat_response([])

    def test_last_message_not_user_raises(self, mgr: SurveyTemplateManager) -> None:
        with pytest.raises(SurveyTemplateValidationError):
            mgr.generate_ai_chat_response([{"role": "assistant", "content": "hi"}])


class TestGenerateAIQuestionsDraft:
    def test_returns_questions(self, mgr: SurveyTemplateManager, mock_ai: Mock) -> None:
        mock_ai.generate_survey_questions_draft.return_value = {
            "summary": "要約",
            "template_name": "テスト",
            "questions": [
                {"question_type": "free_text", "text": "Q1"},
                {
                    "question_type": "multiple_choice",
                    "text": "Q2",
                    "options": ["A", "B"],
                },
            ],
        }
        result = mgr.generate_ai_questions_draft(
            [{"role": "user", "content": "テスト"}]
        )
        assert result["summary"] == "要約"
        assert len(result["questions"]) == 2

    def test_no_questions_raises(
        self, mgr: SurveyTemplateManager, mock_ai: Mock
    ) -> None:
        mock_ai.generate_survey_questions_draft.return_value = {
            "summary": "",
            "questions": [],
        }
        with pytest.raises(SurveyTemplateManagerError):
            mgr.generate_ai_questions_draft([{"role": "user", "content": "test"}])


class TestValidateAIMessages:
    def test_too_long_message(self, mgr: SurveyTemplateManager) -> None:
        long_msg = [{"role": "user", "content": "x" * 2001}]
        with pytest.raises(SurveyTemplateValidationError):
            mgr._validate_ai_messages(long_msg)

    def test_too_many_messages(self, mgr: SurveyTemplateManager) -> None:
        msgs = [{"role": "user", "content": "hi"}] * 41
        with pytest.raises(SurveyTemplateValidationError):
            mgr._validate_ai_messages(msgs)
