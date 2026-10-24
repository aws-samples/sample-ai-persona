"""
Discussion Manager for AI Persona System.

Runs classic discussions and owns the discussion lists, detail, export,
delete and insight regeneration (v1.0.0 U6).

Every entry point takes the signed-in user (``Principal``) and decides with
``AccessPolicy``; bodies, messages and reports are saved and read through the
shared ``DiscussionRecords`` component, the participants through
``ParticipantReader`` (``.claude/rules/architecture.md``: Managers never import
each other).

Rules (``functional-design/rules.md``):

- BR2.8: running needs ``ai:execute``; the participants were read with the
  decision (``PersonaManager.get_personas_for_use``) and are checked again right
  before the save (BR7.1). An unreadable participant stops the run without a
  save (``PERSONA_NOT_FOUND``, the streaming run sends ``stopped``).
- BR2.1〜BR2.3: lists are tabbed (``ResourceSharing``) and re-read strongly;
  search reads the newest 500 of the tab.
- BR2.4 / BR2.5: detail and export need read; delete and regeneration need
  manage (regeneration also needs ``ai:execute``).
- BR2.6: at most 100 insights.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from ..config import config
from ..models.authz import Permission, Principal, ResourceType
from ..models.discussion import Discussion, DiscussionView, DocumentRef
from ..models.errors import CodedError, ErrorCode
from ..models.insight import Insight
from ..models.insight_category import InsightCategory
from ..models.message import Message
from ..models.persona import Persona
from ..models.sharing import ListTab, TabbedList
from ..services.ai_service import AIService, AIServiceError
from ..services.service_factory import service_factory
from .components import discussion_validation
from .components.access_policy import AccessPolicy
from .components.discussion_records import DiscussionRecords
from .components.file_attachments import FileAttachments
from .components.owner_directory import OwnerDirectory
from .components.participant_reader import ParticipantReader
from .components.resource_sharing import PAGE_SIZE, SEARCH_LIMIT, ResourceSharing
from .components.sharing_runtime import (
    DiscussionParts,
    SharingParts,
    access_parts_from,
    discussion_parts_from,
    sharing_parts_from,
)

_DISCUSSION = ResourceType.DISCUSSION
#: Owner label of a discussion without owner (before v1.0.0).
OWNER_UNKNOWN = "所有者不明"
#: Most rows a search returns.
SEARCH_RESULT_LIMIT = 100

#: 種類で絞るときに 1 回の要求で読むページの上限（1 ページ 20 件）
MODE_FILTER_MAX_PAGES = 10
#: SSE event when a participant became unreadable (M12, BR7.5).
STOPPED_EVENT = f"data: {json.dumps({'type': 'stopped'}, ensure_ascii=False)}\n\n"


class DiscussionManagerError(CodedError):
    """Custom exception for discussion manager related errors."""

    pass


def _sse(data: Dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def discussion_parts() -> DiscussionParts:
    """The per-task ``DiscussionRecords`` / ``ParticipantReader`` (U6)."""
    return discussion_parts_from(service_factory)


class DiscussionManager:
    """Classic discussions and the discussion lists / detail / delete (U6)."""

    def __init__(
        self,
        ai_service: AIService | None = None,
        file_attachments: Optional[FileAttachments] = None,
        discussion_records: Optional[DiscussionRecords] = None,
        participants: Optional[ParticipantReader] = None,
        resource_sharing: Optional[ResourceSharing] = None,
        owners: Optional[OwnerDirectory] = None,
        s3_service: Any = None,
    ):
        """
        Initialize discussion manager.

        Args:
            ai_service: AI service (the singleton when omitted)
            file_attachments: The per-task ``FileAttachments`` (U5)
            discussion_records: The per-task ``DiscussionRecords`` (U6)
            participants: The per-task ``ParticipantReader`` (U6)
            resource_sharing: The per-task ``ResourceSharing`` (U6 lists)
            owners: The owner-name copy (display only)
            s3_service: Signs document URLs (``None`` without S3)
        """
        self.logger = logging.getLogger(__name__)
        self.ai_service = ai_service or service_factory.get_ai_service()
        self._attachments = file_attachments
        self._records = discussion_records
        self._reader = participants
        self._sharing = resource_sharing
        self._owner_directory = owners
        self._s3 = s3_service

    # --- parts (built lazily) -------------------------------------------------------

    @property
    def _file_attachments(self) -> FileAttachments:
        if self._attachments is None:
            self._attachments = access_parts_from(service_factory).files
        return self._attachments

    @property
    def _discussion_records(self) -> DiscussionRecords:
        if self._records is None:
            self._records = discussion_parts().records
        return self._records

    @property
    def _participant_reader(self) -> ParticipantReader:
        if self._reader is None:
            self._reader = discussion_parts().participants
        return self._reader

    @property
    def _resource_sharing(self) -> ResourceSharing:
        if self._sharing is None:
            self._sharing = self._sharing_parts().sharing
        return self._sharing

    @property
    def _owners(self) -> OwnerDirectory:
        if self._owner_directory is None:
            self._owner_directory = self._sharing_parts().owners
        return self._owner_directory

    @staticmethod
    def _sharing_parts() -> SharingParts:
        return sharing_parts_from(service_factory)

    @property
    def _s3_service(self) -> Any:
        if self._s3 is None:
            self._s3 = service_factory.get_optional_s3_service()
        return self._s3

    # =========================================================================
    # 実行（BR2.8、BR7.1）
    # =========================================================================

    def run_classic_discussion_streaming(
        self,
        principal: Principal,
        personas: List[Persona],
        topic: str,
        categories: Optional[List[InsightCategory]] = None,
        document_ids: Optional[List[str]] = None,
    ) -> Generator[str, None, None]:
        """Run a classic discussion and stream it as SSE lines.

        ``personas`` were read with the decision; they are checked again
        right before the save. An unreadable participant ends the stream with
        a ``stopped`` event and nothing is saved (BR7.1, M12).

        Yields:
            str: ``data: {...}`` lines (``message``, ``complete`` / ``stopped``).

        Raises:
            AccessDeniedError: The user lacks ``ai:execute``.
            DiscussionManagerError: Validation, AI or save failures.
        """
        self._require_ai(principal)
        self._validate_discussion_input(personas, topic)
        documents_data, documents = self._load_documents(document_ids, principal)

        messages: List[Message] = []
        for message in self.ai_service.facilitate_discussion_streaming(
            personas, topic, documents=documents_data
        ):
            messages.append(message)
            yield _sse(
                {
                    "type": "message",
                    "persona_id": message.persona_id,
                    "persona_name": message.persona_name,
                    "content": message.content,
                }
            )

        if not self._participant_reader.can_read_all(
            principal, [p.id for p in personas]
        ):
            self.logger.info("Classic discussion stopped: a participant is gone")
            yield STOPPED_EVENT
            return

        discussion = self._save(
            principal, personas, topic, documents, messages, categories
        )
        yield _sse(
            {
                "type": "complete",
                "discussion_id": discussion.id,
                "message_count": len(messages),
                "insight_count": len(discussion.insights),
            }
        )

    def run_classic_discussion(
        self,
        principal: Principal,
        personas: List[Persona],
        topic: str,
        categories: Optional[List[InsightCategory]] = None,
        document_ids: Optional[List[str]] = None,
    ) -> Discussion:
        """Run, add insights and save a classic discussion (BR2.8, BR7.1).

        Raises:
            AccessDeniedError: The user lacks ``ai:execute``.
            CodedError: ``PERSONA_NOT_FOUND`` when a participant became
                unreadable before the save (nothing saved).
            DiscussionManagerError: Validation, AI or save failures.
        """
        self._require_ai(principal)
        self._validate_discussion_input(personas, topic)
        documents_data, documents = self._load_documents(document_ids, principal)
        messages = self._facilitate(personas, topic, documents_data)
        self._participant_reader.ensure_all_readable(
            principal, [p.id for p in personas]
        )
        return self._save(principal, personas, topic, documents, messages, categories)

    def _facilitate(
        self,
        personas: List[Persona],
        topic: str,
        documents_data: Optional[List[Dict[str, Any]]],
    ) -> List[Message]:
        self.logger.info(f"Starting classic discussion with {len(personas)} personas")
        try:
            messages = list(
                self.ai_service.facilitate_discussion(
                    personas, topic.strip(), documents=documents_data
                )
            )
        except AIServiceError as e:
            self.logger.error("AI service error during discussion", exc_info=True)
            raise DiscussionManagerError(
                f"discussion failed ({type(e).__name__})",
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            ) from e
        discussion_validation.validate_results(
            messages,
            personas,
            error_cls=DiscussionManagerError,
            require_min_content=True,
            count_statements_only=False,
        )
        return messages

    def _save(
        self,
        principal: Principal,
        personas: List[Persona],
        topic: str,
        documents: Optional[List[DocumentRef]],
        messages: List[Message],
        categories: Optional[List[InsightCategory]],
    ) -> Discussion:
        discussion = Discussion.create_new(
            topic=topic.strip(),
            participants=[p.id for p in personas],
            documents=documents,
            participant_names={p.id: p.name for p in personas},
        )
        discussion = self._attach_insights(discussion, messages, categories)
        discussion_validation.validate_for_save(
            discussion,
            error_cls=DiscussionManagerError,
            code=ErrorCode.DISCUSSION_INVALID,
            messages=messages,
        )
        return self._discussion_records.save_new(principal, discussion, messages)

    @staticmethod
    def _require_ai(principal: Principal) -> None:
        AccessPolicy.require(
            principal, Permission.AI_EXECUTE, resource_type=_DISCUSSION
        )

    # =========================================================================
    # 一覧（BR2.1〜BR2.3）
    # =========================================================================

    def list_discussions(
        self,
        principal: Principal,
        tab: Optional[str],
        cursor: Optional[str],
        mode: Optional[str] = None,
        search: Optional[str] = None,
    ) -> TabbedList[Discussion]:
        """One page of a tab, or a search inside the tab (BR2.1, BR2.2).

        ``all`` without ``resource:read_all`` is ``mine``; rows are read again
        strongly and checked with ``can_read`` (NFR1.43).

        Raises:
            CodedError: ``LOOKUP_UNAVAILABLE`` when the index or bodies cannot be read.
        """
        sharing = self._resource_sharing
        active = sharing.effective_tab(principal, ListTab.parse(tab) or ListTab.MINE)
        mode = mode if mode in ("classic", "agent", "interview") else None
        query = (search or "").strip()
        if query:
            return self._search(principal, active, query, mode)
        page = sharing.list_page(principal, _DISCUSSION, active, cursor)
        total = page.total
        discussions = self._discussion_records.list_bodies(
            principal, [t.resource_id for t in page.items], mode=mode
        )
        next_cursor = page.next_cursor
        # 種類で絞ると 1 ページが空になりうるため、1 ページぶん集まるまで続きを読む。
        # 上限に達したら、空でもカーソルを返して「もっと見る」で続きを読めるようにする
        pages = 1
        while (
            mode is not None
            and next_cursor
            and len(discussions) < PAGE_SIZE
            and pages < MODE_FILTER_MAX_PAGES
        ):
            page = sharing.list_page(principal, _DISCUSSION, active, next_cursor)
            discussions += self._discussion_records.list_bodies(
                principal, [t.resource_id for t in page.items], mode=mode
            )
            next_cursor = page.next_cursor
            pages += 1
        counts = self._counts(principal, {active: total}) if cursor is None else {}
        return self._listing(
            principal, active, discussions, next_cursor=next_cursor, counts=counts
        )

    def _search(
        self,
        principal: Principal,
        active: ListTab,
        query: str,
        mode: Optional[str],
    ) -> TabbedList[Discussion]:
        ids, truncated = self._tab_ids(principal, active)
        found = self._discussion_records.list_bodies(principal, ids, mode=mode)
        needle = query.lower()
        matched = [d for d in found if needle in d.topic.lower()][:SEARCH_RESULT_LIMIT]
        return self._listing(
            principal,
            active,
            matched,
            counts={active.value: None if truncated else len(ids)},
            search_truncated=truncated,
        )

    def _tab_ids(self, principal: Principal, active: ListTab) -> tuple[List[str], bool]:
        sharing = self._resource_sharing
        if active is not ListTab.ALL:
            return sharing.collect_ids(principal, _DISCUSSION, active)
        ids: List[str] = []
        cursor: Optional[str] = None
        while len(ids) <= SEARCH_LIMIT:
            page = sharing.list_page(principal, _DISCUSSION, ListTab.ALL, cursor)
            ids += [t.resource_id for t in page.items]
            cursor = page.next_cursor
            if not cursor:
                break
        return ids[:SEARCH_LIMIT], len(ids) > SEARCH_LIMIT or bool(cursor)

    def list_accessible_discussions(self, principal: Principal) -> List[Discussion]:
        """The user's own and shared discussions; admins get all (``/api/discussions``)."""
        sharing = self._resource_sharing
        if sharing.effective_tab(principal, ListTab.ALL) is ListTab.ALL:
            ids, _ = self._tab_ids(principal, ListTab.ALL)
        else:
            ids = []
            for tab in (ListTab.MINE, ListTab.SHARED):
                tab_ids, _ = sharing.collect_ids(principal, _DISCUSSION, tab)
                ids += tab_ids
        return self._discussion_records.list_bodies(principal, list(dict.fromkeys(ids)))

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
            else sharing.count(principal, _DISCUSSION, tab)
            for tab in tabs
        }

    def _listing(
        self,
        principal: Principal,
        active: ListTab,
        discussions: List[Discussion],
        *,
        next_cursor: Optional[str] = None,
        counts: Dict[str, Optional[int]],
        search_truncated: bool = False,
    ) -> TabbedList[Discussion]:
        sharing = self._resource_sharing
        owners: Dict[str, Optional[str]] = {}
        if active is not ListTab.MINE and discussions:
            labels = self._owners.labels(
                {d.owner_sub for d in discussions if d.owner_sub}
            )
            owners = {
                d.id: None
                if labels is None
                else (
                    labels.get(d.owner_sub, OWNER_UNKNOWN)
                    if d.owner_sub
                    else OWNER_UNKNOWN
                )
                for d in discussions
            }
        shared_with = {
            d.id: sharing.department_names(
                sorted(
                    d.shared_group_ids & principal.group_ids
                    if active is ListTab.SHARED
                    else d.shared_group_ids
                )
            )
            for d in discussions
        }
        return TabbedList(
            tab=active,
            items=discussions,
            next_cursor=next_cursor,
            counts=counts,
            show_all=sharing.effective_tab(principal, ListTab.ALL) is ListTab.ALL,
            owners=owners,
            shared_with=shared_with,
            manageable=frozenset(
                d.id
                for d in discussions
                if not d.is_legacy
                and sharing.can_manage_body(principal, _DISCUSSION, _list_body(d))
            ),
            no_department=not principal.group_ids,
            search_truncated=search_truncated,
        )

    # =========================================================================
    # 詳細・エクスポート・資料・削除（BR2.4、BR2.5、BR5.1、BR9.1）
    # =========================================================================

    def get_discussion(
        self, principal: Principal, discussion_id: str
    ) -> DiscussionView:
        """The discussion for a reader (BR2.4).

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND`` (unreadable or missing).
        """
        return self._discussion_records.get(principal, discussion_id)

    def export_discussion(
        self, principal: Principal, discussion_id: str
    ) -> tuple[str, str]:
        """``(filename, markdown)`` of a discussion the user may read (BR9.1)."""
        return self._discussion_records.export_markdown(principal, discussion_id)

    def get_document_urls(
        self, principal: Principal, view: DiscussionView
    ) -> Dict[str, str]:
        """Signed URLs of the documents the user may be served (BR5.1, NFR1.53).

        Every document is read through ``FileAttachments.readable_file`` (the
        parent discussion's readers); the stored metadata never yields a URL.
        """
        urls: Dict[str, str] = {}
        if not view.discussion.documents or self._s3_service is None:
            return urls
        for doc in view.discussion.documents:
            try:
                uploaded = self._file_attachments.readable_file(principal, doc.id)
                if uploaded.file_path.startswith("s3://"):
                    urls[doc.id] = self._s3_service.generate_presigned_url(
                        uploaded.file_path
                    )
            except CodedError:
                continue
            except Exception:
                self.logger.warning("Signing a document URL failed", exc_info=True)
        return urls

    def delete_discussion(self, principal: Principal, discussion_id: str) -> bool:
        """Delete a discussion the user manages, then clean up (BR1.5, BR2.5).

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND`` / ``DISCUSSION_OPERATION_FAILED``.
            AccessDeniedError: Readable but not manageable.
        """
        return self._discussion_records.delete(principal, discussion_id)

    # =========================================================================
    # インサイト（BR1.4、BR2.5、BR2.6）
    # =========================================================================

    def regenerate_insights(
        self,
        principal: Principal,
        discussion_id: str,
        categories: Optional[List[InsightCategory]] = None,
    ) -> List[Insight]:
        """Generate the insights again and save only them (BR1.4, BR2.5).

        Raises:
            AccessDeniedError: No ``ai:execute``, or readable but not manageable.
            CodedError: ``DISCUSSION_NOT_FOUND``.
            DiscussionManagerError: ``DISCUSSION_INSIGHT_GENERATION_FAILED``.
        """
        self._require_ai(principal)
        view = self._discussion_records.get_manageable(principal, discussion_id)
        insights = self._generate_insights(view, categories)
        updated = self._discussion_records.update_insights(
            principal, discussion_id, insights
        )
        return updated.insights

    def check_insights_allowed(self, principal: Principal, discussion_id: str) -> None:
        """Refuse an insight job before it is queued (``POST /api/discussions/{id}/insights``).

        The job decides again when it runs (:meth:`regenerate_insights`); this
        answers 403 / 404 to the caller at once (C12).

        Raises:
            AccessDeniedError: No ``ai:execute``, or readable but not manageable.
            CodedError: ``DISCUSSION_NOT_FOUND``, or ``DISCUSSION_OPERATION_FAILED``
                for a discussion before v1.0.0 (read-only; the job would fail).
        """
        self._require_ai(principal)
        # 読めなければ 404、読めるが管理できなければ 403、v1.0.0 より前の議論は断る
        self._discussion_records.ensure_manageable(principal, discussion_id)

    def _generate_insights(
        self, view: DiscussionView, categories: Optional[List[InsightCategory]]
    ) -> List[Insight]:
        from .shared.insight_utils import generate_insights

        if len(view.messages) < 2:
            raise DiscussionManagerError(
                f"discussion has {len(view.messages)} messages, minimum is 2",
                code=ErrorCode.DISCUSSION_RESULT_INVALID,
            )
        try:
            insights = generate_insights(
                view.messages, view.discussion.topic, categories, self.ai_service
            )
        except Exception as e:
            self.logger.error("Insight generation failed", exc_info=True)
            raise DiscussionManagerError(
                f"insight generation failed ({type(e).__name__})",
                code=ErrorCode.DISCUSSION_INSIGHT_GENERATION_FAILED,
            ) from e
        if not insights:
            raise DiscussionManagerError(
                "no insights were generated",
                code=ErrorCode.DISCUSSION_INSIGHT_GENERATION_FAILED,
            )
        return insights

    def get_default_categories(self) -> List[Dict[str, Any]]:
        """デフォルトインサイトカテゴリーを取得する。"""
        from .shared.insight_utils import get_default_insight_categories

        return [cat.to_dict() for cat in get_default_insight_categories()]

    # =========================================================================
    # 入力・資料
    # =========================================================================

    def _validate_discussion_input(self, personas: List[Persona], topic: str) -> None:
        discussion_validation.validate_personas_and_topic(
            personas, topic, error_cls=DiscussionManagerError
        )

    def _readable_document(self, principal: Principal, doc_id: str) -> Any:
        """A document the user may still attach (U5, R-02)."""
        try:
            return self._file_attachments.attachable_file(principal, doc_id)
        except CodedError as e:
            if e.code is not ErrorCode.FILE_NOT_FOUND:
                raise DiscussionManagerError(
                    f"document read failed ({type(e).__name__})",
                    code=ErrorCode.DISCUSSION_OPERATION_FAILED,
                ) from e
            raise DiscussionManagerError(
                "document not attachable", code=ErrorCode.FILE_NOT_FOUND
            ) from e

    def _load_documents(
        self, document_ids: Optional[List[str]], principal: Principal
    ) -> tuple[Optional[List[Dict[str, Any]]], Optional[List[DocumentRef]]]:
        """The documents' contents for the AI and their references (U5, R-02).

        Returns:
            ``(documents_data, documents)``; ``(None, None)`` without documents.

        Raises:
            DiscussionManagerError: ``FILE_NOT_FOUND`` (someone else's, expired
                or attached), ``DISCUSSION_DOCUMENT_NOT_FOUND``,
                ``DISCUSSION_DOCUMENTS_TOO_LARGE``.
        """
        if not document_ids:
            return None, None
        documents_data: List[Dict[str, Any]] = []
        documents: List[DocumentRef] = []
        total_size = 0
        max_total_size = config.MAX_REQUEST_PAYLOAD_SIZE
        for doc_id in document_ids:
            uploaded = self._readable_document(principal, doc_id)
            file_path = uploaded.file_path
            if not file_path or (
                not file_path.startswith("s3://") and not Path(file_path).exists()
            ):
                raise DiscussionManagerError(
                    "document file is missing",
                    code=ErrorCode.DISCUSSION_DOCUMENT_NOT_FOUND,
                )
            total_size += uploaded.file_size
            if total_size > max_total_size:
                raise DiscussionManagerError(
                    f"total document size {total_size} exceeds {max_total_size}",
                    code=ErrorCode.DISCUSSION_DOCUMENTS_TOO_LARGE,
                    context={"max_size_mb": max_total_size / (1024 * 1024)},
                )
            documents_data.append(
                {"file_path": file_path, "mime_type": uploaded.mime_type}
            )
            uploaded_at = uploaded.uploaded_at
            documents.append(
                DocumentRef(
                    id=uploaded.id,
                    filename=uploaded.original_filename,
                    mime_type=uploaded.mime_type,
                    file_size=uploaded.file_size,
                    uploaded_at=uploaded_at.isoformat()
                    if isinstance(uploaded_at, datetime)
                    else str(uploaded_at),
                )
            )
        return documents_data, documents

    def _attach_insights(
        self,
        discussion: Discussion,
        messages: List[Message],
        categories: Optional[List[InsightCategory]],
    ) -> Discussion:
        """インサイト生成・カテゴリー保存の共通処理（100 件まで、BR2.6）。"""
        from .shared.insight_utils import attach_insights_to_discussion

        return attach_insights_to_discussion(
            discussion=discussion,
            messages=messages,
            categories=categories,
            ai_service=self.ai_service,
            logger=self.logger,
        )


def _list_body(discussion: Discussion) -> Dict[str, Any]:
    """The decision columns of a listed discussion, as a body."""
    body: Dict[str, Any] = {"id": discussion.id, "topic": discussion.topic}
    if discussion.owner_sub:
        body["owner_sub"] = discussion.owner_sub
    if discussion.shared_group_ids:
        body["shared_group_ids"] = sorted(discussion.shared_group_ids)
    return body
