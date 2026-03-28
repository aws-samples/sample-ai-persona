"""Survey関連モデル（TemplateImage, PersonaStatistics, VisualAnalysisData, InsightReport）の単体テスト"""
from datetime import datetime

from src.models.survey import InsightReport, VisualAnalysisData, PersonaStatistics
from src.models.survey_template import TemplateImage


class TestTemplateImage:
    def test_create_new(self):
        img = TemplateImage.create_new("商品画像", "survey_images/test.png", "image/png", "test.png")
        assert img.name == "商品画像"
        assert img.file_path == "survey_images/test.png"
        assert img.mime_type == "image/png"
        assert img.original_filename == "test.png"
        assert img.id

    def test_to_dict_from_dict_roundtrip(self):
        img = TemplateImage.create_new("img", "path/img.jpg", "image/jpeg", "orig.jpg")
        data = img.to_dict()
        restored = TemplateImage.from_dict(data)
        assert restored.id == img.id
        assert restored.name == img.name
        assert restored.file_path == img.file_path
        assert restored.mime_type == img.mime_type
        assert restored.original_filename == img.original_filename


class TestInsightReport:
    def test_create_new(self):
        report = InsightReport.create_new("survey-1", "レポート内容")
        assert report.survey_id == "survey-1"
        assert report.content == "レポート内容"
        assert report.id
        assert isinstance(report.created_at, datetime)

    def test_to_dict_from_dict_roundtrip(self):
        report = InsightReport.create_new("s1", "content")
        data = report.to_dict()
        restored = InsightReport.from_dict(data)
        assert restored.id == report.id
        assert restored.survey_id == report.survey_id
        assert restored.content == report.content
        assert restored.created_at == report.created_at


class TestVisualAnalysisData:
    def test_default_empty(self):
        data = VisualAnalysisData()
        assert data.multiple_choice_charts == []
        assert data.scale_rating_charts == []

    def test_with_data(self):
        charts = [{"question": "Q1", "data": [1, 2, 3]}]
        data = VisualAnalysisData(multiple_choice_charts=charts)
        assert len(data.multiple_choice_charts) == 1


class TestPersonaStatistics:
    def test_creation(self):
        stats = PersonaStatistics(
            total_count=100,
            sex_distribution={"男性": 50, "女性": 50},
            age_distribution={"20代": 30, "30代": 70},
            occupation_distribution={"会社員": 80, "自営業": 20},
            region_distribution={"関東": 60, "関西": 40},
            prefecture_distribution={"東京都": 40, "大阪府": 30, "その他": 30},
            marital_status_distribution={"既婚": 50, "未婚": 50},
            age_stats={"min": 20, "max": 59, "average": 35.5},
        )
        assert stats.total_count == 100
        assert stats.sex_distribution["男性"] == 50
        assert stats.age_stats["average"] == 35.5
