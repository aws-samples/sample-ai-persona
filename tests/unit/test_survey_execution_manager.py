"""SurveyExecutionManager のユニットテスト"""

import json
from unittest.mock import Mock

import polars as pl
import pytest

from src.managers.survey_execution_manager import (
    SurveyExecutionManager,
    SurveyExecutionManagerError,
    SurveyExecutionValidationError,
)
from src.models.survey import Survey
from src.models.survey_template import Question, SurveyTemplate
from src.prompts.survey_prompts import (
    build_column_mapping_prompt,
    build_dataset_name_prompt,
    build_insight_prompt,
    build_persona_system_prompt,
    STANDARD_COLUMNS,
)


@pytest.fixture
def mock_db() -> Mock:
    return Mock()


@pytest.fixture
def mock_batch_service() -> Mock:
    return Mock()


@pytest.fixture
def mock_s3_service() -> Mock:
    svc = Mock()
    svc.bucket_name = "test-bucket"
    return svc


@pytest.fixture
def mgr(
    mock_db: Mock, mock_batch_service: Mock, mock_s3_service: Mock
) -> SurveyExecutionManager:
    return SurveyExecutionManager(
        database_service=mock_db,
        survey_batch_service=mock_batch_service,
        s3_service=mock_s3_service,
    )


@pytest.fixture
def sample_template() -> SurveyTemplate:
    return SurveyTemplate.create_new(
        name="テスト",
        questions=[Question.create_free_text("Q1")],
    )


class TestCreateSurvey:
    def test_success(
        self,
        mgr: SurveyExecutionManager,
        mock_db: Mock,
        sample_template: SurveyTemplate,
    ) -> None:
        mock_db.get_survey_template.return_value = sample_template
        result = mgr.create_survey("tid", persona_count=100)
        assert isinstance(result, Survey)
        assert result.status == "pending"
        mock_db.save_survey.assert_called_once()

    def test_template_not_found(
        self, mgr: SurveyExecutionManager, mock_db: Mock
    ) -> None:
        mock_db.get_survey_template.return_value = None
        with pytest.raises(SurveyExecutionManagerError):
            mgr.create_survey("missing")

    def test_persona_count_too_low(
        self,
        mgr: SurveyExecutionManager,
        mock_db: Mock,
        sample_template: SurveyTemplate,
    ) -> None:
        mock_db.get_survey_template.return_value = sample_template
        with pytest.raises(SurveyExecutionValidationError):
            mgr.create_survey("tid", persona_count=50)

    def test_default_name_generated(
        self,
        mgr: SurveyExecutionManager,
        mock_db: Mock,
        sample_template: SurveyTemplate,
    ) -> None:
        mock_db.get_survey_template.return_value = sample_template
        result = mgr.create_survey("tid", name="", persona_count=100)
        assert "テスト" in result.name


class TestGetSurvey:
    def test_found(self, mgr: SurveyExecutionManager, mock_db: Mock) -> None:
        mock_survey = Mock(spec=Survey)
        mock_db.get_survey.return_value = mock_survey
        assert mgr.get_survey("sid") == mock_survey

    def test_not_found(self, mgr: SurveyExecutionManager, mock_db: Mock) -> None:
        mock_db.get_survey.return_value = None
        assert mgr.get_survey("missing") is None


class TestGetAllSurveys:
    def test_sorted_by_created_at(
        self, mgr: SurveyExecutionManager, mock_db: Mock
    ) -> None:
        from datetime import datetime

        s1 = Mock(spec=Survey, created_at=datetime(2024, 1, 1))
        s2 = Mock(spec=Survey, created_at=datetime(2024, 6, 1))
        mock_db.get_all_surveys.return_value = [s1, s2]
        result = mgr.get_all_surveys()
        assert result[0].created_at > result[1].created_at


class TestDeleteSurvey:
    def test_success(self, mgr: SurveyExecutionManager, mock_db: Mock) -> None:
        mock_db.get_survey.return_value = Mock(spec=Survey)
        mgr.delete_survey("sid")
        mock_db.delete_survey.assert_called_once_with("sid")

    def test_not_found(self, mgr: SurveyExecutionManager, mock_db: Mock) -> None:
        mock_db.get_survey.return_value = None
        with pytest.raises(SurveyExecutionManagerError):
            mgr.delete_survey("missing")


