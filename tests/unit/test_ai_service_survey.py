"""
AIService のアンケートAI生成機能のユニットテスト（Issue #23）
"""

import json
from unittest.mock import Mock

import pytest

from src.services.ai_service import AIService, AIServiceError


def _mock_converse_response(text: str) -> dict:
    """Bedrock Converse API のモックレスポンスを構築する。"""
    return {"output": {"message": {"content": [{"text": text}]}}}


@pytest.fixture
def ai_service() -> AIService:
    client = Mock()
    return AIService(bedrock_client=client)


class TestChatForSurvey:
    def test_returns_assistant_message(self, ai_service: AIService) -> None:
        ai_service.bedrock_client.converse.return_value = _mock_converse_response(
            "どのような調査をしたいですか？"
        )
        reply = ai_service.chat_for_survey(
            [{"role": "user", "content": "新商品の購入意向を調べたいです"}]
        )
        assert reply == "どのような調査をしたいですか？"
        # system プロンプトと messages が正しく渡されていること
        _, kwargs = ai_service.bedrock_client.converse.call_args
        assert "system" in kwargs
        assert kwargs["messages"][0]["role"] == "user"

    def test_empty_messages_raises(self, ai_service: AIService) -> None:
        with pytest.raises(AIServiceError):
            ai_service.chat_for_survey([])

    def test_last_not_user_raises(self, ai_service: AIService) -> None:
        with pytest.raises(AIServiceError):
            ai_service.chat_for_survey(
                [{"role": "assistant", "content": "何を聞きますか？"}]
            )

    def test_filters_invalid_roles(self, ai_service: AIService) -> None:
        ai_service.bedrock_client.converse.return_value = _mock_converse_response("ok")
        # system ロールは除外される想定。最後の user がある限り動く
        ai_service.chat_for_survey(
            [
                {"role": "system", "content": "無視される"},
                {"role": "user", "content": "質問内容"},
            ]
        )
        _, kwargs = ai_service.bedrock_client.converse.call_args
        assert len(kwargs["messages"]) == 1
        assert kwargs["messages"][0]["role"] == "user"


class TestGenerateSurveyQuestionsDraft:
    def _draft_json(self) -> str:
        return json.dumps(
            {
                "summary": "購入意向調査の基本設問セット",
                "questions": [
                    {
                        "question_type": "multiple_choice",
                        "text": "普段この商品ジャンルを使いますか？",
                        "options": ["毎日", "時々", "使わない"],
                        "allow_multiple": False,
                        "max_selections": 0,
                    },
                    {
                        "question_type": "scale_rating",
                        "text": "新商品の魅力度",
                        "options": [],
                        "allow_multiple": False,
                        "max_selections": 0,
                    },
                    {
                        "question_type": "free_text",
                        "text": "改善してほしい点があれば教えてください",
                        "options": [],
                        "allow_multiple": False,
                        "max_selections": 0,
                    },
                ],
            },
            ensure_ascii=False,
        )

    def test_returns_parsed_dict(self, ai_service: AIService) -> None:
        ai_service.bedrock_client.converse.return_value = _mock_converse_response(
            self._draft_json()
        )
        result = ai_service.generate_survey_questions_draft(
            [{"role": "user", "content": "20代女性の化粧品購入意向"}]
        )
        assert result["summary"].startswith("購入意向調査")
        assert len(result["questions"]) == 3
        assert result["questions"][0]["question_type"] == "multiple_choice"

    def test_parses_json_with_preamble(self, ai_service: AIService) -> None:
        # モデルが前置きを含めても JSON 抽出できること
        text = "以下がドラフトです:\n" + self._draft_json() + "\n以上です"
        ai_service.bedrock_client.converse.return_value = _mock_converse_response(text)
        result = ai_service.generate_survey_questions_draft(
            [{"role": "user", "content": "調査目的"}]
        )
        assert len(result["questions"]) == 3

    def test_invalid_json_raises(self, ai_service: AIService) -> None:
        ai_service.bedrock_client.converse.return_value = _mock_converse_response(
            "これはJSONではありません"
        )
        with pytest.raises(AIServiceError):
            ai_service.generate_survey_questions_draft(
                [{"role": "user", "content": "調査目的"}]
            )

    def test_missing_questions_key_raises(self, ai_service: AIService) -> None:
        ai_service.bedrock_client.converse.return_value = _mock_converse_response(
            json.dumps({"summary": "概要"})
        )
        with pytest.raises(AIServiceError):
            ai_service.generate_survey_questions_draft(
                [{"role": "user", "content": "調査目的"}]
            )

    def test_empty_questions_raises(self, ai_service: AIService) -> None:
        ai_service.bedrock_client.converse.return_value = _mock_converse_response(
            json.dumps({"summary": "", "questions": []})
        )
        with pytest.raises(AIServiceError):
            ai_service.generate_survey_questions_draft(
                [{"role": "user", "content": "調査目的"}]
            )

    def test_empty_messages_raises(self, ai_service: AIService) -> None:
        with pytest.raises(AIServiceError):
            ai_service.generate_survey_questions_draft([])

    def test_template_name_included(self, ai_service: AIService) -> None:
        payload = json.dumps(
            {
                "template_name": "新商品購入意向調査",
                "summary": "概要",
                "questions": [
                    {
                        "question_type": "free_text",
                        "text": "自由記述",
                        "options": [],
                    }
                ],
            },
            ensure_ascii=False,
        )
        ai_service.bedrock_client.converse.return_value = _mock_converse_response(
            payload
        )
        result = ai_service.generate_survey_questions_draft(
            [{"role": "user", "content": "調査"}]
        )
        assert result["template_name"] == "新商品購入意向調査"

    def test_template_name_truncated(self, ai_service: AIService) -> None:
        long = "あ" * 100
        payload = json.dumps(
            {
                "template_name": long,
                "summary": "",
                "questions": [
                    {"question_type": "free_text", "text": "q", "options": []}
                ],
            },
            ensure_ascii=False,
        )
        ai_service.bedrock_client.converse.return_value = _mock_converse_response(
            payload
        )
        result = ai_service.generate_survey_questions_draft(
            [{"role": "user", "content": "調査"}]
        )
        assert len(result["template_name"]) == 50
