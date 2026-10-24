"""SurveyAnalysisManager のユニットテスト（U7）。

BR2.4、BR2.5、BR2.7、BR2.11、NFR1.67〜NFR1.69、NFR2.22、NFR2.25。DynamoDB と S3 は
moto、ResourceLookup・ResourceSharing・AccessPolicy・ResultCsvCache は本物
（tests/unit/survey_world.py）、AI はモック。
"""

from unittest.mock import Mock

import pytest

from src.managers.shared.result_csv_cache import ResultCsvCache
from src.managers.survey_analysis_manager import (
    MAX_SURVEY_BYTES,
    SurveyAnalysisManager,
    SurveyAnalysisManagerError,
)
from src.models.authz import ResourceType
from src.models.errors import AccessDeniedError, CodedError, ErrorCode
from src.models.survey import InsightReport, PersonaStatistics, VisualAnalysisData
from src.models.survey_template import Question, SurveyTemplate
from src.services.survey_template_store import SurveyTemplateStore
from tests.error_helpers import raises_code
from tests.unit.survey_world import (
    ADMIN,
    MEMBER_A,
    MEMBER_B,
    MEMBER_C,
    NO_ROLE,
    SurveyWorld,
)

SURVEY = ResourceType.SURVEY


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
    return ("\n".join([header] + rows)).encode("utf-8-sig")


@pytest.fixture
def ai() -> Mock:
    ai = Mock()
    ai.invoke_model.return_value = "# レポート"
    ai.generate_standard_report_streaming.side_effect = lambda **_: iter(["# ", "本文"])
    return ai


@pytest.fixture
def batch() -> Mock:
    batch = Mock()
    batch.generate_statistical_summary.return_value = {"total": 3}
    return batch


@pytest.fixture
def cache() -> ResultCsvCache:
    return ResultCsvCache()


@pytest.fixture
def am(survey_world: SurveyWorld, ai, batch, cache) -> SurveyAnalysisManager:
    return SurveyAnalysisManager(
        database_service=survey_world.db,
        survey_template_store=survey_world.template_store,
        s3_service=survey_world.s3,
        ai_service=ai,
        survey_batch_service=batch,
        resource_lookup=survey_world.lookup,
        resource_sharing=survey_world.sharing,
        result_cache=cache,
    )


def _survey(world: SurveyWorld, template: SurveyTemplate, owner: str = "a", **kw):
    return world.survey(owner, template, csv=_make_csv_bytes(template), **kw)


# =============================================================================
# 判定のあとでだけ CSV を読む（BR2.4、BR2.5、NFR1.67、NFR1.68）
# =============================================================================


class TestVisualAnalysis:
    def test_owner_gets_charts_from_the_snapshot(
        self, am, survey_world, sample_template
    ):
        survey = _survey(survey_world, sample_template)
        result = am.get_visual_analysis(MEMBER_A, survey.id)
        assert isinstance(result, VisualAnalysisData)
        assert len(result.multiple_choice_charts) == 1
        assert len(result.scale_rating_charts) == 1

    def test_shared_reader_gets_them_without_the_template(
        self, am, survey_world, sample_template
    ):
        survey = _survey(survey_world, sample_template)
        survey_world.share(SURVEY, survey.id, "sales")
        # テンプレートは保存されていない（共有もされていない）が、写しで分析が出る
        assert survey_world.template_store.get(sample_template.id) is None
        result = am.get_visual_analysis(MEMBER_B, survey.id)
        assert result.multiple_choice_charts[0]["question_text"] == "好きな色は？"

    @pytest.mark.parametrize("which", ["unrelated", "missing"])
    def test_unreadable_reads_neither_cache_nor_s3(
        self, am, survey_world, sample_template, cache, monkeypatch, which
    ):
        survey = _survey(survey_world, sample_template)
        am.get_visual_analysis(MEMBER_A, survey.id)  # the owner fills the cache
        download = Mock(wraps=survey_world.s3.download_file)
        monkeypatch.setattr(survey_world.s3, "download_file", download)
        get = Mock(wraps=cache.get)
        monkeypatch.setattr(cache, "get", get)
        target = survey.id if which == "unrelated" else "missing-id"

        with raises_code(CodedError, ErrorCode.SURVEY_NOT_FOUND):
            am.get_visual_analysis(MEMBER_C, target)
        with raises_code(CodedError, ErrorCode.SURVEY_NOT_FOUND):
            am.get_persona_statistics(MEMBER_C, target)

        get.assert_not_called()
        download.assert_not_called()

    def test_not_completed_is_not_ready(self, am, survey_world, sample_template):
        survey = survey_world.survey("a", sample_template, status="running")
        with raises_code(CodedError, ErrorCode.SURVEY_RESULT_NOT_READY):
            am.get_visual_analysis(MEMBER_A, survey.id)

    def test_old_survey_reads_its_template_when_readable(
        self, am, survey_world, sample_template
    ):
        template = sample_template.owned_by("c")
        survey_world.template_store.put_new(template)
        legacy = survey_world.survey(
            None, template, snapshot=False, csv=_make_csv_bytes(template)
        )
        result = am.get_visual_analysis(ADMIN, legacy.id)
        assert result.multiple_choice_charts[0]["question_text"] == "好きな色は？"

    def test_old_survey_without_a_readable_template_has_no_analysis(
        self, am, survey_world, sample_template
    ):
        legacy = survey_world.survey(
            None, sample_template, snapshot=False, csv=_make_csv_bytes(sample_template)
        )
        with raises_code(CodedError, ErrorCode.SURVEY_TEMPLATE_NOT_FOUND):
            am.get_visual_analysis(ADMIN, legacy.id)


