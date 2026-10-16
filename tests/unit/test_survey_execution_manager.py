"""SurveyExecutionManager のユニットテスト

U7: BR1.1〜BR1.4、BR1.8、BR2.1、BR2.2、BR2.6〜BR2.10、BR2.12、BR4.2、NFR1.66、
NFR1.67、NFR1.69〜NFR1.71、NFR1.84、NFR1.86、NFR2.24、NFR2.25、NFR8.26、
AC2.5.2、AC4.1.2、AC4.4.1、AC4.4.4。DynamoDB と S3 は moto、判定の部品は本物
（tests/unit/survey_world.py）、バッチ推論はモック。
"""

import io
import json
from unittest.mock import Mock

import polars as pl
import pytest

from src.models.authz import ResourceType
from src.models.errors import AccessDeniedError, CodedError, ErrorCode

from src.managers.survey_execution_manager import (
    SurveyExecutionError,
    SurveyExecutionManager,
    SurveyExecutionManagerError,
    SurveyExecutionValidationError,
    SurveyStart,
)
from src.models.survey_template import Question, SurveyTemplate, TemplateImage
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture
from tests.unit.survey_world import (
    ADMIN,
    BUCKET,
    MEMBER_A,
    MEMBER_B,
    MEMBER_C,
    NO_ROLE,
    SurveyWorld,
)
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
    svc = Mock()
    # 実件数の検証（Issue #118）を通す既定値。件数不足のケースは個別に上書きする。
    svc.get_filtered_count.return_value = 1000
    svc.has_parquet_uri.return_value = True
    return svc


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

        with pytest.raises(SurveyExecutionError) as exc_info:
            mgr._ensure_parquet_uri("nemotron")
        assert exc_info.value.code is ErrorCode.SURVEY_DATASET_NOT_DOWNLOADED


# =============================================================================
# U7: 判定・写し・裏の実行・一覧・削除（moto の世界）
# =============================================================================

SURVEY = ResourceType.SURVEY
TEMPLATE = ResourceType.SURVEY_TEMPLATE


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def _sampled(rows: int = 120) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "uuid": [f"id{i}" for i in range(rows)],
            "sex": ["女性"] * rows,
            "age": [30] * rows,
            "occupation": ["会社員"] * rows,
            "country": ["日本"] * rows,
            "region": ["関東"] * rows,
            "prefecture": ["東京都"] * rows,
            "marital_status": ["未婚"] * rows,
            "persona": ["ペルソナ"] * rows,
            "hobby": ["釣り"] * rows,
        }
    )


def _batch() -> Mock:
    batch = Mock()
    batch.get_filtered_count.return_value = 1000
    batch.has_parquet_uri.return_value = True
    batch.filter_and_sample_personas.side_effect = lambda *a, **k: _sampled()
    batch.build_batch_prompts.side_effect = lambda df, systems, users, schema: [
        {"recordId": f"id{i}", "system": systems[i], "user": users[i]}
        for i in range(len(systems))
    ]
    batch.execute_batch_inference.side_effect = lambda prompts: [
        {
            "recordId": p["recordId"],
            "modelOutput": {"content": [{"text": json.dumps({"answers": []})}]},
        }
        for p in prompts
    ]
    return batch


@pytest.fixture
def batch() -> Mock:
    return _batch()


@pytest.fixture
def cache() -> Mock:
    return Mock()


@pytest.fixture
def started() -> list:
    return []


@pytest.fixture
def em(survey_world: SurveyWorld, batch: Mock, cache: Mock) -> SurveyExecutionManager:
    return SurveyExecutionManager(
        database_service=survey_world.db,
        survey_batch_service=batch,
        s3_service=survey_world.s3,
        resource_sharing=survey_world.sharing,
        owners=survey_world.owners,
        resource_lookup=survey_world.lookup,
        custom_persona_data_store=survey_world.custom_store,
        result_cache=cache,
        sleep=lambda _s: None,
    )


