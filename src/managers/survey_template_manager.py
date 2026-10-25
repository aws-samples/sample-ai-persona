"""
SurveyTemplateManager
テンプレートCRUD + AI設問生成を担当するマネージャークラス。

v1.0.0 U7: every entry point takes the signed-in user (``Principal``) and
decides with ``AccessPolicy`` (``functional-design/rules.md``):

- BR1.1: the creator owns a new template; it starts unshared. The body is
  written "id does not exist yet" (no shares yet, so no index items), then
  the images are attached; a failed attach removes the body.
- BR1.2: lists are tabbed (``ResourceSharing``); bodies are read again
  strongly and checked with ``can_read``; a search reads the newest 500 of
  the tab. Members never scan.
- BR1.3〜BR1.5: reading needs ``ensure_readable``, changing needs
  ``ensure_manageable``; an edit writes only the changed columns, so the
  owner, the shares and ``created_at`` stay (an admin's edit included).
- BR1.6: templates without an owner (before v1.0.0) are admin only.
- BR1.7: delete removes the body and its share items in one transaction,
  then the images (three tries, then ``survey.cleanup_failed``).
- BR2.3: AI generation needs ``ai:execute``.
- NFR2.25: a template body is at most 200KB.
"""

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..config import config
from ..models.authz import Permission, Principal, ResourceType
from ..models.errors import CodedError, ErrorCode, LookupUnavailableError
from ..models.sharing import ListTab, TabbedList
from ..models.survey_template import Question, SurveyTemplate, TemplateImage
from ..models.uploaded_file import file_id_from_key, is_uuid
from ..prompts.survey_prompts import (
    SURVEY_CHAT_SYSTEM_PROMPT,
    SURVEY_DRAFT_SYSTEM_PROMPT,
)
from ..services.ai_service import AIService
from ..services.service_factory import service_factory
from ..services.stores.survey_template_store import (
    SurveyTemplateStore,
    SurveyTemplateStoreError,
)
from .components.access_policy import AccessPolicy
from .components.file_attachments import FileAttachments
from .components.owner_directory import OwnerDirectory
from .components.resource_lookup import ResourceLookup
from .components.resource_sharing import SEARCH_LIMIT, ResourceSharing
from .components.sharing_runtime import access_parts_from, sharing_parts_from
from .shared.cleanup import run_cleanup
from .shared.structured_log import log_event

logger = logging.getLogger(__name__)

