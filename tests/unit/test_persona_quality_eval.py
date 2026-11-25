"""The pure parts of the persona quality evaluation (U6 addendum)."""

from __future__ import annotations

import pytest

from tests.e2e.persona_quality import (
    CRITERIA,
    JudgeScoreError,
    distinctness,
    invented_numbers,
    judge_prompt,
    parse_scores,
)


class TestParseScores:
    def test_scores_inside_a_json_object_are_read(self) -> None:
        reply = (
            "評価です。\n```json\n"
            + '{"concrete": 4, "distinct": 3, "consistent": 5, "usable": 4,'
            + ' "restraint": 2, "comment": "具体的"}\n```'
        )

        scores = parse_scores(reply)

        assert scores.values == {c: v for c, v in zip(CRITERIA, (4, 3, 5, 4, 2))}
        assert scores.comment == "具体的"
        assert scores.mean == pytest.approx(3.6)

    def test_a_reply_cut_off_inside_its_comment_keeps_the_scores(self) -> None:
        reply = (
            '{"concrete": 4, "distinct": 3, "consistent": 5, "usable": 4,'
            ' "restraint": 2, "comment": "途中で切れた講'
        )

        scores = parse_scores(reply)

        assert scores.values["usable"] == 4

    def test_a_comment_that_breaks_the_json_does_not_lose_the_scores(self) -> None:
        reply = (
            '{"concrete": 4, "distinct": 3, "consistent": 5, "usable": 4,'
            ' "restraint": 2, "comment": "1行目\n2行目の"引用"}'
        )

        scores = parse_scores(reply)

        assert scores.values["restraint"] == 2
        assert "1行目" in scores.comment

    @pytest.mark.parametrize(
        "reply",
        [
            "no json at all",
            '{"concrete": 4}',
            '{"concrete": 9, "distinct": 3, "consistent": 5, "usable": 4, "restraint": 2}',
            '{"concrete": "high", "distinct": 3, "consistent": 5, "usable": 4, "restraint": 2}',
        ],
    )
    def test_a_reply_that_is_not_a_full_score_is_an_error(self, reply: str) -> None:
        with pytest.raises(JudgeScoreError):
            parse_scores(reply)


class TestJudgePrompt:
    def test_the_rubric_and_the_material_are_in_the_prompt(self) -> None:
        prompt = judge_prompt(
            purpose="優良顧客を知りたい",
            segments_text="優良 33% / 年齢 43.5（全体 42.1）",
            personas_text="村上 麻衣（44）",
        )

        for criterion in CRITERIA:
            assert criterion in prompt
        assert "優良顧客を知りたい" in prompt
        assert "年齢 43.5（全体 42.1）" in prompt
        assert "村上 麻衣" in prompt


class TestInventedNumbers:
    def test_numbers_that_are_not_in_the_material_are_counted(self) -> None:
        material = "優良 33% 年齢 43.5 全体 42.1 60 人"

        found = invented_numbers("年齢は43.5。月に12回、3,000円ほど使う。", material)

        assert found == ["12", "3,000"]

    def test_nothing_is_invented_when_every_number_is_in_the_material(self) -> None:
        assert invented_numbers("33% の層", "優良 33%") == []

    def test_ages_and_family_counts_are_not_counted(self) -> None:
        # a persona's own age is part of the persona, not a claim of the data
        assert invented_numbers("44歳。子ども2人。", "優良 33%", allowed=("44",)) == [
            "2"
        ]


class TestDistinctness:
    def test_the_same_text_is_not_distinct(self) -> None:
        text = "平日は会社に行き、週末にまとめて買い物をする。"

        assert distinctness([text, text]) == pytest.approx(0.0)

    def test_different_texts_are_distinct(self) -> None:
        score = distinctness(["ゴールド会員で毎月買う", "購入が途絶えて半年たつ"])

        assert score > 0.5

    def test_one_text_has_nothing_to_compare(self) -> None:
        assert distinctness(["だけ"]) == 1.0
