"""Saving, reading and deleting discussions (U6, ADR-002, ADR-006).

A Component (``.claude/rules/architecture.md``): ``DiscussionManager``,
``AgentDiscussionManager``, ``InterviewManager`` and ``ReportManager`` share one
instance (``sharing_runtime.shared_discussion_parts``). The body, its messages
and its reports are only written and read here; the tables are reached
through the injected ``DiscussionStore``.

Rules (``functional-design/rules.md``):

- BR1.1: the saver owns a new discussion; it starts unshared.
- BR1.2 / BR2.7: the size is measured first (over 350KB: nothing happens,
  ``DISCUSSION_TOO_LARGE``), then the documents are attached, the messages
  written, and the body written last in one transaction. A failed body write
  detaches the documents and deletes the messages (NFR1.57).
- BR1.3 / BR2.4: messages and reports are read only after the body passed
  ``ensure_readable`` (unreadable = missing = ``DISCUSSION_NOT_FOUND``).
- BR1.4 / BR2.5 / BR2.6: changes write only their columns, after
  ``ensure_manageable``; insights are cut at 100.
- BR1.5 / BR1.6: delete removes the body and its share items in one
  transaction (``ResourceSharing.delete_with_shares``), then the messages,
  reports and files, each tried three times; what is left is logged as
  ``discussion.cleanup_failed`` (one line per step, NFR8.18) and the delete
  still succeeds.
- BR1.7: a discussion saved before v1.0.0 (no ``message_count``) shows its
  inner messages and reports; it can be deleted but not changed.
- BR4.4: reports get a server ID; the body's ``report_count`` limits them.
- BR7.3 / BR9.1: each reader sees the participants as readable / no access /
  deleted; the Markdown export carries no owner and no shares.
"""

import logging
import re
import time
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol, Sequence

from ...models.authz import AccessTarget, Principal, ResourceType
from ...models.discussion import (
    MAX_INSIGHTS,
    Discussion,
    DiscussionView,
    DocumentRef,
    ParticipantView,
    legacy_messages,
    legacy_reports,
)
from ...models.discussion_report import DiscussionReport
from ...models.errors import CodedError, ErrorCode, LookupUnavailableError
from ...models.insight import Insight
from ...models.message import DiscussionMessage, Message
from ...models.uploaded_file import AttachResult
from ...services.discussion_store import (
    MAX_BODY_BYTES,
    DiscussionConditionFailed,
    DiscussionStoreError,
)
from ..shared.structured_log import log_event
from .access_policy import AccessPolicy
from .file_attachments import FileAttachments
from .participant_reader import ParticipantReader
from .resource_sharing import ResourceSharing

logger = logging.getLogger(__name__)

_DISCUSSION = ResourceType.DISCUSSION
#: Most reports one discussion keeps (BR4.4, ``MAX_REPORTS_PER_DISCUSSION``).
MAX_REPORTS = 3
#: Attempts of each cleanup step before it is logged (BR1.6).
CLEANUP_ATTEMPTS = 3
#: Waits between cleanup attempts (reliability-design.md §2).
CLEANUP_BACKOFF_SECONDS: tuple[float, ...] = (0.2, 0.4, 0.8)
#: Columns a list page reads (performance-design.md, no insights / documents).
LIST_ATTRIBUTES: tuple[str, ...] = (
    "id",
    "topic",
    "mode",
    "created_at",
    "owner_sub",
    "shared_group_ids",
    "participants",
    "participant_names",
    "message_count",
)
_STATUS_LABELS = {"no_access": "〔参照権限なし〕", "deleted": "〔削除済み〕"}
_MODE_LABELS = {
    "classic": "簡易議論",
    "agent": "しっかり議論",
    "interview": "インタビュー",
}
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]#|<>])")


class DiscussionRecordsError(CodedError):
    """A discussion could not be saved, changed or read."""

    code = ErrorCode.DISCUSSION_OPERATION_FAILED


