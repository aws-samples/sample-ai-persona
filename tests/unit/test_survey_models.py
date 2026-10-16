"""Survey関連モデル（Survey, TemplateImage, PersonaStatistics, VisualAnalysisData, InsightReport）の単体テスト"""

from datetime import datetime

from src.models.survey import (
    Survey,
    InsightReport,
    TemplateSnapshot,
    VisualAnalysisData,
    PersonaStatistics,
)
from src.models.survey_template import Question, SurveyTemplate, TemplateImage


class TestTemplateImage:
    def test_create_new(self):
        img = TemplateImage.create_new(
            "商品画像", "survey_images/test.png", "image/png", "test.png"
        )
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


class TestSurvey:
    def test_create_new_with_datasource(self):
        survey = Survey.create_new(
            name="テスト",
            description="説明",
            template_id="tmpl-1",
            persona_count=200,
            datasource="custom_segment",
        )
        assert survey.datasource == "custom_segment"
        assert survey.status == "pending"

    def test_create_new_without_datasource(self):
        survey = Survey.create_new(
            name="テスト",
            description="",
            template_id="tmpl-1",
            persona_count=100,
        )
        assert survey.datasource is None

    def test_to_dict_includes_datasource(self):
        survey = Survey.create_new(
            name="テスト",
            description="",
            template_id="tmpl-1",
            persona_count=100,
            datasource="nemotron",
        )
        data = survey.to_dict()
        assert data["datasource"] == "nemotron"

    def test_to_dict_excludes_none_datasource(self):
        survey = Survey.create_new(
            name="テスト",
            description="",
            template_id="tmpl-1",
            persona_count=100,
        )
        data = survey.to_dict()
        assert "datasource" not in data

    def test_from_dict_with_datasource(self):
        survey = Survey.create_new(
            name="テスト",
            description="",
            template_id="tmpl-1",
            persona_count=100,
            datasource="my_data",
        )
        data = survey.to_dict()
        restored = Survey.from_dict(data)
        assert restored.datasource == "my_data"

    def test_from_dict_without_datasource_field(self):
        """既存データ（datasourceフィールドなし）との後方互換性"""
        data = {
            "id": "test-id",
            "name": "テスト",
            "description": "",
            "template_id": "tmpl-1",
            "persona_count": 100,
            "filters": None,
            "status": "completed",
            "s3_result_path": None,
            "insight_report": None,
            "created_at": "2025-01-01T00:00:00",
            "updated_at": "2025-01-01T00:00:00",
            "error_message": None,
        }
        restored = Survey.from_dict(data)
        assert restored.datasource is None


# =============================================================================
# U7: 所有者・共有先・テンプレートの写し（entities.md、約束 C8）
# =============================================================================


def _legacy_template_dict() -> dict:
    """A template written before v1.0.0 (no owner, no shares)."""
    return {
        "id": "tmpl-old",
        "name": "古いテンプレート",
        "questions": [
            {
                "id": "q1",
                "text": "好きな色は？",
                "question_type": "free_text",
                "options": [],
                "scale_min": 1,
                "scale_max": 5,
            }
        ],
        "created_at": "2025-01-01T00:00:00",
        "updated_at": "2025-01-01T00:00:00",
    }


class TestSurveyTemplateOwnership:
    def test_create_new_has_no_owner_and_no_shares(self):
        template = SurveyTemplate.create_new(
            name="t", questions=[Question.create_free_text("Q")]
        )
        assert template.owner_sub is None
        assert template.shared_group_ids == frozenset()

    def test_owned_by_returns_a_new_instance(self):
        template = SurveyTemplate.create_new(
            name="t", questions=[Question.create_free_text("Q")]
        )
        owned = template.owned_by("user-a")
        assert owned.owner_sub == "user-a"
        assert template.owner_sub is None
        assert owned.id == template.id

    def test_to_dict_omits_missing_owner_and_empty_shares(self):
        template = SurveyTemplate.create_new(
            name="t", questions=[Question.create_free_text("Q")]
        )
        data = template.to_dict()
        assert "owner_sub" not in data
        assert "shared_group_ids" not in data

    def test_to_dict_and_from_dict_keep_owner_and_shares(self):
        data = {
            **_legacy_template_dict(),
            "owner_sub": "user-a",
            "shared_group_ids": {"sales", "plan"},
        }
        template = SurveyTemplate.from_dict(data)
        assert template.owner_sub == "user-a"
        assert template.shared_group_ids == frozenset({"sales", "plan"})
        out = template.to_dict()
        assert out["owner_sub"] == "user-a"
        assert out["shared_group_ids"] == ["plan", "sales"]

    def test_from_dict_reads_a_template_without_owner(self):
        template = SurveyTemplate.from_dict(_legacy_template_dict())
        assert template.owner_sub is None
        assert template.shared_group_ids == frozenset()

    def test_edited_keeps_owner_shares_and_created_at(self):
        template = SurveyTemplate.from_dict(
            {
                **_legacy_template_dict(),
                "owner_sub": "user-a",
                "shared_group_ids": ["sales"],
            }
        )
        edited = template.edited(
            name="新しい名前", questions=[Question.create_free_text("Q2")], images=[]
        )
        assert edited.name == "新しい名前"
        assert edited.owner_sub == "user-a"
        assert edited.shared_group_ids == frozenset({"sales"})
        assert edited.created_at == template.created_at
        assert edited.updated_at >= template.updated_at