@pytest.fixture
def logs(capsys) -> LogCapture:
    return LogCapture(capsys)


def _surveys(world: SurveyWorld) -> int:
    return world.client.scan(TableName=world.db.surveys_table)["Count"]


def _image(world: SurveyWorld, *, upload: bool = True) -> TemplateImage:
    key = "uploads/a/survey_image/11111111-1111-4111-8111-111111111111_a.png"
    if upload:
        world.s3.upload_file(_png(), key)
    return TemplateImage(
        id="img1",
        name="商品画像",
        file_path=f"s3://{BUCKET}/{key}",
        mime_type="image/png",
        original_filename="a.png",
    )


class TestCreateSurveyDecisions:
    def test_needs_ai_execute_first(self, em, survey_world, batch):
        template = survey_world.template("x")
        with pytest.raises(AccessDeniedError):
            em.create_survey(NO_ROLE, template.id)
        assert _surveys(survey_world) == 0
        batch.get_filtered_count.assert_not_called()

    @pytest.mark.parametrize("kind", ["unrelated", "missing"])
    def test_unreadable_template_is_not_found(self, em, survey_world, batch, kind):
        template = survey_world.template("c")
        target = template.id if kind == "unrelated" else "missing-id"
        with raises_code(CodedError, ErrorCode.SURVEY_TEMPLATE_NOT_FOUND):
            em.create_survey(MEMBER_A, target)
        assert _surveys(survey_world) == 0
        batch.get_filtered_count.assert_not_called()

    def test_shared_template_can_be_used_and_is_copied(self, em, survey_world):
        image = _image(survey_world)
        template = survey_world.template("a", "共有テンプレート", images=[image])
        survey_world.share(TEMPLATE, template.id, "sales")

        start = em.create_survey(MEMBER_B, template.id)

        assert isinstance(start, SurveyStart)
        survey = survey_world.db.get_survey(start.survey.id, consistent=True)
        assert survey.owner_sub == "b"
        assert survey.shared_group_ids == frozenset()
        assert survey.status == "pending"
        assert survey.template_snapshot.name == "共有テンプレート"
        assert [q.id for q in survey.template_snapshot.questions] == [
            q.id for q in template.questions
        ]
        assert survey.template_snapshot.images[0].id == image.id
        assert survey.datasource == "nemotron"

    def test_custom_data_outside_the_scope_is_not_found(self, em, survey_world, batch):
        template = survey_world.template("c")
        data = survey_world.custom_data("営業データ", "sales")
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND):
            em.create_survey(MEMBER_C, template.id, datasource=f"custom:{data.id}")
        assert _surveys(survey_world) == 0
        batch.get_filtered_count.assert_not_called()

    def test_custom_data_in_scope_records_its_name(self, em, survey_world):
        template = survey_world.template("a")
        data = survey_world.custom_data("営業データ", "sales")
        start = em.create_survey(MEMBER_A, template.id, datasource=f"custom:{data.id}")
        assert start.survey.datasource == f"custom:{data.id}"
        assert start.survey.datasource_name == "営業データ"

    def test_admin_uses_admin_only_data(self, em, survey_world):
        template = survey_world.template("admin")
        data = survey_world.custom_data("admin だけ")
        start = em.create_survey(ADMIN, template.id, datasource=f"custom:{data.id}")
        assert start.survey.datasource_name == "admin だけ"
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND):
            em.create_survey(
                MEMBER_A,
                survey_world.template("a").id,
                datasource=f"custom:{data.id}",
            )

    @pytest.mark.parametrize("value", ["custom:顧客データ", "other"])
    def test_old_or_unknown_datasource_is_not_found(self, em, survey_world, value):
        template = survey_world.template("a")
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND):
            em.create_survey(MEMBER_A, template.id, datasource=value)
        assert _surveys(survey_world) == 0

    def test_reads_two_targets_for_custom_and_one_for_nemotron(self, em, survey_world):
        template = survey_world.template("a")
        data = survey_world.custom_data("営業データ", "sales")
        spy = Mock(wraps=survey_world.lookup.get_many)
        survey_world.lookup.get_many = spy
        em.create_survey(MEMBER_A, template.id, datasource=f"custom:{data.id}")
        assert spy.call_count == 2
        spy.reset_mock()
        em.create_survey(MEMBER_A, template.id)
        assert spy.call_count == 1

    def test_lookup_failure_starts_nothing(self, em, survey_world, batch):
        template = survey_world.template("a")
        survey_world.lookup_service.read_targets = Mock(
            side_effect=CodedError("down", code=ErrorCode.LOOKUP_UNAVAILABLE)
        )
        with raises_code(CodedError, ErrorCode.LOOKUP_UNAVAILABLE):
            em.create_survey(MEMBER_A, template.id)
        assert _surveys(survey_world) == 0
        batch.get_filtered_count.assert_not_called()

    def test_over_350kb_writes_nothing(self, em, survey_world, monkeypatch):
        import src.managers.survey_execution_manager as module

        template = survey_world.template("a")
        monkeypatch.setattr(module, "MAX_SURVEY_BYTES", 100)
        with raises_code(CodedError, ErrorCode.SURVEY_TEMPLATE_TOO_LARGE):
            em.create_survey(MEMBER_A, template.id)
        assert _surveys(survey_world) == 0

    def test_the_size_limit_is_350kb(self):
        import src.managers.survey_execution_manager as module

        assert module.MAX_SURVEY_BYTES == 350 * 1024

    def test_persona_count_is_still_validated(self, em, survey_world):
        template = survey_world.template("a")
        with pytest.raises(SurveyExecutionValidationError):
            em.create_survey(MEMBER_A, template.id, persona_count=50)
        assert _surveys(survey_world) == 0

    def test_filtered_count_too_low_suggests_loosening(self, em, survey_world, batch):
        template = survey_world.template("a")
        batch.get_filtered_count.return_value = 30
        with raises_code(
            SurveyExecutionValidationError,
            ErrorCode.SURVEY_AVAILABLE_PERSONAS_TOO_FEW,
            available_count=30,
        ):
            em.create_survey(
                MEMBER_A, template.id, persona_count=1000, filters={"性別": "男性"}
            )
        assert _surveys(survey_world) == 0

    def test_count_query_failure_hides_its_text(self, em, survey_world, batch):
        template = survey_world.template("a")
        batch.get_filtered_count.side_effect = RuntimeError(
            "IO Error: failed to read s3://prod-bucket/secret.parquet"
        )
        with raises_code(SurveyExecutionError, ErrorCode.SURVEY_OPERATION_FAILED) as e:
            em.create_survey(MEMBER_A, template.id)
        assert "prod-bucket" not in str(e.value)

    def test_default_name_comes_from_the_template(self, em, survey_world):
        template = survey_world.template("a", "顧客満足度")
        start = em.create_survey(MEMBER_A, template.id, name="")
        assert "顧客満足度" in start.survey.name

    def test_execution_is_not_public(self):
        assert not hasattr(SurveyExecutionManager, "execute_survey")
        assert not hasattr(SurveyExecutionManager, "start_survey")