class TestPersonaStatistics:
    def test_counts_the_respondents(self, am, survey_world, sample_template):
        survey = _survey(survey_world, sample_template)
        result = am.get_persona_statistics(MEMBER_A, survey.id)
        assert isinstance(result, PersonaStatistics)
        assert result.total_count == 3
        assert result.sex_distribution["男性"] == 2

    def test_one_s3_read_for_the_whole_screen(
        self, am, survey_world, sample_template, monkeypatch
    ):
        survey = _survey(survey_world, sample_template)
        download = Mock(wraps=survey_world.s3.download_file)
        monkeypatch.setattr(survey_world.s3, "download_file", download)
        am.get_visual_analysis(MEMBER_A, survey.id)
        am.get_persona_statistics(MEMBER_A, survey.id)
        am.get_visual_analysis(MEMBER_A, survey.id)
        assert download.call_count == 1

    def test_the_cache_key_is_the_survey_id(
        self, am, survey_world, sample_template, cache
    ):
        survey = _survey(survey_world, sample_template)
        am.get_persona_statistics(MEMBER_A, survey.id)
        assert cache.get(survey.id) is not None

    def test_deleted_survey_is_not_served_from_the_cache(
        self, am, survey_world, sample_template, cache
    ):
        survey = _survey(survey_world, sample_template)
        am.get_persona_statistics(MEMBER_A, survey.id)
        survey_world.client.delete_item(
            TableName=survey_world.db.surveys_table, Key={"id": {"S": survey.id}}
        )
        cache.evict(survey.id)
        with raises_code(CodedError, ErrorCode.SURVEY_NOT_FOUND):
            am.get_persona_statistics(MEMBER_A, survey.id)
        assert cache.get(survey.id) is None


# =============================================================================
# インサイトレポート（BR2.11、NFR1.69、NFR2.25）
# =============================================================================


