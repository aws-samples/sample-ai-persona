"""インタビューセッションモデル。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Any, List, Optional

from .message import Message


@dataclass
class InterviewSession:
    """アクティブなインタビューセッションを表す。"""

    id: str
    participants: List[str]  # persona_ids
    messages: List[Message]
    created_at: datetime
    is_saved: bool = False
    enable_memory: bool = False
    documents: Optional[List[Dict[str, Any]]] = None  # Attached documents metadata
    memory_mode: str = "full"  # "full", "retrieve_only", or "disabled"
    enable_dataset: bool = False  # Whether external dataset access is enabled

    def add_user_message(self, content: str) -> "InterviewSession":
        """
        Add a user message to the interview session.

        Args:
            content: User message content

        Returns:
            New InterviewSession instance with the user message added
        """
        user_message = Message.create_new(
            persona_id="user",
            persona_name="User",
            content=content,
            message_type="user_message",
        )

        new_messages = self.messages + [user_message]
        return InterviewSession(
            id=self.id,
            participants=self.participants,
            messages=new_messages,
            created_at=self.created_at,
            is_saved=self.is_saved,
            enable_memory=self.enable_memory,
            documents=self.documents,
            memory_mode=self.memory_mode,
            enable_dataset=self.enable_dataset,
        )

    def add_persona_response(
        self, persona_id: str, persona_name: str, content: str
    ) -> "InterviewSession":
        """
        Add a persona response to the interview session.

        Args:
            persona_id: ID of the responding persona
            persona_name: Name of the responding persona
            content: Persona response content

        Returns:
            New InterviewSession instance with the persona response added
        """
        response_message = Message.create_new(
            persona_id=persona_id,
            persona_name=persona_name,
            content=content,
            message_type="statement",
        )

        new_messages = self.messages + [response_message]
        return InterviewSession(
            id=self.id,
            participants=self.participants,
            messages=new_messages,
            created_at=self.created_at,
            is_saved=self.is_saved,
            enable_memory=self.enable_memory,
            documents=self.documents,
            memory_mode=self.memory_mode,
            enable_dataset=self.enable_dataset,
        )

    def add_document(self, document_metadata: Dict[str, Any]) -> "InterviewSession":
        """
        Add a document to the interview session.

        Args:
            document_metadata: Document metadata (filename, mime_type, file_size, etc.)

        Returns:
            New InterviewSession instance with the document added
        """
        current_docs = self.documents or []
        new_documents = current_docs + [document_metadata]
        return InterviewSession(
            id=self.id,
            participants=self.participants,
            messages=self.messages,
            created_at=self.created_at,
            is_saved=self.is_saved,
            enable_memory=self.enable_memory,
            documents=new_documents,
            memory_mode=self.memory_mode,
            enable_dataset=self.enable_dataset,
        )
