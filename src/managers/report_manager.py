"""Report generation and management Manager.

Handles report generation from discussion results, CRUD and follow-up analysis.
Shared by both classic and agent discussions.

Rules:

- Generation requires ``ai:execute`` and the discussion's ``ensure_readable``
  (people it is shared with can also generate and see)
- Saving, updating content and deleting require the discussion's
  ``ensure_manageable`` (decided by ``DiscussionRecords``)
- The short-term memory of follow-up generation is separated per discussion and
  user pair (:func:`report_session_id`)
- Report IDs are assigned by the server, and the count limit is enforced in the
  same transaction as the body's count
- Participant profiles are read with ``ParticipantReader``, only for personas
  the user can read
"""

import hashlib
import logging
import re
from typing import Any, Dict, Iterator, List, Optional

from ..models.authz import Permission, Principal, ResourceType
from ..models.discussion import DiscussionView
from ..models.discussion_report import DiscussionReport
from ..models.errors import CodedError, ParticipantNotFoundError
from ..models.persona import Persona
from ..prompts.report_prompts import (
    build_data_driven_followup_system_prompt,
    build_data_driven_system_prompt,
    build_report_system_prompt,
)
from ..services.agent_service import AgentService
from ..services.ai_service import AIService
from ..services.service_factory import service_factory
from .components.access_policy import AccessPolicy
from .components.data_agent_policy import DataAgentPolicy
from .components.discussion_records import DiscussionRecords
from .components.participant_reader import ParticipantReader
from .components.sharing_runtime import DiscussionParts, discussion_parts_from


class ReportManagerError(CodedError):
    """Exception specific to the report Manager."""

    pass


def report_session_id(discussion_id: str, sub: str) -> str:
    """The short-term memory session of ``sub``'s reports on a discussion.

    The sub is hashed so that the session ID (which reaches the browser and the
    logs of the memory service) does not carry the user's identifier.
    """
    digest = hashlib.sha256(sub.encode("utf-8")).hexdigest()[:16]
    return f"report-{discussion_id}-{digest}"


def _discussion_parts() -> DiscussionParts:
    return discussion_parts_from(service_factory)


