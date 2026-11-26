"""Pure composition helper for agent integration features.

Used by AgentDiscussionManager and InterviewManager. **This layer holds no
business rules** (shared layer, see architecture.md): the Manager does the
enable check (flag), Service calls, DB resolution, de-duplication and prompt
section building; this function only joins the finished prompt sections and
tool groups.
"""

from typing import Any, List, Optional


def combine_integration(
    base_prompt: str,
    sections: List[str],
    tool_groups: List[List[Any]],
) -> tuple[str, Optional[List[Any]]]:
    """Join finished prompt sections and tool groups onto ``base_prompt``.

    Args:
        base_prompt: Base system prompt.
        sections: Prompt section strings to add (empty strings are ignored).
        tool_groups: Tool lists to add (empty lists are ignored).

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
