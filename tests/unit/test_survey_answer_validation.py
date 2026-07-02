"""バッチ推論回答バリデーションのテスト"""

from src.managers.survey_execution_manager import SurveyExecutionManager
from src.models.survey_template import Question


def test_validate_single_choice_valid():
    """単一選択の有効な回答"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=False
    )
    result = SurveyExecutionManager._validate_answer("赤", question)
    assert result == "赤"


def test_validate_single_choice_invalid():
    """単一選択の無効な回答（選択肢外）"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=False
    )
    result = SurveyExecutionManager._validate_answer("黄色", question)
    assert result == ""


def test_validate_single_choice_with_explanation():
    """単一選択で選択肢+説明が含まれる場合（無効）"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=False
    )
    result = SurveyExecutionManager._validate_answer("赤が好きです", question)
    assert result == ""


def test_validate_multiple_choice_valid():
    """複数選択の有効な回答"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=True
    )
    result = SurveyExecutionManager._validate_answer("赤|青", question)
    assert result == "赤|青"


def test_validate_multiple_choice_partial_invalid():
    """複数選択で一部無効な回答"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=True
    )
    result = SurveyExecutionManager._validate_answer("赤|黄色|青", question)
    assert result == "赤|青"


def test_validate_multiple_choice_all_invalid():
    """複数選択で全て無効な回答"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=True
    )
    result = SurveyExecutionManager._validate_answer("黄色|紫", question)
    assert result == ""


def test_validate_multiple_choice_exceed_max():
    """複数選択で最大数超過"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=True, max_selections=2
    )
    result = SurveyExecutionManager._validate_answer("赤|青|緑", question)
    assert result == "赤|青"


def test_validate_scale_rating_valid():
    """スケール評価の有効な回答"""
    question = Question.create_scale_rating("満足度は？")
    result = SurveyExecutionManager._validate_answer("3", question)
    assert result == "3"


def test_validate_scale_rating_out_of_range():
    """スケール評価の範囲外回答"""
    question = Question.create_scale_rating("満足度は？")
    result = SurveyExecutionManager._validate_answer("6", question)
    assert result == ""


def test_validate_scale_rating_non_integer():
    """スケール評価の非数値回答"""
    question = Question.create_scale_rating("満足度は？")
    result = SurveyExecutionManager._validate_answer("とても満足", question)
    assert result == ""


def test_validate_free_text():
    """自由記述はそのまま返る"""
    question = Question.create_free_text("ご意見をどうぞ")
    result = SurveyExecutionManager._validate_answer(
        "これは自由記述の回答です", question
    )
    assert result == "これは自由記述の回答です"


def test_validate_empty_answer():
    """空回答は空文字列を返す"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=False
    )
    result = SurveyExecutionManager._validate_answer("", question)
    assert result == ""

    result = SurveyExecutionManager._validate_answer("   ", question)
    assert result == ""