class TestGetDownloadUrl:
    def test_success(
        self, mgr: SurveyExecutionManager, mock_db: Mock, mock_s3_service: Mock
    ) -> None:
        mock_db.get_survey.return_value = Mock(
            spec=Survey, s3_result_path="s3://bucket/key.csv"
        )
        mock_s3_service.generate_presigned_url.return_value = "https://signed-url"
        url = mgr.get_download_url("sid")
        assert url == "https://signed-url"

    def test_no_result_raises(self, mgr: SurveyExecutionManager, mock_db: Mock) -> None:
        mock_db.get_survey.return_value = Mock(spec=Survey, s3_result_path="")
        with pytest.raises(SurveyExecutionManagerError):
            mgr.get_download_url("sid")

    def test_not_found_raises(self, mgr: SurveyExecutionManager, mock_db: Mock) -> None:
        mock_db.get_survey.return_value = None
        with pytest.raises(SurveyExecutionManagerError):
            mgr.get_download_url("missing")


class TestDownloadResultsCsv:
    def test_success(
        self, mgr: SurveyExecutionManager, mock_db: Mock, mock_s3_service: Mock
    ) -> None:
        mock_db.get_survey.return_value = Mock(
            spec=Survey, s3_result_path="s3://bucket/key.csv"
        )
        mock_s3_service.download_file.return_value = b"csv_data"
        result = mgr.download_results_csv("sid")
        assert result == b"csv_data"


class TestNormalizeFilters:
    def test_removes_empty(self) -> None:
        raw = {"性別": "", "職業": "エンジニア", "年齢": {"min": "20", "max": ""}}
        result = SurveyExecutionManager.normalize_filters(raw)
        assert result == {"職業": "エンジニア", "年齢": {"min": 20}}

    def test_returns_none_for_all_empty(self) -> None:
        raw = {"性別": "", "職業": ""}
        result = SurveyExecutionManager.normalize_filters(raw)
        assert result is None

    def test_preserves_list(self) -> None:
        raw = {"居住地域": ["東京都", "大阪府"]}
        result = SurveyExecutionManager.normalize_filters(raw)
        assert result == {"居住地域": ["東京都", "大阪府"]}


class TestValidatePersonaCount:
    def test_valid(self) -> None:
        SurveyExecutionManager._validate_persona_count(500)

    def test_too_low(self) -> None:
        with pytest.raises(SurveyExecutionValidationError):
            SurveyExecutionManager._validate_persona_count(99)

    def test_images_max_1000(self) -> None:
        with pytest.raises(SurveyExecutionValidationError):
            SurveyExecutionManager._validate_persona_count(1001, has_images=True)

    def test_no_images_max_10000(self) -> None:
        with pytest.raises(SurveyExecutionValidationError):
            SurveyExecutionManager._validate_persona_count(10001, has_images=False)


