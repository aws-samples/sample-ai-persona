"""
Memory Strategy Abstract Base Class
記憶戦略の抽象基底クラス

拡張可能な記憶戦略パターンを実装するためのインターフェース定義。
新しい記憶戦略（Summary、Semantic、User Preference等）を追加する際は
このインターフェースを実装する。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from src.models.memory import MemoryEntry


def is_under_namespaces(record_namespaces: List[str], prefixes: List[str]) -> bool:
    """Tell whether a memory record lives under one of ``prefixes`` (U5, R-01).

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
    記憶戦略の抽象基底クラス

    全ての記憶戦略はこのインターフェースを実装する必要がある。
    Strategy Patternにより、MemoryServiceは具体的な戦略実装に依存せず、
    異なる記憶戦略を動的に切り替えることができる。

    Requirements:
        - 2.1: Strategy patternの実装
        - 2.4: 共通インターフェース（save, retrieve, delete, list_all）の提供
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
        記憶を保存する

        Args:
            actor_id: ペルソナID（AgentCore MemoryのactorIdとして使用）
            session_id: 議論ID（AgentCore MemoryのsessionIdとして使用）
            content: 保存する記憶の内容
            metadata: 追加のメタデータ（重要度、タイプ等）

        Returns:
            保存された記憶のID

        Raises:
            MemoryOperationError: 保存に失敗した場合
        """
        pass

    @abstractmethod
    def retrieve(self, actor_id: str, query: str, top_k: int = 5) -> List[MemoryEntry]:
        """
        関連する記憶を検索する

        セマンティック検索により、クエリに関連する記憶を取得する。
        結果は関連度スコアの降順でソートされる。

        Args:
            actor_id: ペルソナID
            query: 検索クエリ（トピックやキーワード）
            top_k: 取得する最大件数（デフォルト: 5）

        Returns:
            関連する記憶エントリのリスト（関連度スコア降順）

        Raises:
            MemoryOperationError: 検索に失敗した場合
        """
        pass

    @abstractmethod
    def delete(self, actor_id: str, memory_id: str) -> bool:
        """
        特定の記憶を削除する

        Args:
            actor_id: ペルソナID
            memory_id: 削除する記憶のID

        Returns:
            削除に成功した場合True、記憶が見つからない場合False

        Raises:
            MemoryOperationError: 削除に失敗した場合
        """
        pass

    @abstractmethod
    def list_all(self, actor_id: str) -> List[MemoryEntry]:
        """
        ペルソナの全記憶を取得する

        Args:
            actor_id: ペルソナID

        Returns:
            ペルソナに関連する全ての記憶エントリのリスト

        Raises:
            MemoryOperationError: 取得に失敗した場合
        """
        pass
