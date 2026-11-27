"""
Retrieve-Only Session Manager for AgentCore Memory Integration
Session manager for retrieve-only mode

Wraps AgentCoreMemorySessionManager and provides a session manager that
disables saving and only retrieves.
"""

import logging
from typing import Any, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from strands import Agent
    from strands.types.content import Message

logger = logging.getLogger(__name__)


class RetrieveOnlySessionManager:
    """
    Session manager for retrieve-only mode

    Wraps AgentCoreMemorySessionManager and disables saving.
    It retrieves from LTM but does not save the conversation to STM/LTM.

    Important: AgentCore Memory automatically extracts STM into LTM, so
    retrieve-only mode must also disable saving to STM (create_event).

    In the Strands Agent SDK, passing a session_manager to an Agent calls
    hooks.add_hook(session_manager), and the methods registered in
    register_hooks are used as event handlers.

    This class registers its own methods in register_hooks so that this class's
    append_message (a no-op) is called instead of the base manager's
    append_message (which saves to STM).

    Attributes:
        _base_manager: The underlying AgentCoreMemorySessionManager
        _actor_id: Persona ID
        _session_id: Session ID
    """

    def __init__(self, base_manager: Any, actor_id: str, session_id: str):
        """
        Initialize RetrieveOnlySessionManager

        Args:
            base_manager: The underlying AgentCoreMemorySessionManager
            actor_id: Persona ID
            session_id: Session ID
        """
        self._base_manager = base_manager
        self._actor_id = actor_id
        self._session_id = session_id
        self._messages: List[
            Any
        ] = []  # Keep messages locally (for the retrieval context)

        logger.info(
            f"Created RetrieveOnlySessionManager: actor_id={actor_id}, session_id={session_id}"
        )

    def initialize(self, agent: "Agent", **kwargs: Any) -> None:
        """
        Initialize the agent

        Does not call the base manager's initialization.
        Only the needed hooks are registered via register_hooks.

        Args:
            agent: Strands Agent
            **kwargs: Extra keyword arguments
        """
        # Do not call the base manager's initialization
        # (initialize may call register_hooks)
        logger.debug(
            f"RetrieveOnlySessionManager: Initialized for actor={self._actor_id} "
            "(retrieve-only mode, no base manager initialization)"
        )

    def register_hooks(self, registry: Any, **kwargs: Any) -> None:
        """
        Register hooks (only the LTM retrieval hook; the save hook is not registered)

        Important: calling the base manager's register_hooks would register the
        base manager's append_message as a hook,
        which would save to STM.

        By registering this class's own methods directly,
        saving is disabled and only LTM retrieval is enabled.

        Args:
            registry: HookRegistry
            **kwargs: Extra keyword arguments
        """
        try:
            from strands.hooks import MessageAddedEvent

            # Register the LTM retrieval hook (retrieve_customer_context)
            # Register retrieve_customer_context for MessageAddedEvent
            registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)

            # Note: append_message is not registered (disables STM saving)
            # Do not call the base manager's register_hooks

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
        Add a message (not saved to STM in retrieve-only mode)

        Important: AgentCore Memory automatically extracts STM into LTM, so
        create_event is not called in retrieve-only mode.

        This method is not registered via register_hooks, so it is normally not called.
        It is kept for compatibility.

        Args:
            message: Message to add
            agent: Strands Agent
            **kwargs: Extra keyword arguments
        """
        # Skip saving to STM (do not call create_event)
        logger.debug(
            f"RetrieveOnlySessionManager: append_message called but skipped "
            f"(retrieve-only mode) for actor={self._actor_id}"
        )

    def create_session(self, session: Any, **kwargs: Any) -> Any:
        """
        Create a session (no-op in retrieve-only mode)

        Args:
            session: Session object
            **kwargs: Extra keyword arguments

        Returns:
            Session object (unchanged)
        """
        logger.debug(
            f"RetrieveOnlySessionManager: create_session called but skipped "
            f"(retrieve-only mode) for actor={self._actor_id}"
        )
        return session

    def create_agent(self, session_id: str, session_agent: Any, **kwargs: Any) -> None:
        """
        Create an agent (no-op in retrieve-only mode)

        Args:
            session_id: Session ID
            session_agent: Session agent
            **kwargs: Extra keyword arguments
        """
        logger.debug(
            f"RetrieveOnlySessionManager: create_agent called but skipped "
            f"(retrieve-only mode) for actor={self._actor_id}"
        )

    def create_message(
        self, session_id: str, agent_id: str, session_message: Any, **kwargs: Any
    ) -> Optional[dict]:
        """
        Create a message (no-op in retrieve-only mode)

        Args:
            session_id: Session ID
            agent_id: Agent ID
            session_message: Session message
            **kwargs: Extra keyword arguments

        Returns:
            None (nothing is saved)
        """
        logger.debug(
            f"RetrieveOnlySessionManager: create_message called but skipped "
            f"(retrieve-only mode) for actor={self._actor_id}"
        )
        return None

    def retrieve_customer_context(self, event: Any) -> None:
        """
        Retrieve context from LTM (delegates to the base manager)

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
        Save messages (no-op in retrieve-only mode)

        Args:
            messages: List of messages to save (ignored)
        """
        # Do not save in retrieve-only mode
        logger.debug(
            f"RetrieveOnlySessionManager: save called but skipped (retrieve-only mode) "
            f"for actor={self._actor_id}, message_count={len(messages)}"
        )

    def get_messages(self) -> List[Any]:
        """
        Get the current message list

        Returns:
            List of messages
        """
        return self._messages

    def clear(self) -> None:
        """
        Clear messages
        """
        self._messages = []
        logger.debug(
            f"RetrieveOnlySessionManager: Cleared messages for actor={self._actor_id}"
        )

    @property
    def actor_id(self) -> str:
        """Get the persona ID."""
        return self._actor_id

    @property
    def session_id(self) -> str:
        """Get the session ID."""
        return self._session_id

    @property
    def config(self) -> Any:
        """Get the base manager's config."""
        return self._base_manager.config

    @property
    def memory_client(self) -> Any:
        """Get the base manager's memory client."""
        return self._base_manager.memory_client

    def __getattr__(self, name: str) -> Any:
        """
        Delegate undefined attributes to the base manager

        Args:
            name: Attribute name

        Returns:
            The base manager's attribute
        """
        return getattr(self._base_manager, name)