class TestBackgroundRun:
    def _start(self, em, world, principal=MEMBER_A, **kwargs):
        template = kwargs.pop("template", None) or world.template(principal.sub)
        return em.create_survey(principal, template.id, **kwargs)

    def test_runs_from_the_snapshot_and_saves_the_csv(self, em, survey_world, batch):
        template = survey_world.template("a", "写しで動く")
        start = em.create_survey(MEMBER_A, template.id)
        # 開始のあとでテンプレートが消えても、写しで最後まで進む（BR2.2）
        survey_world.client.delete_item(
            TableName=survey_world.db.survey_templates_table,
            Key={"id": {"S": template.id}},
        )

        start.run()

        survey = survey_world.db.get_survey(start.survey.id, consistent=True)
        assert survey.status == "completed"
        assert survey.s3_result_path == (
            f"s3://{BUCKET}/survey-results/{survey.id}/results.csv"
        )
        csv_text = survey_world.s3.download_file(survey.s3_result_path).decode(
            "utf-8-sig"
        )
        assert template.questions[0].text in csv_text
        assert survey.persona_count == 120

    def test_custom_data_columns_come_from_the_record(self, em, survey_world, batch):
        data = survey_world.custom_data("営業データ", "sales")
        start = self._start(em, survey_world, datasource=f"custom:{data.id}")
        start.run()
        system_prompts = batch.build_batch_prompts.call_args.args[1]
        assert "趣味" in system_prompts[0]
        assert batch.filter_and_sample_personas.call_args.kwargs["datasource"] == (
            f"custom:{data.id}"
        )

    @pytest.mark.parametrize("what", ["record", "file"])
    def test_missing_custom_data_is_an_error(self, em, survey_world, batch, logs, what):
        data = survey_world.custom_data("営業データ", "sales")
        start = self._start(em, survey_world, datasource=f"custom:{data.id}")
        if what == "record":
            survey_world.custom_store.delete(data.id, data.name)
        else:
            survey_world.s3.delete_file(survey_world.s3.path_of(data.s3_key))

        start.run()

        survey = survey_world.db.get_survey(start.survey.id, consistent=True)
        assert survey.status == "error"
        assert survey.error_code == ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND.value
        batch.execute_batch_inference.assert_not_called()
        events = logs.events("survey.dependency_missing")
        assert events[0]["survey_id"] == survey.id
        assert events[0]["missing"] == "custom_persona_data"

    def test_unreadable_snapshot_image_stops_the_survey(
        self, em, survey_world, batch, logs
    ):
        image = _image(survey_world, upload=False)
        template = survey_world.template("a", images=[image])
        start = em.create_survey(MEMBER_A, template.id, persona_count=100)

        start.run()

        survey = survey_world.db.get_survey(start.survey.id, consistent=True)
        assert survey.status == "error"
        assert survey.error_code == ErrorCode.SURVEY_TEMPLATE_IMAGE_UNAVAILABLE.value
        batch.execute_batch_inference.assert_not_called()
        assert logs.events("survey.dependency_missing")[0]["missing"] == "image"

    def test_snapshot_image_is_sent_with_the_questions(self, em, survey_world, batch):
        image = _image(survey_world)
        template = survey_world.template("a", images=[image])
        em.create_survey(MEMBER_A, template.id, persona_count=100).run()
        user_messages = batch.build_batch_prompts.call_args.args[2]
        assert any(part.get("type") == "image" for part in user_messages[0])

    def test_survey_deleted_while_running_saves_nothing(
        self, em, survey_world, batch, logs
    ):
        start = self._start(em, survey_world)

        def delete_then_answer(prompts):
            survey_world.client.delete_item(
                TableName=survey_world.db.surveys_table,
                Key={"id": {"S": start.survey.id}},
            )
            return _batch().execute_batch_inference(prompts)

        batch.execute_batch_inference.side_effect = delete_then_answer

        start.run()

        assert survey_world.db.get_survey(start.survey.id) is None
        listed = survey_world.s3.s3_client.list_objects_v2(
            Bucket=BUCKET, Prefix="survey-results/"
        )
        assert listed.get("KeyCount", 0) == 0
        assert logs.events("survey.dependency_missing")[0]["missing"] == "survey"

    def test_failed_final_update_removes_the_csv(self, em, survey_world, monkeypatch):
        start = self._start(em, survey_world)
        real = survey_world.db.update_survey_fields

        def gone_at_completion(survey_id, fields, **kwargs):
            if fields.get("status") == "completed":
                return False
            return real(survey_id, fields, **kwargs)

        monkeypatch.setattr(survey_world.db, "update_survey_fields", gone_at_completion)

        start.run()

        listed = survey_world.s3.s3_client.list_objects_v2(
            Bucket=BUCKET, Prefix="survey-results/"
        )
        assert listed.get("KeyCount", 0) == 0

    def test_batch_failure_keeps_a_code_and_no_text(
        self, em, survey_world, batch, logs
    ):
        start = self._start(em, survey_world)
        batch.filter_and_sample_personas.side_effect = RuntimeError(
            "Batch failed at s3://secret/key"
        )

        start.run()

        survey = survey_world.db.get_survey(start.survey.id, consistent=True)
        assert survey.status == "error"
        assert survey.error_code == ErrorCode.SURVEY_EXECUTION_FAILED.value
        assert "secret" not in str(survey.to_dict())
        failed = logs.events("survey.execution_failed")
        assert failed[0]["survey_id"] == survey.id
        assert failed[0]["error_code"] == ErrorCode.SURVEY_EXECUTION_FAILED.value
        assert "secret" not in json.dumps(failed)

    def test_coded_failure_keeps_its_code(self, em, survey_world, batch):
        start = self._start(em, survey_world)
        batch.execute_batch_inference.side_effect = CodedError(
            "role missing", code=ErrorCode.SURVEY_BATCH_ROLE_NOT_CONFIGURED
        )
        start.run()
        survey = survey_world.db.get_survey(start.survey.id, consistent=True)
        assert survey.error_code == ErrorCode.SURVEY_BATCH_ROLE_NOT_CONFIGURED.value

    def test_too_few_sampled_without_filters_points_at_the_dataset(
        self, em, survey_world, batch
    ):
        start = self._start(em, survey_world)
        batch.filter_and_sample_personas.side_effect = lambda *a, **k: _sampled(2)
        start.run()
        survey = survey_world.db.get_survey(start.survey.id, consistent=True)
        assert survey.error_code == ErrorCode.SURVEY_DATASET_TOO_FEW_ROWS.value
        assert survey.error_context == {"row_count": 2, "min_rows": 100}
        batch.execute_batch_inference.assert_not_called()

    def test_too_few_sampled_with_filters_suggests_loosening(
        self, em, survey_world, batch
    ):
        start = self._start(em, survey_world, filters={"性別": "男性"})
        batch.filter_and_sample_personas.side_effect = lambda *a, **k: _sampled(2)
        start.run()
        survey = survey_world.db.get_survey(start.survey.id, consistent=True)
        assert survey.error_code == ErrorCode.SURVEY_AVAILABLE_PERSONAS_TOO_FEW.value

    def test_a_vanished_survey_is_logged_and_left_alone(self, em, survey_world, logs):
        start = self._start(em, survey_world)
        survey_world.client.delete_item(
            TableName=survey_world.db.surveys_table,
            Key={"id": {"S": start.survey.id}},
        )
        start.run()
        assert survey_world.db.get_survey(start.survey.id) is None
        assert logs.events("survey.dependency_missing")[0]["missing"] == "survey"


