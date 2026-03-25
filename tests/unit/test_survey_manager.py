"""
Unit tests for SurveyManager.

Tests cover template management, survey execution, results retrieval,
visual analysis computation, and insight report generation.
"""

from datetime import datetime
from unittest.mock import Mock

import pytest

from src.models.survey import InsightReport, Survey
from src.models.survey_template import Question, SurveyTemplate
from src.managers.survey_manager import (
    SurveyManager,
    SurveyManagerError,
    SurveyValidationError,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_db() -> Mock:
    db = Mock()
    db.save_survey_template.return_value = "tmpl-id"
    db.get_survey_template.return_value = None
    db.get_all_survey_templates.return_value = []
    db.update_survey_template.return_value = True
    db.delete_survey_template.return_value = True
    db.save_survey.return_value = "survey-id"
    db.get_survey.return_value = None
    db.get_all_surveys.return_value = []
    db.update_survey.return_value = True
    return db


@pytest.fixture
def mock_survey_service() -> Mock:
    svc = Mock()
    svc.load_results_from_s3.return_value = b"col1,col2\nv1,v2"
    svc.parse_results_csv.return_value = []
    return svc


@pytest.fixture
def manager(mock_db, mock_survey_service) -> SurveyManager:
    return SurveyManager(
        database_service=mock_db,
        survey_service=mock_survey_service,
    )


@pytest.fixture
def sample_questions() -> list[Question]:
    return [
        Question.create_multiple_choice("Q1", ["A", "B", "C"]),
        Question.create_free_text("Q2"),
        Question.create_scale_rating("Q3"),
    ]


@pytest.fixture
def saved_template(sample_questions) -> SurveyTemplate:
    return SurveyTemplate.create_new(name="テスト", questions=sample_questions)


# =============================================================================
# テンプレート管理
# =============================================================================


class TestCreateTemplate:
    def test_creates_and_saves(self, manager, mock_db, sample_questions):
        result = manager.create_template("テスト", sample_questions)
        assert result.name == "テスト"
        assert len(result.questions) == 3
        mock_db.save_survey_template.assert_called_once()

    def test_strips_name(self, manager, sample_questions):
        result = manager.create_template("  テスト  ", sample_questions)
        assert result.name == "テスト"

    def test_rejects_empty_name(self, manager, sample_questions):
        with pytest.raises(SurveyValidationError):
            manager.create_template("", sample_questions)

    def test_rejects_whitespace_name(self, manager, sample_questions):
        with pytest.raises(SurveyValidationError):
            manager.create_template("   ", sample_questions)

    def test_rejects_empty_questions(self, manager):
        with pytest.raises(SurveyValidationError):
            manager.create_template("テスト", [])

    def test_rejects_choice_with_one_option(self, manager):
        bad_q = Question.create_multiple_choice("Q", ["A"])
        with pytest.raises(SurveyValidationError):
            manager.create_template("テスト", [bad_q])

    def test_rejects_choice_with_zero_options(self, manager):
        bad_q = Question(id="q1", text="Q", question_type="multiple_choice", options=[])
        with pytest.raises(SurveyValidationError):
            manager.create_template("テスト", [bad_q])


class TestGetTemplate:
    def test_returns_template(self, manager, mock_db, saved_template):
        mock_db.get_survey_template.return_value = saved_template
        result = manager.get_template(saved_template.id)
        assert result == saved_template

    def test_returns_none_when_not_found(self, manager):
        assert manager.get_template("nonexistent") is None


class TestUpdateTemplate:
    def test_updates_template(self, manager, mock_db, saved_template, sample_questions):
        mock_db.get_survey_template.return_value = saved_template
        result = manager.update_template(saved_template.id, "新名前", sample_questions)
        assert result.name == "新名前"
        assert result.updated_at > saved_template.updated_at
        mock_db.update_survey_template.assert_called_once()

    def test_raises_when_not_found(self, manager, sample_questions):
        with pytest.raises(SurveyManagerError, match="見つかりません"):
            manager.update_template("bad-id", "名前", sample_questions)

    def test_validates_name(self, manager, mock_db, saved_template, sample_questions):
        mock_db.get_survey_template.return_value = saved_template
        with pytest.raises(SurveyValidationError):
            manager.update_template(saved_template.id, "", sample_questions)


class TestDeleteTemplate:
    def test_deletes(self, manager, mock_db):
        assert manager.delete_template("tmpl-id") is True
        mock_db.delete_survey_template.assert_called_once_with("tmpl-id")


# =============================================================================
# アンケート実行
# =============================================================================


class TestStartSurvey:
    def test_rejects_persona_count_zero(self, manager):
        with pytest.raises(SurveyValidationError):
            manager.start_survey("tmpl-id", persona_count=0)

    def test_rejects_persona_count_negative(self, manager):
        with pytest.raises(SurveyValidationError):
            manager.start_survey("tmpl-id", persona_count=-1)

    def test_rejects_persona_count_over_9999(self, manager):
        with pytest.raises(SurveyValidationError):
            manager.start_survey("tmpl-id", persona_count=10000)

    def test_rejects_missing_template(self, manager, mock_db):
        mock_db.get_survey_template.return_value = None
        with pytest.raises(SurveyManagerError, match="見つかりません"):
            manager.start_survey("bad-id", persona_count=100)


class TestGenerateDefaultSurveyName:
    def test_format(self):
        name = SurveyManager.generate_default_survey_name("テスト")
        date_str = datetime.now().strftime("%Y%m%d")
        assert name == f"テスト {date_str}"


# =============================================================================
# 結果取得・分析
# =============================================================================


class TestGetAllSurveys:
    def test_sorted_descending(self, manager, mock_db):
        s1 = Survey.create_new("S1", "", "t1", 10)
        s2 = Survey.create_new("S2", "", "t1", 10)
        # s1 is older
        s1.created_at = datetime(2025, 1, 1)
        s2.created_at = datetime(2025, 6, 1)
        mock_db.get_all_surveys.return_value = [s1, s2]

        result = manager.get_all_surveys()
        assert result[0].name == "S2"
        assert result[1].name == "S1"


class TestDownloadResultsCsv:
    def test_returns_bytes(self, manager, mock_db, mock_survey_service):
        survey = Survey.create_new("S", "", "t1", 10)
        survey.s3_result_path = "s3://bucket/path.csv"
        mock_db.get_survey.return_value = survey
        mock_survey_service.load_results_from_s3.return_value = b"csv-data"

        result = manager.download_results_csv(survey.id)
        assert result == b"csv-data"

    def test_raises_when_no_survey(self, manager):
        with pytest.raises(SurveyManagerError, match="見つかりません"):
            manager.download_results_csv("bad-id")

    def test_raises_when_no_results(self, manager, mock_db):
        survey = Survey.create_new("S", "", "t1", 10)
        survey.s3_result_path = None
        mock_db.get_survey.return_value = survey
        with pytest.raises(SurveyManagerError, match="まだ生成されていません"):
            manager.download_results_csv(survey.id)


class TestGetVisualAnalysis:
    def test_multiple_choice_distribution(self, manager, mock_db, mock_survey_service):
        template = SurveyTemplate.create_new(
            "T", [Question.create_multiple_choice("色は？", ["赤", "青", "緑"])]
        )
        q_id = template.questions[0].id
        survey = Survey.create_new("S", "", template.id, 3)
        survey.s3_result_path = "s3://bucket/path.csv"
        mock_db.get_survey.return_value = survey
        mock_db.get_survey_template.return_value = template

        # Build CSV bytes with answers
        import csv as csv_mod
        import io

        buf = io.StringIO()
        w = csv_mod.writer(buf, quoting=csv_mod.QUOTE_ALL)
        w.writerow(["persona_id", f"{q_id}_text", f"{q_id}_answer"])
        w.writerow(["p1", "色は？", "赤"])
        w.writerow(["p2", "色は？", "青"])
        w.writerow(["p3", "色は？", "赤"])
        csv_bytes = buf.getvalue().encode("utf-8-sig")
        mock_survey_service.load_results_from_s3.return_value = csv_bytes

        result = manager.get_visual_analysis(survey.id)
        assert len(result.multiple_choice_charts) == 1
        chart = result.multiple_choice_charts[0]
        # 赤=2, 青=1, 緑=0
        idx_red = chart["options"].index("赤")
        idx_blue = chart["options"].index("青")
        idx_green = chart["options"].index("緑")
        assert chart["counts"][idx_red] == 2
        assert chart["counts"][idx_blue] == 1
        assert chart["counts"][idx_green] == 0

    def test_scale_rating_average(self, manager, mock_db, mock_survey_service):
        template = SurveyTemplate.create_new(
            "T", [Question.create_scale_rating("満足度")]
        )
        q_id = template.questions[0].id
        survey = Survey.create_new("S", "", template.id, 3)
        survey.s3_result_path = "s3://bucket/path.csv"
        mock_db.get_survey.return_value = survey
        mock_db.get_survey_template.return_value = template

        import csv as csv_mod
        import io

        buf = io.StringIO()
        w = csv_mod.writer(buf, quoting=csv_mod.QUOTE_ALL)
        w.writerow(["persona_id", f"{q_id}_text", f"{q_id}_answer"])
        w.writerow(["p1", "満足度", "3"])
        w.writerow(["p2", "満足度", "5"])
        w.writerow(["p3", "満足度", "4"])
        csv_bytes = buf.getvalue().encode("utf-8-sig")
        mock_survey_service.load_results_from_s3.return_value = csv_bytes

        result = manager.get_visual_analysis(survey.id)
        assert len(result.scale_rating_charts) == 1
        chart = result.scale_rating_charts[0]
        assert chart["average"] == 4.0
        assert chart["distribution"][3] == 1
        assert chart["distribution"][5] == 1
        assert chart["distribution"][4] == 1


class TestGenerateInsightReport:
    def test_generates_and_saves(self, manager, mock_db, mock_survey_service):
        template = SurveyTemplate.create_new("T", [Question.create_free_text("Q")])
        survey = Survey.create_new("S", "", template.id, 10)
        survey.s3_result_path = "s3://bucket/path.csv"
        mock_db.get_survey.return_value = survey
        mock_db.get_survey_template.return_value = template
        mock_survey_service.load_results_from_s3.return_value = b"data"

        report = InsightReport.create_new(survey.id, "insight content")
        mock_survey_service.generate_insights.return_value = report

        result = manager.generate_insight_report(survey.id)
        assert result.survey_id == survey.id
        assert result.content == "insight content"
        mock_db.update_survey.assert_called()

    def test_raises_when_no_survey(self, manager):
        with pytest.raises(SurveyManagerError, match="見つかりません"):
            manager.generate_insight_report("bad-id")

    def test_raises_when_no_results(self, manager, mock_db):
        survey = Survey.create_new("S", "", "t1", 10)
        survey.s3_result_path = None
        mock_db.get_survey.return_value = survey
        with pytest.raises(SurveyManagerError, match="まだ生成されていません"):
            manager.generate_insight_report(survey.id)
