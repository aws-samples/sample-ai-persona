"""
Semantic Memory Strategy Implementation
Semantic strategy implementation

Uses the AgentCore Memory Semantic strategy to store and retrieve factual information as long-term memory.
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
from .summary_strategy import MemoryOperationError

logger = logging.getLogger(__name__)


class SemanticStrategy(MemoryStrategy):
    """
    Semantic memory strategy implementation

    Uses the AgentCore Memory Semantic strategy to store factual information as long-term memory.
    namespace pattern: /strategies/{memoryStrategyId}/actors/{actorId}

    Attributes:
        memory_id: AgentCore Memory resource ID
        region: AWS region
        memory_strategy_id: memoryStrategyId used in the namespace
    """

    # Namespace pattern for semantic strategy
    NAMESPACE_PATTERN = "/strategies/{memoryStrategyId}/actors/{actorId}"

    def __init__(
        self,
        memory_id: str,
        region: str = "us-east-1",
        memory_strategy_id: Optional[str] = None,
    ):
        """
        Initialize SemanticStrategy

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
            self.memory_strategy_id = config.SEMANTIC_MEMORY_STRATEGY_ID
        else:
            self.memory_strategy_id = memory_strategy_id

        self._client: Any = None
        if self.memory_strategy_id:
            self._init_client()

    def _init_client(self) -> None:
        """Initialize the AgentCore Memory Client."""
        try:
            from bedrock_agentcore.memory import MemoryClient

            self._client = MemoryClient(region_name=self.region)
            logger.info(
                "Initialized SemanticStrategy with memory_id=%s, region=%s, strategy_id=%s",
                self.memory_id,
                self.region,
                self.memory_strategy_id,
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

    def _build_namespace(self, actor_id: str, session_id: str = "") -> str:
        """
        Build the namespace

        Args:
            actor_id: Persona ID
            session_id: Discussion ID (unused by the Semantic strategy)

        Returns:
            The built namespace string
        """
        return self.NAMESPACE_PATTERN.format(
            memoryStrategyId=self.memory_strategy_id, actorId=actor_id
        )

    def namespaces_for(self, actor_id: str) -> List[str]:
        """Namespaces that hold every semantic record of ``actor_id``."""
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
        Save factual information as long-term memory

        Note: the Semantic strategy normally extracts facts from conversations
        automatically, so direct saving is rare.

        Args:
            actor_id: Persona ID
            session_id: Discussion ID
            content: Content of the memory to save
            metadata: Additional metadata

        Returns:
            ID of the saved event
        """
        if not self._client:
            raise MemoryOperationError(
                "SemanticStrategy not initialized (no strategy_id)"
            )

        try:
            event = self._client.create_event(
                memory_id=self.memory_id,
                actor_id=actor_id,
                session_id=session_id,
                messages=[(content, "ASSISTANT")],
            )

            event_id = event.get("eventId", str(uuid.uuid4()))
            logger.info(
                "Saved semantic memory event for actor=%s, event_id=%s",
                actor_id,
                event_id,
            )

            return str(event_id)

        except Exception as e:
            logger.error("Failed to save semantic memory: %s", e)
            raise MemoryOperationError(f"Failed to save memory: {e}") from e

    def save_directly_to_ltm(
        self, actor_id: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Save directly to long-term memory (LTM) without going through short-term memory

        Uses the BatchCreateMemoryRecords API to create memory records
        directly in the Semantic strategy namespace.

        Args:
            actor_id: Persona ID
            content: Content of the memory to save
            metadata: Additional metadata

        Returns:
            ID of the saved memory record
        """
        import boto3

        if not self._client:
            raise MemoryOperationError(
                "SemanticStrategy not initialized (no strategy_id)"
            )

        try:
            # Use the boto3 client directly (MemoryClient has no batch_create_memory_records)
            gmdp_client = boto3.client("bedrock-agentcore", region_name=self.region)

            # Build the Semantic strategy namespace
            namespace = self._build_namespace(actor_id)

            # Generate a unique request identifier
            request_id = f"manual-{uuid.uuid4().hex[:16]}"

            # Timestamp
            timestamp = datetime.now()

            # Call the BatchCreateMemoryRecords API
            response = gmdp_client.batch_create_memory_records(
                memoryId=self.memory_id,
                records=[
                    {
                        "content": {"text": content},
                        "namespaces": [namespace],
                        "requestIdentifier": request_id,
                        "timestamp": timestamp,
                        "memoryStrategyId": self.memory_strategy_id,
                    }
                ],
            )

            # Check the successful records
            successful_records = response.get("successfulRecords", [])
            failed_records = response.get("failedRecords", [])

            if failed_records:
                error_msg = failed_records[0].get("errorMessage", "Unknown error")
                logger.error(f"Failed to create memory record: {error_msg}")
                raise MemoryOperationError(
                    f"Failed to create memory record: {error_msg}"
                )

            if successful_records:
                memory_record_id = successful_records[0].get(
                    "memoryRecordId", request_id
                )
                logger.info(
                    "Directly saved to LTM for actor=%s, memory_record_id=%s, namespace=%s",
                    actor_id,
                    memory_record_id,
                    namespace,
                )
                return str(memory_record_id)

            # Neither success nor failure (normally does not happen)
            logger.warning("No records returned from batch_create_memory_records")
            return request_id

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))
            logger.error(
                f"AWS API error saving directly to LTM: {error_code} - {error_msg}"
            )
            raise MemoryOperationError(f"Failed to save memory: {error_msg}") from e
        except Exception as e:
            logger.error(f"Failed to save directly to LTM: {e}")
            raise MemoryOperationError(f"Failed to save memory: {e}") from e

    def retrieve(self, actor_id: str, query: str, top_k: int = 5) -> List[MemoryEntry]:
        """
        Retrieve related factual information by semantic search

        Args:
            actor_id: Persona ID
            query: Search query
            top_k: Maximum number of results

        Returns:
            List of related memory entries
        """
        if not self._client:
            return []

        try:
            namespace_prefix = self._build_actor_namespace_prefix(actor_id)

            memories = self._client.retrieve_memories(
                memory_id=self.memory_id,
                namespace=namespace_prefix,
                query=query,
                top_k=top_k,
            )

            entries = []
            for memory in memories:
                entry = self._convert_to_memory_entry(memory, actor_id)
                if entry:
                    entries.append(entry)

            entries.sort(key=lambda x: x.relevance_score or 0, reverse=True)

            logger.info(
                "Retrieved %d semantic memories for actor=%s", len(entries), actor_id
            )

            return entries

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "ResourceNotFoundException":
                logger.warning("Semantic namespace not found for actor=%s", actor_id)
                return []
            raise MemoryOperationError(f"Failed to retrieve memories: {e}") from e
        except Exception as e:
            logger.error("Unexpected error retrieving semantic memories: %s", e)
            raise MemoryOperationError(f"Failed to retrieve memories: {e}") from e

    def delete(self, actor_id: str, memory_id: str) -> bool:
        """
        Delete a specific memory

        Args:
            actor_id: Persona ID
            memory_id: ID of the memory to delete

        Returns:
            True if deletion succeeded
        """
        if not self._client:
            return False

        try:
            # Only a record under this actor's namespace is deleted
            found = self._client.get_memory_record(
                memoryId=self.memory_id, memoryRecordId=memory_id
            )
            if not is_under_namespaces(
                record_namespaces(found), self.namespaces_for(actor_id)
            ):
                logger.warning(
                    "Semantic record is not the actor's: actor=%s, memory_id=%s",
                    actor_id,
                    memory_id,
                )
                return False

            self._client.delete_memory_record(
                memoryId=self.memory_id, memoryRecordId=memory_id
            )

            logger.info(
                "Deleted semantic memory: actor=%s, memory_id=%s", actor_id, memory_id
            )
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "ResourceNotFoundException":
                return False
            raise MemoryOperationError(f"Failed to delete memory: {e}") from e
        except Exception as e:
            logger.error("Unexpected error deleting semantic memory: %s", e)
            raise MemoryOperationError(f"Failed to delete memory: {e}") from e

    def list_all(self, actor_id: str) -> List[MemoryEntry]:
        """
        Get all Semantic memories of a persona

        Args:
            actor_id: Persona ID

        Returns:
            List of all Semantic memory entries related to the persona
        """
        if not self._client:
            return []

        try:
            namespace_prefix = self._build_actor_namespace_prefix(actor_id)

            all_records = list_all_record_summaries(
                self._client, self.memory_id, namespace_prefix
            )

            entries = []
            for record in all_records:
                entry = self._convert_to_memory_entry(record, actor_id)
                if entry:
                    entries.append(entry)

            logger.info(
                "Listed %d semantic memories for actor=%s", len(entries), actor_id
            )

            return entries

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "ResourceNotFoundException":
                logger.warning("Semantic namespace not found for actor=%s", actor_id)
                return []
            raise MemoryOperationError(f"Failed to list memories: {e}") from e
        except Exception as e:
            logger.error("Unexpected error listing semantic memories: %s", e)
            raise MemoryOperationError(f"Failed to list memories: {e}") from e

    def _convert_to_memory_entry(
        self, record: Dict[str, Any], actor_id: str
    ) -> Optional[MemoryEntry]:
        """
        Convert an API response to a MemoryEntry
        """
        try:
            memory_record_id = record.get("memoryRecordId", str(uuid.uuid4()))

            content_data = record.get("content", {})
            if isinstance(content_data, dict):
                content = content_data.get("text", "")
            else:
                content = str(content_data) if content_data else ""

            namespace = record.get("namespace", "")

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

            relevance_score = record.get("relevanceScore")

            metadata = {
                "namespace": namespace,
                "strategy_id": self.memory_strategy_id,
                "strategy_type": "semantic",  # Identify the strategy type
            }

            return MemoryEntry(
                id=memory_record_id,
                actor_id=actor_id,
                session_id="",  # The Semantic strategy has no session_id
                content=content,
                metadata=metadata,
                created_at=created_at,
                relevance_score=relevance_score,
            )

        except Exception as e:
            logger.warning("Failed to convert record to MemoryEntry: %s", e)
            return None