class TestListSurveys:
    @pytest.fixture
    def seeded(self, survey_world):
        world = survey_world
        mine = world.survey("a", name="A 共有なし")
        shared = world.survey("a", name="A 営業共有")
        world.share(SURVEY, shared.id, "sales")
        legacy = world.survey(None, name="古い結果", snapshot=False)
        b_shared = world.survey("b", name="B 営業共有", status="running")
        world.share(SURVEY, b_shared.id, "sales")
        c_own = world.survey("c", name="C の結果", status="error")
        return {
            "mine": mine,
            "shared": shared,
            "legacy": legacy,
            "b": b_shared,
            "c": c_own,
        }

    def test_tabs_and_counts(self, em, seeded):
        mine = em.list_surveys(MEMBER_A, None, None)
        assert {s.id for s in mine.items} == {seeded["mine"].id, seeded["shared"].id}
        assert mine.counts == {"mine": 2, "shared": 1}
        shared = em.list_surveys(MEMBER_A, "shared", None)
        assert [s.id for s in shared.items] == [seeded["b"].id]
        assert shared.manageable == frozenset()

    def test_member_does_not_scan(self, em, seeded, survey_world):
        scan = survey_world.spy("scan")
        em.list_surveys(MEMBER_A, "mine", None)
        em.list_surveys(MEMBER_A, "shared", None, search="B")
        assert scan.call_count == 0

    def test_admin_all_includes_ownerless(self, em, seeded):
        listing = em.list_surveys(ADMIN, "all", None)
        assert seeded["legacy"].id in {s.id for s in listing.items}
        assert listing.owners[seeded["legacy"].id] == "所有者不明"

    def test_search_inside_the_tab(self, em, seeded):
        listing = em.list_surveys(MEMBER_A, "mine", None, search="営業")
        assert [s.id for s in listing.items] == [seeded["shared"].id]


