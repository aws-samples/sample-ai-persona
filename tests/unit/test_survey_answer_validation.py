"""回答のスキーマと、応答の読み取り・検査のテスト（rules.md BR5.1〜BR5.6）。

依頼の構造化出力のスキーマは設問 ID をキーにした必須のオブジェクトで、値は設問の種類で決まる。
応答が JSON でない・設問が欠ける・値が列挙の外や型違いなら、その人は取れなかった回答になる
（一部の設問だけを残すことはしない、BR5.5）。
"""

import json

import pytest

from src.managers.survey_run_manager import parse_answers
from src.models.survey_template import Question
from src.prompts.survey_prompts import build_answer_schema, format_questions_for_prompt

SINGLE = Question(
    id="q1",
    text="好きな色は？",
    question_type="multiple_choice",
    options=["赤", "青|紺", "改行\nあり"],
)
MULTI = Question(
    id="q2",
    text="好きな食べ物は？",
    question_type="multiple_choice",
    options=["寿司", "ラーメン", "カレー"],
    allow_multiple=True,
    max_selections=2,
)
SCALE = Question(
    id="q3", text="満足度は？", question_type="scale_rating", scale_min=1, scale_max=5
)
FREE = Question(id="q4", text="ご意見は？", question_type="free_text")
QUESTIONS = [SINGLE, MULTI, SCALE, FREE]


def _text(**answers: object) -> str:
    base: dict[str, object] = {"q1": "赤", "q2": ["寿司"], "q3": 3, "q4": "とても良い"}
    base.update(answers)
    return json.dumps({"answers": base}, ensure_ascii=False)


class TestSchema:
    def test_answers_object_requires_every_question(self):
        """BR5.1: 設問 ID をキーにしたオブジェクト。全設問を必須にし、余計なキーは許さない。"""
        schema = build_answer_schema(QUESTIONS)

        assert schema["required"] == ["answers"]
        assert schema["additionalProperties"] is False
        answers = schema["properties"]["answers"]
        assert answers["type"] == "object"
        assert answers["required"] == ["q1", "q2", "q3", "q4"]
        assert answers["additionalProperties"] is False

    def test_single_choice_is_the_option_enum(self):
        """BR5.2・FR6.1: 選択肢の文字列をそのまま列挙する（記号や改行を含んでも）。"""
        prop = build_answer_schema(QUESTIONS)["properties"]["answers"]["properties"][
            "q1"
        ]
        assert prop == {"type": "string", "enum": ["赤", "青|紺", "改行\nあり"]}

    def test_multiple_choice_is_an_array_of_the_enum(self):
        """BR5.3: 選択肢の列挙を要素にした配列。"""
        prop = build_answer_schema(QUESTIONS)["properties"]["answers"]["properties"][
            "q2"
        ]
        assert prop["type"] == "array"
        assert prop["items"] == {
            "type": "string",
            "enum": ["寿司", "ラーメン", "カレー"],
        }

    def test_multiple_choice_needs_at_least_one_choice(self):
        """BR5.3: 空の配列は回答として使えない（parse_answers が外す）ので、スキーマでも許さない。

        構造化出力の配列の制約は minItems の 0 と 1 だけが使える。
        """
        prop = build_answer_schema(QUESTIONS)["properties"]["answers"]["properties"][
            "q2"
        ]
        assert prop["minItems"] == 1
        assert parse_answers(_text(q2=[]), QUESTIONS) is None

    def test_scale_is_the_integer_enum(self):
        """BR5.4: scale_min〜scale_max の整数の列挙。"""
        prop = build_answer_schema(QUESTIONS)["properties"]["answers"]["properties"][
            "q3"
        ]
        assert prop == {"type": "integer", "enum": [1, 2, 3, 4, 5]}

    def test_free_text_is_a_string(self):
        """BR5.6"""
        prop = build_answer_schema(QUESTIONS)["properties"]["answers"]["properties"][
            "q4"
        ]
        assert prop == {"type": "string"}


class TestParseAnswers:
    def test_valid_response_becomes_csv_values(self):
        answers = parse_answers(_text(q2=["寿司", "カレー"]), QUESTIONS)
        assert answers == {
            "q1": "赤",
            "q2": "寿司|カレー",
            "q3": "3",
            "q4": "とても良い",
        }

    def test_options_with_symbols_match_as_they_are(self):
        assert parse_answers(_text(q1="改行\nあり"), QUESTIONS)["q1"] == "改行\nあり"

    def test_duplicates_are_removed_and_the_count_is_cut(self):
        """BR5.3: 重複を除き、max_selections を超えたら先頭から切る（今の振る舞い）。"""
        answers = parse_answers(
            _text(q2=["寿司", "寿司", "ラーメン", "カレー"]), QUESTIONS
        )
        assert answers["q2"] == "寿司|ラーメン"

    @pytest.mark.parametrize(
        "text",
        [
            "これは JSON ではない",
            json.dumps({"answer": {}}),
            json.dumps({"answers": []}),
            _text(q1="黄色"),
            _text(q2=["寿司", "パスタ"]),
            _text(q2=[]),
            _text(q2="寿司"),
            _text(q3=6),
            _text(q3="3"),
            _text(q3=True),
            _text(q4=5),
        ],
    )
    def test_anything_outside_the_schema_is_unusable(self, text):
        """BR5.5: 一部の設問だけを残さず、その人を取れなかった回答にする。"""
        assert parse_answers(text, QUESTIONS) is None

    def test_missing_question_is_unusable(self):
        data = json.loads(_text())
        del data["answers"]["q3"]
        assert parse_answers(json.dumps(data), QUESTIONS) is None

    def test_no_text_is_unusable(self):
        assert parse_answers(None, QUESTIONS) is None


class TestPromptWordingIsUnchanged:
    """NFR2.4: 指示文と並びは今のまま。"""

    def test_single_choice_wording(self):
        text = format_questions_for_prompt([SINGLE])
        assert "質問1 (ID: q1): 好きな色は？" in text
        assert "【必ず以下の選択肢から1つ選んでください】" in text
        assert "  1. 赤" in text

    def test_multiple_choice_wording(self):
        text = format_questions_for_prompt([MULTI])
        assert "タイプ: 選択式・複数回答（最大2個まで）" in text
        assert "【回答例】寿司|ラーメン" in text

    def test_scale_and_free_text_wording(self):
        text = format_questions_for_prompt([SCALE, FREE])
        assert "タイプ: スケール評価（1〜5の整数で回答してください）" in text
        assert "質問2 (ID: q4): ご意見は？" in text
        assert "200文字以内" in text
