"""Interview sessions kept outside the app (v1.0.0 U6, ADR-004, entities.md).

A session and its messages live in ``InterviewSessions`` / ``InterviewMessages``
(both with a DynamoDB TTL) so that any task can carry the conversation on
(FR7.2). Only the user who started it may use it (BR3.2).

- The session expires ``min(last_active_at + 8 h, created_at + 7 days)``; the
  check is made on every read, before the TTL removes the item (BR3.5).
- ``reply_token`` / ``reply_started_at`` mark a reply in progress. A mark older
  than five minutes counts as absent, so a crashed task frees the session
  (BR3.3, NFR1.59; ``reply_started_at`` is renewed after each persona reply).
- A message expires eight days after it was written, always after its
  session (BR3.5).
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional
import uuid

from .discussion import DocumentRef
from .message import Message

#: Idle limit of a session (BR3.5).
SESSION_IDLE_SECONDS = 8 * 3600
#: Hard limit from the start of a session (the login's longest, FR1.5).
SESSION_MAX_SECONDS = 7 * 24 * 3600
#: Age after which a reply mark is ignored (BR3.3).
REPLY_TIMEOUT_SECONDS = 5 * 60
#: Lifetime of a stored interview message (longer than any session).
MESSAGE_TTL_SECONDS = 8 * 24 * 3600
#: Memory modes the user can choose.
MEMORY_MODES: tuple[str, ...] = ("full", "retrieve_only", "disabled")
#: Who said an interview message.
SPEAKERS: tuple[str, ...] = ("user", "persona")
#: ``persona_id`` of the user's own messages.
USER_PERSONA_ID = "user"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _epoch(value: datetime) -> int:
    return int(_utc(value).timestamp())


def _parse(value: Any) -> datetime:
    return _utc(datetime.fromisoformat(str(value)))


def stamp(value: datetime) -> str:
    """Comparable UTC text with a fixed precision (stored and compared as is)."""
    return _utc(value).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class InterviewSession:
    """A running interview (entities.md InterviewSession).

    Attributes:
        id: UUID.
        owner_sub: The user who started it.
        persona_ids: 1〜5 participants.
        persona_names: Persona ID -> name at the start.
        memory_mode: The user's choice (``full`` / ``retrieve_only`` /
            ``disabled``); personas the user does not own only retrieve (BR6.1).
        created_at: Start (aware UTC).
        last_active_at: Last send or save (aware UTC).
        expires_at: Epoch seconds; the TTL attribute.
        enable_dataset: Whether dataset tools are used.
        enable_kb: Whether knowledge-base tools are used.
        persona_models: Persona ID -> model ID.
        document_ids: Attached upload IDs (read again on a rebuild, BR3.6).
        documents: Display metadata of the attachments.
        last_seq: Number of the last message (BR3.3).
        reply_token: Mark of a reply in progress.
        reply_started_at: When the mark was set or last renewed.
        saved_discussion_id: The discussion it was saved as (BR3.8).
    """

    id: str
    owner_sub: str
    persona_ids: List[str]
    persona_names: Dict[str, str]
    memory_mode: str
    created_at: datetime
    last_active_at: datetime
    expires_at: int
    enable_dataset: bool = False
    enable_kb: bool = False
    persona_models: Optional[Dict[str, str]] = None
    document_ids: List[str] = field(default_factory=list)
    documents: List[DocumentRef] = field(default_factory=list)
    last_seq: int = 0
    reply_token: Optional[str] = None
    reply_started_at: Optional[datetime] = None
    saved_discussion_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.memory_mode not in MEMORY_MODES:
            raise ValueError(f"unknown memory mode {self.memory_mode!r}")
        if self.last_seq < 0:
            raise ValueError("last_seq must not be negative")

    @classmethod
    def create_new(
        cls,
        *,
        owner_sub: str,
        persona_ids: List[str],
        persona_names: Dict[str, str],
        memory_mode: str,
        now: datetime,
        enable_dataset: bool = False,
        enable_kb: bool = False,
        persona_models: Optional[Dict[str, str]] = None,
    ) -> "InterviewSession":
        """A new session owned by ``owner_sub`` (BR3.1)."""
        now = _utc(now)
        return cls(
            id=str(uuid.uuid4()),
            owner_sub=owner_sub,
            persona_ids=list(persona_ids),
            persona_names=dict(persona_names),
            memory_mode=memory_mode,
            created_at=now,
            last_active_at=now,
            expires_at=_epoch(cls.expiry_for(now, now)),
            enable_dataset=enable_dataset,
            enable_kb=enable_kb,
            persona_models=dict(persona_models) if persona_models else None,
        )

    @staticmethod
    def expiry_for(created_at: datetime, last_active_at: datetime) -> datetime:
        """``min(last_active_at + 8 h, created_at + 7 days)`` (BR3.5)."""
        return min(
            _utc(last_active_at) + timedelta(seconds=SESSION_IDLE_SECONDS),
            _utc(created_at) + timedelta(seconds=SESSION_MAX_SECONDS),
        )

    def is_expired(self, now: datetime) -> bool:
        """Whether the session can no longer be used at ``now``."""
        return _utc(now) >= self.expiry_for(self.created_at, self.last_active_at)

    def reply_in_progress(self, now: datetime) -> bool:
        """A reply mark that is less than five minutes old (BR3.3)."""
        if not self.reply_token or self.reply_started_at is None:
            return False
        age = _utc(now) - _utc(self.reply_started_at)
        return age < timedelta(seconds=REPLY_TIMEOUT_SECONDS)

    def touched(self, now: datetime) -> "InterviewSession":
        """A copy whose last action is ``now`` (the expiry moves with it)."""
        now = _utc(now)
        return replace(
            self,
            last_active_at=now,
            expires_at=_epoch(self.expiry_for(self.created_at, now)),
        )

    @property
    def enable_memory(self) -> bool:
        """Whether long-term memory is used at all."""
        return self.memory_mode != "disabled"

    def to_dict(self) -> Dict[str, Any]:
        """Plain values for storage; ``None`` fields are left out."""
        data: Dict[str, Any] = {
            "id": self.id,
            "owner_sub": self.owner_sub,
            "persona_ids": list(self.persona_ids),
            "persona_names": dict(self.persona_names),
            "memory_mode": self.memory_mode,
            "created_at": _utc(self.created_at).isoformat(timespec="microseconds"),
            "last_active_at": _utc(self.last_active_at).isoformat(
                timespec="microseconds"
            ),
            "expires_at": self.expires_at,
            "enable_dataset": self.enable_dataset,
            "enable_kb": self.enable_kb,
            "document_ids": list(self.document_ids),
            "documents": [doc.to_dict() for doc in self.documents],
            "last_seq": self.last_seq,
        }
        optional: Dict[str, Any] = {
            "persona_models": self.persona_models,
            "reply_token": self.reply_token,
            "reply_started_at": (
                _utc(self.reply_started_at).isoformat(timespec="microseconds")
                if self.reply_started_at
                else None
            ),
            "saved_discussion_id": self.saved_discussion_id,
        }
        data.update({k: v for k, v in optional.items() if v is not None})
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InterviewSession":
        """Read a stored session."""
        started = data.get("reply_started_at")
        return cls(
            id=str(data["id"]),
            owner_sub=str(data.get("owner_sub") or ""),
            persona_ids=[str(p) for p in data.get("persona_ids") or []],
            persona_names={
                str(k): str(v) for k, v in (data.get("persona_names") or {}).items()
            },
            memory_mode=str(data.get("memory_mode") or "disabled"),
            created_at=_parse(data["created_at"]),
            last_active_at=_parse(data["last_active_at"]),
            expires_at=int(data["expires_at"]),
            enable_dataset=bool(data.get("enable_dataset", False)),
            enable_kb=bool(data.get("enable_kb", False)),
            persona_models=dict(data["persona_models"])
            if data.get("persona_models")
            else None,
            document_ids=[str(i) for i in data.get("document_ids") or []],
            documents=[DocumentRef.from_dict(d) for d in data.get("documents") or []],
            last_seq=int(data.get("last_seq") or 0),
            reply_token=str(data["reply_token"]) if data.get("reply_token") else None,
            reply_started_at=_parse(started) if started else None,
            saved_discussion_id=(
                str(data["saved_discussion_id"])
                if data.get("saved_discussion_id")
                else None
            ),
        )


@dataclass(frozen=True)
class InterviewMessage:
    """One message of a running interview (entities.md InterviewMessage).

    Read only after the session's owner and expiry were checked.
    """

    session_id: str
    seq: int
    speaker: str
    persona_id: str
    persona_name_at_time: str
    content: str
    created_at: datetime
    expires_at: int
    document_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.seq < 1:
            raise ValueError("seq starts at 1")
        if self.speaker not in SPEAKERS:
            raise ValueError(f"unknown speaker {self.speaker!r}")

    @classmethod
    def create_new(
        cls,
        *,
        session_id: str,
        seq: int,
        speaker: str,
        persona_id: str,
        persona_name_at_time: str,
        content: str,
        now: datetime,
        document_ids: tuple[str, ...] = (),
    ) -> "InterviewMessage":
        """A new message that expires eight days from ``now`` (BR3.5)."""
        now = _utc(now)
        return cls(
            session_id=session_id,
            seq=seq,
            speaker=speaker,
            persona_id=persona_id,
            persona_name_at_time=persona_name_at_time,
            content=content,
            created_at=now,
            expires_at=_epoch(now + timedelta(seconds=MESSAGE_TTL_SECONDS)),
            document_ids=tuple(document_ids),
        )

    @property
    def is_user(self) -> bool:
        """Whether the user said it."""
        return self.speaker == "user"

    def to_message(self) -> Message:
        """The display / discussion shape."""
        return Message(
            persona_id=self.persona_id,
            persona_name=self.persona_name_at_time,
            content=self.content,
            timestamp=self.created_at,
            message_type="user_message" if self.is_user else "statement",
        )

    def to_dict(self) -> Dict[str, Any]:
        """Plain values for storage."""
        data: Dict[str, Any] = {
            "session_id": self.session_id,
            "seq": self.seq,
            "speaker": self.speaker,
            "persona_id": self.persona_id,
            "persona_name_at_time": self.persona_name_at_time,
            "content": self.content,
            "created_at": _utc(self.created_at).isoformat(timespec="microseconds"),
            "expires_at": self.expires_at,
        }
        if self.document_ids:
            data["document_ids"] = list(self.document_ids)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InterviewMessage":
        """Read a stored message."""
        return cls(
            session_id=str(data["session_id"]),
            seq=int(data["seq"]),
            speaker=str(data["speaker"]),
            persona_id=str(data["persona_id"]),
            persona_name_at_time=str(data.get("persona_name_at_time") or ""),
            content=str(data.get("content") or ""),
            created_at=_parse(data["created_at"]),
            expires_at=int(data["expires_at"]),
            document_ids=tuple(str(i) for i in data.get("document_ids") or []),
        )


@dataclass(frozen=True)
class InterviewView:
    """What the owner of a session is shown (the chat screen, its status)."""

    session: InterviewSession
    messages: List[Message]

    @property
    def id(self) -> str:
        return self.session.id

    @property
    def persona_ids(self) -> List[str]:
        return self.session.persona_ids

    @property
    def documents(self) -> List[DocumentRef]:
        return self.session.documents

    @property
    def is_saved(self) -> bool:
        return self.session.saved_discussion_id is not None


@dataclass(frozen=True)
class InterviewReply:
    """The replies to one send (BR3.3, BR3.6).

    Attributes:
        responses: One message per persona that answered.
        excluded_files: Names of attachments that could no longer be read and
            were left out of the conversation.
    """

    responses: List[Message]
    excluded_files: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class InterviewSaveResult:
    """A saved interview (BR3.7, BR3.8).

    Attributes:
        discussion_id: The ``mode=interview`` discussion it is saved as.
        excluded_files: Names of attachments that were no longer usable and
            were not attached.
    """

    discussion_id: str
    excluded_files: List[str] = field(default_factory=list)