class TestReadSurvey:
    def test_owner_and_shared_reader(self, em, survey_world):
        survey = survey_world.survey("a")
        survey_world.share(SURVEY, survey.id, "sales")
        assert em.get_survey(MEMBER_A, survey.id).id == survey.id
        assert em.get_survey(MEMBER_B, survey.id).id == survey.id

    @pytest.mark.parametrize("which", ["unrelated", "missing"])
    def test_unreadable_is_the_same_as_missing(self, em, survey_world, which):
        survey = survey_world.survey("a")
        target = survey.id if which == "unrelated" else "missing-id"
        with raises_code(CodedError, ErrorCode.SURVEY_NOT_FOUND):
            em.get_survey(MEMBER_C, target)

    def test_ownerless_survey_is_admin_only(self, em, survey_world):
        legacy = survey_world.survey(None, snapshot=False)
        with raises_code(CodedError, ErrorCode.SURVEY_NOT_FOUND):
            em.get_survey(MEMBER_A, legacy.id)
        assert em.get_survey(ADMIN, legacy.id).id == legacy.id

    def test_detail_for_a_shared_reader_is_read_only(self, em, survey_world):
        survey = survey_world.survey("a")
        survey_world.share(SURVEY, survey.id, "sales")
        owner = em.get_survey_detail(MEMBER_A, survey.id)
        reader = em.get_survey_detail(MEMBER_B, survey.id)
        assert owner.manageable is True
        assert reader.manageable is False
        assert reader.template_name == "写しのテンプレート"
        assert reader.analysis_available is True

    def test_legacy_detail_reads_the_template_only_when_readable(
        self, em, survey_world
    ):
        template = survey_world.template("c", "C のテンプレート")
        legacy = survey_world.survey(None, template, snapshot=False)
        detail = em.get_survey_detail(ADMIN, legacy.id)
        assert detail.template_name == "C のテンプレート"
        assert detail.analysis_available is True
        survey_world.client.delete_item(
            TableName=survey_world.db.survey_templates_table,
            Key={"id": {"S": template.id}},
        )
        detail = em.get_survey_detail(ADMIN, legacy.id)
        assert detail.template_name is None
        assert detail.analysis_available is False


