"""
Discussion data model for the AI Persona System.
"""

from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import List, Dict, Any, Optional
import json
import uuid

from .message import Message
from .insight import Insight
from .discussion_report import DiscussionReport


@dataclass
class Discussion:
    """
    Represents a discussion between multiple personas.
    """

    id: str
    topic: str
    participants: List[str]  # persona_ids
    messages: List[Message]
    insights: List[Insight]
    created_at: datetime
    mode: str = "classic"  # "classic", "agent", or "interview"
    agent_config: Optional[Dict[str, Any]] = None  # AI agent mode configuration
    documents: Optional[List[Dict[str, Any]]] = None  # Attached documents metadata
    reports: List[DiscussionReport] = field(default_factory=list)  # Generated reports

    @classmethod
    def create_new(
        cls,
        topic: str,
        participants: List[str],
        mode: str = "classic",
        agent_config: Optional[Dict[str, Any]] = None,
        documents: Optional[List[Dict[str, Any]]] = None,
    ) -> "Discussion":
        """
        Create a new Discussion instance with auto-generated ID and timestamp.
        """
        return cls(
            id=str(uuid.uuid4()),
            topic=topic,
            participants=participants,
            messages=[],
            insights=[],
            created_at=datetime.now(),
            mode=mode,
            agent_config=agent_config,
            documents=documents,
        )

    @classmethod
    def create_interview_session(cls, participants: List[str]) -> "Discussion":
        """
        Create a new interview session with specified participants.

        Args:
            participants: List of persona IDs participating in the interview

        Returns:
            Discussion instance configured for interview mode
        """
        return cls.create_new(
            topic="Interview Session", participants=participants, mode="interview"
        )

    def add_message(self, message: Message) -> "Discussion":
        """
        Add a message to the discussion and return a new instance.
        """
        new_messages = self.messages + [message]
        return Discussion(
            id=self.id,
            topic=self.topic,
            participants=self.participants,
            messages=new_messages,
            insights=self.insights,
            created_at=self.created_at,
            mode=self.mode,
            agent_config=self.agent_config,
            documents=self.documents,
            reports=self.reports,
        )

    def add_insight(self, insight: Insight) -> "Discussion":
        """
        Add an insight to the discussion and return a new instance.
        """
        new_insights = self.insights + [insight]
        return Discussion(
            id=self.id,
            topic=self.topic,
            participants=self.participants,
            messages=self.messages,
            insights=new_insights,
            created_at=self.created_at,
            mode=self.mode,
            agent_config=self.agent_config,
            documents=self.documents,
            reports=self.reports,
        )

    def get_messages_by_persona(self, persona_id: str) -> List[Message]:
        """
        Get all messages from a specific persona.
        """
        return [msg for msg in self.messages if msg.persona_id == persona_id]

    def add_user_message(self, content: str) -> "Discussion":
        """
        Add a user message to the interview session and return a new instance.

        Args:
            content: The user's message content

        Returns:
            New Discussion instance with the user message added
        """
        user_message = Message.create_new(
            persona_id="user",
            persona_name="User",
            content=content,
            message_type="user_message",
        )
        return self.add_message(user_message)

    def add_persona_response(
        self, persona_id: str, persona_name: str, content: str
    ) -> "Discussion":
        """
        Add a persona response to the interview session and return a new instance.

        Args:
            persona_id: ID of the responding persona
            persona_name: Name of the responding persona
            content: The persona's response content

        Returns:
            New Discussion instance with the persona response added
        """
        response_message = Message.create_new(
            persona_id=persona_id,
            persona_name=persona_name,
            content=content,
            message_type="statement",
        )
        return self.add_message(response_message)

    def is_interview_session(self) -> bool:
        """
        Check if this discussion is an interview session.

        Returns:
            True if this is an interview session, False otherwise
        """
        return self.mode == "interview"

    def get_user_messages(self) -> List[Message]:
        """
        Get all user messages from the interview session.

        Returns:
            List of messages from the user
        """
        return [msg for msg in self.messages if msg.message_type == "user_message"]

    def get_persona_responses(self) -> List[Message]:
        """
        Get all persona responses from the interview session.

        Returns:
            List of messages from personas (excluding user messages)
        """
        return [msg for msg in self.messages if msg.message_type != "user_message"]

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert Discussion to dictionary for serialization.
        """
        data = asdict(self)
        # Convert datetime object to ISO format string
        data["created_at"] = self.created_at.isoformat()
        # Convert nested objects to dictionaries
        data["messages"] = [msg.to_dict() for msg in self.messages]
        data["insights"] = [insight.to_dict() for insight in self.insights]
        data["reports"] = [report.to_dict() for report in self.reports]
        # agent_config is already a dict or None, no conversion needed
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Discussion":
        """
        Create Discussion instance from dictionary.
        """
        # Convert ISO format string back to datetime object
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        # Convert nested dictionaries back to objects
        data["messages"] = [
            Message.from_dict(msg_data) for msg_data in data["messages"]
        ]
        data["insights"] = [
            Insight.from_dict(insight_data) for insight_data in data["insights"]
        ]
        # Handle backward compatibility: set default values if fields are missing
        data.setdefault("mode", "classic")
        data.setdefault("agent_config", None)
        data.setdefault("documents", None)
        data["reports"] = [
            DiscussionReport.from_dict(r) for r in data.get("reports", [])
        ]
        return cls(**data)

    def to_json(self) -> str:
        """
        Convert Discussion to JSON string.
        """
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "Discussion":
        """
        Create Discussion instance from JSON string.
        """
        data = json.loads(json_str)
        return cls.from_dict(data)
