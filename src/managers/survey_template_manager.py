"""
SurveyTemplateManager
テンプレートCRUD + AI設問生成を担当するマネージャークラス。
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..models.errors import CodedError, ErrorCode
from ..models.survey_template import Question, SurveyTemplate, TemplateImage
from ..prompts.survey_prompts import (
    SURVEY_CHAT_SYSTEM_PROMPT,
    SURVEY_DRAFT_SYSTEM_PROMPT,
)
from ..services.ai_service import AIService
from ..services.database_service import DatabaseService
from ..services.service_factory import service_factory

logger = logging.getLogger(__name__)


class SurveyTemplateManagerError(CodedError):
    """SurveyTemplateManager層の基底例外"""

    pass


class SurveyTemplateValidationError(SurveyTemplateManagerError):
    """バリデーションエラー"""

    pass


class SurveyTemplateManager:
    """テンプレートCRUD + AI設問生成"""

    _MAX_AI_MESSAGES = 40
    _MAX_AI_MESSAGE_LENGTH = 2000

    def __init__(
        self,
        database_service: Optional[DatabaseService] = None,
        ai_service: Optional[AIService] = None,
    ) -> None:
        self.db = database_service or service_factory.get_database_service()
        self.ai_service = ai_service or service_factory.get_ai_service()

    # =========================================================================
    # テンプレートCRUD
    # =========================================================================

    def create_template(
        self,
        name: str,
        questions: List[Question],
        images: Optional[List[TemplateImage]] = None,
    ) -> SurveyTemplate:
        """アンケートテンプレートを作成して保存する。"""
        self._validate_template_name(name)
        self._validate_questions(questions)
        if images:
            self._validate_images(images)

        template = SurveyTemplate.create_new(
            name=name.strip(), questions=questions, images=images or []
        )
        self.db.save_survey_template(template)
        logger.info(f"Template created: {template.id} ({template.name})")
        return template

    def get_template(self, template_id: str) -> Optional[SurveyTemplate]:
        """テンプレートをIDで取得する。"""
        return self.db.get_survey_template(template_id)

    def get_all_templates(self) -> List[SurveyTemplate]:
        """全テンプレートを取得する。"""
        return self.db.get_all_survey_templates()

    def update_template(
        self,
        template_id: str,
        name: str,
        questions: List[Question],
        images: Optional[List[TemplateImage]] = None,
    ) -> SurveyTemplate:
        """既存テンプレートを更新する。"""
        self._validate_template_name(name)
        self._validate_questions(questions)
        if images:
            self._validate_images(images)

        existing = self.db.get_survey_template(template_id)
        if existing is None:
            raise SurveyTemplateManagerError(
                f"survey template {template_id!r} not found",
                code=ErrorCode.SURVEY_TEMPLATE_NOT_FOUND,
            )

        updated = SurveyTemplate(
            id=existing.id,
            name=name.strip(),
            questions=questions,
            created_at=existing.created_at,
            updated_at=datetime.now(),
            images=images or [],
        )
        self.db.update_survey_template(updated)
        logger.info(f"Template updated: {updated.id} ({updated.name})")
        return updated

    def delete_template(self, template_id: str) -> bool:
        """テンプレートを削除する。"""
        result = self.db.delete_survey_template(template_id)
        if result:
            logger.info(f"Template deleted: {template_id}")
        return result

    # =========================================================================
    # AI設問生成
    # =========================================================================

    def generate_ai_chat_response(self, messages: List[Dict[str, str]]) -> str:
        """AIチャットヒアリングの1ターンを処理してassistant発言を返す。"""
        if self.ai_service is None:
            raise SurveyTemplateManagerError(
                "ai service is not configured",
                code=ErrorCode.SURVEY_AI_UNAVAILABLE,
            )
        self._validate_ai_messages(messages)
        if messages[-1].get("role") != "user":
            raise SurveyTemplateValidationError(
                "last message role is not 'user'",
                code=ErrorCode.SURVEY_AI_CONVERSATION_INVALID,
            )
        return self.ai_service.chat_for_survey(
            messages, system_prompt=SURVEY_CHAT_SYSTEM_PROMPT
        )

    def generate_ai_questions_draft(
        self, messages: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        """会話履歴からアンケート設問ドラフトを生成して返す。"""
        if self.ai_service is None:
            raise SurveyTemplateManagerError(
                "ai service is not configured",
                code=ErrorCode.SURVEY_AI_UNAVAILABLE,
            )
        self._validate_ai_messages(messages)

        raw = self.ai_service.generate_survey_questions_draft(
            messages, system_prompt=SURVEY_DRAFT_SYSTEM_PROMPT
        )
        questions = [self._build_question_from_ai(q) for q in raw.get("questions", [])]
        if not questions:
            raise SurveyTemplateManagerError(
                "ai returned no valid questions",
                code=ErrorCode.SURVEY_AI_NO_QUESTIONS,
            )
        self._validate_questions(questions)
        return {
            "summary": raw.get("summary", ""),
            "template_name": raw.get("template_name", ""),
            "questions": [q.to_dict() for q in questions],
        }

    # =========================================================================
    # バリデーション
    # =========================================================================

    @staticmethod
    def _validate_template_name(name: str) -> None:
        """テンプレート名のバリデーション。"""
        if not name or not name.strip():
            raise SurveyTemplateValidationError(
                "template name is blank",
                code=ErrorCode.SURVEY_TEMPLATE_NAME_BLANK,
            )

    @staticmethod
    def _validate_questions(questions: List[Question]) -> None:
        """質問リストのバリデーション。"""
        if not questions:
            raise SurveyTemplateValidationError(
                "question list is empty",
                code=ErrorCode.SURVEY_TEMPLATE_NO_QUESTIONS,
            )
        for q in questions:
            if q.question_type == "multiple_choice" and len(q.options) < 2:
                raise SurveyTemplateValidationError(
                    f"multiple choice question has {len(q.options)} options",
                    code=ErrorCode.SURVEY_TEMPLATE_TOO_FEW_OPTIONS,
                    context={"question_text": q.text},
                )

    @staticmethod
    def _validate_images(images: List[TemplateImage]) -> None:
        """画像リストのバリデーション。"""
        if len(images) > 1:
            raise SurveyTemplateValidationError(
                f"{len(images)} images attached, max is 1",
                code=ErrorCode.SURVEY_TEMPLATE_TOO_MANY_IMAGES,
            )
        for img in images:
            if not img.name or not img.name.strip():
                raise SurveyTemplateValidationError(
                    "image name is blank",
                    code=ErrorCode.SURVEY_TEMPLATE_IMAGE_NAME_MISSING,
                )

    def _validate_ai_messages(self, messages: List[Dict[str, str]]) -> None:
        if not isinstance(messages, list) or not messages:
            raise SurveyTemplateValidationError(
                "conversation history is empty",
                code=ErrorCode.SURVEY_AI_CONVERSATION_INVALID,
            )
        if len(messages) > self._MAX_AI_MESSAGES:
            raise SurveyTemplateValidationError(
                f"conversation has {len(messages)} messages, "
                f"max is {self._MAX_AI_MESSAGES}",
                code=ErrorCode.SURVEY_AI_CONVERSATION_TOO_LONG,
                context={"max_messages": self._MAX_AI_MESSAGES},
            )
        for m in messages:
            if not isinstance(m, dict):
                raise SurveyTemplateValidationError(
                    f"message is {type(m).__name__}, expected dict",
                    code=ErrorCode.SURVEY_AI_CONVERSATION_INVALID,
                )
            if m.get("role") not in ("user", "assistant"):
                raise SurveyTemplateValidationError(
                    "message role is neither 'user' nor 'assistant'",
                    code=ErrorCode.SURVEY_AI_CONVERSATION_INVALID,
                )
            content = m.get("content")
            if not isinstance(content, str) or not content.strip():
                raise SurveyTemplateValidationError(
                    "message content is empty",
                    code=ErrorCode.SURVEY_AI_CONVERSATION_INVALID,
                )
            if len(content) > self._MAX_AI_MESSAGE_LENGTH:
                raise SurveyTemplateValidationError(
                    f"message length {len(content)} exceeds "
                    f"{self._MAX_AI_MESSAGE_LENGTH}",
                    code=ErrorCode.SURVEY_AI_MESSAGE_TOO_LONG,
                    context={"max_length": self._MAX_AI_MESSAGE_LENGTH},
                )

    @staticmethod
    def _build_question_from_ai(data: Dict[str, Any]) -> Question:
        """AI生成のJSON 1件を Question に変換する。不正値は安全側に丸める。"""
        qtype = str(data.get("question_type", "")).strip()
        text = str(data.get("text", "")).strip()
        if not text:
            raise SurveyTemplateValidationError(
                "ai generated question has no text",
                code=ErrorCode.SURVEY_AI_NO_QUESTIONS,
            )
        if qtype == "multiple_choice":
            options = [
                str(o).strip() for o in data.get("options", []) if str(o).strip()
            ]
            allow_multiple = bool(data.get("allow_multiple", False))
            try:
                max_selections = int(data.get("max_selections", 0) or 0)
            except (TypeError, ValueError):
                max_selections = 0
            if max_selections < 0 or max_selections > len(options):
                max_selections = 0
            return Question.create_multiple_choice(
                text=text,
                options=options,
                allow_multiple=allow_multiple,
                max_selections=max_selections if allow_multiple else 0,
            )
        if qtype == "free_text":
            return Question.create_free_text(text=text)
        if qtype == "scale_rating":
            return Question.create_scale_rating(text=text)
        raise SurveyTemplateValidationError(
            f"ai generated question type {qtype!r} is invalid",
            code=ErrorCode.SURVEY_AI_NO_QUESTIONS,
        )