class ReportManager:
    """Manager responsible for report generation and management."""

    def __init__(
        self,
        ai_service: Optional[AIService] = None,
        agent_service: Optional[AgentService] = None,
        discussion_records: Optional[DiscussionRecords] = None,
        participants: Optional[ParticipantReader] = None,
    ):
        self.ai_service = ai_service or service_factory.get_ai_service()
        self.agent_service = agent_service or service_factory.get_agent_service()
        self._records = discussion_records
        self._reader = participants
        self.logger = logging.getLogger(__name__)

    @property
    def _discussion_records(self) -> DiscussionRecords:
        if self._records is None:
            self._records = _discussion_parts().records
        return self._records

    @property
    def _participant_reader(self) -> ParticipantReader:
        if self._reader is None:
            self._reader = _discussion_parts().participants
        return self._reader

    def get_discussion(
        self, principal: Principal, discussion_id: str
    ) -> DiscussionView:
        """The discussion (with its reports) for a reader.

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND`` when it cannot be read.
        """
        return self._discussion_records.get(principal, discussion_id)

    # ==========================================================================
    # Generation
    # ==========================================================================

    def _readable_for_generation(
        self, principal: Principal, discussion_id: str
    ) -> DiscussionView:
        AccessPolicy.require(
            principal, Permission.AI_EXECUTE, resource_type=ResourceType.DISCUSSION
        )
        return self._discussion_records.get(principal, discussion_id)

    def _get_report_context(
        self, principal: Principal, view: DiscussionView
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Return the insight data for report generation and the profiles of readable personas."""
        insights_data = [
            {
                "category": ins.category,
                "description": ins.description,
                "confidence_score": ins.confidence_score,
            }
            for ins in view.insights
        ]
        readable = [p.persona_id for p in view.participants if p.status == "readable"]
        personas: List[Persona] = []
        if readable:
            try:
                personas = self._participant_reader.read_for_use(principal, readable)
            except ParticipantNotFoundError:
                # It became unreadable between the re-reads. Continue without the profile
                self.logger.info("Report context: a participant became unreadable")
        personas_data = [
            {
                "name": persona.name,
                "age": persona.age,
                "occupation": persona.occupation,
                "values": persona.values,
                "pain_points": persona.pain_points,
                "goals": persona.goals,
            }
            for persona in personas
        ]
        return insights_data, personas_data

    def _build_report_user_content(
        self,
        messages: List[Any],
        insights: List[Dict[str, Any]],
        topic: str,
        personas: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Build the user context for report generation in Markdown."""
        context_parts = [f"## 議論トピック\n{topic}\n"]

        if personas:
            context_parts.append("## 参加ペルソナのプロフィール")
            for p in personas:
                context_parts.append(
                    f"### {p['name']}（{p['age']}歳 / {p['occupation']}）\n"
                    f"- 価値観: {', '.join(p.get('values', []))}\n"
                    f"- 課題: {', '.join(p.get('pain_points', []))}\n"
                    f"- 目標: {', '.join(p.get('goals', []))}"
                )

        context_parts.append("\n## 議論ログ")
        for msg in messages:
            context_parts.append(f"**{msg.persona_name}**: {msg.content}")
        context_parts.append("\n## 抽出済みインサイト")
        for ins in insights:
            context_parts.append(
                f"- [{ins.get('category', '')}] {ins.get('description', '')} "
                f"(信頼度: {ins.get('confidence_score', 0)})"
            )

        return "\n".join(context_parts)

    def ensure_data_agent_allowed(self, principal: Principal) -> None:
        """Refuse a data-driven report before the router starts streaming.

        Raises:
            AccessDeniedError: The user lacks ``ai:execute``.
            ReportManagerError: ``DATA_AGENT_NOT_ALLOWED``.
        """
        AccessPolicy.require(
            principal, Permission.AI_EXECUTE, resource_type=ResourceType.DISCUSSION
        )
        DataAgentPolicy.ensure_allowed(ReportManagerError)

    def generate_report_streaming(
        self,
        principal: Principal,
        discussion_id: str,
        template_type: str,
        custom_prompt: Optional[str] = None,
        event_queue: Any = None,
    ) -> Iterator[str]:
        """
        Stream-generate a report from a discussion.

        Args:
            principal: The person generating (must be able to read the discussion)
            discussion_id: Discussion ID
            template_type: Template type (summary/review/custom/data_driven)
            custom_prompt: Custom prompt
            event_queue: Queue for real-time events (data_driven)

        Yields:
            str: Text chunks

        Raises:
            AccessDeniedError: No ``ai:execute``
            CodedError: ``DISCUSSION_NOT_FOUND`` (unreadable or missing)
        """
        view = self._readable_for_generation(principal, discussion_id)
        yield from self._generate(
            principal,
            view,
            template_type,
            custom_prompt,
            event_queue=event_queue,
            is_followup=False,
        )

    def _generate(
        self,
        principal: Principal,
        view: DiscussionView,
        template_type: str,
        custom_prompt: Optional[str],
        *,
        event_queue: Any,
        is_followup: bool,
    ) -> Iterator[str]:
        if template_type == "data_driven":
            # Closed network: before AgentService is called
            DataAgentPolicy.ensure_allowed(ReportManagerError)
        insights_data, personas_data = self._get_report_context(principal, view)
        if template_type == "data_driven":
            yield from self._generate_data_driven_report(
                messages=view.messages,
                insights=insights_data,
                topic=view.topic,
                custom_prompt=custom_prompt,
                personas=personas_data,
                event_queue=event_queue,
                session_id=report_session_id(view.id, principal.sub),
                is_followup=is_followup,
            )
            return
        system_prompt = build_report_system_prompt(
            view.topic, template_type, custom_prompt
        )
        user_content = self._build_report_user_content(
            view.messages, insights_data, view.topic, personas_data
        )
        converse_messages = [{"role": "user", "content": [{"text": user_content}]}]
        yield from self.ai_service.generate_standard_report_streaming(
            system_prompt=system_prompt,
            converse_messages=converse_messages,
        )

    def _generate_data_driven_report(
        self,
        messages: Any,
        insights: Any,
        topic: str,
        custom_prompt: Optional[str],
        personas: Any,
        event_queue: Any = None,
        session_id: Optional[str] = None,
        is_followup: bool = False,
    ) -> Iterator[str]:
        """Delegate data-driven report generation to AgentService."""
        if is_followup:
            system_prompt = build_data_driven_followup_system_prompt(
                topic, custom_prompt
            )
        else:
            system_prompt = build_data_driven_system_prompt(topic, custom_prompt)

        user_content = self._build_report_user_content(
            messages, insights, topic, personas
        )

        yield from self.agent_service.run_report_agent_streaming(
            system_prompt=system_prompt,
            user_content=user_content,
            event_queue=event_queue,
            session_id=session_id,
        )

    def generate_followup_report_streaming(
        self,
        principal: Principal,
        discussion_id: str,
        followup_prompt: str,
        previous_report: str,
        event_queue: Any = None,
    ) -> Iterator[str]:
        """
        Stream-generate a follow-up analysis based on the previous report.

        The short-term memory session is determined from the discussion and user pair (not received from the screen).
        If the STM session is valid, the previous history is restored automatically, so only the extra instruction is passed.
        If the STM has expired, previous_report is injected into the prompt as a fallback.

        Raises:
            AccessDeniedError: No ``ai:execute``
            CodedError: ``DISCUSSION_NOT_FOUND`` (unreadable or missing)
        """
        view = self._readable_for_generation(principal, discussion_id)
        session_id = report_session_id(view.id, principal.sub)
        if self._check_stm_session(session_id):
            self.logger.info("フォローアップ: STMセッション継続")
            effective_prompt = (
                f"追加指示: {followup_prompt}\n\n"
                "重要: 前回の会話でテーブル構造やデータ分析は実施済みです。"
                "テーブル一覧の再確認は不要です。追加指示に直接関連するクエリのみ実行してください。"
            )
        else:
            self.logger.info("フォローアップ: STMセッション未検出、フォールバック使用")
            effective_prompt = (
                "以下は先に生成した分析レポートです。"
                "このレポートを前提として追加分析を行ってください:\n\n"
                f"---\n{previous_report}\n---\n\n"
                f"追加指示: {followup_prompt}\n\n"
                "重要: 前回レポートでテーブル構造は確認済みです。"
                "テーブル一覧の再確認は不要です。"
                "追加指示に直接関連するクエリのみ実行してください。"
            )

        yield from self._generate(
            principal,
            view,
            "data_driven",
            effective_prompt,
            event_queue=event_queue,
            is_followup=True,
        )

    def _check_stm_session(self, session_id: str) -> bool:
        """Check whether session history exists in AgentCore Memory STM."""
        return bool(service_factory.check_stm_session_exists(session_id))

    # ==========================================================================
    # Reading and export
    # ==========================================================================

    def get_report(
        self, principal: Principal, discussion_id: str, report_id: str
    ) -> DiscussionReport:
        """One report of a discussion the user may read.

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND`` / ``REPORT_NOT_FOUND``
        """
        return self._discussion_records.get_report(principal, discussion_id, report_id)

    def can_manage(self, principal: Principal, discussion_id: str) -> bool:
        """Whether the user may save, edit and delete the reports (screen only)."""
        return self._discussion_records.can_manage(principal, discussion_id)

    def export_report(
        self, principal: Principal, discussion_id: str, report_id: str, fmt: str
    ) -> tuple[str, str]:
        """The file name and text of a report (``md`` as is, ``txt`` plain).

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND`` / ``REPORT_NOT_FOUND``
        """
        report = self._discussion_records.get_report(
            principal, discussion_id, report_id
        )
        extension = "txt" if fmt == "txt" else "md"
        content = report.content
        if extension == "txt":
            # Strip Markdown syntax
            content = re.sub(r"#{1,6}\s*", "", content)
            content = re.sub(r"\*{1,2}(.*?)\*{1,2}", r"\1", content)
            content = re.sub(r"_{1,2}(.*?)_{1,2}", r"\1", content)
        timestamp = report.created_at.strftime("%Y%m%d_%H%M%S")
        return f"report_{report.template_type}_{timestamp}.{extension}", content

    # ==========================================================================
    # Saving, updating and deleting
    # ==========================================================================

    def save_report(
        self,
        principal: Principal,
        discussion_id: str,
        template_type: str,
        content: str,
        custom_prompt: Optional[str] = None,
    ) -> DiscussionReport:
        """
        Save a generated report (the ID is assigned by the server).

        The permission check is done in the component before the template type
        is validated (so input from people who cannot touch the discussion is
        never rendered in the preview).

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND``, ``REPORT_LIMIT_REACHED``,
                ``REPORT_OPERATION_FAILED`` (invalid template type)
            AccessDeniedError: Readable but not manageable
        """
        return self._discussion_records.save_report(
            principal, discussion_id, template_type, content, custom_prompt
        )

    def update_report_content(
        self, principal: Principal, discussion_id: str, report_id: str, content: str
    ) -> None:
        """
        Update the content of an existing report.

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND`` / ``REPORT_NOT_FOUND``
            AccessDeniedError: Readable but not manageable
        """
        self._discussion_records.update_report(
            principal, discussion_id, report_id, content
        )

    def delete_report(
        self, principal: Principal, discussion_id: str, report_id: str
    ) -> bool:
        """
        Delete a report from a discussion.

        Returns:
            True if deleted

        Raises:
            CodedError: ``DISCUSSION_NOT_FOUND`` / ``REPORT_NOT_FOUND``
            AccessDeniedError: Readable but not manageable
        """
        self._discussion_records.delete_report(principal, discussion_id, report_id)
        return True