@pytest.mark.unit
class TestValidateAnswer:
    def test_empty_string_returns_empty(self) -> None:
        q = Question.create_free_text("Q")
        assert SurveyExecutionManager._validate_answer("", q) == ""

    def test_whitespace_only_returns_empty(self) -> None:
        q = Question.create_free_text("Q")
        assert SurveyExecutionManager._validate_answer("   ", q) == ""

    def test_none_returns_empty(self) -> None:
        q = Question.create_free_text("Q")
        assert SurveyExecutionManager._validate_answer(None, q) == ""

    def test_free_text_returns_as_is(self) -> None:
        q = Question.create_free_text("Q")
        assert (
            SurveyExecutionManager._validate_answer("hello world", q) == "hello world"
        )

    def test_free_text_strips_whitespace(self) -> None:
        q = Question.create_free_text("Q")
        assert SurveyExecutionManager._validate_answer("  hello  ", q) == "hello"

    def test_multiple_choice_single_valid(self) -> None:
        q = Question.create_multiple_choice("Q", ["A", "B", "C"])
        assert SurveyExecutionManager._validate_answer("B", q) == "B"

    def test_multiple_choice_single_invalid(self) -> None:
        q = Question.create_multiple_choice("Q", ["A", "B", "C"])
        assert SurveyExecutionManager._validate_answer("D", q) == ""

    def test_multiple_choice_multiple_valid(self) -> None:
        q = Question.create_multiple_choice("Q", ["A", "B", "C"], allow_multiple=True)
        assert SurveyExecutionManager._validate_answer("A|C", q) == "A|C"

    def test_multiple_choice_multiple_filters_invalid(self) -> None:
        q = Question.create_multiple_choice("Q", ["A", "B", "C"], allow_multiple=True)
        assert SurveyExecutionManager._validate_answer("A|D|B", q) == "A|B"

    def test_multiple_choice_multiple_all_invalid(self) -> None:
        q = Question.create_multiple_choice("Q", ["A", "B", "C"], allow_multiple=True)
        assert SurveyExecutionManager._validate_answer("X|Y", q) == ""

    def test_multiple_choice_multiple_respects_max_selections(self) -> None:
        q = Question.create_multiple_choice(
            "Q", ["A", "B", "C", "D"], allow_multiple=True, max_selections=2
        )
        assert SurveyExecutionManager._validate_answer("A|B|C", q) == "A|B"

    def test_multiple_choice_multiple_ignores_empty_segments(self) -> None:
        q = Question.create_multiple_choice("Q", ["A", "B"], allow_multiple=True)
        assert SurveyExecutionManager._validate_answer("A||B|", q) == "A|B"

    def test_scale_rating_valid(self) -> None:
        q = Question.create_scale_rating("Q")
        assert SurveyExecutionManager._validate_answer("3", q) == "3"

    def test_scale_rating_min_boundary(self) -> None:
        q = Question.create_scale_rating("Q")
        assert SurveyExecutionManager._validate_answer("1", q) == "1"

    def test_scale_rating_max_boundary(self) -> None:
        q = Question.create_scale_rating("Q")
        assert SurveyExecutionManager._validate_answer("5", q) == "5"

    def test_scale_rating_below_min(self) -> None:
        q = Question.create_scale_rating("Q")
        assert SurveyExecutionManager._validate_answer("0", q) == ""

    def test_scale_rating_above_max(self) -> None:
        q = Question.create_scale_rating("Q")
        assert SurveyExecutionManager._validate_answer("6", q) == ""

    def test_scale_rating_non_integer(self) -> None:
        q = Question.create_scale_rating("Q")
        assert SurveyExecutionManager._validate_answer("abc", q) == ""

    def test_scale_rating_float_string(self) -> None:
        q = Question.create_scale_rating("Q")
        assert SurveyExecutionManager._validate_answer("3.5", q) == ""


@pytest.mark.unit
class TestFormatQuestionsForPrompt:
    def test_free_text_format(self) -> None:
        q = Question.create_free_text("好きな食べ物は？")
        result = SurveyExecutionManager._format_questions_for_prompt([q])
        assert f"質問1 (ID: {q.id}): 好きな食べ物は？" in result
        assert "自由記述" in result

    def test_multiple_choice_single_format(self) -> None:
        q = Question.create_multiple_choice("性別は？", ["男性", "女性", "その他"])
        result = SurveyExecutionManager._format_questions_for_prompt([q])
        assert "選択式・単一回答" in result
        assert "1. 男性" in result
        assert "2. 女性" in result
        assert "3. その他" in result
        assert "【回答例】男性" in result

    def test_multiple_choice_multiple_format(self) -> None:
        q = Question.create_multiple_choice(
            "趣味は？",
            ["読書", "映画", "スポーツ"],
            allow_multiple=True,
            max_selections=2,
        )
        result = SurveyExecutionManager._format_questions_for_prompt([q])
        assert "選択式・複数回答" in result
        assert "最大2個まで" in result
        assert "読書|映画" in result

    def test_multiple_choice_multiple_no_max(self) -> None:
        q = Question.create_multiple_choice(
            "趣味は？", ["読書", "映画"], allow_multiple=True, max_selections=0
        )
        result = SurveyExecutionManager._format_questions_for_prompt([q])
        assert "選択式・複数回答" in result
        assert "最大" not in result

    def test_scale_rating_format(self) -> None:
        q = Question.create_scale_rating("満足度は？")
        result = SurveyExecutionManager._format_questions_for_prompt([q])
        assert "スケール評価" in result
        assert "1〜5" in result

    def test_multiple_questions_numbered(self) -> None:
        q1 = Question.create_free_text("Q1")
        q2 = Question.create_free_text("Q2")
        result = SurveyExecutionManager._format_questions_for_prompt([q1, q2])
        assert f"質問1 (ID: {q1.id}): Q1" in result
        assert f"質問2 (ID: {q2.id}): Q2" in result