class TestDownloadAndImages:
    def test_download_url_after_the_read_check(self, em, survey_world):
        survey = survey_world.survey("a")
        survey_world.share(SURVEY, survey.id, "sales")
        assert em.get_download_url(MEMBER_B, survey.id).startswith("https://")

    def test_stranger_gets_404_and_nothing_is_signed(
        self, em, survey_world, monkeypatch
    ):
        survey = survey_world.survey("a")
        sign = Mock(wraps=survey_world.s3.generate_presigned_url)
        monkeypatch.setattr(survey_world.s3, "generate_presigned_url", sign)
        with raises_code(CodedError, ErrorCode.SURVEY_NOT_FOUND):
            em.get_download_url(MEMBER_C, survey.id)
        with raises_code(CodedError, ErrorCode.SURVEY_NOT_FOUND):
            em.download_results_csv(MEMBER_C, survey.id)
        sign.assert_not_called()

    def test_not_ready_result(self, em, survey_world):
        survey = survey_world.survey("a", status="running")
        with raises_code(CodedError, ErrorCode.SURVEY_RESULT_NOT_READY):
            em.get_download_url(MEMBER_A, survey.id)

    def test_csv_bytes_for_a_reader(self, em, survey_world):
        survey = survey_world.survey("a")
        assert b"persona_id" in em.download_results_csv(MEMBER_A, survey.id)

    def test_result_image_only_from_the_snapshot(self, em, survey_world):
        image = _image(survey_world)
        template = survey_world.template("a", images=[image])
        survey = survey_world.survey("a", template)
        survey_world.share(SURVEY, survey.id, "sales")
        assert em.get_result_image_url(MEMBER_B, survey.id, image.id).startswith(
            "https://"
        )
        with raises_code(CodedError, ErrorCode.FILE_NOT_FOUND):
            em.get_result_image_url(MEMBER_B, survey.id, "not-in-snapshot")
        with raises_code(CodedError, ErrorCode.SURVEY_NOT_FOUND):
            em.get_result_image_url(MEMBER_C, survey.id, image.id)

    def test_deleted_image_is_not_found(self, em, survey_world):
        image = _image(survey_world)
        template = survey_world.template("a", images=[image])
        survey = survey_world.survey("a", template)
        survey_world.s3.delete_file(image.file_path)
        with raises_code(CodedError, ErrorCode.FILE_NOT_FOUND):
            em.get_result_image_url(MEMBER_A, survey.id, image.id)


