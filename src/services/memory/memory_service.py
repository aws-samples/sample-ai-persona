"""
Memory Service Implementation
Core service that works with AgentCore Memory
"""

import logging
import time
from typing import Any, Callable, List, Optional

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from src.models.memory import DeleteAllResult, MemoryEntry

from .retry import RetryExhaustedError, with_retry
from .strategy import (
    MemoryStrategy,
    is_under_namespaces,
    list_page_params,
    record_namespaces,
)
from .summary_strategy import MemoryOperationError, SummaryStrategy
from .semantic_strategy import SemanticStrategy

logger = logging.getLogger(__name__)


def _error_code(error: ClientError) -> str:
    code: str = error.response.get("Error", {}).get("Code", "")
    return code


#: The client used only by :meth:`MemoryService.delete_all`:
#: short timeouts and no botocore retry, so one call
#: never takes longer than :data:`CALL_BUDGET_SECONDS`. ``delete_all`` retries
#: by itself, inside the deadline.
#: ``total_max_attempts`` counts the first call, so 1 means botocore never
#: retries (``max_attempts: 1`` in botocore's default legacy mode would still
#: allow one retry).
DELETE_CLIENT_CONFIG = Config(
    connect_timeout=2,
    read_timeout=5,
    retries={"total_max_attempts": 1, "mode": "standard"},
)
#: Longest one call can take (connect 2 s + read 5 s). With less than this left
#: before the deadline, no call is started.
CALL_BUDGET_SECONDS = 7.0


class _DeleteStopped(Exception):
    """Internal: the deletion stopped (deadline or a failure)."""

    def __init__(self, *, timed_out: bool, error_type: Optional[str] = None) -> None:
        super().__init__("delete_all stopped")
        self.timed_out = timed_out
        self.error_type = error_type


class MemoryConnectionError(Exception):
    """AgentCore Memory connection error."""

    pass


class MemoryServiceError(Exception):
    """Errors raised by the Memory Service."""

    pass


