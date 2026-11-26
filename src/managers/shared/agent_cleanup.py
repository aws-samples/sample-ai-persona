"""Shared utility for disposing agents.

Used by AgentDiscussionManager and InterviewManager: try ``dispose()`` on
every agent and log a warning on failure instead of leaking.
"""

import logging
from typing import Any, Callable, Iterable, Optional


def dispose_agents(
    agents: Iterable[Any],
    logger: logging.Logger,
    error_message: Callable[[Any, Exception], str] = (
        lambda agent, e: f"エージェント {agent!r} の解放中にエラー: {e}"
    ),
    success_message: Optional[Callable[[Any], str]] = None,
) -> None:
    """Try ``dispose()`` on every agent.

    One failure must not block the release of the others, so each exception
    is caught individually and only logged as a warning, never propagated
    (raising here would leave the remaining agents undisposed).

    Args:
        agents: Agents to dispose.
        logger: The caller's logger.
        error_message: (agent, exception) -> warning text (generic by default).
        success_message: agent -> debug text on success (no log if omitted).
    """
    for agent in agents:
        try:
            agent.dispose()
        except Exception as e:
            logger.warning(error_message(agent, e))
        else:
            if success_message is not None:
                logger.debug(success_message(agent))