@pytest.mark.unit
class TestParseBatchResultAnswers:
    def test_valid_json_with_answers(self, mgr: SurveyExecutionManager) -> None:
        import json

        q = Question.create_free_text("Q1")
        result = {
            "modelOutput": {
                "content": [
                    {
                        "text": json.dumps(
                            {"answers": [{"question_id": q.id, "answer": "回答テスト"}]}
                        )
                    }
                ]
            }
        }
        answers = mgr._parse_batch_result_answers(result, [q])
        assert len(answers) == 1
        assert answers[0]["question_id"] == q.id
        assert answers[0]["answer"] == "回答テスト"

    def test_invalid_json_returns_empty(self, mgr: SurveyExecutionManager) -> None:
        q = Question.create_free_text("Q1")
        result = {"modelOutput": {"content": [{"text": "not json at all"}]}}
        answers = mgr._parse_batch_result_answers(result, [q])
        assert answers == []

    def test_missing_answers_key_returns_empty(
        self, mgr: SurveyExecutionManager
    ) -> None:
        import json

        q = Question.create_free_text("Q1")
        result = {
            "modelOutput": {"content": [{"text": json.dumps({"data": "something"})}]}
        }
        answers = mgr._parse_batch_result_answers(result, [q])
        assert answers == []

    def test_answers_not_list_returns_empty(self, mgr: SurveyExecutionManager) -> None:
        import json

        q = Question.create_free_text("Q1")
        result = {
            "modelOutput": {
                "content": [{"text": json.dumps({"answers": "not a list"})}]
            }
        }
        answers = mgr._parse_batch_result_answers(result, [q])
        assert answers == []

    def test_validates_each_answer(self, mgr: SurveyExecutionManager) -> None:
        import json

        q = Question.create_multiple_choice("Q1", ["A", "B", "C"])
        result = {
            "modelOutput": {
                "content": [
                    {
                        "text": json.dumps(
                            {"answers": [{"question_id": q.id, "answer": "D"}]}
                        )
                    }
                ]
            }
        }
        answers = mgr._parse_batch_result_answers(result, [q])
        assert len(answers) == 1
        assert answers[0]["answer"] == ""

    def test_unknown_question_id_passes_through(
        self, mgr: SurveyExecutionManager
    ) -> None:
        import json

        q = Question.create_free_text("Q1")
        result = {
            "modelOutput": {
                "content": [
                    {
                        "text": json.dumps(
                            {
                                "answers": [
                                    {"question_id": "unknown-id", "answer": "何か"}
                                ]
                            }
                        )
                    }
                ]
            }
        }
        answers = mgr._parse_batch_result_answers(result, [q])
        assert len(answers) == 1
        assert answers[0]["question_id"] == "unknown-id"
        assert answers[0]["answer"] == "何か"

    def test_empty_content_list_returns_empty(
        self, mgr: SurveyExecutionManager
    ) -> None:
        q = Question.create_free_text("Q1")
        result = {"modelOutput": {"content": []}}
        answers = mgr._parse_batch_result_answers(result, [q])
        assert answers == []


@pytest.mark.unit
class TestNormalizeFiltersEdgeCases:
    def test_empty_list_excluded(self) -> None:
        raw = {"居住地域": []}
        result = SurveyExecutionManager.normalize_filters(raw)
        assert result is None

    def test_range_with_non_numeric_values_excluded(self) -> None:
        raw = {"年齢": {"min": "abc", "max": "xyz"}}
        result = SurveyExecutionManager.normalize_filters(raw)
        assert result is None

    def test_range_with_partial_valid(self) -> None:
        raw = {"年齢": {"min": "25", "max": "not_a_number"}}
        result = SurveyExecutionManager.normalize_filters(raw)
        assert result == {"年齢": {"min": 25}}

    def test_range_with_float_string_converts(self) -> None:
        raw = {"年齢": {"min": "20.5", "max": "30.9"}}
        result = SurveyExecutionManager.normalize_filters(raw)
        assert result == {"年齢": {"min": 20, "max": 30}}