class TestTemplateSnapshot:
    def test_from_template_copies_name_questions_and_image_refs(self):
        image = TemplateImage.create_new(
            "商品", "s3://b/uploads/u/survey_image/x_a.png", "image/png", "a.png"
        )
        template = SurveyTemplate.create_new(
            name="t", questions=[Question.create_free_text("Q")], images=[image]
        )
        snapshot = TemplateSnapshot.from_template(template)
        assert snapshot.name == "t"
        assert [q.text for q in snapshot.questions] == ["Q"]
        assert snapshot.images[0].id == image.id
        assert snapshot.image(image.id) == image
        assert snapshot.image("other") is None

    def test_roundtrip(self):
        template = SurveyTemplate.create_new(
            name="t", questions=[Question.create_scale_rating("満足度")]
        )
        snapshot = TemplateSnapshot.from_template(template)
        restored = TemplateSnapshot.from_dict(snapshot.to_dict())
        assert restored == snapshot

    def test_to_dict_omits_empty_images(self):
        template = SurveyTemplate.create_new(
            name="t", questions=[Question.create_free_text("Q")]
        )
        assert "images" not in TemplateSnapshot.from_template(template).to_dict()

    def test_from_dict_converts_decimal_scale_values(self):
        from decimal import Decimal

        data = {
            "name": "t",
            "questions": [
                {
                    "id": "q1",
                    "text": "Q",
                    "question_type": "scale_rating",
                    "options": [],
                    "scale_min": Decimal("1"),
                    "scale_max": Decimal("7"),
                }
            ],
        }
        snapshot = TemplateSnapshot.from_dict(data)
        assert snapshot.questions[0].scale_max == 7


class TestSurveyOwnershipAndSnapshot:
    def _survey(self, **kwargs: object) -> Survey:
        template = SurveyTemplate.create_new(
            name="テンプレ", questions=[Question.create_free_text("Q")]
        )
        return Survey.create_new(
            name="s",
            description="",
            template_id=template.id,
            persona_count=100,
            datasource="nemotron",
            owner_sub="user-a",
            template_snapshot=TemplateSnapshot.from_template(template),
            **kwargs,
        )

    def test_create_new_records_owner_snapshot_and_no_shares(self):
        survey = self._survey(datasource_name=None)
        assert survey.owner_sub == "user-a"
        assert survey.shared_group_ids == frozenset()
        assert survey.template_snapshot is not None
        assert survey.template_snapshot.name == "テンプレ"

    def test_to_dict_from_dict_keeps_snapshot_and_datasource_name(self):
        survey = self._survey(datasource_name="顧客データ")
        data = survey.to_dict()
        assert data["owner_sub"] == "user-a"
        assert data["datasource_name"] == "顧客データ"
        assert data["template_snapshot"]["name"] == "テンプレ"
        assert "shared_group_ids" not in data
        restored = Survey.from_dict({**data, "shared_group_ids": {"sales"}})
        assert restored.owner_sub == "user-a"
        assert restored.datasource_name == "顧客データ"
        assert restored.template_snapshot == survey.template_snapshot
        assert restored.shared_group_ids == frozenset({"sales"})

    def test_to_dict_omits_missing_owner_snapshot_and_datasource_name(self):
        survey = Survey.create_new(
            name="s", description="", template_id="t", persona_count=100
        )
        data = survey.to_dict()
        for key in (
            "owner_sub",
            "template_snapshot",
            "datasource_name",
            "shared_group_ids",
        ):
            assert key not in data

    def test_from_dict_reads_a_survey_before_v1(self):
        data = {
            "id": "old",
            "name": "古い",
            "description": "",
            "template_id": "t",
            "persona_count": 100,
            "status": "completed",
            "created_at": "2025-01-01T00:00:00",
            "updated_at": "2025-01-01T00:00:00",
        }
        survey = Survey.from_dict(data)
        assert survey.owner_sub is None
        assert survey.template_snapshot is None
        assert survey.shared_group_ids == frozenset()

    def test_template_name_prefers_the_snapshot(self):
        assert self._survey().template_name == "テンプレ"
        legacy = Survey.create_new(
            name="s", description="", template_id="t", persona_count=100
        )
        assert legacy.template_name is None
