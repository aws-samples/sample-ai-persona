"""バッチ推論回答バリデーションのテスト"""

import pytest
from src.services.survey_service import SurveyService
from src.models.survey_template import Question


@pytest.fixture
def survey_service():
    """SurveyServiceインスタンス"""
    from unittest.mock import Mock

    ai_service = Mock()
    s3_service = Mock()

    service = SurveyService(ai_service=ai_service, s3_service=s3_service)
    return service


def test_validate_single_choice_valid(survey_service):
    """単一選択の有効な回答"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=False
    )
    result = survey_service._validate_answer("赤", question)
    assert result == "赤"


def test_validate_single_choice_invalid(survey_service):
    """単一選択の無効な回答（選択肢外）"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=False
    )
    result = survey_service._validate_answer("黄色", question)
    assert result == ""


def test_validate_single_choice_with_explanation(survey_service):
    """単一選択で説明文が含まれている場合（無効）"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=False
    )
    result = survey_service._validate_answer("赤が好きです", question)
    assert result == ""


def test_validate_multiple_choice_valid(survey_service):
    """複数選択の有効な回答"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=True, max_selections=3
    )
    result = survey_service._validate_answer("赤|青", question)
    assert result == "赤|青"


def test_validate_multiple_choice_partial_invalid(survey_service):
    """複数選択で一部が無効な場合（有効なもののみ残す）"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=True, max_selections=3
    )
    result = survey_service._validate_answer("赤|黄色|青", question)
    assert result == "赤|青"


def test_validate_multiple_choice_all_invalid(survey_service):
    """複数選択で全て無効な場合"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=True, max_selections=3
    )
    result = survey_service._validate_answer("黄色|紫", question)
    assert result == ""


def test_validate_multiple_choice_exceed_max(survey_service):
    """複数選択で最大数を超える場合（最大数まで切り詰め）"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑", "黄"], allow_multiple=True, max_selections=2
    )
    result = survey_service._validate_answer("赤|青|緑", question)
    assert result == "赤|青"


def test_validate_scale_rating_valid(survey_service):
    """スケール評価の有効な回答"""
    question = Question.create_scale_rating("満足度は？")
    question.scale_min = 1
    question.scale_max = 5
    result = survey_service._validate_answer("3", question)
    assert result == "3"


def test_validate_scale_rating_out_of_range(survey_service):
    """スケール評価で範囲外の回答"""
    question = Question.create_scale_rating("満足度は？")
    question.scale_min = 1
    question.scale_max = 5
    result = survey_service._validate_answer("6", question)
    assert result == ""


def test_validate_scale_rating_non_integer(survey_service):
    """スケール評価で整数以外の回答"""
    question = Question.create_scale_rating("満足度は？")
    question.scale_min = 1
    question.scale_max = 5
    result = survey_service._validate_answer("とても満足", question)
    assert result == ""


def test_validate_free_text(survey_service):
    """自由記述はそのまま返す"""
    question = Question.create_free_text("理由を教えてください")
    result = survey_service._validate_answer("これは自由記述の回答です", question)
    assert result == "これは自由記述の回答です"


def test_validate_empty_answer(survey_service):
    """空の回答"""
    question = Question.create_multiple_choice(
        "好きな色は？", ["赤", "青", "緑"], allow_multiple=False
    )
    result = survey_service._validate_answer("", question)
    assert result == ""

    result = survey_service._validate_answer("   ", question)
    assert result == ""
