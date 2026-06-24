"""
Message data model for the AI Persona System.
"""

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, Any, Optional


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

