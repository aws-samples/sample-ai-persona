"""
SurveyManager のアンケートAI生成機能のユニットテスト（Issue #23）
"""

from unittest.mock import Mock

import pytest

from src.managers.survey_manager import (
    SurveyManager,
    SurveyManagerError,
    SurveyValidationError,
)


@pytest.fixture
def mgr_with_ai() -> SurveyManager:
    return SurveyManager(
        database_service=Mock(),
        survey_service=Mock(),
        ai_service=Mock(),
    )


@pytest.fixture
def mgr_without_ai() -> SurveyManager:
    return SurveyManager(
        database_service=Mock(),
        survey_service=Mock(),
    )


class TestGenerateAIChatResponse:
    def test_returns_ai_reply(self, mgr_with_ai: SurveyManager) -> None:
        mgr_with_ai.ai_service.chat_for_survey.return_value = "次に何を聞きますか？"
        reply = mgr_with_ai.generate_ai_chat_response(
            [{"role": "user", "content": "調査開始"}]
        )
        assert reply == "次に何を聞きますか？"
        mgr_with_ai.ai_service.chat_for_survey.assert_called_once()

    def test_without_ai_raises(self, mgr_without_ai: SurveyManager) -> None:
        with pytest.raises(SurveyManagerError):
            mgr_without_ai.generate_ai_chat_response(
                [{"role": "user", "content": "hi"}]
            )

    def test_empty_messages_raises(self, mgr_with_ai: SurveyManager) -> None:
        with pytest.raises(SurveyValidationError):
            mgr_with_ai.generate_ai_chat_response([])

    def test_invalid_role_raises(self, mgr_with_ai: SurveyManager) -> None:
        with pytest.raises(SurveyValidationError):
            mgr_with_ai.generate_ai_chat_response([{"role": "system", "content": "x"}])

    def test_last_must_be_user(self, mgr_with_ai: SurveyManager) -> None:
        with pytest.raises(SurveyValidationError):
            mgr_with_ai.generate_ai_chat_response(
                [{"role": "assistant", "content": "yo"}]
            )

    def test_too_many_messages_raises(self, mgr_with_ai: SurveyManager) -> None:
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(50)]
        with pytest.raises(SurveyValidationError):
            mgr_with_ai.generate_ai_chat_response(msgs)

    def test_too_long_message_raises(self, mgr_with_ai: SurveyManager) -> None:
        long_content = "あ" * 2001
        with pytest.raises(SurveyValidationError):
            mgr_with_ai.generate_ai_chat_response(
                [{"role": "user", "content": long_content}]
            )


class TestGenerateAIQuestionsDraft:
    def _good_draft(self) -> dict:
        return {
            "summary": "基本的な購入意向調査",
            "questions": [
                {
                    "question_type": "multiple_choice",
                    "text": "利用頻度は？",
                    "options": ["毎日", "時々", "使わない"],
                    "allow_multiple": False,
                    "max_selections": 0,
                },
                {
                    "question_type": "scale_rating",
                    "text": "魅力度",
                    "options": [],
                    "allow_multiple": False,
                    "max_selections": 0,
                },
                {
                    "question_type": "free_text",
                    "text": "自由記述",
                    "options": [],
                    "allow_multiple": False,
                    "max_selections": 0,
                },
            ],
        }

    def test_returns_dict_with_questions(self, mgr_with_ai: SurveyManager) -> None:
        mgr_with_ai.ai_service.generate_survey_questions_draft.return_value = (
            self._good_draft()
        )
        result = mgr_with_ai.generate_ai_questions_draft(
            [{"role": "user", "content": "購入意向"}]
        )
        assert result["summary"] == "基本的な購入意向調査"
        assert len(result["questions"]) == 3
        # Question.to_dict() の形式
        q0 = result["questions"][0]
        assert q0["question_type"] == "multiple_choice"
        assert q0["options"] == ["毎日", "時々", "使わない"]
        assert "id" in q0

    def test_template_name_passed_through(self, mgr_with_ai: SurveyManager) -> None:
        draft = self._good_draft()
        draft["template_name"] = "新商品購入意向調査"
        mgr_with_ai.ai_service.generate_survey_questions_draft.return_value = draft
        result = mgr_with_ai.generate_ai_questions_draft(
            [{"role": "user", "content": "x"}]
        )
        assert result["template_name"] == "新商品購入意向調査"

    def test_multiple_choice_with_insufficient_options_raises(
        self, mgr_with_ai: SurveyManager
    ) -> None:
        # 選択肢1つしかない → _validate_questions で拒否されること
        draft = self._good_draft()
        draft["questions"] = [
            {
                "question_type": "multiple_choice",
                "text": "1つだけ",
                "options": ["A"],
                "allow_multiple": False,
                "max_selections": 0,
            }
        ]
        mgr_with_ai.ai_service.generate_survey_questions_draft.return_value = draft
        with pytest.raises(SurveyValidationError):
            mgr_with_ai.generate_ai_questions_draft([{"role": "user", "content": "x"}])

    def test_invalid_question_type_raises(self, mgr_with_ai: SurveyManager) -> None:
        draft = {
            "summary": "",
            "questions": [
                {"question_type": "unknown_type", "text": "hi", "options": []}
            ],
        }
        mgr_with_ai.ai_service.generate_survey_questions_draft.return_value = draft
        with pytest.raises(SurveyValidationError):
            mgr_with_ai.generate_ai_questions_draft([{"role": "user", "content": "x"}])

    def test_missing_text_raises(self, mgr_with_ai: SurveyManager) -> None:
        draft = {
            "summary": "",
            "questions": [{"question_type": "free_text", "text": "", "options": []}],
        }
        mgr_with_ai.ai_service.generate_survey_questions_draft.return_value = draft
        with pytest.raises(SurveyValidationError):
            mgr_with_ai.generate_ai_questions_draft([{"role": "user", "content": "x"}])

    def test_max_selections_clamped_when_not_allow_multiple(
        self, mgr_with_ai: SurveyManager
    ) -> None:
        draft = {
            "summary": "",
            "questions": [
                {
                    "question_type": "multiple_choice",
                    "text": "ok",
                    "options": ["a", "b"],
                    "allow_multiple": False,
                    "max_selections": 2,  # allow_multiple=False なのに値入り → 0 にクランプ
                }
            ],
        }
        mgr_with_ai.ai_service.generate_survey_questions_draft.return_value = draft
        result = mgr_with_ai.generate_ai_questions_draft(
            [{"role": "user", "content": "x"}]
        )
        q = result["questions"][0]
        assert q.get("allow_multiple", False) is False
        # allow_multiple=False の時は to_dict() で allow_multiple/max_selections 省略される
        assert "max_selections" not in q

    def test_without_ai_raises(self, mgr_without_ai: SurveyManager) -> None:
        with pytest.raises(SurveyManagerError):
            mgr_without_ai.generate_ai_questions_draft(
                [{"role": "user", "content": "x"}]
            )
