"""エージェント統合機能の純粋な合成ユーティリティ。

AgentDiscussionManager / InterviewManager から共通利用される。
**この層はビジネスルールを持たない**（architecture.md: shared 層の責務）。
有効判定（flag）・Service 呼び出し・DB 解決・重複除外・prompt section 生成はすべて
Manager が行い、この関数は完成済みの prompt section 群と tool group 群を連結するだけ。
"""

from typing import Any, List, Optional


def combine_integration(
    base_prompt: str,
    sections: List[str],
    tool_groups: List[List[Any]],
) -> tuple[str, Optional[List[Any]]]:
    """完成済みの prompt section 群と tool group 群を base_prompt に合成する。

    Args:
        base_prompt: ベースのシステムプロンプト
        sections: 追加する prompt section 文字列のリスト（空文字は無視）
        tool_groups: 追加する tool リストのリスト（空リストは無視）

    Returns:
        (enhanced_prompt, additional_tools or None)
    """
    enhanced_prompt = base_prompt
    for section in sections:
        if section:
            enhanced_prompt += section

    additional_tools: list[Any] = []
    for group in tool_groups:
        if group:
            additional_tools.extend(group)

    return enhanced_prompt, additional_tools if additional_tools else None