# ---------------------------------------------------------------------------
# survey_prompts.py ヘルパー関数テスト
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSurveyPromptHelpers:
    """build_column_mapping_prompt / build_insight_prompt / build_persona_system_prompt / build_dataset_name_prompt"""

    # -- build_column_mapping_prompt --

    def test_column_mapping_contains_columns_and_samples(self) -> None:
        columns = ["gender", "age_group"]
        samples = {"gender": ["M", "F"], "age_group": ["20代", "30代"]}
        result = build_column_mapping_prompt(columns, samples, STANDARD_COLUMNS)
        assert "gender" in result
        assert "age_group" in result
        assert "['M', 'F']" in result
        assert "['20代', '30代']" in result

    def test_column_mapping_contains_standard_labels_json(self) -> None:
        columns = ["x"]
        samples = {"x": ["v"]}
        result = build_column_mapping_prompt(columns, samples, STANDARD_COLUMNS)
        # standard_info_json should have label values
        assert '"性別"' in result
        assert '"年齢"' in result
        assert '"ペルソナ概要"' in result

    def test_column_mapping_empty_columns(self) -> None:
        result = build_column_mapping_prompt([], {}, STANDARD_COLUMNS)
        # Should still produce a valid string with standard info
        assert '"性別"' in result

    # -- build_insight_prompt --

    def test_insight_prompt_contains_summary_json(self) -> None:
        summary = {"total_responses": 500, "questions": [{"id": "q1", "mean": 3.5}]}
        template = SurveyTemplate.create_new(
            name="テスト", questions=[Question.create_free_text("Q1")]
        )
        result = build_insight_prompt(summary, template)
        assert '"total_responses": 500' in result
        assert '"mean": 3.5' in result

    def test_insight_prompt_contains_instruction_text(self) -> None:
        summary = {"key": "value"}
        template = SurveyTemplate.create_new(
            name="T", questions=[Question.create_free_text("Q")]
        )
        result = build_insight_prompt(summary, template)
        assert "統計要約データ" in result
        assert "インサイトレポート" in result

    # -- build_persona_system_prompt --

    def test_persona_prompt_basic_attributes(self) -> None:
        row = {"sex": "女性", "age": 35, "occupation": "エンジニア"}
        result = build_persona_system_prompt(row)
        assert "性別: 女性" in result
        assert "年齢: 35" in result
        assert "職業: エンジニア" in result

    def test_persona_prompt_profile_columns(self) -> None:
        row = {
            "persona": "都市部に住む共働き世帯の母親",
            "cultural_background": "関東出身、IT業界歴10年",
        }
        result = build_persona_system_prompt(row)
        assert "【ペルソナ概要】" in result
        assert "都市部に住む共働き世帯の母親" in result
        assert "【文化的背景】" in result
        assert "関東出身、IT業界歴10年" in result

    def test_persona_prompt_skips_none_values(self) -> None:
        row = {"sex": "男性", "age": None, "occupation": "教師"}
        result = build_persona_system_prompt(row)
        assert "性別: 男性" in result
        assert "年齢" not in result
        assert "職業: 教師" in result

    def test_persona_prompt_extra_columns_with_description(self) -> None:
        row = {"sex": "男性", "purchase_count": 15}
        extra = [
            {
                "csv_column": "purchase_count",
                "label": "購入回数",
                "description": "過去1年間の購入件数",
            }
        ]
        result = build_persona_system_prompt(row, extra_columns=extra)
        assert "【その他情報】" in result
        assert "購入回数（過去1年間の購入件数）: 15" in result

    def test_persona_prompt_extra_columns_without_description(self) -> None:
        row = {"loyalty_rank": "ゴールド"}
        extra = [
            {"csv_column": "loyalty_rank", "label": "会員ランク", "description": ""}
        ]
        result = build_persona_system_prompt(row, extra_columns=extra)
        assert "会員ランク: ゴールド" in result
        # description が空なので括弧表記にならない
        assert "（" not in result

    def test_persona_prompt_extra_columns_none_value_skipped(self) -> None:
        row = {"loyalty_rank": None}
        extra = [
            {"csv_column": "loyalty_rank", "label": "会員ランク", "description": ""}
        ]
        result = build_persona_system_prompt(row, extra_columns=extra)
        assert "その他情報" not in result
        assert "会員ランク" not in result

    # -- build_dataset_name_prompt --

    def test_dataset_name_prompt_contains_condition(self) -> None:
        result = build_dataset_name_prompt("東京在住30代女性")
        assert "東京在住30代女性" in result

    def test_dataset_name_prompt_instruction(self) -> None:
        result = build_dataset_name_prompt("テスト条件")
        assert "データセット名" in result
        assert "15文字以内" in result


