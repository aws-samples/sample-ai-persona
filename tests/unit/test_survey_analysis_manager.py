"""SurveyAnalysisManager のユニットテスト"""

from unittest.mock import Mock

import pytest

from src.managers.survey_analysis_manager import (
    SurveyAnalysisManager,
    SurveyAnalysisManagerError,
)
from src.models.survey import (
    InsightReport,
    PersonaStatistics,
    Survey,
    VisualAnalysisData,
)
from src.models.survey_template import Question, SurveyTemplate


@pytest.fixture
def mock_db() -> Mock:
    return Mock()


@pytest.fixture
def mock_s3_service() -> Mock:
    return Mock()


@pytest.fixture
def mock_ai_service() -> Mock:
    return Mock()


@pytest.fixture
def mgr(
    mock_db: Mock, mock_s3_service: Mock, mock_ai_service: Mock
) -> SurveyAnalysisManager:
    return SurveyAnalysisManager(
        database_service=mock_db,
        s3_service=mock_s3_service,
        ai_service=mock_ai_service,
    )


@pytest.fixture
def sample_template() -> SurveyTemplate:
    return SurveyTemplate.create_new(
        name="テスト",
        questions=[
            Question.create_multiple_choice("好きな色は？", ["赤", "青", "緑"]),
            Question.create_scale_rating("満足度は？"),
            Question.create_free_text("自由に書いてください"),
        ],
    )


def _make_csv_bytes(template: SurveyTemplate) -> bytes:
    """テスト用CSVバイト列を生成"""
    q_mc = template.questions[0]
    q_scale = template.questions[1]
    q_free = template.questions[2]

    header = f"sex,age,{q_mc.id}_answer,{q_scale.id}_answer,{q_free.id}_answer"
    rows = [
        "男性,25,赤,4,テスト回答1",
        "女性,35,青,5,テスト回答2",
        "男性,45,赤,3,テスト回答3",
    ]
    csv_text = "\n".join([header] + rows)
    return csv_text.encode("utf-8-sig")


class TestGetVisualAnalysis:
    def test_success(
        self,
        mgr: SurveyAnalysisManager,
        mock_db: Mock,
        mock_s3_service: Mock,
        sample_template: SurveyTemplate,
    ) -> None:
        mock_db.get_survey.return_value = Mock(
            s3_result_path="s3://bucket/result.csv",
            template_id="tid",
        )
        mock_db.get_survey_template.return_value = sample_template
        mock_s3_service.download_file.return_value = _make_csv_bytes(sample_template)

        result = mgr.get_visual_analysis("sid")
        assert isinstance(result, VisualAnalysisData)
        assert len(result.multiple_choice_charts) == 1
        assert len(result.scale_rating_charts) == 1

    def test_survey_not_found(self, mgr: SurveyAnalysisManager, mock_db: Mock) -> None:
        mock_db.get_survey.return_value = None
        with pytest.raises(SurveyAnalysisManagerError):
            mgr.get_visual_analysis("missing")

    def test_no_result_path(self, mgr: SurveyAnalysisManager, mock_db: Mock) -> None:
        mock_db.get_survey.return_value = Mock(s3_result_path="")
        with pytest.raises(SurveyAnalysisManagerError):
            mgr.get_visual_analysis("sid")


class TestComputeVisualAnalysis:
    def test_multiple_choice_counts(
        self, mgr: SurveyAnalysisManager, sample_template: SurveyTemplate
    ) -> None:
        q_mc = sample_template.questions[0]
        rows = [
            {f"{q_mc.id}_answer": "赤", "sex": "男性", "age": "25"},
            {f"{q_mc.id}_answer": "赤", "sex": "女性", "age": "35"},
            {f"{q_mc.id}_answer": "青", "sex": "男性", "age": "45"},
        ]
        result = mgr._compute_visual_analysis(rows, sample_template)
        chart = result.multiple_choice_charts[0]
        assert chart["counts"][chart["options"].index("赤")] == 2
        assert chart["counts"][chart["options"].index("青")] == 1

    def test_scale_average(
        self, mgr: SurveyAnalysisManager, sample_template: SurveyTemplate
    ) -> None:
        q_scale = sample_template.questions[1]
        rows = [
            {f"{q_scale.id}_answer": "4", "sex": "男性", "age": "25"},
            {f"{q_scale.id}_answer": "5", "sex": "女性", "age": "35"},
            {f"{q_scale.id}_answer": "3", "sex": "男性", "age": "45"},
        ]
        result = mgr._compute_visual_analysis(rows, sample_template)
        chart = result.scale_rating_charts[0]
        assert chart["average"] == 4.0


class TestGetPersonaStatistics:
    def test_success(
        self,
        mgr: SurveyAnalysisManager,
        mock_db: Mock,
        mock_s3_service: Mock,
    ) -> None:
        csv_text = "sex,age,occupation,region\n男性,25,エンジニア,関東\n女性,35,デザイナー,関西\n男性,28,エンジニア,関東"
        mock_db.get_survey.return_value = Mock(s3_result_path="s3://bucket/result.csv")
        mock_s3_service.download_file.return_value = csv_text.encode("utf-8-sig")

        result = mgr.get_persona_statistics("sid")
        assert isinstance(result, PersonaStatistics)
        assert result.total_count == 3
        assert result.sex_distribution["男性"] == 2
        assert result.age_stats["average"] == pytest.approx(29.3, abs=0.1)


class TestSaveInsightReport:
    def test_success(self, mgr: SurveyAnalysisManager, mock_db: Mock) -> None:
        mock_db.get_survey.return_value = Mock(spec=Survey)
        report = mgr.save_insight_report("sid", "# レポート内容")
        assert isinstance(report, InsightReport)
        assert report.content == "# レポート内容"
        assert report.survey_id == "sid"
        mock_db.update_survey.assert_called_once()

    def test_survey_not_found(self, mgr: SurveyAnalysisManager, mock_db: Mock) -> None:
        mock_db.get_survey.return_value = None
        with pytest.raises(SurveyAnalysisManagerError):
            mgr.save_insight_report("missing", "content")


class TestGetAgeBracket:
    def test_normal(self) -> None:
        assert SurveyAnalysisManager._get_age_bracket("25") == "20代"
        assert SurveyAnalysisManager._get_age_bracket("39") == "30代"

    def test_invalid(self) -> None:
        assert SurveyAnalysisManager._get_age_bracket("") is None
        assert SurveyAnalysisManager._get_age_bracket("abc") is None


class TestCsvCache:
    def test_cache_hit(self, mgr: SurveyAnalysisManager, mock_s3_service: Mock) -> None:
        mock_s3_service.download_file.return_value = b"csv_data"
        mgr._load_results_csv("s3://bucket/key")
        mgr._load_results_csv("s3://bucket/key")
        mock_s3_service.download_file.assert_called_once()
