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
