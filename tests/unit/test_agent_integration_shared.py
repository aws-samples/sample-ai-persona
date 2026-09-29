"""
shared/agent_integration.py 単体テスト

combine_integration は純粋な合成関数（service/db/flag に触れない）。
gate・DB 解決・build は Manager テスト側で検証する。
"""

import pytest
from unittest.mock import Mock

from src.managers.shared.agent_integration import combine_integration


@pytest.mark.unit
class TestCombineIntegration:
    """combine_integration のテスト"""

    def test_no_sections_no_tools(self):
        """section も tool も無ければプロンプト変更なし・ツールなし"""
        prompt, tools = combine_integration("base", [], [])
        assert prompt == "base"
        assert tools is None

    def test_empty_strings_and_lists_ignored(self):
        """空文字 section・空 tool グループは無視される"""
        prompt, tools = combine_integration("base", ["", ""], [[], []])
        assert prompt == "base"
        assert tools is None

    def test_sections_concatenated_in_order(self):
        """section は順に連結される"""
        prompt, _ = combine_integration("base", ["\nA", "\nB"], [])
        assert prompt == "base\nA\nB"

    def test_tool_groups_flattened(self):
        """tool グループは平坦化して結合される"""
        t1, t2, t3 = Mock(), Mock(), Mock()
        _, tools = combine_integration("base", [], [[t1], [t2, t3]])
        assert tools == [t1, t2, t3]

    def test_sections_and_tools_together(self):
        """section と tool の両方を合成する"""
        kb_tool, ds_tool = Mock(), Mock()
        prompt, tools = combine_integration(
            "base",
            ["\n# KB section", "\n# dataset section"],
            [[kb_tool], [ds_tool]],
        )
        assert "# KB section" in prompt
        assert "# dataset section" in prompt
        assert tools == [kb_tool, ds_tool]

    def test_is_pure_no_service_or_db_arguments(self):
        """combine_integration は base_prompt / sections / tool_groups のみを受け取る。

        （service / database_service / flag を引数に取らない = 純粋性の担保）
        """
        import inspect

        params = list(inspect.signature(combine_integration).parameters)
        assert params == ["base_prompt", "sections", "tool_groups"]