# ---------------------------------------------------------------------------
# execute_survey / _save_results_to_s3 / _ensure_parquet_uri テスト
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExecuteSurvey:
    """execute_survey のフルフロー統合テスト（全サービスモック）"""

    @pytest.fixture
    def template_with_questions(self) -> SurveyTemplate:
        q1 = Question.create_multiple_choice("好きな色は？", ["赤", "青", "緑"])
        q2 = Question.create_free_text("理由を教えてください")
        return SurveyTemplate.create_new(name="カラーテスト", questions=[q1, q2])

    @pytest.fixture
    def survey_record(self, template_with_questions: SurveyTemplate) -> Survey:
        return Survey.create_new(
            name="テスト調査",
            description="",
            template_id=template_with_questions.id,
            persona_count=2,
        )

    def test_success_flow(
        self,
        mgr: SurveyExecutionManager,
        mock_db: Mock,
        mock_batch_service: Mock,
        mock_s3_service: Mock,
        template_with_questions: SurveyTemplate,
        survey_record: Survey,
    ) -> None:
        mock_db.get_survey.return_value = survey_record
        mock_db.get_survey_template.return_value = template_with_questions

        mock_batch_service.has_parquet_uri.return_value = True
        sampled_df = pl.DataFrame(
            {
                "uuid": ["id1", "id2"],
                "sex": ["男性", "女性"],
                "age": [30, 25],
                "occupation": ["エンジニア", "デザイナー"],
                "country": ["JP", "JP"],
                "region": ["東京", "大阪"],
                "prefecture": ["東京都", "大阪府"],
                "marital_status": ["未婚", "既婚"],
                "persona": ["テストペルソナ1", "テストペルソナ2"],
            }
        )
        mock_batch_service.filter_and_sample_personas.return_value = sampled_df
        mock_batch_service.build_batch_prompts.return_value = [
            {"prompt": "p1"},
            {"prompt": "p2"},
        ]

        q1_id = template_with_questions.questions[0].id
        q2_id = template_with_questions.questions[1].id
        mock_batch_service.execute_batch_inference.return_value = [
            {
                "recordId": "id1",
                "modelOutput": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "answers": [
                                        {"question_id": q1_id, "answer": "赤"},
                                        {"question_id": q2_id, "answer": "好きだから"},
                                    ]
                                }
                            )
                        }
                    ]
                },
            },
            {
                "recordId": "id2",
                "modelOutput": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "answers": [
                                        {"question_id": q1_id, "answer": "青"},
                                        {
                                            "question_id": q2_id,
                                            "answer": "落ち着くから",
                                        },
                                    ]
                                }
                            )
                        }
                    ]
                },
            },
        ]
        mock_s3_service.upload_file.return_value = (
            "s3://test-bucket/survey-results/sid/results.csv"
        )

        mgr.execute_survey(survey_record.id)

        assert mock_db.update_survey.call_count == 2
        final_update = mock_db.update_survey.call_args_list[-1][0][0]
        assert final_update.status == "completed"
        assert final_update.s3_result_path is not None
        mock_s3_service.upload_file.assert_called_once()

    def test_survey_not_found(self, mgr: SurveyExecutionManager, mock_db: Mock) -> None:
        mock_db.get_survey.return_value = None
        with pytest.raises(SurveyExecutionManagerError, match="見つかりません"):
            mgr.execute_survey("missing_id")

    def test_template_not_found(
        self,
        mgr: SurveyExecutionManager,
        mock_db: Mock,
        survey_record: Survey,
    ) -> None:
        mock_db.get_survey.return_value = survey_record
        mock_db.get_survey_template.return_value = None

        with pytest.raises(
            SurveyExecutionManagerError, match="テンプレートが見つかりません"
        ):
            mgr.execute_survey(survey_record.id)

    def test_batch_error_sets_error_status(
        self,
        mgr: SurveyExecutionManager,
        mock_db: Mock,
        mock_batch_service: Mock,
        template_with_questions: SurveyTemplate,
        survey_record: Survey,
    ) -> None:
        mock_db.get_survey.return_value = survey_record
        mock_db.get_survey_template.return_value = template_with_questions
        mock_batch_service.has_parquet_uri.return_value = True
        mock_batch_service.filter_and_sample_personas.side_effect = RuntimeError(
            "Batch failed"
        )

        from src.managers.survey_execution_manager import SurveyExecutionError

        with pytest.raises(SurveyExecutionError):
            mgr.execute_survey(survey_record.id)

        final_update = mock_db.update_survey.call_args_list[-1][0][0]
        assert final_update.status == "error"
        assert "Batch failed" in final_update.error_message