_TEMPLATE = ResourceType.SURVEY_TEMPLATE
#: Largest template body written (NFR2.25).
MAX_TEMPLATE_BYTES = 200 * 1024
#: Owner label of a template without owner (before v1.0.0).
OWNER_UNKNOWN = "所有者不明"
#: Most rows a search returns.
SEARCH_RESULT_LIMIT = 100


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
        survey_template_store: Optional[SurveyTemplateStore] = None,
        ai_service: Optional[AIService] = None,
        file_attachments: Optional[FileAttachments] = None,
        resource_sharing: Optional[ResourceSharing] = None,
        owners: Optional[OwnerDirectory] = None,
        resource_lookup: Optional[ResourceLookup] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the manager.

        Args:
            survey_template_store: Template bodies (the singleton when omitted).
            ai_service: AI generation (the singleton when omitted).
            file_attachments: The per-task ``FileAttachments`` (U5).
            resource_sharing: The per-task ``ResourceSharing`` (tabs, delete).
            owners: The owner-name copy (display only).
            resource_lookup: The per-task ``ResourceLookup`` (decisions).
            sleep: Wait between cleanup attempts (tests).
        """
        self._templates = (
            survey_template_store or service_factory.get_survey_template_store()
        )
        self.ai_service = ai_service or service_factory.get_ai_service()
        self._attachments = file_attachments
        self._sharing = resource_sharing
        self._owner_directory = owners
        self._lookup = resource_lookup
        self._sleep = sleep

    # --- parts (built lazily) -------------------------------------------------------

    @property
    def _files(self) -> FileAttachments:
        if self._attachments is None:
            self._attachments = access_parts_from(service_factory).files
        return self._attachments

    @property
    def _resource_sharing(self) -> ResourceSharing:
        if self._sharing is None:
            self._sharing = sharing_parts_from(service_factory).sharing
        return self._sharing

    @property
    def _owners(self) -> OwnerDirectory:
        if self._owner_directory is None:
            self._owner_directory = sharing_parts_from(service_factory).owners
        return self._owner_directory

    @property
    def _resource_lookup(self) -> ResourceLookup:
        if self._lookup is None:
            self._lookup = access_parts_from(service_factory).lookup
        return self._lookup

    # =========================================================================
    # 作成（BR1.1、NFR1.83、NFR2.25）
    # =========================================================================

    def create_template(
        self,
        principal: Principal,
        name: str,
        questions: List[Question],
        images: Optional[List[TemplateImage]] = None,
    ) -> SurveyTemplate:
        """Create a template owned by ``principal`` (BR1.1).

        Raises:
            AccessDeniedError: The user lacks ``resource:create``.
            SurveyTemplateValidationError: Invalid name, questions or images.
            CodedError: ``SURVEY_TEMPLATE_TOO_LARGE`` (nothing written), the
                attachment's codes (the body is removed again) or
                ``SURVEY_OPERATION_FAILED``.
        """
        AccessPolicy.require(
            principal, Permission.RESOURCE_CREATE, resource_type=_TEMPLATE
        )
        self._validate_template_name(name)
        self._validate_questions(questions)
        if images:
            self._validate_images(images)
        file_ids = [self._image_file_id(principal, img) for img in images or []]

        template = SurveyTemplate.create_new(
            name=name.strip(), questions=questions, images=images or []
        ).owned_by(principal.sub)
        self._check_size(template)
        try:
            self._templates.put_new(template)
        except SurveyTemplateStoreError as e:
            logger.error("Template save failed: %s", type(e).__name__)
            raise SurveyTemplateManagerError(
                "template save failed", code=ErrorCode.SURVEY_OPERATION_FAILED
            ) from e
        if file_ids:
            try:
                self._files.attach_all(
                    principal, file_ids, "survey_template", template.id
                )
            except CodedError as e:
                self._remove_after_failed_attach(principal, template.id)
                raise SurveyTemplateManagerError(
                    f"images could not be attached ({type(e).__name__})",
                    code=e.code,
                    context=e.context,
                ) from e
        logger.info("Template created: %s", template.id)
        return template

    def _remove_after_failed_attach(
        self, principal: Principal, template_id: str
    ) -> None:
        try:
            self._resource_sharing.delete_with_shares(principal, _TEMPLATE, template_id)
        except CodedError as e:
            log_event(
                "survey.cleanup_failed",
                level="ERROR",
                resource_type=_TEMPLATE.value,
                resource_id=template_id,
                step="rollback",
                error_type=type(e).__name__,
            )

    def _check_size(self, template: SurveyTemplate) -> None:
        if self._templates.item_size(template.to_dict()) > MAX_TEMPLATE_BYTES:
            raise SurveyTemplateManagerError(
                "the template body exceeds 200KB",
                code=ErrorCode.SURVEY_TEMPLATE_TOO_LARGE,
            )

    # =========================================================================
    # 一覧（BR1.2）
    # =========================================================================

    def list_templates(
        self,
        principal: Principal,
        tab: Optional[str],
        cursor: Optional[str],
        search: Optional[str] = None,
    ) -> TabbedList[SurveyTemplate]:
        """One page of a tab, or a search inside the tab (BR1.2).

        Raises:
            CodedError: ``LOOKUP_UNAVAILABLE`` when the index or the bodies
                cannot be read.
        """
        sharing = self._resource_sharing
        active = sharing.effective_tab(principal, ListTab.parse(tab) or ListTab.MINE)
        query = (search or "").strip()
        if query:
            ids, truncated = self._tab_ids(principal, active)
            needle = query.lower()
            matched = [
                t
                for t in self._readable_bodies(principal, ids)
                if needle in t.name.lower()
            ][:SEARCH_RESULT_LIMIT]
            return self._listing(
                principal,
                active,
                matched,
                counts={active.value: None if truncated else len(ids)},
                search_truncated=truncated,
            )
        page = sharing.list_page(principal, _TEMPLATE, active, cursor)
        templates = self._readable_bodies(
            principal, [t.resource_id for t in page.items]
        )
        counts = self._counts(principal, {active: page.total}) if cursor is None else {}
        return self._listing(
            principal, active, templates, next_cursor=page.next_cursor, counts=counts
        )

    def _readable_bodies(
        self, principal: Principal, template_ids: List[str]
    ) -> List[SurveyTemplate]:
        if not template_ids:
            return []
        try:
            found = self._templates.get_many(template_ids)
        except SurveyTemplateStoreError as e:
            raise LookupUnavailableError("template bodies unreadable") from e
        bodies = [found[i] for i in dict.fromkeys(template_ids) if i in found]
        return [
            t
            for t in bodies
            if self._resource_sharing.can_read_body(principal, _TEMPLATE, t.to_dict())
        ]

    def _tab_ids(self, principal: Principal, active: ListTab) -> tuple[List[str], bool]:
        sharing = self._resource_sharing
        if active is not ListTab.ALL:
            return sharing.collect_ids(principal, _TEMPLATE, active)
        ids: List[str] = []
        cursor: Optional[str] = None
        while len(ids) <= SEARCH_LIMIT:
            page = sharing.list_page(principal, _TEMPLATE, ListTab.ALL, cursor)
            ids += [t.resource_id for t in page.items]
            cursor = page.next_cursor
            if not cursor:
                break
        return ids[:SEARCH_LIMIT], len(ids) > SEARCH_LIMIT or bool(cursor)

    def _counts(
        self, principal: Principal, known: Dict[ListTab, Optional[int]]
    ) -> Dict[str, Optional[int]]:
        sharing = self._resource_sharing
        tabs = [ListTab.MINE, ListTab.SHARED]
        if sharing.effective_tab(principal, ListTab.ALL) is ListTab.ALL:
            tabs.append(ListTab.ALL)
        return {
            tab.value: known[tab]
            if known.get(tab) is not None
            else sharing.count(principal, _TEMPLATE, tab)
            for tab in tabs
        }

    def _listing(
        self,
        principal: Principal,
        active: ListTab,
        templates: List[SurveyTemplate],
        *,
        next_cursor: Optional[str] = None,
        counts: Dict[str, Optional[int]],
        search_truncated: bool = False,
    ) -> TabbedList[SurveyTemplate]:
        sharing = self._resource_sharing
        owners: Dict[str, Optional[str]] = {}
        if active is not ListTab.MINE and templates:
            labels = self._owners.labels(
                {t.owner_sub for t in templates if t.owner_sub}
            )
            owners = {
                t.id: None
                if labels is None
                else (
                    labels.get(t.owner_sub, OWNER_UNKNOWN)
                    if t.owner_sub
                    else OWNER_UNKNOWN
                )
                for t in templates
            }
        shared_with = {
            t.id: sharing.department_names(
                sorted(
                    t.shared_group_ids & principal.group_ids
                    if active is ListTab.SHARED
                    else t.shared_group_ids
                )
            )
            for t in templates
        }
        return TabbedList(
            tab=active,
            items=templates,
            next_cursor=next_cursor,
            counts=counts,
            show_all=sharing.effective_tab(principal, ListTab.ALL) is ListTab.ALL,
            owners=owners,
            shared_with=shared_with,
            manageable=frozenset(
                t.id
                for t in templates
                if sharing.can_manage_body(principal, _TEMPLATE, t.to_dict())
            ),
            no_department=not principal.group_ids,
            search_truncated=search_truncated,
        )

    # =========================================================================
    # 読む・変える・消す（BR1.3〜BR1.7）
    # =========================================================================

    def get_template(self, principal: Principal, template_id: str) -> SurveyTemplate:
        """A template the user may read (BR1.3).

        Raises:
            CodedError: ``SURVEY_TEMPLATE_NOT_FOUND`` (unreadable or missing).
        """
        target = self._resource_lookup.get(_TEMPLATE, template_id)
        AccessPolicy.ensure_readable(
            principal, target, ErrorCode.SURVEY_TEMPLATE_NOT_FOUND
        )
        return self._body(template_id)

    def get_template_for_edit(
        self, principal: Principal, template_id: str
    ) -> SurveyTemplate:
        """A template the user may change (the edit screen, BR1.4).

        Raises:
            CodedError: ``SURVEY_TEMPLATE_NOT_FOUND``.
            AccessDeniedError: Readable but not manageable.
        """
        self._ensure_manageable(principal, template_id)
        return self._body(template_id)

    def _ensure_manageable(self, principal: Principal, template_id: str) -> None:
        target = self._resource_lookup.get(_TEMPLATE, template_id)
        AccessPolicy.ensure_manageable(
            principal, target, ErrorCode.SURVEY_TEMPLATE_NOT_FOUND
        )

    def _body(self, template_id: str) -> SurveyTemplate:
        try:
            template = self._templates.get(template_id, consistent=True)
        except SurveyTemplateStoreError as e:
            raise LookupUnavailableError("template body unreadable") from e
        if template is None:
            raise SurveyTemplateManagerError(
                "template is gone", code=ErrorCode.SURVEY_TEMPLATE_NOT_FOUND
            )
        return template

    def update_template(
        self,
        principal: Principal,
        template_id: str,
        name: str,
        questions: List[Question],
        images: Optional[List[TemplateImage]] = None,
    ) -> SurveyTemplate:
        """Change the name, questions and images only (BR1.4, BR1.5).

        New images are attached; images taken off are deleted (results
        stop showing them as well, Q5 A).

        Raises:
            CodedError: ``SURVEY_TEMPLATE_NOT_FOUND``, validation codes,
                ``SURVEY_TEMPLATE_TOO_LARGE``, attachment codes or
                ``SURVEY_OPERATION_FAILED``.
            AccessDeniedError: Readable but not manageable.
        """
        self._validate_template_name(name)
        self._validate_questions(questions)
        if images:
            self._validate_images(images)
        self._ensure_manageable(principal, template_id)
        existing = self._body(template_id)
        updated = existing.edited(
            name=name.strip(), questions=questions, images=images or []
        )
        self._check_size(updated)
        kept = {img.file_path for img in images or []}
        before = {img.file_path for img in existing.images or []}
        added = [img for img in images or [] if img.file_path not in before]
        removed = [img for img in existing.images or [] if img.file_path not in kept]
        file_ids = [self._image_file_id(principal, img) for img in added]
        attached = None
        if file_ids:
            try:
                attached = self._files.attach_all(
                    principal, file_ids, "survey_template", template_id
                )
            except CodedError as e:
                raise SurveyTemplateManagerError(
                    f"images could not be attached ({type(e).__name__})",
                    code=e.code,
                    context=e.context,
                ) from e
        try:
            written = self._templates.update_content(updated)
        except SurveyTemplateStoreError as e:
            if attached is not None:
                self._files.detach(attached)
            raise SurveyTemplateManagerError(
                "template update failed", code=ErrorCode.SURVEY_OPERATION_FAILED
            ) from e
        if not written:
            if attached is not None:
                self._files.detach(attached)
            raise SurveyTemplateManagerError(
                "template is gone", code=ErrorCode.SURVEY_TEMPLATE_NOT_FOUND
            )
        removed_ids = [i for i in (_stored_file_id(img) for img in removed) if i]
        if removed_ids:
            self._cleanup(
                template_id,
                "images",
                lambda: self._delete_images(template_id, removed_ids),
            )
        logger.info("Template updated: %s", template_id)
        return updated

    def delete_template(self, principal: Principal, template_id: str) -> bool:
        """Delete the body and its shares, then its images (BR1.7).

        Results made from the template stay (they carry a snapshot, BR2.7).

        Returns:
            ``True`` once the body is gone (cleanup failures are logged).

        Raises:
            CodedError: ``SURVEY_TEMPLATE_NOT_FOUND`` / ``SURVEY_OPERATION_FAILED``.
            AccessDeniedError: Readable but not manageable.
        """
        deleted = self._resource_sharing.delete_with_shares(
            principal, _TEMPLATE, template_id
        )
        if deleted is None:
            raise SurveyTemplateManagerError(
                "nothing to delete", code=ErrorCode.SURVEY_TEMPLATE_NOT_FOUND
            )
        images = [TemplateImage.from_dict(i) for i in deleted.get("images") or []]
        file_ids = [i for i in (_stored_file_id(img) for img in images) if i]
        if file_ids:
            self._cleanup(
                template_id,
                "images",
                lambda: self._delete_images(template_id, file_ids),
            )
        logger.info("Template deleted: %s", template_id)
        return True

    def _delete_images(self, template_id: str, file_ids: List[str]) -> None:
        left = self._files.delete_attached("survey_template", template_id, file_ids)
        if left:
            raise SurveyTemplateManagerError(f"{len(left)} images left")

    def _cleanup(
        self, template_id: str, step: str, action: Callable[[], object]
    ) -> None:
        run_cleanup(
            action,
            resource_type=_TEMPLATE.value,
            resource_id=template_id,
            step=step,
            sleep=self._sleep,
        )

    # =========================================================================
    # 画像（security-design.md §4）
    # =========================================================================

    @staticmethod
    def _image_file_id(principal: Principal, image: TemplateImage) -> str:
        """The upload ID inside an image path of the user's own upload.

        S3 paths must be ``s3://{bucket}/uploads/{the user}/survey_image/{id}_{name}``
        (the bucket checked when configured); local development paths end in
        ``{id}_{name}``. Anything else is ``FILE_NOT_FOUND``.
        """
        path = image.file_path or ""
        file_id: Optional[str] = None
        if path.startswith("s3://"):
            bucket, _, key = path[len("s3://") :].partition("/")
            parts = key.split("/")
            if (
                (not config.S3_BUCKET_NAME or bucket == config.S3_BUCKET_NAME)
                and len(parts) == 4
                and parts[2] == "survey_image"
                and parts[1] == principal.sub
            ):
                file_id = file_id_from_key(key)
        elif path:
            candidate, sep, _ = Path(path).name.partition("_")
            file_id = candidate if sep and is_uuid(candidate) else None
        if not file_id:
            raise SurveyTemplateValidationError(
                "image path is not an upload of this user",
                code=ErrorCode.FILE_NOT_FOUND,
            )
        return file_id

    # =========================================================================
    # AI設問生成（BR2.3）
    # =========================================================================

    def generate_ai_chat_response(
        self, principal: Principal, messages: List[Dict[str, str]]
    ) -> str:
        """AIチャットヒアリングの1ターンを処理してassistant発言を返す。"""
        AccessPolicy.require(principal, Permission.AI_EXECUTE, resource_type=_TEMPLATE)
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
        self, principal: Principal, messages: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        """会話履歴からアンケート設問ドラフトを生成して返す。"""
        AccessPolicy.require(principal, Permission.AI_EXECUTE, resource_type=_TEMPLATE)
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


def _stored_file_id(image: TemplateImage) -> Optional[str]:
    """The upload ID of an image already on a template (any uploader).

    Used for images taken off or deleted with the template, which may have
    been uploaded by someone else (the owner when an admin edits).
    """
    path = image.file_path or ""
    if path.startswith("s3://"):
        _, _, key = path[len("s3://") :].partition("/")
        return file_id_from_key(key)
    candidate, sep, _ = Path(path).name.partition("_")
    return candidate if sep and is_uuid(candidate) else None