class TestDeleteSurvey:
    def test_owner_deletes_body_shares_csv_and_cache(self, em, survey_world, cache):
        survey = survey_world.survey("a")
        survey_world.share(SURVEY, survey.id, "sales")

        em.delete_survey(MEMBER_A, survey.id)

        assert survey_world.db.get_survey(survey.id) is None
        assert (
            survey_world.client.scan(TableName=survey_world.shares_table)["Count"] == 0
        )
        listed = survey_world.s3.s3_client.list_objects_v2(
            Bucket=BUCKET, Prefix=f"survey-results/{survey.id}/"
        )
        assert listed.get("KeyCount", 0) == 0
        cache.evict.assert_called_once_with(survey.id)

    def test_shared_reader_403_and_stranger_404(self, em, survey_world):
        survey = survey_world.survey("a")
        survey_world.share(SURVEY, survey.id, "sales")
        with pytest.raises(AccessDeniedError):
            em.delete_survey(MEMBER_B, survey.id)
        with raises_code(CodedError, ErrorCode.SURVEY_NOT_FOUND):
            em.delete_survey(MEMBER_C, survey.id)
        assert survey_world.db.get_survey(survey.id) is not None

    def test_missing_survey_is_not_found(self, em):
        with raises_code(SurveyExecutionManagerError, ErrorCode.SURVEY_NOT_FOUND):
            em.delete_survey(MEMBER_A, "missing-id")

    def test_cleanup_failure_is_logged_and_the_delete_succeeds(
        self, em, survey_world, logs, monkeypatch
    ):
        survey = survey_world.survey("a")
        monkeypatch.setattr(
            survey_world.s3, "delete_file", Mock(side_effect=RuntimeError("down"))
        )
        em.delete_survey(MEMBER_A, survey.id)
        assert survey_world.db.get_survey(survey.id) is None
        assert survey_world.s3.delete_file.call_count == 3
        events = logs.events("survey.cleanup_failed")
        assert events[0]["step"] == "results"
        assert events[0]["resource_type"] == "survey"
        assert "s3://" not in json.dumps(events)
