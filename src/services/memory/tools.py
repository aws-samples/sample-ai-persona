"""
Memory Tools for Persona Agent
ペルソナエージェントが長期記憶にアクセスするためのStrands SDKツール

Requirements:
    - 4.2: Memory_Retrieval_Toolでトピックやキーワードで記憶を検索
    - 4.3: Memory_Save_Toolで重要な気づきを長期記憶に保存
    - 5.1: クエリ文字列を受け取り関連する記憶エントリを返す
    - 5.2: 関連度スコアの降順でソート
    - 5.3: 設定可能な最大件数で結果を制限
    - 5.4: コンテンツとオプションのメタデータを受け取る
    - 5.5: 記憶をペルソナのactor_idに自動的に関連付け
    - 10.1: ツール実行失敗時、エージェントは記憶結果なしで継続
    - 10.2: 保存ツール失敗時、エラーメッセージを返す
"""

import logging
from typing import TYPE_CHECKING, Callable

from botocore.exceptions import ClientError

try:
    from strands import tool
except ImportError:
    # Strands SDKがインストールされていない場合のフォールバック
    def tool(func: Callable) -> Callable:  # type: ignore[no-redef]
        """Fallback decorator when strands is not installed"""
        return func


if TYPE_CHECKING:
    from .memory_service import MemoryService

logger = logging.getLogger(__name__)


class MemoryToolError(Exception):
    """メモリツール実行エラー"""

    pass


def _format_error_message(error: Exception, operation: str) -> str:
    """
    エラーメッセージをユーザーフレンドリーな形式にフォーマット

    Args:
        error: 発生した例外
        operation: 実行中の操作（"検索" or "保存"）

    Returns:
        フォーマットされたエラーメッセージ
    """
    # ClientErrorの場合は詳細なエラーコードを取得
    if isinstance(error, ClientError):
        error_code = error.response.get("Error", {}).get("Code", "Unknown")

        # 一般的なエラーコードに対するユーザーフレンドリーなメッセージ
        error_messages = {
            "ThrottlingException": f"記憶の{operation}が一時的に制限されています。しばらく待ってから再試行してください。",
            "ServiceUnavailable": "記憶サービスが一時的に利用できません。後でもう一度お試しください。",
            "ResourceNotFoundException": "記憶リソースが見つかりません。設定を確認してください。",
            "AccessDeniedException": "記憶サービスへのアクセスが拒否されました。権限を確認してください。",
            "ValidationException": "入力データが無効です。内容を確認してください。",
        }

        if error_code in error_messages:
            return error_messages[error_code]

    # その他のエラーは一般的なメッセージを返す
    return f"記憶の{operation}中にエラーが発生しました。操作を続行します。"


def create_memory_retrieval_tool(
    memory_service: "MemoryService", actor_id: str
) -> Callable:
    """
    ペルソナ用の記憶検索ツールを作成

    Args:
        memory_service: MemoryServiceインスタンス
        actor_id: ペルソナID（記憶の検索対象）

    Returns:
        Callable: Strands SDKツールとして使用可能な関数

    Requirements:
        - 4.2: Memory_Retrieval_Toolでトピックやキーワードで記憶を検索
        - 5.1: クエリ文字列を受け取り関連する記憶エントリを返す
        - 5.2: 関連度スコアの降順でソート
        - 5.3: 設定可能な最大件数で結果を制限
        - 10.1: ツール実行失敗時、エージェントは記憶結果なしで継続
    """

    @tool
    def retrieve_memories(query: str, max_results: int = 5) -> str:
        """
        過去の議論や経験から関連する記憶を検索します。

        Args:
            query: 検索したい内容やトピック
            max_results: 取得する最大件数（デフォルト: 5）

        Returns:
            関連する記憶の内容
        """
        try:
            # 入力検証
            if not query or not query.strip():
                logger.warning(
                    "Empty query provided for memory retrieval, actor=%s", actor_id
                )
                return "検索クエリが空です。検索したい内容を指定してください。"

            # max_resultsの範囲を制限（1-20）
            max_results = max(1, min(20, max_results))

            # メモリサービスが有効か確認
            if not memory_service or not memory_service.enabled:
                logger.info(
                    "Memory service disabled, returning empty result for actor=%s",
                    actor_id,
                )
                return "長期記憶機能は現在無効です。"

            # 記憶を検索
            memories = memory_service.retrieve_memories(
                actor_id=actor_id, query=query.strip(), top_k=max_results
            )

            if not memories:
                return "関連する記憶は見つかりませんでした。"

            # 結果をフォーマット（関連度スコア降順でソート済み）
            result = "過去の記憶:\n"
            for i, memory in enumerate(memories, 1):
                result += f"\n{i}. {memory.content}\n"
                if memory.relevance_score is not None:
                    result += f"   (関連度: {memory.relevance_score:.2f})\n"

            logger.info(
                "Memory retrieval tool returned %d memories for actor=%s, query='%s'",
                len(memories),
                actor_id,
                query[:50],
            )

            return result

        except ClientError as e:
            # AWS ClientErrorの詳細なハンドリング
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = _format_error_message(e, "検索")

            logger.error(
                "Memory retrieval ClientError for actor=%s: code=%s, message=%s",
                actor_id,
                error_code,
                str(e),
            )

            # エージェントは継続可能（Requirements 10.1）
            return error_msg

        except Exception as e:
            # その他の予期しないエラー
            error_msg = _format_error_message(e, "検索")

            logger.error(
                "Memory retrieval tool unexpected error for actor=%s: %s",
                actor_id,
                e,
                exc_info=True,
            )

            # エージェントは継続可能（Requirements 10.1）
            return error_msg

    return retrieve_memories


