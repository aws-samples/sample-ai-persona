"""
Summary Memory Strategy Implementation
Summary strategy implementation

Uses the AgentCore Memory Summary strategy to store and retrieve discussion summaries as long-term memory.
Works with AgentCore Memory through MemoryClient.
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

from src.models.memory import MemoryEntry

from .strategy import (
    MemoryStrategy,
    is_under_namespaces,
    list_all_record_summaries,
    record_namespaces,
)

logger = logging.getLogger(__name__)


class MemoryOperationError(Exception):
    """Memory operation error."""

    pass


class SummaryStrategy(MemoryStrategy):
    """
    Summary memory strategy implementation

    Uses the AgentCore Memory Summary strategy to store discussion summaries as long-term memory.
    namespace pattern: /strategies/{memoryStrategyId}/actors/{actorId}/sessions/{sessionId}

    Attributes:
        memory_id: AgentCore Memory resource ID
        region: AWS region
        memory_strategy_id: memoryStrategyId used in the namespace
    """

    # Namespace pattern for summary strategy (must start with /)
    NAMESPACE_PATTERN = (
        "/strategies/{memoryStrategyId}/actors/{actorId}/sessions/{sessionId}"
    )

    def __init__(
        self,
        memory_id: str,
        region: str = "us-east-1",
        memory_strategy_id: Optional[str] = None,
    ):
        """
        Initialize SummaryStrategy

        Args:
            memory_id: AgentCore Memory resource ID
            region: AWS region (default: us-east-1)
            memory_strategy_id: memoryStrategyId used in the namespace (default: taken from config)
        """
        from src.config import config

        self.memory_id = memory_id
        self.region = region

        # If memory_strategy_id is not given, take it from config
        if memory_strategy_id is None:
            self.memory_strategy_id = config.SUMMARY_MEMORY_STRATEGY_ID
        else:
            self.memory_strategy_id = memory_strategy_id

        self._client: Any = None
        self._init_client()

    def _init_client(self) -> None:
        """Initialize the AgentCore Memory Client."""
        try:
            from bedrock_agentcore.memory import MemoryClient

            self._client = MemoryClient(region_name=self.region)
            logger.info(
                "Initialized SummaryStrategy with memory_id=%s, region=%s",
                self.memory_id,
                self.region,
            )
        except ImportError as e:
            logger.error("Failed to import bedrock_agentcore: %s", e)
            raise MemoryOperationError(
                "bedrock_agentcore package is not installed. "
                "Please install it with: pip install bedrock-agentcore"
            ) from e
        except Exception as e:
            logger.error("Failed to initialize MemoryClient: %s", e)
            raise MemoryOperationError(f"Failed to initialize MemoryClient: {e}") from e

    def _build_namespace(self, actor_id: str, session_id: str) -> str:
        """
        Build the namespace

        Args:
            actor_id: Persona ID
            session_id: Discussion ID

        Returns:
            The built namespace string
        """
        return self.NAMESPACE_PATTERN.format(
            memoryStrategyId=self.memory_strategy_id,
            actorId=actor_id,
            sessionId=session_id,
        )

    def namespaces_for(self, actor_id: str) -> List[str]:
        """Namespaces that hold every summary record of ``actor_id``."""
        return [self._build_actor_namespace_prefix(actor_id)]

    def _build_actor_namespace_prefix(self, actor_id: str) -> str:
        """
        Build a namespace prefix from actor_id

        Args:
            actor_id: Persona ID

        Returns:
            Namespace prefix
        """
        return f"/strategies/{self.memory_strategy_id}/actors/{actor_id}"

    def save(
        self,
        actor_id: str,
        session_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Save a discussion summary as long-term memory

        Creates an event with AgentCore Memory's create_event, and the
        Summary strategy then extracts long-term memory automatically.

        Args:
            actor_id: Persona ID (used as the actorId in AgentCore Memory)
            session_id: Discussion ID (used as the sessionId in AgentCore Memory)
            content: Content of the memory to save
            metadata: Additional metadata (importance, type, etc.)

        Returns:
            ID of the saved event

        Raises:
            MemoryOperationError: If saving fails
        """
        try:
            # Create event with the content as a conversation turn
            # This triggers LTM extraction via the summary strategy
            event = self._client.create_event(
                memory_id=self.memory_id,
                actor_id=actor_id,
                session_id=session_id,
                messages=[
                    (
                        content,
                        "ASSISTANT",
                    )  # Store as assistant message for summary extraction
                ],
            )

            event_id = event.get("eventId", str(uuid.uuid4()))
            logger.info(
                "Saved memory event for actor=%s, session=%s, event_id=%s",
                actor_id,
                session_id,
                event_id,
            )

            return str(event_id)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))
            logger.error(
                "Failed to save memory: actor=%s, session=%s, error=%s: %s",
                actor_id,
                session_id,
                error_code,
                error_msg,
            )
            raise MemoryOperationError(f"Failed to save memory: {error_msg}") from e
        except Exception as e:
            logger.error("Unexpected error saving memory: %s", e)
            raise MemoryOperationError(f"Failed to save memory: {e}") from e

    def retrieve(self, actor_id: str, query: str, top_k: int = 5) -> List[MemoryEntry]:
        """
        Retrieve related memories by semantic search

        Args:
            actor_id: Persona ID
            query: Search query (topic or keywords)
            top_k: Maximum number of results (default: 5)

        Returns:
            List of related memory entries (by relevance score, descending)

        Raises:
            MemoryOperationError: If the search fails
        """
        try:
            namespace_prefix = self._build_actor_namespace_prefix(actor_id)

            # Use retrieve_memories for semantic search
            memories = self._client.retrieve_memories(
                memory_id=self.memory_id,
                namespace=namespace_prefix,
                query=query,
                top_k=top_k,
            )

            # Convert API response to MemoryEntry objects
            entries = []
            for memory in memories:
                entry = self._convert_to_memory_entry(memory, actor_id)
                if entry:
                    entries.append(entry)

            # Sort by relevance score (descending)
            entries.sort(key=lambda x: x.relevance_score or 0, reverse=True)

            logger.info(
                "Retrieved %d memories for actor=%s, query='%s'",
                len(entries),
                actor_id,
                query[:50],
            )

            return entries

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))

            # Handle specific error cases gracefully
            if error_code == "ResourceNotFoundException":
                logger.warning(
                    "Memory or namespace not found for actor=%s: %s",
                    actor_id,
                    error_msg,
                )
                return []

            logger.error(
                "Failed to retrieve memories: actor=%s, error=%s: %s",
                actor_id,
                error_code,
                error_msg,
            )
            raise MemoryOperationError(
                f"Failed to retrieve memories: {error_msg}"
            ) from e
        except Exception as e:
            logger.error("Unexpected error retrieving memories: %s", e)
            raise MemoryOperationError(f"Failed to retrieve memories: {e}") from e

    def delete(self, actor_id: str, memory_id: str) -> bool:
        """
        Delete a specific memory

        Args:
            actor_id: Persona ID
            memory_id: ID of the memory to delete (memoryRecordId)

        Returns:
            True if deletion succeeded, False if the memory was not found

        Raises:
            MemoryOperationError: If deletion fails
        """
        try:
            # Only a record under this actor's namespace is deleted
            found = self._client.get_memory_record(
                memoryId=self.memory_id, memoryRecordId=memory_id
            )
            if not is_under_namespaces(
                record_namespaces(found), self.namespaces_for(actor_id)
            ):
                logger.warning(
                    "Memory record is not the actor's: actor=%s, memory_id=%s",
                    actor_id,
                    memory_id,
                )
                return False

            # Use delete_memory_record to delete a specific memory record
            self._client.delete_memory_record(
                memoryId=self.memory_id, memoryRecordId=memory_id
            )

            logger.info(
                "Deleted memory record: actor=%s, memory_id=%s", actor_id, memory_id
            )

            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))

            if error_code == "ResourceNotFoundException":
                logger.warning(
                    "Memory record not found: actor=%s, memory_id=%s",
                    actor_id,
                    memory_id,
                )
                return False

            logger.error(
                "Failed to delete memory: actor=%s, memory_id=%s, error=%s: %s",
                actor_id,
                memory_id,
                error_code,
                error_msg,
            )
            raise MemoryOperationError(f"Failed to delete memory: {error_msg}") from e
        except Exception as e:
            logger.error("Unexpected error deleting memory: %s", e)
            raise MemoryOperationError(f"Failed to delete memory: {e}") from e

    def list_all(self, actor_id: str) -> List[MemoryEntry]:
        """
        Get all memories of a persona

        Args:
            actor_id: Persona ID

        Returns:
            List of all memory entries related to the persona

        Raises:
            MemoryOperationError: If retrieval fails
        """
        try:
            namespace_prefix = self._build_actor_namespace_prefix(actor_id)

            # Use list_memory_records to get all records without semantic search
            all_records = list_all_record_summaries(
                self._client, self.memory_id, namespace_prefix
            )

            # Convert API response to MemoryEntry objects
            entries = []
            for record in all_records:
                entry = self._convert_to_memory_entry(record, actor_id)
                if entry:
                    entries.append(entry)

            logger.info("Listed %d memories for actor=%s", len(entries), actor_id)

            return entries

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))

            if error_code == "ResourceNotFoundException":
                logger.warning(
                    "Memory or namespace not found for actor=%s: %s",
                    actor_id,
                    error_msg,
                )
                return []

            logger.error(
                "Failed to list memories: actor=%s, error=%s: %s",
                actor_id,
                error_code,
                error_msg,
            )
            raise MemoryOperationError(f"Failed to list memories: {error_msg}") from e
        except Exception as e:
            logger.error("Unexpected error listing memories: %s", e)
            raise MemoryOperationError(f"Failed to list memories: {e}") from e

    def _convert_to_memory_entry(
        self, record: Dict[str, Any], actor_id: str
    ) -> Optional[MemoryEntry]:
        """
        Convert an API response to a MemoryEntry

        Args:
            record: Response record from the AgentCore Memory API
            actor_id: Persona ID

        Returns:
            A MemoryEntry object, or None if conversion fails
        """
        try:
            # Extract memory record ID
            memory_record_id = record.get("memoryRecordId", str(uuid.uuid4()))

            # Extract content from the record
            content_data = record.get("content", {})
            if isinstance(content_data, dict):
                content = content_data.get("text", "")
            else:
                content = str(content_data) if content_data else ""

            # Extract session_id from namespace if available
            namespace = record.get("namespace", "")
            session_id = self._extract_session_id_from_namespace(namespace)

            # Extract timestamp
            created_at_str = record.get("createdAt") or record.get("eventTimestamp")
            if created_at_str:
                if isinstance(created_at_str, datetime):
                    created_at = created_at_str
                else:
                    created_at = datetime.fromisoformat(
                        str(created_at_str).replace("Z", "+00:00")
                    )
            else:
                created_at = datetime.now()

            # Extract relevance score if available
            relevance_score = record.get("relevanceScore")

            # Build metadata
            metadata = {
                "namespace": namespace,
                "strategy_id": record.get("memoryStrategyId"),
                "strategy_type": "summary",  # Identify the strategy type
            }

            return MemoryEntry(
                id=memory_record_id,
                actor_id=actor_id,
                session_id=session_id,
                content=content,
                metadata=metadata,
                created_at=created_at,
                relevance_score=relevance_score,
            )

        except Exception as e:
            logger.warning("Failed to convert record to MemoryEntry: %s", e)
            return None

    def _extract_session_id_from_namespace(self, namespace: str) -> str:
        """
        Extract the session_id from a namespace

        Args:
            namespace: Namespace string (e.g. "/strategies/summary/actors/actor123/sessions/session456")

        Returns:
            The extracted session_id, or an empty string if it cannot be extracted
        """
        if not namespace:
            return ""

        # New pattern: /strategies/{memoryStrategyId}/actors/{actorId}/sessions/{sessionId}
        parts = namespace.strip("/").split("/")
        # parts = ["strategies", "{memoryStrategyId}", "actors", "{actorId}", "sessions", "{sessionId}"]
        if (
            len(parts) >= 6
            and parts[0] == "strategies"
            and parts[2] == "actors"
            and parts[4] == "sessions"
        ):
            return parts[5]

        # Old pattern (backward compatibility): summaries/{actorId}/{sessionId}
        if len(parts) >= 3 and parts[0] == "summaries":
            return parts[2]

        return ""