class MemoryService:
    """
    Core service that works with AgentCore Memory

    Uses the Strategy pattern so different memory strategies can be swapped dynamically.
    SummaryStrategy is the default and SemanticStrategy can be used alongside it.

    Attributes:
        memory_id: AgentCore Memory resource ID
        region: AWS region
        _strategy: Current memory strategy (primary)
        _semantic_strategy: Semantic strategy (optional)
        _enabled: Whether the service is enabled
    """

    def __init__(
        self,
        memory_id: str,
        region: str = "us-east-1",
        strategy: Optional[MemoryStrategy] = None,
        validate_connection: bool = True,
        delete_client: Any = None,
        boto3_session: Any = None,
    ):
        """
        Initialize MemoryService

        Args:
            memory_id: AgentCore Memory resource ID
            region: AWS region (default: us-east-1)
            strategy: Memory strategy to use (default: SummaryStrategy)
            validate_connection: Whether to validate the connection at initialization
            delete_client: Pre-built ``bedrock-agentcore`` client for
                :meth:`delete_all` (tests); built lazily otherwise
            boto3_session: Session the deletion client is built from (the
                same credentials and region as the memory client)

        Raises:
            MemoryConnectionError: If connection validation fails
            ValueError: If memory_id is empty
        """
        if not memory_id:
            raise ValueError("memory_id is required")

        self.memory_id = memory_id
        self.region = region
        self._strategy: Optional[MemoryStrategy] = None
        self._semantic_strategy: Optional[SemanticStrategy] = None
        self._enabled = True
        self._deletion_client = delete_client
        self._boto3_session = boto3_session

        # Set the strategy (SummaryStrategy by default)
        if strategy:
            self._strategy = strategy
        else:
            self._init_default_strategy()

        # Also initialize the Semantic strategy (if configured)
        self._init_semantic_strategy()

        # Validate the connection
        if validate_connection:
            self._validate_connection()

        logger.info(
            "MemoryService initialized: memory_id=%s, region=%s, strategy=%s, semantic=%s",
            self.memory_id,
            self.region,
            type(self._strategy).__name__ if self._strategy else "None",
            "enabled" if self._semantic_strategy else "disabled",
        )

    def _init_default_strategy(self) -> None:
        """Initialize the default memory strategy (SummaryStrategy)."""
        try:
            self._strategy = SummaryStrategy(
                memory_id=self.memory_id, region=self.region
            )
        except Exception as e:
            logger.error("Failed to initialize default strategy: %s", e)
            raise MemoryConnectionError(
                f"Failed to initialize memory strategy: {e}"
            ) from e

    def _init_semantic_strategy(self) -> None:
        """Initialize the Semantic strategy (if configured)."""
        # The ID looked up with GetMemory at startup lives only in the shared config (not in environment variables)
        from src.config import config

        if not config.SEMANTIC_MEMORY_STRATEGY_ID:
            logger.debug("Semantic strategy not configured, skipping initialization")
            return

        try:
            self._semantic_strategy = SemanticStrategy(
                memory_id=self.memory_id,
                region=self.region,
                memory_strategy_id=config.SEMANTIC_MEMORY_STRATEGY_ID,
            )
            logger.info(
                "Semantic strategy initialized: strategy_id=%s",
                config.SEMANTIC_MEMORY_STRATEGY_ID,
            )
        except Exception as e:
            logger.warning("Failed to initialize semantic strategy: %s", e)
            # A Semantic strategy initialization failure must not stop the whole service
            self._semantic_strategy = None

    def _validate_connection(self) -> None:
        """
        Validate the connection to AgentCore Memory

        Raises:
            MemoryConnectionError: If connection validation fails
        """
        try:
            # Check that the strategy is initialized
            if not self._strategy:
                raise MemoryConnectionError("Memory strategy not initialized")

            logger.info("Connection validated for memory_id=%s", self.memory_id)
        except Exception as e:
            logger.error("Connection validation failed: %s", e)
            raise MemoryConnectionError(
                f"Failed to validate connection to AgentCore Memory: {e}"
            ) from e

    def _ensure_strategy(self) -> MemoryStrategy:
        """Check that a strategy is set and return it."""
        if not self._strategy:
            raise MemoryServiceError("Memory strategy not configured")
        return self._strategy

    @property
    def is_semantic_enabled(self) -> bool:
        """Return whether the Semantic strategy is enabled."""
        return self._semantic_strategy is not None

    @with_retry(
        max_retries=3, base_delay=1.0, retryable_exceptions=(MemoryOperationError,)
    )
    def save_knowledge(
        self,
        actor_id: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        Save knowledge directly to LTM with the Semantic strategy.

        Args:
            actor_id: Persona ID
            content: Content to save
            metadata: Additional metadata

        Returns:
            ID of the saved memory

        Raises:
            MemoryServiceError: If the Semantic strategy is not configured or saving fails
        """
        if not self._semantic_strategy:
            raise MemoryServiceError(
                "Semantic memory strategy is not configured. "
                "Set SEMANTIC_MEMORY_STRATEGY_ID to enable this feature."
            )

        try:
            memory_id = self._semantic_strategy.save_directly_to_ltm(
                actor_id=actor_id,
                content=content,
                metadata=metadata,
            )
            logger.info("Knowledge saved: actor=%s, memory_id=%s", actor_id, memory_id)
            return memory_id

        except (MemoryOperationError, RetryExhaustedError):
            raise
        except Exception as e:
            logger.error("Unexpected error saving knowledge: %s", e)
            raise MemoryServiceError(f"Failed to save knowledge: {e}") from e

    @with_retry(
        max_retries=3, base_delay=1.0, retryable_exceptions=(MemoryOperationError,)
    )
    def delete_memory(self, actor_id: str, memory_id: str) -> bool:
        """
        Delete a memory (tries both the Summary and Semantic strategies)

        Args:
            actor_id: Persona ID
            memory_id: ID of the memory to delete

        Returns:
            True if deletion succeeded

        Raises:
            MemoryServiceError: If deletion fails
        """
        strategy = self._ensure_strategy()

        try:
            # Try the Summary strategy first
            result = strategy.delete(actor_id=actor_id, memory_id=memory_id)

            if result:
                logger.info(
                    "Memory deleted (summary): actor=%s, memory_id=%s",
                    actor_id,
                    memory_id,
                )
                return True

            # If not found in the Summary strategy, try the Semantic strategy
            if self._semantic_strategy:
                result = self._semantic_strategy.delete(
                    actor_id=actor_id, memory_id=memory_id
                )

                if result:
                    logger.info(
                        "Memory deleted (semantic): actor=%s, memory_id=%s",
                        actor_id,
                        memory_id,
                    )
                    return True

            logger.warning(
                "Memory not found for deletion: actor=%s, memory_id=%s",
                actor_id,
                memory_id,
            )
            return False

        except RetryExhaustedError as e:
            logger.error("Retry exhausted deleting memory: %s", e)
            raise MemoryServiceError(
                f"Failed to delete memory after retries: {e.last_exception}"
            ) from e
        except MemoryOperationError:
            # Re-raise errors that are retry targets
            raise
        except Exception as e:
            logger.error("Unexpected error deleting memory: %s", e)
            raise MemoryServiceError(f"Failed to delete memory: {e}") from e

    @with_retry(
        max_retries=3, base_delay=1.0, retryable_exceptions=(MemoryOperationError,)
    )
    def list_memories(self, actor_id: str) -> List[MemoryEntry]:
        """
        Get all memories of a persona (from both the Summary and Semantic strategies)

        Args:
            actor_id: Persona ID

        Returns:
            List of all memory entries related to the persona

        Raises:
            MemoryServiceError: If retrieval fails
        """
        strategy = self._ensure_strategy()
        all_memories: List[MemoryEntry] = []

        try:
            # Get memories from the Summary strategy
            summary_memories = strategy.list_all(actor_id=actor_id)
            # Add the strategy type to the metadata
            for memory in summary_memories:
                if memory.metadata is None:
                    memory.metadata = {}
                memory.metadata["strategy_type"] = "summary"
            all_memories.extend(summary_memories)

            logger.info(
                "Listed %d summary memories for actor=%s",
                len(summary_memories),
                actor_id,
            )

        except RetryExhaustedError as e:
            logger.error("Retry exhausted listing summary memories: %s", e)
            raise MemoryServiceError(
                f"Failed to list memories after retries: {e.last_exception}"
            ) from e
        except MemoryOperationError:
            raise
        except Exception as e:
            logger.error("Unexpected error listing summary memories: %s", e)
            raise MemoryServiceError(f"Failed to list memories: {e}") from e

        # Get memories from the Semantic strategy (if configured)
        if self._semantic_strategy:
            try:
                semantic_memories = self._semantic_strategy.list_all(actor_id=actor_id)
                all_memories.extend(semantic_memories)

                logger.info(
                    "Listed %d semantic memories for actor=%s",
                    len(semantic_memories),
                    actor_id,
                )

            except Exception as e:
                # Log Semantic strategy errors but continue processing
                logger.warning(
                    "Failed to list semantic memories for actor=%s: %s", actor_id, e
                )

        logger.info(
            "Total %d memories listed for actor=%s", len(all_memories), actor_id
        )

        return all_memories

    # ------------------------------------------------------------------
    # Deleting one record of an actor
    # ------------------------------------------------------------------

    def delete_owned_memory(self, actor_id: str, memory_id: str) -> bool:
        """Delete one record only when it belongs to ``actor_id``.

        The record is read with ``GetMemoryRecord`` first; it is deleted only
        when one of its namespaces lies under a namespace of ``actor_id``
        (both strategies). A record of another actor is left untouched and
        reported exactly like a record that does not exist.

        Args:
            actor_id: The persona ID.
            memory_id: The memory record ID.

        Returns:
            True when the record was deleted; False when it does not exist or
            belongs to another actor.

        Raises:
            MemoryServiceError: Reading or deleting the record failed.
        """
        namespaces = self._namespaces(actor_id)
        if not namespaces or not memory_id:
            return False
        client = self._delete_client()
        found = self._get_record(client, memory_id)
        if found is None:
            return False
        if not is_under_namespaces(record_namespaces(found), namespaces):
            logger.warning(
                "Memory record is not the actor's: actor=%s, memory_id=%s",
                actor_id,
                memory_id,
            )
            return False
        try:
            client.delete_memory_record(
                memoryId=self.memory_id, memoryRecordId=memory_id
            )
        except ClientError as e:
            if _error_code(e) == "ResourceNotFoundException":
                return False
            raise MemoryServiceError(
                f"memory record delete failed ({type(e).__name__})"
            ) from e
        except BotoCoreError as e:
            raise MemoryServiceError(
                f"memory record delete failed ({type(e).__name__})"
            ) from e
        logger.info("Memory deleted: actor=%s, memory_id=%s", actor_id, memory_id)
        return True

    def _get_record(self, client: Any, memory_id: str) -> Optional[dict[str, Any]]:
        """Read one record; None when it does not exist (or the ID is malformed)."""
        try:
            found: dict[str, Any] = client.get_memory_record(
                memoryId=self.memory_id, memoryRecordId=memory_id
            )
            return found
        except ParamValidationError:
            return None  # an ID AgentCore would never issue
        except ClientError as e:
            if _error_code(e) == "ResourceNotFoundException":
                return None
            raise MemoryServiceError(
                f"memory record read failed ({type(e).__name__})"
            ) from e
        except BotoCoreError as e:
            raise MemoryServiceError(
                f"memory record read failed ({type(e).__name__})"
            ) from e

    # ------------------------------------------------------------------
    # Deleting every memory of an actor
    # ------------------------------------------------------------------

    def _delete_client(self) -> Any:
        """The ``bedrock-agentcore`` client used only for :meth:`delete_all`."""
        if self._deletion_client is None:
            import boto3

            session = self._boto3_session or boto3.Session()
            self._deletion_client = session.client(
                "bedrock-agentcore",
                region_name=self.region,
                config=DELETE_CLIENT_CONFIG,
            )
        return self._deletion_client

    def _namespaces(self, actor_id: str) -> List[str]:
        names: List[str] = []
        for strategy in (self._strategy, self._semantic_strategy):
            namespaces_for = getattr(strategy, "namespaces_for", None)
            if callable(namespaces_for):
                names += [n for n in namespaces_for(actor_id) if n not in names]
        return names

    def delete_all(
        self,
        actor_id: str,
        deadline: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> DeleteAllResult:
        """Delete every memory record of ``actor_id`` before ``deadline``.

        Both strategies' namespaces are read page by page and every record is
        deleted with the dedicated client. No call starts with less than
        :data:`CALL_BUDGET_SECONDS` left, so the deadline is never exceeded. A
        failed delete is retried once (time permitting). Nothing is raised:
        the outcome comes back as a :class:`DeleteAllResult`.

        Args:
            actor_id: The persona ID.
            deadline: A ``clock`` value after which no call may finish.
            clock: Monotonic clock (tests pass a fake one).
        """
        if not self._enabled:
            return DeleteAllResult.nothing()
        namespaces = self._namespaces(actor_id)
        if not namespaces:
            return DeleteAllResult.nothing()

        deleted = 0
        found = 0
        # Namespaces whose last page has been listed; when the work stops, the
        # remaining count is known only if every namespace was listed to the end.
        listed_to_end = 0

        def ensure_time() -> None:
            if deadline - clock() < CALL_BUDGET_SECONDS:
                raise _DeleteStopped(timed_out=True)

        try:
            client = self._delete_client()
            for namespace in namespaces:
                next_token: Optional[str] = None
                while True:
                    ensure_time()
                    page = self._list_page(client, namespace, next_token)
                    records = page.get("memoryRecordSummaries", [])
                    found += len(records)
                    next_token = page.get("nextToken")
                    if not next_token:
                        listed_to_end += 1
                    for record in records:
                        self._delete_record(client, record, ensure_time)
                        deleted += 1
                    if not next_token:
                        break
        except _DeleteStopped as stop:
            remaining = found - deleted if listed_to_end == len(namespaces) else None
            logger.warning(
                "delete_all stopped for actor=%s (timed_out=%s, deleted=%d)",
                actor_id,
                stop.timed_out,
                deleted,
            )
            return DeleteAllResult(
                deleted=deleted,
                remaining=remaining,
                timed_out=stop.timed_out,
                error_type=stop.error_type,
            )
        except Exception as e:  # never raise from a cleanup step
            logger.warning(
                "delete_all failed for actor=%s: %s", actor_id, type(e).__name__
            )
            return DeleteAllResult(
                deleted=deleted,
                remaining=None,
                timed_out=False,
                error_type=type(e).__name__,
            )
        return DeleteAllResult(deleted=deleted, remaining=0, timed_out=False)

    def _list_page(
        self, client: Any, namespace: str, next_token: Optional[str]
    ) -> dict[str, Any]:
        params = list_page_params(self.memory_id, namespace, next_token)
        try:
            page: dict[str, Any] = client.list_memory_records(**params)
            return page
        except (ClientError, BotoCoreError) as e:
            raise _DeleteStopped(timed_out=False, error_type=type(e).__name__) from e

    def _delete_record(
        self,
        client: Any,
        record: dict[str, Any],
        ensure_time: Callable[[], None],
    ) -> None:
        record_id = record.get("memoryRecordId", "")
        for attempt in range(2):  # the call and one retry
            ensure_time()
            try:
                client.delete_memory_record(
                    memoryId=self.memory_id, memoryRecordId=record_id
                )
                return
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code == "ResourceNotFoundException":
                    return  # already gone
                if attempt == 1:
                    raise _DeleteStopped(
                        timed_out=False, error_type=type(e).__name__
                    ) from e
            except BotoCoreError as e:
                if attempt == 1:
                    raise _DeleteStopped(
                        timed_out=False, error_type=type(e).__name__
                    ) from e
