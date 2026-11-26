"""
Memory Strategy Abstract Base Class
Abstract base class for memory strategies

Interface definition for implementing an extensible memory strategy pattern.
Implement this interface when adding a new memory strategy (Summary, Semantic, User Preference, etc.).
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from src.models.memory import MemoryEntry


def is_under_namespaces(record_namespaces: List[str], prefixes: List[str]) -> bool:
    """Tell whether a memory record lives under one of ``prefixes``.

    A namespace matches a prefix when it equals the prefix or continues it
    with ``/``, so ``.../actors/p1`` never matches ``.../actors/p10/...``.
    """
    for namespace in record_namespaces:
        for prefix in prefixes:
            base = prefix.rstrip("/")
            if namespace == base or namespace.startswith(base + "/"):
                return True
    return False


def record_namespaces(response: Dict[str, Any]) -> List[str]:
    """The namespaces of a ``GetMemoryRecord`` response (empty when absent)."""
    record = response.get("memoryRecord") or {}
    namespaces = record.get("namespaces") or []
    return [n for n in namespaces if isinstance(n, str)]


#: Records read per ``ListMemoryRecords`` page.
LIST_PAGE_SIZE = 100
#: Most pages one listing reads (10,000 records). A listing runs inside a
#: request, so it is bounded even if the service keeps returning tokens.
MAX_LIST_PAGES = 100


def list_page_params(
    memory_id: str, namespace: str, next_token: Optional[str] = None
) -> Dict[str, Any]:
    """The ``ListMemoryRecords`` parameters for one page under ``namespace``."""
    params: Dict[str, Any] = {
        "memoryId": memory_id,
        "namespace": namespace,
        "maxResults": LIST_PAGE_SIZE,
    }
    if next_token:
        params["nextToken"] = next_token
    return params


def list_all_record_summaries(
    client: Any, memory_id: str, namespace: str
) -> List[Dict[str, Any]]:
    """Every ``ListMemoryRecords`` summary under ``namespace``, following ``nextToken``.

    The API returns one page at a time; reading only the first page hides
    records from the memory screen and from per-strategy deletes.

    Raises:
        RuntimeError: The service repeated a token or more than
            ``MAX_LIST_PAGES`` pages were needed. The listing is refused rather
            than cut short, so a delete never works from a partial list.
    """
    summaries: List[Dict[str, Any]] = []
    seen_tokens: set[str] = set()
    next_token: Optional[str] = None
    for _ in range(MAX_LIST_PAGES):
        page = client.list_memory_records(
            **list_page_params(memory_id, namespace, next_token)
        )
        summaries.extend(page.get("memoryRecordSummaries", []))
        next_token = page.get("nextToken")
        if not next_token:
            return summaries
        if next_token in seen_tokens:
            raise RuntimeError("ListMemoryRecords repeated a nextToken")
        seen_tokens.add(next_token)
    raise RuntimeError(f"ListMemoryRecords needed more than {MAX_LIST_PAGES} pages")


class MemoryStrategy(ABC):
    """
    Abstract base class for memory strategies

    Every memory strategy must implement this interface.
    With the Strategy pattern, MemoryService does not depend on a concrete strategy
    and can switch between memory strategies dynamically.
    """

    @abstractmethod
    def save(
        self,
        actor_id: str,
        session_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Save a memory

        Args:
            actor_id: Persona ID (used as the actorId in AgentCore Memory)
            session_id: Discussion ID (used as the sessionId in AgentCore Memory)
            content: Content of the memory to save
            metadata: Additional metadata (importance, type, etc.)

        Returns:
            ID of the saved memory

        Raises:
            MemoryOperationError: If saving fails
        """
        pass

    @abstractmethod
    def retrieve(self, actor_id: str, query: str, top_k: int = 5) -> List[MemoryEntry]:
        """
        Search for related memories

        Retrieves memories related to the query by semantic search.
        Results are sorted by relevance score in descending order.

        Args:
            actor_id: Persona ID
            query: Search query (topic or keywords)
            top_k: Maximum number of results (default: 5)

        Returns:
            List of related memory entries (by relevance score, descending)

        Raises:
            MemoryOperationError: If the search fails
        """
        pass

    @abstractmethod
    def delete(self, actor_id: str, memory_id: str) -> bool:
        """
        Delete a specific memory

        Args:
            actor_id: Persona ID
            memory_id: ID of the memory to delete

        Returns:
            True if deletion succeeded, False if the memory was not found

        Raises:
            MemoryOperationError: If deletion fails
        """
        pass

    @abstractmethod
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
        pass