class DiscussionStorePort(Protocol):
    """What this component needs from ``DiscussionStore``."""

    @staticmethod
    def item_size(body: Mapping[str, Any]) -> int: ...

    def put_new(
        self, body: Mapping[str, Any], extra_items: Sequence[dict[str, Any]] = ()
    ) -> None: ...

    def get(self, discussion_id: str) -> dict[str, Any] | None: ...

    def get_many(
        self, discussion_ids: Sequence[str], attributes: Sequence[str]
    ) -> dict[str, dict[str, Any]]: ...

    def write_messages(self, messages: Sequence[DiscussionMessage]) -> None: ...

    def query_messages(
        self, discussion_id: str, *, up_to: int | None = None
    ) -> list[DiscussionMessage]: ...

    def delete_messages(self, discussion_id: str) -> int: ...

    def query_reports(self, discussion_id: str) -> list[DiscussionReport]: ...

    def delete_reports(self, discussion_id: str) -> int: ...

    def update_insights(
        self, discussion_id: str, insights: list[dict[str, Any]], updated_at: str
    ) -> None: ...

    def advance_message_count(
        self,
        discussion_id: str,
        *,
        expected: int,
        new: int,
        updated_at: str,
        documents: list[dict[str, Any]] | None = None,
    ) -> None: ...

    def put_report(
        self, report: DiscussionReport, *, max_reports: int, updated_at: str
    ) -> None: ...

    def update_report(self, report: DiscussionReport, *, updated_at: str) -> None: ...

    def delete_report(
        self, discussion_id: str, report_id: str, *, updated_at: str
    ) -> None: ...


def _failed(
    message: str, code: ErrorCode = ErrorCode.DISCUSSION_OPERATION_FAILED
) -> DiscussionRecordsError:
    return DiscussionRecordsError(message, code=code)


