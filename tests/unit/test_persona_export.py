"""PersonaManager.export_persona（Markdown のエクスポート、U5）。

BR7.1〜BR7.3、AC4.1.2、entities.md PersonaExport。
"""

from dataclasses import replace

import pytest

from src.models.errors import CodedError, ErrorCode
from src.models.persona_binding import PersonaBinding
from tests.error_helpers import raises_code
from tests.unit.persona_u5_world import (
    ADMIN_P,
    A,
    B,
    NOW,
    SALES_PEER,
    World,
    make_persona,
)


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def persona(world):
    p = make_persona(A.sub, ["sales"], name="佐藤 花子")
    p = replace(
        p,
        gender="female",
        country="JP",
        city="東京",
        generation_log=[
            {"type": "thinking", "content": "一行目\n# 見出しにしない"},
            {"type": "tool_call", "content": "query: SELECT 1"},
        ],
        generation_context={"data_type": "interview", "persona_count": 1},
    )
    world.add_persona(p)
    world.bindings.put(PersonaBinding.for_kb(p.id, "kb-secret-id", now=NOW))
    return p


class TestWhoMayExport:
    @pytest.mark.parametrize("who", [A, SALES_PEER, ADMIN_P])
    def test_every_reader_gets_the_same_document(self, world, persona, who):
        owner_export = world.manager().export_persona(A, persona.id)
        assert world.manager().export_persona(who, persona.id) == owner_export

    def test_unrelated_user_gets_not_found(self, world, persona):
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            world.manager().export_persona(B, persona.id)


class TestContent:
    def test_profile_and_generation_record_are_included(self, world, persona):
        body = world.manager().export_persona(A, persona.id).body

        for text in (
            "佐藤 花子",
            "34",
            "会社員",
            "東京で働く",
            "効率",
            "時間",
            "成長",
            "都市",
            "東京",
        ):
            assert text in body
        assert "interview" in body
        assert "> 一行目" in body
        assert "query: SELECT 1" in body

    def test_owner_shares_memory_and_bindings_are_not_included(self, world, persona):
        body = world.manager().export_persona(A, persona.id).body
        assert A.sub not in body
        assert "sales" not in body
        assert "kb-secret-id" not in body
        world.memory.list_memories.assert_not_called()

    def test_markdown_in_values_does_not_break_the_document(self, world):
        hostile = world.add_persona(
            replace(
                make_persona(A.sub),
                name="# 見出し\n- リスト",
                background="```\nコード\n```",
                values=["*強調*", "[リンク](http://x)"],
            )
        )
        body = world.manager().export_persona(A, hostile.id).body
        lines = body.splitlines()

        assert lines[0].startswith("# ")  # the one document title
        assert not any(line.startswith("# 見出し") for line in lines)
        assert not any(line.startswith("- リスト") for line in lines)
        assert "```\nコード" not in body
        assert "\\*強調\\*" in body
        assert "\\[リンク\\]" in body
        # the thinking log keeps every line inside a quote
        assert "> # 見出しにしない" not in body or "\\#" in body


class TestFilename:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("佐藤 花子", "佐藤 花子.md"),
            ('a/b\\c:d*e?f"g<h>i|j', "a_b_c_d_e_f_g_h_i_j.md"),
            ("   ", "persona.md"),
            ("改行\nあり", "改行_あり.md"),
        ],
    )
    def test_unusable_characters_are_replaced(self, world, name, expected):
        p = world.add_persona(replace(make_persona(A.sub), name=name))
        assert world.manager().export_persona(A, p.id).filename == expected
