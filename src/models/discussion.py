"""
Discussion data model for the AI Persona System (v1.0.0 U6, entities.md).

The body of a discussion no longer holds its messages and reports: they live
in ``DiscussionMessages`` and ``DiscussionReports`` and are read only after the
body passed ``can_read`` (ADR-002). The body carries its owner, its department
shares and the counts of what was written elsewhere.

Discussions saved before v1.0.0 still hold ``messages`` and ``reports`` inside
and have no ``owner_sub`` / ``message_count`` (BR1.7). :meth:`Discussion.from_dict`
reads them, and :func:`legacy_messages` / :func:`legacy_reports` read their
inner lists for display only; they are never rewritten in the new shape.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional
import uuid

from .discussion_report import DiscussionReport
from .insight import Insight
from .message import Message

#: The three kinds of saved discussions.
DISCUSSION_MODES: tuple[str, ...] = ("classic", "agent", "interview")
#: Most insights one discussion keeps (BR2.6).
MAX_INSIGHTS = 100
#: Participant states seen by the reader of a discussion (BR7.3, M9).
PARTICIPANT_STATUSES: tuple[str, ...] = ("readable", "no_access", "deleted")


@dataclass(frozen=True)
class DocumentRef:
    """Display metadata of an attached document (BR5.2).

    The S3 key is never kept here: content and delivery are looked up by
    ``id`` in ``UploadedFiles`` (``FileAttachments``).
    """

    id: str
    filename: str
    mime_type: str
    file_size: int
    uploaded_at: str

    def __post_init__(self) -> None:
        if self.file_size < 0:
            raise ValueError("file_size must not be negative")

    def to_dict(self) -> Dict[str, Any]:
        """Plain values for storage."""
        return {
            "id": self.id,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "file_size": self.file_size,
            "uploaded_at": self.uploaded_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DocumentRef":
        """Read a stored reference; extra keys (an old ``file_path``) are dropped."""
        uploaded_at = data.get("uploaded_at") or ""
        if isinstance(uploaded_at, datetime):
            uploaded_at = uploaded_at.isoformat()
        return cls(
            id=str(data.get("id") or ""),
            filename=str(data.get("filename") or data.get("original_filename") or ""),
            mime_type=str(data.get("mime_type") or "application/octet-stream"),
            file_size=int(data.get("file_size") or 0),
            uploaded_at=str(uploaded_at),
        )


@dataclass(frozen=True)
class ParticipantView:
    """One participant as seen by the reader of a discussion (M9, BR7.3).

    Attributes:
        persona_id: The persona.
        display_name: The name kept when the discussion was saved.
        status: ``readable``, ``no_access`` or ``deleted``.
    """

    persona_id: str
    display_name: str
    status: str

    def __post_init__(self) -> None:
        if self.status not in PARTICIPANT_STATUSES:
            raise ValueError(f"unknown participant status {self.status!r}")

    @property
    def linkable(self) -> bool:
        """Only a readable persona links to its detail page."""
        return self.status == "readable"


@dataclass
class Discussion:
    """The body of a discussion (classic, agent or a saved interview).

    Attributes:
        id: UUID.
        topic: The theme (an interview's saved name).
        participants: Persona IDs; kept even when a persona becomes unreadable.
        insights: At most :data:`MAX_INSIGHTS` insights.
        created_at: When it was saved (never changes).
        mode: ``classic``, ``agent`` or ``interview``.
        agent_config: Rounds, instructions, memory and model settings.
        documents: Attached documents (metadata only).
        owner_sub: Who saved it; ``None`` for discussions before v1.0.0.
        shared_group_ids: Departments it is shared with (``ResourceSharing``).
        participant_names: Persona ID -> name when it was saved.
        message_count: Messages written to ``DiscussionMessages``; ``None`` for
            discussions before v1.0.0 (their messages are inside the body).
        report_count: Reports in ``DiscussionReports`` (at most 3).
        updated_at: Last insight / report / interview change.
    """

    id: str
    topic: str
    participants: List[str]
    insights: List[Insight]
    created_at: datetime
    mode: str = "classic"
    agent_config: Optional[Dict[str, Any]] = None
    documents: Optional[List[DocumentRef]] = None
    owner_sub: Optional[str] = None
    shared_group_ids: frozenset[str] = field(default_factory=frozenset)
    participant_names: Dict[str, str] = field(default_factory=dict)
    message_count: Optional[int] = 0
    report_count: int = 0
    updated_at: Optional[datetime] = None

    @classmethod
    def create_new(
        cls,
        topic: str,
        participants: List[str],
        mode: str = "classic",
        agent_config: Optional[Dict[str, Any]] = None,
        documents: Optional[List[DocumentRef]] = None,
        *,
        owner_sub: Optional[str] = None,
        participant_names: Optional[Dict[str, str]] = None,
        discussion_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> "Discussion":
        """Create a new discussion (not saved yet, no messages written).

        Args:
            discussion_id: A preset ID (an agent discussion decides its ID at
                the start so the memory session uses the same value, BR6.2).
            now: Creation time (tests); the current time when omitted.

        Raises:
            ValueError: ``mode`` is not one of :data:`DISCUSSION_MODES`.
        """
        if mode not in DISCUSSION_MODES:
            raise ValueError(f"unknown discussion mode {mode!r}")
        return cls(
            id=discussion_id or str(uuid.uuid4()),
            topic=topic,
            participants=list(participants),
            insights=[],
            created_at=now or datetime.now(),
            mode=mode,
            agent_config=agent_config,
            documents=list(documents) if documents else None,
            owner_sub=owner_sub,
            participant_names=dict(participant_names or {}),
        )

    @property
    def is_legacy(self) -> bool:
        """Saved before v1.0.0: its messages are inside the body (BR1.7)."""
        return self.message_count is None

    def with_insights(
        self, insights: List[Insight], *, now: Optional[datetime] = None
    ) -> "Discussion":
        """Return a copy holding ``insights`` (replacing the old ones)."""
        return replace(self, insights=list(insights), updated_at=now or self.updated_at)

    def to_dict(self) -> Dict[str, Any]:
        """Plain values for storage; ``None`` fields and an empty share set are left out."""
        data: Dict[str, Any] = {
            "id": self.id,
            "topic": self.topic,
            "participants": list(self.participants),
            "insights": [insight.to_dict() for insight in self.insights],
            "created_at": self.created_at.isoformat(),
            "mode": self.mode,
            "participant_names": dict(self.participant_names),
            "report_count": self.report_count,
        }
        optional: Dict[str, Any] = {
            "agent_config": self.agent_config,
            "documents": (
                [doc.to_dict() for doc in self.documents]
                if self.documents is not None
                else None
            ),
            "owner_sub": self.owner_sub,
            "message_count": self.message_count,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        data.update({k: v for k, v in optional.items() if v is not None})
        if self.shared_group_ids:
            data["shared_group_ids"] = sorted(self.shared_group_ids)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Discussion":
        """Read a stored body, in the v1.0.0 shape or the older one (BR1.7)."""
        documents = data.get("documents")
        legacy = "message_count" not in data
        names = dict(data.get("participant_names") or {})
        if not names and legacy:
            names = {
                m.persona_id: m.persona_name
                for m in legacy_messages(data)
                if m.persona_id in (data.get("participants") or [])
            }
        updated_at = data.get("updated_at")
        report_count = data.get("report_count")
        if report_count is None:
            report_count = len(data.get("reports") or [])
        return cls(
            id=str(data["id"]),
            topic=str(data.get("topic") or ""),
            participants=list(data.get("participants") or []),
            insights=[Insight.from_dict(dict(i)) for i in data.get("insights") or []],
            created_at=datetime.fromisoformat(str(data["created_at"])),
            mode=str(data.get("mode") or "classic"),
            agent_config=data.get("agent_config"),
            documents=(
                [DocumentRef.from_dict(d) for d in documents]
                if documents is not None
                else None
            ),
            owner_sub=str(data["owner_sub"]) if data.get("owner_sub") else None,
            shared_group_ids=frozenset(data.get("shared_group_ids") or ()),
            participant_names={str(k): str(v) for k, v in names.items()},
            message_count=None if legacy else int(data["message_count"]),
            report_count=int(report_count),
            updated_at=datetime.fromisoformat(str(updated_at)) if updated_at else None,
        )


def legacy_messages(data: Mapping[str, Any]) -> List[Message]:
    """The messages inside a body saved before v1.0.0 (display only, BR1.7)."""
    return [Message.from_dict(dict(m)) for m in data.get("messages") or []]


def legacy_reports(
    data: Mapping[str, Any], discussion_id: str
) -> List[DiscussionReport]:
    """The reports inside a body saved before v1.0.0 (display only, BR1.7)."""
    return [
        DiscussionReport.from_dict({**dict(r), "discussion_id": discussion_id})
        for r in data.get("reports") or []
    ]


@dataclass(frozen=True)
class DiscussionView:
    """What the reader of a discussion is shown (``DiscussionRecords.get``).

    Attributes:
        discussion: The body.
        messages: The messages in ``seq`` order (display shape).
        reports: The saved reports.
        participants: Each participant's state for this reader (M9).
        manageable: Whether the reader may delete, share and edit it.
    """

    discussion: Discussion
    messages: List[Message]
    reports: List[DiscussionReport]
    participants: List[ParticipantView]
    manageable: bool = False

    # 画面・API が本体の項目をそのまま読めるように（読み取り専用）
    @property
    def id(self) -> str:
        return self.discussion.id

    @property
    def topic(self) -> str:
        return self.discussion.topic

    @property
    def mode(self) -> str:
        return self.discussion.mode

    @property
    def created_at(self) -> datetime:
        return self.discussion.created_at

    @property
    def insights(self) -> List[Insight]:
        return self.discussion.insights

    @property
    def documents(self) -> List[DocumentRef]:
        return list(self.discussion.documents or [])

    @property
    def agent_config(self) -> Optional[Dict[str, Any]]:
        return self.discussion.agent_config

    @property
    def participant_ids(self) -> List[str]:
        return self.discussion.participants