@pytest.mark.unit
class TestSaveResultsToS3:
    """_save_results_to_s3 のテスト"""

    def test_builds_csv_and_uploads(
        self, mgr: SurveyExecutionManager, mock_s3_service: Mock
    ) -> None:
        q1 = Question.create_free_text("質問1")
        template = SurveyTemplate.create_new(name="T", questions=[q1])

        personas_df = pl.DataFrame(
            {
                "uuid": ["uid1"],
                "sex": ["男性"],
                "age": [30],
                "occupation": ["会社員"],
                "country": ["JP"],
                "region": ["東京"],
                "prefecture": ["東京都"],
                "marital_status": ["未婚"],
            }
        )

        batch_results = [
            {
                "recordId": "uid1",
                "modelOutput": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "answers": [
                                        {"question_id": q1.id, "answer": "テスト回答"}
                                    ]
                                }
                            )
                        }
                    ]
                },
            }
        ]

        mock_s3_service.upload_file.return_value = (
            "s3://test-bucket/survey-results/sid/results.csv"
        )
        result = mgr._save_results_to_s3(batch_results, personas_df, template, "sid")

        assert result == "s3://test-bucket/survey-results/sid/results.csv"
        uploaded_bytes = mock_s3_service.upload_file.call_args[0][0]
        csv_text = uploaded_bytes.decode("utf-8-sig")
        assert "質問1" in csv_text
        assert "テスト回答" in csv_text
        assert "男性" in csv_text

    def test_missing_persona_uses_empty(
        self, mgr: SurveyExecutionManager, mock_s3_service: Mock
    ) -> None:
        q1 = Question.create_free_text("Q")
        template = SurveyTemplate.create_new(name="T", questions=[q1])

        personas_df = pl.DataFrame(
            {
                "uuid": ["uid1"],
                "sex": ["F"],
                "age": [20],
                "occupation": ["X"],
                "country": ["JP"],
                "region": ["Y"],
                "prefecture": ["Z"],
                "marital_status": [""],
            }
        )

        batch_results = [
            {
                "recordId": "unknown_id",
                "modelOutput": {
                    "content": [
                        {
                            "text": json.dumps(
                                {"answers": [{"question_id": q1.id, "answer": "A"}]}
                            )
                        }
                    ]
                },
            }
        ]

        mock_s3_service.upload_file.return_value = "s3://b/k"
        mgr._save_results_to_s3(batch_results, personas_df, template, "sid")

        uploaded_bytes = mock_s3_service.upload_file.call_args[0][0]
        csv_text = uploaded_bytes.decode("utf-8-sig")
        assert "unknown_id" in csv_text


@pytest.mark.unit
class TestEnsureParquetUri:
    """_ensure_parquet_uri のテスト"""

    def test_custom_datasource_skips(
        self, mgr: SurveyExecutionManager, mock_batch_service: Mock
    ) -> None:
        mgr._ensure_parquet_uri("custom:my_dataset")
        mock_batch_service.has_parquet_uri.assert_not_called()

    def test_already_set_skips(
        self, mgr: SurveyExecutionManager, mock_batch_service: Mock
    ) -> None:
        mock_batch_service.has_parquet_uri.return_value = True
        mgr._ensure_parquet_uri("nemotron")
        # Should not attempt head_object
        mgr.s3_service.s3_client.head_object.assert_not_called()

    def test_sets_uri_when_exists(
        self,
        mgr: SurveyExecutionManager,
        mock_batch_service: Mock,
        mock_s3_service: Mock,
    ) -> None:
        mock_batch_service.has_parquet_uri.return_value = False
        mock_s3_service.s3_client.head_object.return_value = {}
        mgr._ensure_parquet_uri("nemotron")
        mock_batch_service.set_parquet_s3_uri.assert_called_once()

    def test_raises_when_not_found(
        self,
        mgr: SurveyExecutionManager,
        mock_batch_service: Mock,
        mock_s3_service: Mock,
    ) -> None:
        mock_batch_service.has_parquet_uri.return_value = False
        mock_s3_service.s3_client.head_object.side_effect = Exception("Not found")
        from src.managers.survey_execution_manager import SurveyExecutionError

        with pytest.raises(SurveyExecutionError, match="ダウンロードされていません"):
            mgr._ensure_parquet_uri("nemotron")
