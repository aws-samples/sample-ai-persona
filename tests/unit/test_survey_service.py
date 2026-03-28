"""
Unit tests for SurveyService.

Tests cover persona dataset filtering, sampling, prompt building,
CSV generation/parsing, and insight report generation.
Uses Polars DataFrames instead of Pandas.
"""

import csv
import io
from unittest.mock import Mock

import polars as pl
import pytest

from src.models.survey import InsightReport
from src.models.survey_template import Question, SurveyTemplate
from src.services.survey_service import (
    SurveyService,
    SurveyServiceError,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_ai_service_for_survey() -> Mock:
    mock = Mock()
    mock._invoke_model.return_value = "テストインサイトレポート内容"
    return mock


@pytest.fixture
def mock_s3_service() -> Mock:
    mock = Mock()
    mock.bucket_name = "test-bucket"
    mock.upload_file.return_value = "s3://test-bucket/survey-results/test/results.csv"
    mock.download_file.return_value = b"header1,header2\nval1,val2"
    return mock


@pytest.fixture
def survey_service(mock_ai_service_for_survey, mock_s3_service) -> SurveyService:
    return SurveyService(
        ai_service=mock_ai_service_for_survey,
        s3_service=mock_s3_service,
    )


@pytest.fixture
def sample_persona_df() -> pl.DataFrame:
    """テスト用の小規模ペルソナPolars DataFrame"""
    return pl.DataFrame(
        [
            {
                "uuid": "p001",
                "sex": "女",
                "age": 35,
                "occupation": "会社員",
                "country": "日本",
                "region": "関東地方",
                "prefecture": "東京都",
                "marital_status": "既婚",
                "education_level": "大学卒",
                "persona": "マーケティング担当者",
                "cultural_background": "東京育ち",
                "skills_and_expertise": "データ分析",
                "hobbies_and_interests": "読書",
                "career_goals_and_ambitions": "マネージャー昇進",
            },
            {
                "uuid": "p002",
                "sex": "男",
                "age": 28,
                "occupation": "エンジニア",
                "country": "日本",
                "region": "関西地方",
                "prefecture": "大阪府",
                "marital_status": "未婚",
                "education_level": "大学院卒",
                "persona": "ソフトウェアエンジニア",
                "cultural_background": "大阪育ち",
                "skills_and_expertise": "プログラミング",
                "hobbies_and_interests": "ゲーム",
                "career_goals_and_ambitions": "CTO",
            },
            {
                "uuid": "p003",
                "sex": "女",
                "age": 45,
                "occupation": "教師",
                "country": "日本",
                "region": "関東地方",
                "prefecture": "神奈川県",
                "marital_status": "既婚",
                "education_level": "大学卒",
                "persona": "高校教師",
                "cultural_background": "横浜育ち",
                "skills_and_expertise": "教育指導",
                "hobbies_and_interests": "旅行",
                "career_goals_and_ambitions": "教頭",
            },
        ]
    )


@pytest.fixture
def sample_template() -> SurveyTemplate:
    return SurveyTemplate.create_new(
        name="テストアンケート",
        questions=[
            Question.create_multiple_choice("好きな色は？", ["赤", "青", "緑"]),
            Question.create_free_text("改善点を教えてください"),
            Question.create_scale_rating("満足度を評価してください"),
        ],
    )


# =============================================================================
# 4.1: ペルソナデータセットのフィルタリング機能（Polarsインメモリ）
# =============================================================================


class TestFilterPersonas:
    """filter_personas のテスト（dfパラメータ渡し＝Polarsインメモリフィルタ）"""

    def test_filter_by_sex(
        self, survey_service: SurveyService, sample_persona_df: pl.DataFrame
    ) -> None:
        result = survey_service.filter_personas({"性別": "女"}, df=sample_persona_df)
        assert len(result) == 2
        assert all(v == "女" for v in result["sex"].to_list())

    def test_filter_by_region(
        self, survey_service: SurveyService, sample_persona_df: pl.DataFrame
    ) -> None:
        result = survey_service.filter_personas(
            {"居住地域": "関東地方"}, df=sample_persona_df
        )
        assert len(result) == 2

    def test_filter_by_multiple_conditions(
        self, survey_service: SurveyService, sample_persona_df: pl.DataFrame
    ) -> None:
        result = survey_service.filter_personas(
            {"性別": "女", "居住地域": "関東地方"}, df=sample_persona_df
        )
        assert len(result) == 2

    def test_filter_by_list_values(
        self, survey_service: SurveyService, sample_persona_df: pl.DataFrame
    ) -> None:
        result = survey_service.filter_personas(
            {"職業": ["会社員", "教師"]}, df=sample_persona_df
        )
        assert len(result) == 2

    def test_filter_no_match(
        self, survey_service: SurveyService, sample_persona_df: pl.DataFrame
    ) -> None:
        result = survey_service.filter_personas(
            {"性別": "その他"}, df=sample_persona_df
        )
        assert len(result) == 0

    def test_filter_empty_filters(
        self, survey_service: SurveyService, sample_persona_df: pl.DataFrame
    ) -> None:
        result = survey_service.filter_personas({}, df=sample_persona_df)
        assert len(result) == 3

    def test_filter_unknown_column_ignored(
        self, survey_service: SurveyService, sample_persona_df: pl.DataFrame
    ) -> None:
        result = survey_service.filter_personas(
            {"unknown_col": "value"}, df=sample_persona_df
        )
        assert len(result) == 3

    def test_filter_none_value_ignored(
        self, survey_service: SurveyService, sample_persona_df: pl.DataFrame
    ) -> None:
        result = survey_service.filter_personas({"性別": None}, df=sample_persona_df)
        assert len(result) == 3

    def test_filter_empty_list_ignored(
        self, survey_service: SurveyService, sample_persona_df: pl.DataFrame
    ) -> None:
        result = survey_service.filter_personas({"性別": []}, df=sample_persona_df)
        assert len(result) == 3


class TestSamplePersonas:
    """sample_personas のテスト"""

    def test_sample_exact_count(
        self, survey_service: SurveyService, sample_persona_df: pl.DataFrame
    ) -> None:
        result = survey_service.sample_personas(sample_persona_df, 2)
        assert len(result) == 2

    def test_sample_more_than_available(
        self, survey_service: SurveyService, sample_persona_df: pl.DataFrame
    ) -> None:
        result = survey_service.sample_personas(sample_persona_df, 100)
        assert len(result) == 3

    def test_sample_zero(
        self, survey_service: SurveyService, sample_persona_df: pl.DataFrame
    ) -> None:
        result = survey_service.sample_personas(sample_persona_df, 0)
        assert len(result) == 0

    def test_sample_from_empty_df(self, survey_service: SurveyService) -> None:
        empty_df = pl.DataFrame({"uuid": [], "sex": []})
        result = survey_service.sample_personas(empty_df, 5)
        assert len(result) == 0


# =============================================================================
# 4.2: Parquet S3配置
# =============================================================================


class TestEnsureParquetOnS3:
    """_ensure_parquet_on_s3 のテスト"""

    def test_returns_cached_uri(self, survey_service: SurveyService) -> None:
        survey_service._parquet_s3_uri = "s3://bucket/cached.parquet"
        result = survey_service._ensure_parquet_on_s3()
        assert result == "s3://bucket/cached.parquet"

    def test_skips_download_when_exists(
        self, survey_service: SurveyService, mock_s3_service: Mock
    ) -> None:
        survey_service._parquet_s3_uri = None
        # head_object succeeds → file exists
        mock_s3_service.s3_client.head_object.return_value = {}
        result = survey_service._ensure_parquet_on_s3()
        assert "test-bucket" in result
        assert survey_service._parquet_s3_uri is not None


# =============================================================================
# 4.3: ペルソナプロンプト構築とバッチ推論
# =============================================================================


class TestBuildPersonaPrompts:
    """build_persona_prompts のテスト"""

    def test_builds_correct_number_of_prompts(
        self,
        survey_service: SurveyService,
        sample_persona_df: pl.DataFrame,
        sample_template: SurveyTemplate,
    ) -> None:
        prompts = survey_service.build_persona_prompts(
            sample_persona_df, sample_template
        )
        assert len(prompts) == 3

    def test_prompt_structure(
        self,
        survey_service: SurveyService,
        sample_persona_df: pl.DataFrame,
        sample_template: SurveyTemplate,
    ) -> None:
        prompts = survey_service.build_persona_prompts(
            sample_persona_df, sample_template
        )
        prompt = prompts[0]
        assert "recordId" in prompt
        assert "modelInput" in prompt
        model_input = prompt["modelInput"]
        assert "anthropic_version" in model_input
        assert "max_tokens" in model_input
        assert "system" in model_input
        assert "messages" in model_input

    def test_system_prompt_contains_attributes(
        self,
        survey_service: SurveyService,
        sample_persona_df: pl.DataFrame,
        sample_template: SurveyTemplate,
    ) -> None:
        prompts = survey_service.build_persona_prompts(
            sample_persona_df, sample_template
        )
        system_prompt = prompts[0]["modelInput"]["system"]
        assert "女" in system_prompt
        assert "35" in system_prompt
        assert "会社員" in system_prompt
        assert "日本" in system_prompt
        assert "関東地方" in system_prompt
        assert "既婚" in system_prompt

    def test_user_message_contains_questions(
        self,
        survey_service: SurveyService,
        sample_persona_df: pl.DataFrame,
        sample_template: SurveyTemplate,
    ) -> None:
        prompts = survey_service.build_persona_prompts(
            sample_persona_df, sample_template
        )
        content = prompts[0]["modelInput"]["messages"][0]["content"]
        # content is now a list of dicts; extract all text
        user_msg = " ".join(c["text"] for c in content if c.get("type") == "text")
        assert "好きな色は？" in user_msg
        assert "改善点を教えてください" in user_msg
        assert "満足度を評価してください" in user_msg


# =============================================================================
# 4.5: CSV結果ファイルの生成・読み込み
# =============================================================================


class TestCSVOperations:
    """CSV生成・パースのテスト"""

    def test_build_csv_bytes(self) -> None:
        headers = ["col1", "col2"]
        rows = [["a", "b"], ["c", "d"]]
        result = SurveyService._build_csv_bytes(headers, rows)
        text = result.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text))
        parsed_rows = list(reader)
        assert len(parsed_rows) == 3  # header + 2 rows

    def test_parse_results_csv(self) -> None:
        csv_content = '"persona_id","sex","age"\n"p001","女","35"\n"p002","男","28"\n'
        csv_bytes = csv_content.encode("utf-8-sig")
        result = SurveyService.parse_results_csv(csv_bytes)
        assert len(result) == 2
        assert result[0]["persona_id"] == "p001"
        assert result[0]["sex"] == "女"

    def test_csv_roundtrip(self) -> None:
        """CSV書き出し→パースのラウンドトリップ"""
        headers = ["persona_id", "sex", "q1_text", "q1_answer"]
        rows = [
            ["p001", "女", "好きな色は？", "赤"],
            ["p002", "男", "好きな色は？", "青"],
        ]
        csv_bytes = SurveyService._build_csv_bytes(headers, rows)
        parsed = SurveyService.parse_results_csv(csv_bytes)
        assert len(parsed) == 2
        assert parsed[0]["persona_id"] == "p001"
        assert parsed[0]["q1_answer"] == "赤"
        assert parsed[1]["persona_id"] == "p002"
        assert parsed[1]["q1_answer"] == "青"

    def test_load_results_from_s3(
        self, survey_service: SurveyService, mock_s3_service: Mock
    ) -> None:
        result = survey_service.load_results_from_s3("s3://bucket/path")
        mock_s3_service.download_file.assert_called_once_with("s3://bucket/path")

    def test_load_results_from_s3_error(
        self, survey_service: SurveyService, mock_s3_service: Mock
    ) -> None:
        mock_s3_service.download_file.side_effect = Exception("S3 error")
        with pytest.raises(SurveyServiceError):
            survey_service.load_results_from_s3("s3://bucket/path")


