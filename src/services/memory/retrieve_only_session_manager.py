"""
Retrieve-Only Session Manager for AgentCore Memory Integration
検索のみモードのセッションマネージャー

AgentCoreMemorySessionManagerをラップし、保存機能を無効化して
検索のみを行うセッションマネージャーを提供する。
"""

import logging
from typing import Any, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from strands import Agent
    from strands.types.content import Message

logger = logging.getLogger(__name__)


class RetrieveOnlySessionManager:
    """
    検索のみモードのセッションマネージャー

    AgentCoreMemorySessionManagerをラップし、保存機能を無効化する。
    LTMからの検索は行うが、会話内容をSTM/LTMに保存しない。

    重要: AgentCore MemoryはSTMに保存すると自動的にLTMに抽出される仕組みのため、
    検索のみモードではSTMへの保存（create_event）も無効化する必要がある。

    Strands Agent SDKでは、Agentにsession_managerを渡すと、
    hooks.add_hook(session_manager)が呼び出され、register_hooksで
    登録されたメソッドがイベントハンドラとして使用される。

    このクラスでは、register_hooksで自分自身のメソッドを登録することで、
    ベースマネージャーのappend_message（STM保存）ではなく、
    このクラスのappend_message（何もしない）が呼び出されるようにする。

    Attributes:
        _base_manager: ベースとなるAgentCoreMemorySessionManager
        _actor_id: ペルソナID
        _session_id: セッションID
    """

    def __init__(self, base_manager: Any, actor_id: str, session_id: str):
        """
        RetrieveOnlySessionManagerを初期化

        Args:
            base_manager: ベースとなるAgentCoreMemorySessionManager
            actor_id: ペルソナID
            session_id: セッションID
        """
        self._base_manager = base_manager
        self._actor_id = actor_id
        self._session_id = session_id
        self._messages: List[
            Any
        ] = []  # ローカルでメッセージを保持（検索コンテキスト用）
        self._latest_agent_message: dict = {}  # ベースマネージャーとの互換性のため

        logger.info(
            f"Created RetrieveOnlySessionManager: actor_id={actor_id}, session_id={session_id}"
        )

    def initialize(self, agent: "Agent", **kwargs: Any) -> None:
        """
        エージェントを初期化

        ベースマネージャーの初期化は呼び出さない。
        register_hooksで必要なフックのみを登録する。

        Args:
            agent: Strands Agent
            **kwargs: 追加のキーワード引数
        """
        # ベースマネージャーの初期化は呼び出さない
        # （initializeがregister_hooksを呼び出す可能性があるため）
        logger.debug(
            f"RetrieveOnlySessionManager: Initialized for actor={self._actor_id} "
            "(retrieve-only mode, no base manager initialization)"
        )

    def register_hooks(self, registry: Any, **kwargs: Any) -> None:
        """
        フックを登録（LTM検索のフックのみ登録、保存フックは登録しない）

        重要: ベースマネージャーのregister_hooksを呼び出すと、
        ベースマネージャーのappend_messageがフックとして登録され、
        STMに保存されてしまう。

        このメソッドでは、自分自身のメソッドを直接登録することで、
        保存を無効化しつつLTM検索のみを有効にする。

        Args:
            registry: HookRegistry
            **kwargs: 追加のキーワード引数
        """
        try:
            from strands.hooks import MessageAddedEvent

            # LTM検索のフックを登録（retrieve_customer_context）
            # MessageAddedEventに対してretrieve_customer_contextを登録
            registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)

            # 注意: append_messageは登録しない（STM保存を無効化）
            # ベースマネージャーのregister_hooksは呼び出さない

            logger.debug(
                f"RetrieveOnlySessionManager: Registered retrieve_customer_context hook "
                f"for actor={self._actor_id} (append_message NOT registered)"
            )
        except ImportError as e:
            logger.warning(
                f"RetrieveOnlySessionManager: Failed to import strands hooks: {e}. "
                "LTM retrieval may not work."
            )
        except Exception as e:
            logger.warning(f"RetrieveOnlySessionManager: Error registering hooks: {e}")

    def append_message(self, message: "Message", agent: "Agent", **kwargs: Any) -> None:
        """
        メッセージを追加（検索のみモードではSTMに保存しない）

        重要: AgentCore MemoryはSTMに保存すると自動的にLTMに抽出されるため、
        検索のみモードではcreate_eventを呼び出さない。

        このメソッドはregister_hooksで登録されないため、通常は呼び出されない。
        互換性のために残している。

        Args:
            message: 追加するメッセージ
            agent: Strands Agent
            **kwargs: 追加のキーワード引数
        """
        # STMへの保存をスキップ（create_eventを呼び出さない）
        logger.debug(
            f"RetrieveOnlySessionManager: append_message called but skipped "
            f"(retrieve-only mode) for actor={self._actor_id}"
        )

    def create_session(self, session: Any, **kwargs: Any) -> Any:
        """
        セッションを作成（検索のみモードでは何もしない）

        Args:
            session: セッションオブジェクト
            **kwargs: 追加のキーワード引数

        Returns:
            セッションオブジェクト（変更なし）
        """
        logger.debug(
            f"RetrieveOnlySessionManager: create_session called but skipped "
            f"(retrieve-only mode) for actor={self._actor_id}"
        )
        return session

    def create_agent(self, session_id: str, session_agent: Any, **kwargs: Any) -> None:
        """
        エージェントを作成（検索のみモードでは何もしない）

        Args:
            session_id: セッションID
            session_agent: セッションエージェント
            **kwargs: 追加のキーワード引数
        """
        logger.debug(
            f"RetrieveOnlySessionManager: create_agent called but skipped "
            f"(retrieve-only mode) for actor={self._actor_id}"
        )

    def create_message(
        self, session_id: str, agent_id: str, session_message: Any, **kwargs: Any
    ) -> Optional[dict]:
        """
        メッセージを作成（検索のみモードでは何もしない）

        Args:
            session_id: セッションID
            agent_id: エージェントID
            session_message: セッションメッセージ
            **kwargs: 追加のキーワード引数

        Returns:
            None（保存しない）
        """
        logger.debug(
            f"RetrieveOnlySessionManager: create_message called but skipped "
            f"(retrieve-only mode) for actor={self._actor_id}"
        )
        return None

    def retrieve_customer_context(self, event: Any) -> None:
        """
        LTMからコンテキストを検索（ベースマネージャーに委譲）

        Args:
            event: MessageAddedEvent
        """
        try:
            self._base_manager.retrieve_customer_context(event)
            logger.debug(
                f"RetrieveOnlySessionManager: Retrieved customer context "
                f"for actor={self._actor_id}"
            )
        except Exception as e:
            logger.warning(
                f"RetrieveOnlySessionManager: Error in retrieve_customer_context: {e}"
            )

    def save(self, messages: List[Any]) -> None:
        """
        メッセージを保存（検索のみモードでは何もしない）

        Args:
            messages: 保存するメッセージリスト（無視される）
        """
        # 検索のみモードでは保存しない
        logger.debug(
            f"RetrieveOnlySessionManager: save called but skipped (retrieve-only mode) "
            f"for actor={self._actor_id}, message_count={len(messages)}"
        )

    def get_messages(self) -> List[Any]:
        """
        現在のメッセージリストを取得

        Returns:
            メッセージリスト
        """
        return self._messages

    def clear(self) -> None:
        """
        メッセージをクリア
        """
        self._messages = []
        logger.debug(
            f"RetrieveOnlySessionManager: Cleared messages for actor={self._actor_id}"
        )

    @property
    def actor_id(self) -> str:
        """ペルソナIDを取得"""
        return self._actor_id

    @property
    def session_id(self) -> str:
        """セッションIDを取得"""
        return self._session_id

    @property
    def config(self) -> Any:
        """ベースマネージャーの設定を取得"""
        return self._base_manager.config

    @property
    def memory_client(self) -> Any:
        """ベースマネージャーのメモリクライアントを取得"""
        return self._base_manager.memory_client

    def __getattr__(self, name: str) -> Any:
        """
        未定義の属性はベースマネージャーに委譲

        Args:
            name: 属性名

        Returns:
            ベースマネージャーの属性
        """
        return getattr(self._base_manager, name)
