"""
エージェント破棄処理の共通ユーティリティ

AgentDiscussionManager, InterviewManager から共通利用される
「dispose()を試行し、失敗してもリークさせず警告ログに残す」ループ。
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
    """エージェント群のdispose()を全て試行する。

    1体の失敗が他のエージェントの解放を妨げないよう、例外は個別に捕捉して
    警告ログに残すのみで呼び出し元には伝播させない（リソースリーク防止が目的のため、
    ここで例外を投げると他のエージェントが未解放のまま残ってしまう）。

    Args:
        agents: 破棄対象のエージェント群
        logger: 呼び出し元のロガー
        error_message: (agent, exception) -> 警告ログの文言（既定は汎用文言）
        success_message: agent -> 成功時のdebugログ文言（省略時はログ出力しない）
    """
    for agent in agents:
        try:
            agent.dispose()
        except Exception as e:
            logger.warning(error_message(agent, e))
        else:
            if success_message is not None:
                logger.debug(success_message(agent))