class DiscussionRecords:
    """The one owner of discussion bodies, messages and reports."""

    def __init__(
        self,
        store: DiscussionStorePort,
        sharing: ResourceSharing,
        files: FileAttachments,
        participants: ParticipantReader,
        *,
        clock: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the component.

        Args:
            store: ``DiscussionStore`` (injected).
            sharing: The per-task ``ResourceSharing`` (delete with shares).
            files: The per-task ``FileAttachments``.
            participants: Participant states for the readers.
            clock: Wall clock for ``updated_at`` (tests).
            sleep: Wait between cleanup attempts (tests).
        """
        self._store = store
        self._sharing = sharing
        self._files = files
        self._participants = participants
        self._clock = clock
        self._sleep = sleep

    # --- bodies and decisions ------------------------------------------------------

    def _read_body(self, discussion_id: str) -> dict[str, Any] | None:
        try:
            return self._store.get(discussion_id) if discussion_id else None
        except DiscussionStoreError as e:
            raise LookupUnavailableError("discussion body unreadable") from e

    def _target(
        self, body: Mapping[str, Any] | None, discussion_id: str
    ) -> AccessTarget:
        return self._sharing.target_of(_DISCUSSION, body, discussion_id)

    def _readable_body(
        self, principal: Principal, discussion_id: str
    ) -> tuple[dict[str, Any], AccessTarget]:
        body = self._read_body(discussion_id)
        target = self._target(body, discussion_id)
        AccessPolicy.ensure_readable(principal, target, ErrorCode.DISCUSSION_NOT_FOUND)
        assert body is not None  # ensure_readable raised for a missing body
        return body, target

    def _manageable(
        self, principal: Principal, discussion_id: str, *, allow_legacy: bool = False
    ) -> tuple[dict[str, Any], Discussion]:
        body = self._read_body(discussion_id)
        AccessPolicy.ensure_manageable(
            principal, self._target(body, discussion_id), ErrorCode.DISCUSSION_NOT_FOUND
        )
        assert body is not None
        discussion = Discussion.from_dict(body)
        if discussion.is_legacy and not allow_legacy:
            raise _failed("discussions before v1.0.0 cannot be changed")
        return body, discussion

    def ensure_manageable(self, principal: Principal, discussion_id: str) -> None:
        """Refuse a change before any work is queued (reads the body only).

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND`` / ``DISCUSSION_OPERATION_FAILED``
                (a discussion before v1.0.0, which is read-only).
            AccessDeniedError: Readable but not manageable.
        """
        self._manageable(principal, discussion_id)

    def can_manage(self, principal: Principal, discussion_id: str) -> bool:
        """Whether ``principal`` may change the discussion (missing: ``False``)."""
        body = self._read_body(discussion_id)
        return AccessPolicy.can_manage(principal, self._target(body, discussion_id))

    def _now(self) -> str:
        return self._clock().isoformat()

    # --- saving ----------------------------------------------------------------------

    def save_new(
        self, principal: Principal, discussion: Discussion, messages: Sequence[Message]
    ) -> Discussion:
        """Save a new discussion owned by ``principal`` (BR1.1, BR1.2).

        The documents of ``discussion`` are attached to it.

        Returns:
            The discussion as saved.

        Raises:
            CodedError: ``DISCUSSION_TOO_LARGE`` (nothing written), the
                attachment's codes (``FILE_NOT_FOUND`` etc.), or
                ``DISCUSSION_OPERATION_FAILED``.
        """
        if not principal.sub:
            raise _failed("a discussion needs its saver")
        discussion = replace(
            discussion,
            owner_sub=principal.sub,
            shared_group_ids=frozenset(),
            message_count=len(messages),
            report_count=0,
        )
        body = discussion.to_dict()
        self._check_size(body)
        attached = self._attach(principal, discussion.id, discussion.documents or [])
        stored = [
            DiscussionMessage.from_message(discussion.id, seq, message)
            for seq, message in enumerate(messages, start=1)
        ]
        try:
            self._store.write_messages(stored)
            self._store.put_new(body)
        except DiscussionStoreError as e:
            logger.warning("Saving discussion failed: %s", type(e).__name__)
            # 同じ ID の議論がすでにある（別の要求が先に保存した）ときは、その発言を消さない
            exists = isinstance(e, DiscussionConditionFailed)
            self._roll_back(
                discussion.id, attached, wrote_messages=bool(stored) and not exists
            )
            raise _failed("the discussion could not be saved") from e
        return discussion

    def _check_size(self, body: Mapping[str, Any]) -> None:
        if self._store.item_size(body) > MAX_BODY_BYTES:
            raise _failed(
                "the discussion body exceeds 350KB", ErrorCode.DISCUSSION_TOO_LARGE
            )

    def _attach(
        self, principal: Principal, discussion_id: str, documents: Sequence[DocumentRef]
    ) -> AttachResult | None:
        ids = [d.id for d in documents if d.id]
        if not ids:
            return None
        try:
            return self._files.attach_all(principal, ids, "discussion", discussion_id)
        except CodedError as e:
            raise DiscussionRecordsError(
                f"documents could not be attached ({type(e).__name__})",
                code=e.code,
                context=e.context,
            ) from e

    def _roll_back(
        self, discussion_id: str, attached: AttachResult | None, *, wrote_messages: bool
    ) -> None:
        if attached is not None:
            self._files.detach(attached)
        if wrote_messages:
            self._cleanup(
                discussion_id,
                "rollback_messages",
                lambda: self._store.delete_messages(discussion_id),
            )

    # --- reading ---------------------------------------------------------------------

    def get(self, principal: Principal, discussion_id: str) -> DiscussionView:
        """The discussion with its messages, reports and participants (BR1.3, BR7.3).

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND`` (unreadable or missing).
            LookupUnavailableError: A read failed.
        """
        body, target = self._readable_body(principal, discussion_id)
        return self._view(principal, body, target)

    def get_manageable(
        self, principal: Principal, discussion_id: str
    ) -> DiscussionView:
        """Like :meth:`get`, for a change the reader must be allowed to make.

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND`` / ``DISCUSSION_OPERATION_FAILED``
                (a discussion before v1.0.0).
            AccessDeniedError: Readable but not manageable.
        """
        body, _ = self._manageable(principal, discussion_id)
        return self._view(principal, body, self._target(body, discussion_id))

    def _view(
        self, principal: Principal, body: dict[str, Any], target: AccessTarget
    ) -> DiscussionView:
        discussion = Discussion.from_dict(body)
        try:
            if discussion.is_legacy:
                messages = legacy_messages(body)
                reports = legacy_reports(body, discussion.id)
            else:
                messages = [
                    m.to_message()
                    for m in self._store.query_messages(
                        discussion.id, up_to=discussion.message_count
                    )
                ]
                reports = self._store.query_reports(discussion.id)
        except DiscussionStoreError as e:
            raise LookupUnavailableError("discussion messages unreadable") from e
        return DiscussionView(
            discussion=discussion,
            messages=messages,
            reports=reports,
            participants=self._participant_views(principal, discussion, messages),
            manageable=AccessPolicy.can_manage(principal, target),
        )

    def _participant_views(
        self, principal: Principal, discussion: Discussion, messages: Sequence[Message]
    ) -> list[ParticipantView]:
        statuses, bodies = self._participants.statuses_with_bodies(
            principal, discussion.participants
        )
        spoken = {m.persona_id: m.persona_name for m in messages}
        return [
            ParticipantView(
                persona_id=pid,
                display_name=discussion.participant_names.get(pid)
                or spoken.get(pid)
                or pid[:8],
                status=statuses.get(pid, "deleted"),
                persona=bodies.get(pid) if statuses.get(pid) == "readable" else None,
            )
            for pid in discussion.participants
        ]

    def list_bodies(
        self,
        principal: Principal,
        discussion_ids: Sequence[str],
        *,
        mode: str | None = None,
    ) -> list[Discussion]:
        """The list columns of the bodies the user can still read (NFR2.15).

        Strongly consistent, in the given order; ``mode`` narrows the page.

        Raises:
            LookupUnavailableError: The bodies could not be read.
        """
        if not discussion_ids:
            return []
        try:
            found = self._store.get_many(list(discussion_ids), LIST_ATTRIBUTES)
        except DiscussionStoreError as e:
            raise LookupUnavailableError("discussion bodies unreadable") from e
        bodies = [found[i] for i in dict.fromkeys(discussion_ids) if i in found]
        readable = [
            b
            for b in bodies
            if AccessPolicy.can_read(principal, self._target(b, str(b["id"])))
        ]
        discussions = [Discussion.from_dict(b) for b in readable]
        return [d for d in discussions if mode is None or d.mode == mode]

    # --- changes ---------------------------------------------------------------------

    def update_insights(
        self, principal: Principal, discussion_id: str, insights: Sequence[Insight]
    ) -> Discussion:
        """Replace the insights, nothing else (BR1.4, BR2.5, BR2.6).

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND``, ``DISCUSSION_TOO_LARGE`` or
                ``DISCUSSION_OPERATION_FAILED``.
            AccessDeniedError: Readable but not manageable.
        """
        body, discussion = self._manageable(principal, discussion_id)
        kept = list(insights)[:MAX_INSIGHTS]
        if len(insights) > MAX_INSIGHTS:
            log_event(
                "discussion.insights_truncated",
                discussion_id=discussion_id,
                generated=len(insights),
                kept=MAX_INSIGHTS,
            )
        now = self._clock()
        updated = discussion.with_insights(kept, now=now)
        self._check_size(updated.to_dict())
        try:
            self._store.update_insights(
                discussion_id, [i.to_dict() for i in kept], now.isoformat()
            )
        except DiscussionConditionFailed as e:
            raise _failed(
                "the discussion is gone", ErrorCode.DISCUSSION_NOT_FOUND
            ) from e
        except DiscussionStoreError as e:
            raise _failed("insights could not be saved") from e
        return updated

    def append_messages(
        self,
        principal: Principal,
        discussion_id: str,
        messages: Sequence[Message],
        documents: Sequence[DocumentRef],
        *,
        skip_saved: bool = False,
    ) -> Discussion:
        """Add the messages after ``message_count`` and the new documents (BR3.8).

        Args:
            skip_saved: ``messages`` is the whole conversation; its first
                ``message_count`` messages are already saved and are skipped.

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND``, ``DISCUSSION_TOO_LARGE``,
                attachment codes, or ``DISCUSSION_OPERATION_FAILED`` (the count
                moved; saving again adds them).
            AccessDeniedError: Readable but not manageable.
        """
        _, discussion = self._manageable(principal, discussion_id)
        expected = discussion.message_count or 0
        if skip_saved:
            messages = list(messages)[expected:]
        known = {d.id for d in discussion.documents or []}
        new_documents = [d for d in documents if d.id not in known]
        all_documents = list(discussion.documents or []) + new_documents
        updated = replace(
            discussion,
            message_count=expected + len(messages),
            documents=all_documents or discussion.documents,
            updated_at=self._clock(),
        )
        self._check_size(updated.to_dict())
        attached = self._attach(principal, discussion_id, new_documents)
        stored = [
            DiscussionMessage.from_message(discussion_id, seq, message)
            for seq, message in enumerate(messages, start=expected + 1)
        ]
        try:
            self._store.write_messages(stored)
            self._store.advance_message_count(
                discussion_id,
                expected=expected,
                new=updated.message_count or 0,
                updated_at=self._now(),
                documents=[d.to_dict() for d in all_documents]
                if new_documents
                else None,
            )
        except DiscussionStoreError as e:
            if attached is not None:
                self._files.detach(attached)
            if (
                isinstance(e, DiscussionConditionFailed)
                and self._read_body(discussion_id) is None
            ):
                raise _failed(
                    "the discussion is gone", ErrorCode.DISCUSSION_NOT_FOUND
                ) from e
            raise _failed("messages could not be added") from e
        return updated

    # --- deleting --------------------------------------------------------------------

    def delete(self, principal: Principal, discussion_id: str) -> bool:
        """Delete the body and its shares, then clean up (BR1.5, BR1.6).

        Returns:
            ``True`` once the body is gone (cleanup failures are logged).

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND`` / ``DISCUSSION_OPERATION_FAILED``.
            AccessDeniedError: Readable but not manageable.
        """
        deleted = self._sharing.delete_with_shares(
            principal, _DISCUSSION, discussion_id
        )
        if deleted is None:
            raise _failed("nothing to delete", ErrorCode.DISCUSSION_NOT_FOUND)
        documents = [
            str(d.get("id")) for d in deleted.get("documents") or [] if d.get("id")
        ]
        self._cleanup(
            discussion_id,
            "messages",
            lambda: self._store.delete_messages(discussion_id),
        )
        self._cleanup(
            discussion_id, "reports", lambda: self._store.delete_reports(discussion_id)
        )
        if documents:
            self._cleanup(
                discussion_id,
                "files",
                lambda: self._files_or_raise(discussion_id, documents),
            )
        return True

    def _files_or_raise(self, discussion_id: str, documents: list[str]) -> None:
        left = self._files.delete_attached("discussion", discussion_id, documents)
        if left:
            raise _failed(f"{len(left)} files left")

    def _cleanup(
        self, discussion_id: str, step: str, action: Callable[[], object]
    ) -> None:
        for attempt in range(CLEANUP_ATTEMPTS):
            try:
                action()
                return
            except Exception as e:
                error = e
                if attempt < CLEANUP_ATTEMPTS - 1:
                    self._sleep(CLEANUP_BACKOFF_SECONDS[attempt])
        log_event(
            "discussion.cleanup_failed",
            level="ERROR",
            discussion_id=discussion_id,
            step=step,
            error_type=type(error).__name__,
        )

    # --- reports ---------------------------------------------------------------------

    def save_report(
        self,
        principal: Principal,
        discussion_id: str,
        template_type: str,
        content: str,
        custom_prompt: str | None,
    ) -> DiscussionReport:
        """Save a generated report with a server-assigned ID (BR4.2, BR4.4).

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND``, ``REPORT_LIMIT_REACHED``,
                ``REPORT_OPERATION_FAILED``.
            AccessDeniedError: Readable but not manageable.
        """
        _, discussion = self._manageable(principal, discussion_id)
        if discussion.report_count >= MAX_REPORTS:
            raise self._limit()
        try:
            report = DiscussionReport.create_new(
                discussion_id, template_type, content, custom_prompt, now=self._clock()
            )
        except ValueError as e:
            raise _failed("unknown template", ErrorCode.REPORT_OPERATION_FAILED) from e
        try:
            self._store.put_report(
                report, max_reports=MAX_REPORTS, updated_at=self._now()
            )
        except DiscussionConditionFailed as e:
            if self._read_body(discussion_id) is None:
                raise _failed(
                    "the discussion is gone", ErrorCode.DISCUSSION_NOT_FOUND
                ) from e
            raise self._limit() from e
        except DiscussionStoreError as e:
            raise _failed(
                "the report could not be saved", ErrorCode.REPORT_OPERATION_FAILED
            ) from e
        return report

    @staticmethod
    def _limit() -> DiscussionRecordsError:
        return DiscussionRecordsError(
            f"a discussion keeps at most {MAX_REPORTS} reports",
            code=ErrorCode.REPORT_LIMIT_REACHED,
            context={"max_reports": MAX_REPORTS},
        )

    def _find_report(self, discussion_id: str, report_id: str) -> DiscussionReport:
        try:
            reports = self._store.query_reports(discussion_id)
        except DiscussionStoreError as e:
            raise LookupUnavailableError("reports unreadable") from e
        report = next((r for r in reports if r.report_id == report_id), None)
        if report is None:
            raise _failed("no such report", ErrorCode.REPORT_NOT_FOUND)
        return report

    def get_report(
        self, principal: Principal, discussion_id: str, report_id: str
    ) -> DiscussionReport:
        """One report of a discussion the user may read.

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND`` / ``REPORT_NOT_FOUND``.
        """
        body, _ = self._readable_body(principal, discussion_id)
        if "message_count" not in body:
            report = next(
                (
                    r
                    for r in legacy_reports(body, discussion_id)
                    if r.report_id == report_id
                ),
                None,
            )
            if report is None:
                raise _failed("no such report", ErrorCode.REPORT_NOT_FOUND)
            return report
        return self._find_report(discussion_id, report_id)

    def update_report(
        self, principal: Principal, discussion_id: str, report_id: str, content: str
    ) -> DiscussionReport:
        """Change one report's content (BR4.2, BR4.4).

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND`` / ``REPORT_NOT_FOUND`` /
                ``REPORT_OPERATION_FAILED``.
            AccessDeniedError: Readable but not manageable.
        """
        self._manageable(principal, discussion_id)
        report = self._find_report(discussion_id, report_id).with_content(
            content, now=self._clock()
        )
        try:
            self._store.update_report(report, updated_at=self._now())
        except DiscussionConditionFailed as e:
            raise _failed("the report is gone", ErrorCode.REPORT_NOT_FOUND) from e
        except DiscussionStoreError as e:
            raise _failed(
                "the report could not be saved", ErrorCode.REPORT_OPERATION_FAILED
            ) from e
        return report

    def delete_report(
        self, principal: Principal, discussion_id: str, report_id: str
    ) -> None:
        """Delete one report (BR4.2, BR4.4).

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND`` / ``REPORT_NOT_FOUND`` /
                ``REPORT_OPERATION_FAILED``.
            AccessDeniedError: Readable but not manageable.
        """
        self._manageable(principal, discussion_id)
        try:
            self._store.delete_report(discussion_id, report_id, updated_at=self._now())
        except DiscussionConditionFailed as e:
            raise _failed("the report is gone", ErrorCode.REPORT_NOT_FOUND) from e
        except DiscussionStoreError as e:
            raise _failed(
                "the report could not be deleted", ErrorCode.REPORT_OPERATION_FAILED
            ) from e

    # --- export (BR9.1) --------------------------------------------------------------

    def export_markdown(
        self, principal: Principal, discussion_id: str
    ) -> tuple[str, str]:
        """The discussion as Markdown, for a user who may read it (BR9.1).

        Returns:
            ``(filename, text)``. No owner ``sub`` and no shares are included;
            documents are listed by name only.

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND``.
        """
        return build_export(self.get(principal, discussion_id))


def _escape(text: str) -> str:
    """One-line Markdown text: special characters escaped, newlines folded."""
    folded = " ".join(text.splitlines())
    return _MARKDOWN_SPECIAL.sub(r"\\\1", folded)


def build_export(view: DiscussionView) -> tuple[str, str]:
    """Render a :class:`DiscussionView` as Markdown (BR9.1)."""
    d = view.discussion
    lines = [
        f"# {_escape(d.topic)}",
        "",
        f"- 日時: {d.created_at.strftime('%Y/%m/%d %H:%M')}",
        f"- 種類: {_MODE_LABELS.get(d.mode, d.mode)}",
        "",
        "## 参加者",
        "",
    ]
    for p in view.participants:
        lines.append(f"- {_escape(p.display_name)}{_STATUS_LABELS.get(p.status, '')}")
    if d.documents:
        lines += ["", "## 資料", ""]
        lines += [f"- {_escape(doc.filename)}" for doc in d.documents]
    lines += ["", "## 発言", ""]
    for m in view.messages:
        name = "あなた" if m.persona_id == "user" else m.persona_name or "ペルソナ"
        round_label = f"（ラウンド {m.round_number}）" if m.round_number else ""
        lines += [f"### {_escape(name)}{round_label}", "", m.content, ""]
    if d.insights:
        lines += ["## インサイト", ""]
        lines += [
            f"- [{_escape(i.category)}] {_escape(i.description)}" for i in d.insights
        ]
        lines.append("")
    for report in view.reports:
        lines += [f"## レポート（{report.template_type}）", "", report.content, ""]
    stamp = d.created_at.strftime("%Y%m%d_%H%M%S")
    return f"discussion_{stamp}.md", "\n".join(lines).rstrip() + "\n"
