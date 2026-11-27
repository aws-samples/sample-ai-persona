"""Tests for the insight extraction prompt."""

from src.models.message import Message
from src.prompts.insight_prompts import build_insight_extraction_prompt


def _statements(rounds: int) -> list[Message]:
    return [
        Message.create_new(
            persona_id="p1",
            persona_name="田中太郎",
            content=f"ラウンド{n}の発言",
            round_number=n,
        )
        for n in range(1, rounds + 1)
    ]


class TestInsightExtractionPrompt:
    def test_statements_from_every_round_are_included(self):
        """Early rounds carry the concrete episodes, so none are dropped."""
        prompt = build_insight_extraction_prompt(_statements(5), None)
        for n in range(1, 6):
            assert f"ラウンド{n}の発言" in prompt

    def test_statements_are_labelled_with_their_round(self):
        prompt = build_insight_extraction_prompt(_statements(2), None)
        assert "[R1] **田中太郎**: ラウンド1の発言" in prompt
        assert "[R2] **田中太郎**: ラウンド2の発言" in prompt

    def test_statements_without_round_have_no_label(self):
        """Classic-mode messages have no round number."""
        messages = [Message.create_new("p1", "田中太郎", "発言です")]
        prompt = build_insight_extraction_prompt(messages, None)
        assert "\n**田中太郎**: 発言です" in prompt

    def test_hypotheses_are_allowed_with_observation_format(self):
        prompt = build_insight_extraction_prompt(_statements(1), None)
        assert "観察: …／仮説: …" in prompt

    def test_an_observation_alone_is_asked_for_plain_findings(self):
        """Not every insight is a hypothesis: plain findings stay observations."""
        prompt = build_insight_extraction_prompt(_statements(1), None)
        assert "「観察: …」だけで書いて" in prompt
        assert "観察を言い直しただけの仮説は付けない" in prompt
        # The output example shows both shapes, so the model does not copy one
        assert '"description": "観察: 発言に基づく事実"' in prompt

    def test_confidence_reflects_evidence_strength(self):
        """One persona's concrete episode can still score high."""
        prompt = build_insight_extraction_prompt(_statements(1), None)
        assert "根拠の強さ" in prompt
        assert "1人の具体的な体験" in prompt

    def test_minority_views_are_requested(self):
        prompt = build_insight_extraction_prompt(_statements(1), None)
        assert "最低2個" in prompt

    def test_participant_profiles_are_given_with_instructions(self):
        prompt = build_insight_extraction_prompt(
            _statements(1),
            None,
            participant_profiles=["- 田中太郎: 35歳・男性・会社員"],
        )
        assert "# 参加者のプロフィール\n- 田中太郎: 35歳・男性・会社員" in prompt
        assert "名前だけでなくプロフィール" in prompt
        assert "どんな人（層）に当てはまるか" in prompt

    def test_no_profile_section_without_profiles(self):
        prompt = build_insight_extraction_prompt(_statements(1), None)
        assert "参加者のプロフィール" not in prompt

    def test_supporting_messages_are_requested(self):
        prompt = build_insight_extraction_prompt(_statements(1), None)
        assert '"supporting_messages"' in prompt