class TestSaveResultsToS3:
    """save_results_to_s3 のテスト"""

    def test_save_results(
        self,
        survey_service: SurveyService,
        sample_persona_df: pl.DataFrame,
        sample_template: SurveyTemplate,
        mock_s3_service: Mock,
    ) -> None:
        batch_results = [
            {
                "recordId": "p001",
                "modelOutput": {
                    "content": [
                        {
                            "text": '{"answers": [{"question_id": "'
                            + sample_template.questions[0].id
                            + '", "answer": "赤"}]}'
                        }
                    ]
                },
            }
        ]
        result = survey_service.save_results_to_s3(
            batch_results, sample_persona_df, sample_template, "survey-123"
        )
        mock_s3_service.upload_file.assert_called_once()
        assert result == mock_s3_service.upload_file.return_value


# =============================================================================
# 4.7: インサイトレポート生成
# =============================================================================


class TestGenerateInsights:
    """generate_insights のテスト"""

    def test_generate_insights_success(
        self,
        survey_service: SurveyService,
        sample_template: SurveyTemplate,
        mock_ai_service_for_survey: Mock,
    ) -> None:
        result = survey_service.generate_insights("csv,data", sample_template)
        assert isinstance(result, InsightReport)
        assert result.content == "テストインサイトレポート内容"
        mock_ai_service_for_survey._invoke_model.assert_called_once()

    def test_generate_insights_error(
        self,
        survey_service: SurveyService,
        sample_template: SurveyTemplate,
        mock_ai_service_for_survey: Mock,
    ) -> None:
        mock_ai_service_for_survey._invoke_model.side_effect = Exception("AI error")
        with pytest.raises(SurveyServiceError):
            survey_service.generate_insights("csv,data", sample_template)


