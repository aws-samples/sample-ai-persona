"""Synthesis prompts of the advanced generation: persona quality (U1).

The synthesis agent writes the personas from aggregates only, so it gets the
same persona writing guide as the ordinary generation (one source in
``src/prompts/persona_generation_prompts.py``), and the "do not invent" rule
is limited to numbers.
"""

from __future__ import annotations

import pytest

from src.managers.advanced_generation import prompts
from src.prompts import persona_generation_prompts as common

pytestmark = pytest.mark.unit


class TestSharedPersonaGuide:
    def test_ordinary_generation_uses_the_shared_guide(self) -> None:
        template = common.PERSONA_GENERATION_SYSTEM_PROMPT_TEMPLATE
        assert common.PERSONA_PURPOSE_AND_PRINCIPLES in template
        assert common.PERSONA_FIELDS_GUIDE in template

    def test_synthesis_uses_the_shared_guide(self) -> None:
        instructions = prompts.SYNTHESIS_INSTRUCTIONS
        assert common.PERSONA_PURPOSE_AND_PRINCIPLES in instructions
        assert common.PERSONA_FIELDS_GUIDE in instructions


class TestSynthesisInstructions:
    def test_the_no_invention_rule_is_about_numbers(self) -> None:
        instructions = prompts.SYNTHESIS_INSTRUCTIONS
        # Numbers come from the evidence only; the person around them is
        # imagined (forbidding everything made the personas a restatement of
        # the segment statistics).
        assert "新しい数値を作らない" in instructions
        assert "具体的に想像して" in instructions

    def test_asks_for_a_concrete_person_beyond_the_statistics(self) -> None:
        instructions = prompts.SYNTHESIS_INSTRUCTIONS
        assert "言い換え" in instructions
        assert "断り書き" in instructions


class TestPersonaSchema:
    @pytest.mark.parametrize("field", ["background", "values", "pain_points", "goals"])
    def test_descriptions_are_as_detailed_as_the_ordinary_one(self, field: str) -> None:
        description = prompts.AdvancedPersonaOutput.model_fields[field].description
        assert description is not None
        assert len(description) >= 20

    def test_background_lists_what_to_write(self) -> None:
        description = prompts.AdvancedPersonaOutput.model_fields[
            "background"
        ].description
        assert description is not None
        for item in ("家族構成", "生活リズム", "意思決定", "コミュニケーション"):
            assert item in description
