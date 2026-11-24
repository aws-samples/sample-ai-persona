"""The structure of what the synthesis wrote (U6 addendum, review of the result)."""

from __future__ import annotations

from typing import Any

from src.managers.advanced_generation.persona_check import check_personas
from src.managers.advanced_generation.prompts import AdvancedPersonaOutput


def _persona(**changes: Any) -> AdvancedPersonaOutput:
    fields: dict[str, Any] = {
        "name": "佐藤 美咲",
        "age": 40,
        "occupation": "会社員",
        "background": "共働きで、週末にまとめて買い物をする。",
        "values": ["時間を大切にする"],
        "pain_points": ["比較する時間がない"],
        "goals": ["家事を減らす"],
        "segment_id": "seg-1",
        "evidence_ids": ["ev-1"],
    }
    fields.update(changes)
    return AdvancedPersonaOutput(**fields)


class TestEveryPersonaIsComplete:
    def test_a_full_persona_passes(self) -> None:
        assert check_personas([_persona()], ["seg-1"], has_table=True) is None

    def test_empty_values_pain_points_or_goals_fail(self) -> None:
        for field in ("values", "pain_points", "goals"):
            persona = _persona(**{field: []})
            assert check_personas([persona], ["seg-1"], has_table=True) == "incomplete"

    def test_blank_text_fields_fail(self) -> None:
        for field in ("name", "occupation", "background"):
            persona = _persona(**{field: "  "})
            assert check_personas([persona], ["seg-1"], has_table=True) == "incomplete"

    def test_a_list_holding_only_blanks_fails(self) -> None:
        persona = _persona(goals=["", " "])

        assert check_personas([persona], ["seg-1"], has_table=True) == "incomplete"

    def test_a_table_persona_without_evidence_fails(self) -> None:
        persona = _persona(evidence_ids=[])

        assert check_personas([persona], ["seg-1"], has_table=True) == "no_evidence"


class TestEveryLayerIsStoodFor:
    def test_two_personas_for_the_same_layer_fail(self) -> None:
        personas = [_persona(), _persona(name="田中 健")]

        reason = check_personas(personas, ["seg-1", "seg-2"], has_table=True)

        assert reason == "coverage"

    def test_one_persona_for_each_layer_passes(self) -> None:
        personas = [_persona(), _persona(name="田中 健", segment_id="seg-2")]

        assert check_personas(personas, ["seg-1", "seg-2"], has_table=True) is None

    def test_more_layers_than_personas_needs_different_layers(self) -> None:
        personas = [_persona(), _persona(name="田中 健", segment_id="seg-3")]

        assert (
            check_personas(personas, ["seg-1", "seg-2", "seg-3"], has_table=True)
            is None
        )

    def test_more_personas_than_layers_needs_every_layer(self) -> None:
        personas = [
            _persona(),
            _persona(name="B"),
            _persona(name="C", segment_id="seg-2"),
        ]

        assert check_personas(personas, ["seg-1", "seg-2"], has_table=True) is None

    def test_documents_only_have_no_layers_to_cover(self) -> None:
        personas = [_persona(segment_id=None), _persona(name="B", segment_id=None)]

        assert check_personas(personas, [], has_table=False) is None