# =============================================================================
# 追加テスト: 欠落メソッドのカバレッジ
# =============================================================================


class TestCompressImageForBatch:
    """compress_image_for_batch のテスト"""

    def test_compress_small_image(self):
        from PIL import Image
        img = Image.new("RGB", (100, 100), color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        compressed, media_type = SurveyService.compress_image_for_batch(buf.getvalue())
        assert media_type == "image/jpeg"
        assert len(compressed) > 0

    def test_compress_large_image_resizes(self):
        from PIL import Image
        img = Image.new("RGB", (2000, 1500), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        compressed, media_type = SurveyService.compress_image_for_batch(
            buf.getvalue(), max_side=768
        )
        assert media_type == "image/jpeg"
        # 圧縮後は元より小さいはず
        assert len(compressed) < len(buf.getvalue())

    def test_compress_rgba_image(self):
        from PIL import Image
        img = Image.new("RGBA", (100, 100), color=(255, 0, 0, 128))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        compressed, media_type = SurveyService.compress_image_for_batch(buf.getvalue())
        assert media_type == "image/jpeg"


class TestParseCsvColumns:
    """parse_csv_columns のテスト"""

    def test_parse_basic_csv(self, survey_service: SurveyService):
        csv_data = "name,age,sex\n田中,30,男\n佐藤,25,女\n".encode("utf-8")
        result = survey_service.parse_csv_columns(csv_data)
        assert "columns" in result
        assert "name" in result["columns"]
        assert "samples" in result
        assert "auto_mapping" in result

    def test_parse_csv_auto_mapping(self, survey_service: SurveyService):
        csv_data = "persona,sex,age,occupation\nテスト,男,30,会社員\n".encode("utf-8")
        result = survey_service.parse_csv_columns(csv_data)
        assert "persona" in result["auto_mapping"]
        assert "sex" in result["auto_mapping"]

    def test_parse_empty_csv(self, survey_service: SurveyService):
        with pytest.raises(SurveyServiceError):
            survey_service.parse_csv_columns(b"")


class TestFilterAndSamplePersonas:
    """filter_and_sample_personas のテスト"""

    def test_filter_and_sample(
        self, survey_service: SurveyService, sample_persona_df: pl.DataFrame
    ):
        filtered = survey_service.filter_personas({"sex": "女"}, sample_persona_df)
        result = survey_service.sample_personas(filtered, 2)
        assert len(result) <= 2
        assert all(row["sex"] == "女" for row in result.to_dicts())

    def test_filter_and_sample_no_filters(
        self, survey_service: SurveyService, sample_persona_df: pl.DataFrame
    ):
        filtered = survey_service.filter_personas({}, sample_persona_df)
        result = survey_service.sample_personas(filtered, 10)
        assert len(result) == 3  # 全件


class TestInvalidateResultsCache:
    """invalidate_results_cache のテスト"""

    def test_invalidate_clears_cache(self, survey_service: SurveyService):
        survey_service._csv_cache["path/to/results"] = b"cached_data"
        survey_service.invalidate_results_cache("path/to/results")
        assert "path/to/results" not in survey_service._csv_cache

    def test_invalidate_nonexistent_key(self, survey_service: SurveyService):
        # 存在しないキーでもエラーにならない
        survey_service.invalidate_results_cache("nonexistent")


class TestListAndDeleteCustomDatasets:
    """list_custom_datasets / delete_custom_dataset のテスト"""

    def test_list_custom_datasets_empty(
        self, survey_service: SurveyService, mock_s3_service: Mock
    ):
        mock_s3_service.s3_client.list_objects_v2.return_value = {}
        result = survey_service.list_custom_datasets()
        assert isinstance(result, list)

    def test_delete_custom_dataset(
        self, survey_service: SurveyService, mock_s3_service: Mock
    ):
        mock_s3_service.s3_client.delete_objects.return_value = {}
        mock_s3_service.s3_client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "persona-dataset/custom/test.parquet"},
                {"Key": "persona-dataset/custom/test.meta.json"},
            ]
        }
        # エラーなく完了すればOK
        survey_service.delete_custom_dataset("test")
