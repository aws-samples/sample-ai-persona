"""SurveyBatchService.generate_statistical_summary の空欄の扱い（BR4.6、FR5.5）。

取れなかった人は CSV で回答の列が空欄になる（BR4.3）。設問ごとに空欄の行を除いてから
数え、割合の分母は回答のあった人数にする。行は捨てず、属性の集計には残す。
"""

import pytest

from src.services.survey_batch_service import SurveyBatchService

pytestmark = pytest.mark.integration

_HEADER = "persona_id,sex,age,occupation,country,region,prefecture,marital_status,"
_QUESTIONS = [
    {
        "id": "q1",
        "text": "単一",
        "question_type": "multiple_choice",
        "options": ["A", "B"],
        "allow_multiple": False,
    },
    {
        "id": "q2",
        "text": "複数",
        "question_type": "multiple_choice",
        "options": ["X", "Y"],
        "allow_multiple": True,
    },
    {"id": "q3", "text": "5段階", "question_type": "scale_rating"},
    {"id": "q4", "text": "自由", "question_type": "free_text"},
]


def _csv() -> str:
    header = (
        _HEADER
        + "q1_text,q1_answer,q2_text,q2_answer,q3_text,q3_answer,q4_text,q4_answer"
    )
    rows = [
        "p1,男,30,会社員,日本,関東,東京都,未婚,単一,A,複数,X|Y,5段階,4,自由,良い",
        "p2,女,40,会社員,日本,関西,大阪府,既婚,単一,B,複数,Y,5段階,2,自由,普通",
        "p3,女,50,自営業,日本,関東,東京都,既婚,単一,A,複数,X,5段階,5,自由,楽しい",
        # 取れなかった人（回答の列が空欄）
        "p4,男,20,学生,日本,関東,東京都,未婚,単一,,複数,,5段階,,自由,",
    ]
    return "\n".join([header, *rows]) + "\n"


@pytest.fixture
def summary() -> dict:
    return SurveyBatchService("bucket", "ap-northeast-1").generate_statistical_summary(
        _csv(), _QUESTIONS
    )


def _analysis(summary: dict, qid: str) -> dict:
    return next(a for a in summary["question_analysis"] if a["question_id"] == qid)


def test_rows_stay_in_the_demographics(summary):
    assert summary["total_responses"] == 4
    sexes = {row["sex"]: row["count"] for row in summary["demographics"]["sex"]}
    assert sexes == {"男": 2, "女": 2}


def test_single_choice_drops_blanks_and_uses_answered_count(summary):
    distribution = _analysis(summary, "q1")["distribution"]
    assert set(distribution) == {"A", "B"}
    assert distribution["A"] == {"count": 2, "percentage": 66.7}
    assert distribution["B"]["percentage"] == 33.3


def test_multiple_choice_denominator_is_answered_count(summary):
    distribution = _analysis(summary, "q2")["distribution"]
    assert distribution["X"] == {"count": 2, "percentage": 66.7}
    assert distribution["Y"] == {"count": 2, "percentage": 66.7}


def test_scale_rating_ignores_blanks(summary):
    statistics = _analysis(summary, "q3")["statistics"]
    assert statistics["mean"] == pytest.approx(3.67, abs=0.01)
    counts = sum(row["count"] for row in statistics["distribution"])
    assert counts == 3


def test_cross_tab_has_no_blank_answer_group(summary):
    by_sex = _analysis(summary, "q1")["by_demographics"]["by_sex"]
    assert all(row["q1_answer"] not in (None, "") for row in by_sex)
    assert sum(row["count"] for row in by_sex) == 3


def test_free_text_samples_skip_blanks(summary):
    samples = _analysis(summary, "q4")["sample_responses"]
    assert sorted(samples) == sorted(["良い", "普通", "楽しい"])


def test_all_blank_question_has_an_empty_distribution():
    csv_text = (
        _HEADER
        + "q1_text,q1_answer\n"
        + "p1,男,30,会社員,日本,関東,東京都,未婚,単一,\n"
    )
    summary = SurveyBatchService("b", "r").generate_statistical_summary(
        csv_text, [_QUESTIONS[0]]
    )
    assert _analysis(summary, "q1")["distribution"] == {}
