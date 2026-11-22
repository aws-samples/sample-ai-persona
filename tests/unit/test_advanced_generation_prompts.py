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


class TestDocumentPrompts:
    """U4: BR25.3, BR26.1, BR26.4, BR27.2, BR28.1 (the wording the agents get)."""

    def test_full_text_asks_for_quoted_evidence(self) -> None:
        prompt = prompts.synthesis_prompt(
            2, has_table=False, has_documents=True, full_text=True
        )

        assert "document_evidence" in prompt
        assert "segment_id は空" in prompt
        # BR27.2: value, pain and goal for every persona of documents alone
        assert "value" in prompt and "pain" in prompt and "goal" in prompt

    def test_full_text_with_a_table_asks_for_the_segment(self) -> None:
        prompt = prompts.synthesis_prompt(
            2, has_table=True, has_documents=True, full_text=True
        )

        assert "assignment_reason" in prompt
        assert "全体に対する根拠" in prompt

    def test_the_table_prompt_is_as_before(self) -> None:
        prompt = prompts.synthesis_prompt(3)

        assert "document_evidence" not in prompt
        assert "segments のどれか 1 つ" in prompt

    def test_extraction_tells_the_limits(self) -> None:
        prompt = prompts.evidence_prompt("", 20, 200)

        assert "20 件" in prompt and "200 字" in prompt

    def test_the_extractor_quotes_and_never_follows_the_text(self) -> None:
        assert "そのまま写して" in prompts.EVIDENCE_INSTRUCTIONS
        assert 'source="document"' in prompts.EVIDENCE_INSTRUCTIONS

    def test_the_assignment_picks_zero_or_one_segment(self) -> None:
        assert "0 か 1 つ" in prompts.ASSIGNMENT_INSTRUCTIONS
        fields = prompts.EvidenceAssignmentItem.model_fields
        assert fields["segment_id"].default is None

    def test_a_persona_of_documents_alone_has_no_segment(self) -> None:
        persona = prompts.AdvancedPersonaOutput(
            name="a",
            age=30,
            occupation="o",
            background="b",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )

        assert persona.segment_id is None

    def test_schemas_use_lists_not_maps_for_attributes(self) -> None:
        # Structured output does not take dict fields (agent_service.py note)
        field = prompts.EvidenceItemOutput.model_fields["speaker_attributes"]
        assert "list" in str(field.annotation)