class TestInsightReport:
    def test_owner_generates_and_saves_it(
        self, am, survey_world, sample_template, ai, batch
    ):
        survey = _survey(survey_world, sample_template)
        survey_world.share(SURVEY, survey.id, "sales")

        report = am.generate_insight_report(MEMBER_A, survey.id)

        assert isinstance(report, InsightReport)
        stored = survey_world.db.get_survey(survey.id, consistent=True)
        assert stored.insight_report.content == "# レポート"
        assert stored.owner_sub == "a"
        assert stored.shared_group_ids == frozenset({"sales"})
        # 統計の要約は写しの質問から作る（BR2.7）
        questions = batch.generate_statistical_summary.call_args.args[1]
        assert [q["text"] for q in questions][0] == "好きな色は？"

    def test_shared_reader_gets_403_stranger_404_and_no_ai(
        self, am, survey_world, sample_template, ai
    ):
        survey = _survey(survey_world, sample_template)
        survey_world.share(SURVEY, survey.id, "sales")
        with pytest.raises(AccessDeniedError):
            am.generate_insight_report(MEMBER_B, survey.id)
        with pytest.raises(AccessDeniedError):
            am.generate_insight_report_streaming(MEMBER_B, survey.id)
        with pytest.raises(AccessDeniedError):
            am.save_insight_report(MEMBER_B, survey.id, "x")
        with raises_code(CodedError, ErrorCode.SURVEY_NOT_FOUND):
            am.generate_insight_report_streaming(MEMBER_C, survey.id)
        ai.invoke_model.assert_not_called()
        ai.generate_standard_report_streaming.assert_not_called()

    def test_needs_ai_execute(self, am, survey_world, sample_template, ai):
        survey = _survey(survey_world, sample_template, owner="x")
        with pytest.raises(AccessDeniedError):
            am.generate_insight_report_streaming(NO_ROLE, survey.id)
        ai.generate_standard_report_streaming.assert_not_called()

    def test_streaming_checks_before_the_first_chunk(
        self, am, survey_world, sample_template, ai
    ):
        survey = _survey(survey_world, sample_template)
        chunks = am.generate_insight_report_streaming(MEMBER_A, survey.id)
        assert "".join(chunks) == "# 本文"

    def test_saved_report_keeps_owner_and_shares(
        self, am, survey_world, sample_template
    ):
        survey = _survey(survey_world, sample_template)
        survey_world.share(SURVEY, survey.id, "sales")
        report = am.save_insight_report(MEMBER_A, survey.id, "# 内容")
        assert report.content == "# 内容"
        stored = survey_world.db.get_survey(survey.id, consistent=True)
        assert stored.insight_report.content == "# 内容"
        assert stored.shared_group_ids == frozenset({"sales"})
        assert stored.template_snapshot is not None

    def test_report_over_350kb_is_not_saved(self, am, survey_world, sample_template):
        survey = _survey(survey_world, sample_template)
        with raises_code(CodedError, ErrorCode.SURVEY_REPORT_TOO_LARGE):
            am.save_insight_report(MEMBER_A, survey.id, "あ" * (MAX_SURVEY_BYTES // 2))
        stored = survey_world.db.get_survey(survey.id, consistent=True)
        assert stored.insight_report is None

    def test_admin_may_generate_for_an_ownerless_survey(
        self, am, survey_world, sample_template
    ):
        template = survey_world.template("c")
        legacy = survey_world.survey(
            None, template, snapshot=False, csv=_make_csv_bytes(sample_template)
        )
        assert (
            am.save_insight_report(ADMIN, legacy.id, "# 管理者").content == "# 管理者"
        )

    def test_ai_failure_is_a_coded_error(self, am, survey_world, sample_template, ai):
        survey = _survey(survey_world, sample_template)
        ai.invoke_model.side_effect = RuntimeError("bedrock down")
        with raises_code(
            SurveyAnalysisManagerError, ErrorCode.SURVEY_REPORT_GENERATION_FAILED
        ):
            am.generate_insight_report(MEMBER_A, survey.id)


# =============================================================================
# 計算だけの部品（今のまま）
# =============================================================================


class TestComputeVisualAnalysis:
    def test_multiple_choice_counts(self, sample_template: SurveyTemplate) -> None:
        q_mc = sample_template.questions[0]
        rows = [
            {f"{q_mc.id}_answer": "赤", "sex": "男性", "age": "25"},
            {f"{q_mc.id}_answer": "赤", "sex": "女性", "age": "35"},
            {f"{q_mc.id}_answer": "青", "sex": "男性", "age": "45"},
        ]
        result = SurveyAnalysisManager._compute_visual_analysis(
            Mock(), rows, sample_template
        )
        chart = result.multiple_choice_charts[0]
        assert chart["counts"][chart["options"].index("赤")] == 2
        assert chart["counts"][chart["options"].index("青")] == 1

    def test_scale_average(self, sample_template: SurveyTemplate) -> None:
        q_scale = sample_template.questions[1]
        rows = [
            {f"{q_scale.id}_answer": "4", "sex": "男性", "age": "25"},
            {f"{q_scale.id}_answer": "5", "sex": "女性", "age": "35"},
            {f"{q_scale.id}_answer": "3", "sex": "男性", "age": "45"},
        ]
        mgr = SurveyAnalysisManager(
            database_service=Mock(),
            survey_template_store=Mock(spec=SurveyTemplateStore),
            s3_service=Mock(),
            ai_service=Mock(),
            survey_batch_service=Mock(),
            resource_lookup=Mock(),
            resource_sharing=Mock(),
            result_cache=ResultCsvCache(),
        )
        result = mgr._compute_visual_analysis(rows, sample_template)
        assert result.scale_rating_charts[0]["average"] == 4.0


class TestGetAgeBracket:
    def test_normal(self) -> None:
        assert SurveyAnalysisManager._get_age_bracket("25") == "20代"
        assert SurveyAnalysisManager._get_age_bracket("39") == "30代"

    def test_invalid(self) -> None:
        assert SurveyAnalysisManager._get_age_bracket("") is None
        assert SurveyAnalysisManager._get_age_bracket("abc") is None
