"""
Message data model for the AI Persona System.

``Message`` is the in-memory and display shape of one utterance.
``DiscussionMessage`` (v1.0.0 U6) is how a saved discussion keeps it in
``DiscussionMessages``: keyed by the discussion and a sequence number, with
the speaker's name at the time it was said (entities.md DiscussionMessage).
"""

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, Any, Mapping, Optional

#: The kinds of messages a discussion stores.
MESSAGE_TYPES: tuple[str, ...] = (
    "statement",
    "summary",
    "facilitation",
    "user_message",
)


@dataclass
class Message:
    """
    Represents a message in a discussion between personas.
    """

    persona_id: str
    persona_name: str
    content: str
    timestamp: datetime
    message_type: str = (
        "statement"  # "statement", "summary", "facilitation", "user_message"
    )
    round_number: Optional[int] = None  # Round number for agent mode discussions

    @classmethod
    def create_new(
        cls,
        persona_id: str,
        persona_name: str,
        content: str,
        message_type: str = "statement",
        round_number: Optional[int] = None,
    ) -> "Message":
        """
        Create a new Message instance with current timestamp.

        Args:
            persona_id: ID of the persona
            persona_name: Name of the persona
            content: Message content
            message_type: Type of message ("statement", "summary", "facilitation")
            round_number: Round number for agent mode discussions
        """
        return cls(
            persona_id=persona_id,
            persona_name=persona_name,
            content=content,
            timestamp=datetime.now(),
            message_type=message_type,
            round_number=round_number,
        )

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert Message to dictionary for serialization.
        """
        data = asdict(self)
        # Convert datetime object to ISO format string
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        """
        Create Message instance from dictionary.
        """
        # Convert ISO format string back to datetime object
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)


@dataclass(frozen=True)
class DiscussionMessage:
    """One saved message of a discussion (``DiscussionMessages``, ADR-002).

    It has no owner: it is read only through its discussion's body, after the
    body passed ``can_read`` (BR1.3).

    Attributes:
        discussion_id: The discussion.
        seq: Order inside the discussion, from 1.
        persona_id: The speaker (``user`` / ``facilitator`` for those roles).
        persona_name_at_time: The speaker's name when it was said.
        content: The text.
        message_type: One of :data:`MESSAGE_TYPES`.
        created_at: When it was said.
        round_number: The round of an agent discussion.
    """

    discussion_id: str
    seq: int
    persona_id: str
    persona_name_at_time: str
    content: str
    message_type: str
    created_at: datetime
    round_number: Optional[int] = None

    def __post_init__(self) -> None:
        if self.seq < 1:
            raise ValueError("seq starts at 1")
        if self.message_type not in MESSAGE_TYPES:
            raise ValueError(f"unknown message type {self.message_type!r}")

    @classmethod
    def from_message(
        cls, discussion_id: str, seq: int, message: Message
    ) -> "DiscussionMessage":
        """The stored form of ``message`` as the ``seq``-th of ``discussion_id``."""
        return cls(
            discussion_id=discussion_id,
            seq=seq,
            persona_id=message.persona_id,
            persona_name_at_time=message.persona_name,
            content=message.content,
            message_type=message.message_type,
            created_at=message.timestamp,
            round_number=message.round_number,
        )

    def to_message(self) -> Message:
        """The display shape."""
        return Message(
            persona_id=self.persona_id,
            persona_name=self.persona_name_at_time,
            content=self.content,
            timestamp=self.created_at,
            message_type=self.message_type,
            round_number=self.round_number,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Plain values for storage (``round_number`` only when set)."""
        data: Dict[str, Any] = {
            "discussion_id": self.discussion_id,
            "seq": self.seq,
            "persona_id": self.persona_id,
            "persona_name_at_time": self.persona_name_at_time,
            "content": self.content,
            "message_type": self.message_type,
            "created_at": self.created_at.isoformat(),
        }
        if self.round_number is not None:
            data["round_number"] = self.round_number
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DiscussionMessage":
        """Read a stored message."""
        round_number = data.get("round_number")
        return cls(
            discussion_id=str(data["discussion_id"]),
            seq=int(data["seq"]),
            persona_id=str(data["persona_id"]),
            persona_name_at_time=str(data.get("persona_name_at_time") or ""),
            content=str(data.get("content") or ""),
            message_type=str(data.get("message_type") or "statement"),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            round_number=int(round_number) if round_number is not None else None,
        )