def create_memory_save_tool(
    memory_service: "MemoryService", actor_id: str, session_id: str
) -> Callable:
    """
    ペルソナ用の記憶保存ツールを作成

    Args:
        memory_service: MemoryServiceインスタンス
        actor_id: ペルソナID（記憶の所有者）
        session_id: 議論セッションID

    Returns:
        Callable: Strands SDKツールとして使用可能な関数

    Requirements:
        - 4.3: Memory_Save_Toolで重要な気づきを長期記憶に保存
        - 5.4: コンテンツとオプションのメタデータを受け取る
        - 5.5: 記憶をペルソナのactor_idに自動的に関連付け
        - 10.2: 保存ツール失敗時、エラーメッセージを返す
    """

    @tool
    def save_memory(content: str, importance: str = "normal") -> str:
        """
        重要な気づきや学びを長期記憶として保存します。

        Args:
            content: 保存したい内容（気づき、学び、重要な情報）
            importance: 重要度（"high", "normal", "low"）

        Returns:
            保存結果のメッセージ
        """
        try:
            # 入力検証
            if not content or not content.strip():
                logger.warning(
                    "Empty content provided for memory save, actor=%s, session=%s",
                    actor_id,
                    session_id,
                )
                return "保存する内容が空です。保存したい内容を指定してください。"

            # コンテンツの長さ制限（過度に長い内容を防ぐ）
            max_content_length = 10000
            if len(content) > max_content_length:
                logger.warning(
                    "Content too long for memory save, truncating: actor=%s, length=%d",
                    actor_id,
                    len(content),
                )
                content = content[:max_content_length] + "..."

            # 重要度の検証
            valid_importance = ["high", "normal", "low"]
            if importance not in valid_importance:
                logger.debug(
                    "Invalid importance '%s' provided, defaulting to 'normal'",
                    importance,
                )
                importance = "normal"

            # メモリサービスが有効か確認
            if not memory_service or not memory_service.enabled:
                logger.info(
                    "Memory service disabled, cannot save memory for actor=%s", actor_id
                )
                return "長期記憶機能は現在無効です。記憶を保存できませんでした。"

            # メタデータを構築
            metadata = {"importance": importance, "type": "insight"}

            # 記憶を保存（actor_idは自動的に関連付けられる）
            memory_id = memory_service.save_memory(
                actor_id=actor_id,
                session_id=session_id,
                content=content.strip(),
                metadata=metadata,
            )

            logger.info(
                "Memory save tool saved memory for actor=%s, session=%s, memory_id=%s",
                actor_id,
                session_id,
                memory_id,
            )

            return "記憶を保存しました。"

        except ClientError as e:
            # AWS ClientErrorの詳細なハンドリング（Requirements 10.2）
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = _format_error_message(e, "保存")

            logger.error(
                "Memory save ClientError for actor=%s, session=%s: code=%s, message=%s",
                actor_id,
                session_id,
                error_code,
                str(e),
            )

            # エラーメッセージを返す（Requirements 10.2）
            return error_msg

        except Exception as e:
            # その他の予期しないエラー（Requirements 10.2）
            error_msg = _format_error_message(e, "保存")

            logger.error(
                "Memory save tool unexpected error for actor=%s, session=%s: %s",
                actor_id,
                session_id,
                e,
                exc_info=True,
            )

            # エラーメッセージを返す（Requirements 10.2）
            return error_msg

    return save_memory
