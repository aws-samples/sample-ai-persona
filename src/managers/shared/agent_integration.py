"""
エージェント統合機能の共通オーケストレーション

AgentDiscussionManager, InterviewManager から共通利用される
KB/データセット統合のツール準備+プロンプト拡張ロジック。
"""

from typing import Any, List, Optional

from ...prompts.discussion_interview_prompts import (
    build_kb_prompt_section,
    build_dataset_prompt_section,
)


def prepare_integration_tools_and_prompt(
    agent_service: Any,
    database_service: Any,
    persona_id: str,
    base_prompt: str,
    enable_kb: bool,
    enable_dataset: bool,
) -> tuple[str, Optional[List[Any]]]:
    """KB/データセット統合のツールとプロンプト拡張を準備する。

    Returns:
        (enhanced_prompt, additional_tools or None)
    """
    additional_tools: list[Any] = []
    enhanced_prompt = base_prompt

    if enable_kb:
        kb_tools, kb_info = agent_service.get_kb_tools(persona_id, database_service)
        additional_tools.extend(kb_tools)
        if kb_info:
            enhanced_prompt += build_kb_prompt_section(**kb_info)

    if enable_dataset:
        ds_tools, bindings, datasets = agent_service.get_dataset_tools(
            persona_id, database_service
        )
        additional_tools.extend(ds_tools)
        if bindings and datasets:
            enhanced_prompt += build_dataset_prompt_section(bindings, datasets)

    return enhanced_prompt, additional_tools if additional_tools else None
