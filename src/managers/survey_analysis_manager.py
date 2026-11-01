"""
SurveyAnalysisManager
ビジュアル分析 + ペルソナ統計 + インサイトレポート生成を担当するマネージャークラス。

v1.0.0 U7 (``functional-design/rules.md``):

- BR2.4 / BR2.5: every entry point reads the survey's decision columns and
  calls ``ensure_readable`` first; only then is the result CSV read, from the
  per-process ``ResultCsvCache`` (keyed by the survey ID) or from S3.
- BR2.7: the questions come from the survey's template snapshot. A survey
  saved before v1.0.0 (no snapshot) reads its template, and only when the
  reader may read it; otherwise there is no analysis (the CSV stays).
- BR2.11: generating and saving the insight report needs ``ai:execute`` and
  ``ensure_manageable``; the report is checked against the 350KB body limit
  and written alone, so the owner, the shares and the snapshot stay.
"""

import logging
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

from ..models.authz import Permission, Principal, ResourceType
from ..models.errors import CodedError, ErrorCode, LookupUnavailableError
from ..models.survey import (
    InsightReport,
    PersonaStatistics,
    Survey,
    TemplateSnapshot,
    VisualAnalysisData,
)
from ..prompts.survey_prompts import INSIGHT_REPORT_SYSTEM_PROMPT, build_insight_prompt
from ..services.ai_service import AIService
from ..services.s3_service import S3Service
from ..services.service_factory import service_factory
from ..services.survey_batch_service import SurveyBatchService
from ..services.stores.survey_store import SurveyStore, SurveyStoreError
from ..services.stores.survey_template_store import (
    SurveyTemplateStore,
    SurveyTemplateStoreError,
)
from .components.access_policy import AccessPolicy
from .components.resource_lookup import ResourceLookup
from .components.resource_sharing import ResourceSharing
from .components.sharing_runtime import access_parts_from, sharing_parts_from
from .shared.file_utils import parse_results_csv
from .shared.result_csv_cache import ResultCsvCache, shared_result_cache

logger = logging.getLogger(__name__)

_SURVEY = ResourceType.SURVEY
_TEMPLATE = ResourceType.SURVEY_TEMPLATE
#: Largest survey body written, report included (NFR2.25).
MAX_SURVEY_BYTES = 350 * 1024


def _answered(value: Any) -> bool:
    """Whether a CSV answer cell holds an answer (blank: not answered, BR4.6)."""
    return value is not None and bool(str(value).strip())


class SurveyAnalysisManagerError(CodedError):
    """SurveyAnalysisManager層の基底例外"""

    pass


class SurveyAnalysisManager:
    """ビジュアル分析 + ペルソナ統計 + インサイトレポート生成"""

    def __init__(
        self,
        survey_store: Optional[SurveyStore] = None,
        survey_template_store: Optional[SurveyTemplateStore] = None,
        s3_service: Optional[S3Service] = None,
        ai_service: Optional[AIService] = None,
        survey_batch_service: Optional[SurveyBatchService] = None,
        resource_lookup: Optional[ResourceLookup] = None,
        resource_sharing: Optional[ResourceSharing] = None,
        result_cache: Optional[ResultCsvCache] = None,
    ) -> None:
        """Create the manager.

        Args:
            survey_store: Survey bodies.
            survey_template_store: Templates of surveys made before v1.0.0.
            s3_service: Result CSVs.
            ai_service: Insight reports.
            survey_batch_service: Statistical summaries.
            resource_lookup: The per-task ``ResourceLookup`` (decisions).
            resource_sharing: The per-task ``ResourceSharing``.
            result_cache: The per-process result CSV cache.
        """
        self._surveys = survey_store or service_factory.get_survey_store()
        self._templates = (
            survey_template_store or service_factory.get_survey_template_store()
        )
        self.s3_service: S3Service = s3_service or service_factory.get_s3_service()
        self.ai_service = ai_service or service_factory.get_ai_service()
        self.batch_service = (
            survey_batch_service or service_factory.get_survey_batch_service()
        )
        self._lookup = resource_lookup
        self._sharing = resource_sharing
        self._cache = result_cache

    # --- parts (built lazily) -------------------------------------------------------

    @property
    def _resource_lookup(self) -> ResourceLookup:
        if self._lookup is None:
            self._lookup = access_parts_from(service_factory).lookup
        return self._lookup

    @property
    def _resource_sharing(self) -> ResourceSharing:
        if self._sharing is None:
            self._sharing = sharing_parts_from(service_factory).sharing
        return self._sharing

    @property
    def result_cache(self) -> ResultCsvCache:
        """The per-process result CSV cache (shared with the delete, BR1.8)."""
        if self._cache is None:
            self._cache = shared_result_cache()
        return self._cache

    # =========================================================================
    # 判定と読み取り（BR2.4、BR2.5、BR2.7）
    # =========================================================================

    def _readable_survey(self, principal: Principal, survey_id: str) -> Survey:
        target = self._resource_lookup.get(_SURVEY, survey_id)
        AccessPolicy.ensure_readable(principal, target, ErrorCode.SURVEY_NOT_FOUND)
        return self._body(survey_id)

    def _manageable_survey(self, principal: Principal, survey_id: str) -> Survey:
        AccessPolicy.require(principal, Permission.AI_EXECUTE, resource_type=_SURVEY)
        target = self._resource_lookup.get(_SURVEY, survey_id)
        AccessPolicy.ensure_manageable(principal, target, ErrorCode.SURVEY_NOT_FOUND)
        return self._body(survey_id)

    def _body(self, survey_id: str) -> Survey:
        try:
            survey = self._surveys.get(survey_id, consistent=True)
        except SurveyStoreError as e:
            raise LookupUnavailableError("survey body unreadable") from e
        if survey is None:
            raise SurveyAnalysisManagerError(
                "survey is gone", code=ErrorCode.SURVEY_NOT_FOUND
            )
        return survey

    @staticmethod
    def _require_result(survey: Survey) -> str:
        if survey.status != "completed" or not survey.s3_result_path:
            raise SurveyAnalysisManagerError(
                f"survey {survey.id!r} has no result yet",
                code=ErrorCode.SURVEY_RESULT_NOT_READY,
            )
        return survey.s3_result_path

    def _questions_of(self, principal: Principal, survey: Survey) -> TemplateSnapshot:
        """The snapshot, or a readable template for a survey before v1.0.0."""
        if survey.template_snapshot is not None:
            return survey.template_snapshot
        target = self._resource_lookup.get(_TEMPLATE, survey.template_id)
        template = None
        if AccessPolicy.can_read(principal, target):
            try:
                template = self._templates.get(survey.template_id, consistent=True)
            except SurveyTemplateStoreError as e:
                raise LookupUnavailableError("template body unreadable") from e
        if template is None:
            raise SurveyAnalysisManagerError(
                "the template of this old survey is not readable",
                code=ErrorCode.SURVEY_TEMPLATE_NOT_FOUND,
            )
        return TemplateSnapshot.from_template(template)

    def _load_results_csv(self, survey: Survey) -> bytes:
        """The result CSV, from the cache or S3 (called after the decision only)."""
        cached = self.result_cache.get(survey.id)
        if cached is not None:
            return cached
        data = self.s3_service.download_file(self._require_result(survey))
        self.result_cache.put(survey.id, data)
        return data

    # =========================================================================
    # ビジュアル分析
    # =========================================================================

    def get_visual_analysis(
        self, principal: Principal, survey_id: str
    ) -> VisualAnalysisData:
        """選択式/スケール評価の分布（読める人だけ、BR2.4）。

        Raises:
            CodedError: ``SURVEY_NOT_FOUND``, ``SURVEY_RESULT_NOT_READY`` or
                ``SURVEY_TEMPLATE_NOT_FOUND`` (an old survey's template).
        """
        survey = self._readable_survey(principal, survey_id)
        self._require_result(survey)
        questions = self._questions_of(principal, survey)
        rows = parse_results_csv(self._load_results_csv(survey))
        return self._compute_visual_analysis(rows, questions)

    def _compute_visual_analysis(
        self, rows: List[Dict[str, Any]], template: Any
    ) -> VisualAnalysisData:
        """回答データからビジュアル分析データを計算する。"""
        multiple_choice_charts: List[Dict[str, Any]] = []
        scale_rating_charts: List[Dict[str, Any]] = []

        for q in template.questions:
            answer_key = f"{q.id}_answer"
            # A blank answer is a persona without an answer (BR4.6): it leaves the
            # question's counts, but its row stays in the attribute statistics
            answers = [r[answer_key] for r in rows if _answered(r.get(answer_key))]

            if q.question_type == "multiple_choice":
                counts: Dict[str, int] = {}
                for opt in q.options:
                    counts[opt] = 0
                for ans in answers:
                    if q.allow_multiple:
                        for part in str(ans).split("|"):
                            part = part.strip()
                            if part:
                                if part in counts:
                                    counts[part] += 1
                                else:
                                    counts[part] = counts.get(part, 0) + 1
                    else:
                        if ans in counts:
                            counts[ans] += 1
                        else:
                            counts[ans] = counts.get(ans, 0) + 1

                cross_tab = self._cross_tabulate_choice(
                    rows, answer_key, q.allow_multiple
                )
                multiple_choice_charts.append(
                    {
                        "question_text": q.text,
                        "question_id": q.id,
                        "options": list(counts.keys()),
                        "counts": list(counts.values()),
                        "allow_multiple": q.allow_multiple,
                        "respondent_count": len(answers),
                        "total_count": len(rows),
                        "cross_tab": cross_tab,
                    }
                )

            elif q.question_type == "scale_rating":
                distribution: Dict[int, int] = {}
                for i in range(q.scale_min, q.scale_max + 1):
                    distribution[i] = 0
                numeric_values: List[float] = []
                for ans in answers:
                    try:
                        val = int(ans)
                        if val in distribution:
                            distribution[val] += 1
                        numeric_values.append(float(val))
                    except (ValueError, TypeError):
                        continue  # 非数値回答はスキップ
                average = (
                    sum(numeric_values) / len(numeric_values) if numeric_values else 0.0
                )

                cross_tab = self._cross_tabulate_scale(rows, answer_key)
                scale_rating_charts.append(
                    {
                        "question_text": q.text,
                        "question_id": q.id,
                        "distribution": distribution,
                        "average": round(average, 2),
                        "cross_tab": cross_tab,
                    }
                )

        return VisualAnalysisData(
            multiple_choice_charts=multiple_choice_charts,
            scale_rating_charts=scale_rating_charts,
        )

    @staticmethod
    def _get_age_bracket(age_str: str) -> Optional[str]:
        """年齢文字列を10歳刻みの年齢層ラベルに変換する。"""
        try:
            age = int(float(age_str))
            bracket = age // 10 * 10
            return f"{bracket}代"
        except (ValueError, TypeError):
            return None

    def _cross_tabulate_choice(
        self, rows: List[Dict[str, Any]], answer_key: str, allow_multiple: bool
    ) -> Dict[str, Any]:
        """選択式質問の属性別クロス集計。"""
        by_sex: Dict[str, Dict[str, int]] = {}
        by_age: Dict[str, Dict[str, int]] = {}
        sex_n: Dict[str, int] = {}
        age_n: Dict[str, int] = {}

        for r in rows:
            ans = r.get(answer_key, "")
            if not _answered(ans):
                continue
            sex = r.get("sex", "")
            age_bracket = self._get_age_bracket(r.get("age", ""))
            if sex:
                sex_n[sex] = sex_n.get(sex, 0) + 1
            if age_bracket:
                age_n[age_bracket] = age_n.get(age_bracket, 0) + 1
            parts = (
                [p.strip() for p in str(ans).split("|") if p.strip()]
                if allow_multiple
                else [str(ans)]
            )
            for part in parts:
                if sex:
                    by_sex.setdefault(sex, {})
                    by_sex[sex][part] = by_sex[sex].get(part, 0) + 1
                if age_bracket:
                    by_age.setdefault(age_bracket, {})
                    by_age[age_bracket][part] = by_age[age_bracket].get(part, 0) + 1

        return {"by_sex": by_sex, "by_age": by_age, "sex_n": sex_n, "age_n": age_n}

    def _cross_tabulate_scale(
        self, rows: List[Dict[str, Any]], answer_key: str
    ) -> Dict[str, Any]:
        """スケール評価の属性別平均値を計算する。"""
        sex_vals: Dict[str, List[float]] = {}
        age_vals: Dict[str, List[float]] = {}

        for r in rows:
            ans = r.get(answer_key, "")
            if not _answered(ans):
                continue
            try:
                val = float(ans)
            except (ValueError, TypeError):
                continue
            sex = r.get("sex", "")
            age_bracket = self._get_age_bracket(r.get("age", ""))
            if sex:
                sex_vals.setdefault(sex, []).append(val)
            if age_bracket:
                age_vals.setdefault(age_bracket, []).append(val)

        by_sex = {k: round(sum(v) / len(v), 2) for k, v in sex_vals.items()}
        by_age = {k: round(sum(v) / len(v), 2) for k, v in age_vals.items()}
        return {"by_sex": by_sex, "by_age": by_age}

    # =========================================================================
    # ペルソナ統計
    # =========================================================================

    def get_persona_statistics(
        self, principal: Principal, survey_id: str
    ) -> PersonaStatistics:
        """回答したペルソナの統計（読める人だけ、BR2.4）。

        Raises:
            CodedError: ``SURVEY_NOT_FOUND`` / ``SURVEY_RESULT_NOT_READY``.
        """
        survey = self._readable_survey(principal, survey_id)
        rows = parse_results_csv(self._load_results_csv(survey))
        return self._compute_persona_statistics(rows)

    def _compute_persona_statistics(
        self, rows: List[Dict[str, Any]]
    ) -> PersonaStatistics:
        """回答データからペルソナ統計を計算する。"""
        total = len(rows)

        sex_counter: Counter[str] = Counter()
        age_values: List[int] = []
        age_bracket_counter: Counter[str] = Counter()
        occupation_counter: Counter[str] = Counter()
        region_counter: Counter[str] = Counter()
        prefecture_counter: Counter[str] = Counter()
        marital_counter: Counter[str] = Counter()

        for row in rows:
            sex = row.get("sex", "").strip()
            if sex:
                sex_counter[sex] += 1

            age_str = row.get("age", "").strip()
            if age_str:
                try:
                    age_val = int(age_str)
                    age_values.append(age_val)
                    bracket = f"{(age_val // 10) * 10}代"
                    age_bracket_counter[bracket] += 1
                except (ValueError, TypeError):
                    continue  # 非数値の年齢文字列はスキップ

            occupation = row.get("occupation", "").strip()
            if occupation:
                occupation_counter[occupation] += 1

            region = row.get("region", "").strip()
            if region:
                region_counter[region] += 1

            prefecture = row.get("prefecture", "").strip()
            if prefecture:
                prefecture_counter[prefecture] += 1

            marital = row.get("marital_status", "").strip()
            if marital:
                marital_counter[marital] += 1

        age_stats: Dict[str, Any] = {}
        if age_values:
            age_stats = {
                "min": min(age_values),
                "max": max(age_values),
                "average": round(sum(age_values) / len(age_values), 1),
            }

        sorted_age = dict(sorted(age_bracket_counter.items(), key=lambda x: x[0]))

        return PersonaStatistics(
            total_count=total,
            sex_distribution=dict(sex_counter.most_common()),
            age_distribution=sorted_age,
            occupation_distribution=dict(occupation_counter.most_common(15)),
            region_distribution=dict(region_counter.most_common()),
            prefecture_distribution=dict(prefecture_counter.most_common(15)),
            marital_status_distribution=dict(marital_counter.most_common()),
            age_stats=age_stats,
        )

    # =========================================================================
    # インサイトレポート（BR2.11）
    # =========================================================================

    def _report_prompt(self, principal: Principal, survey: Survey) -> str:
        self._require_result(survey)
        questions = self._questions_of(principal, survey)
        csv_text = self._load_results_csv(survey).decode("utf-8-sig")
        summary = self._generate_statistical_summary(csv_text, questions)
        return build_insight_prompt(summary, questions)  # type: ignore[arg-type]

    def generate_insight_report_streaming(
        self, principal: Principal, survey_id: str
    ) -> Iterator[str]:
        """インサイトレポートをストリーミング生成する（判定は最初の 1 片の前、BR2.11）。

        Raises:
            AccessDeniedError: No ``ai:execute``, or readable but not manageable.
            CodedError: ``SURVEY_NOT_FOUND`` / ``SURVEY_RESULT_NOT_READY``.
        """
        survey = self._manageable_survey(principal, survey_id)
        prompt = self._report_prompt(principal, survey)
        converse_messages = [{"role": "user", "content": [{"text": prompt}]}]
        stream: Iterator[str] = self.ai_service.generate_standard_report_streaming(
            system_prompt=INSIGHT_REPORT_SYSTEM_PROMPT,
            converse_messages=converse_messages,
        )
        return stream

    def save_insight_report(
        self, principal: Principal, survey_id: str, content: str
    ) -> InsightReport:
        """ストリーミング生成済みのレポートを保存する（管理できる人だけ、BR2.11）。

        Raises:
            AccessDeniedError: No ``ai:execute``, or readable but not manageable.
            CodedError: ``SURVEY_NOT_FOUND`` / ``SURVEY_REPORT_TOO_LARGE``.
        """
        survey = self._manageable_survey(principal, survey_id)
        return self._store_report(survey, content)

    def _store_report(self, survey: Survey, content: str) -> InsightReport:
        report = InsightReport.create_new(survey_id=survey.id, content=content)
        with_report = survey.to_dict()
        with_report["insight_report"] = report.to_dict()
        if self._surveys.item_size(with_report) > MAX_SURVEY_BYTES:
            raise SurveyAnalysisManagerError(
                "the report pushes the survey over 350KB",
                code=ErrorCode.SURVEY_REPORT_TOO_LARGE,
            )
        try:
            written = self._surveys.update_fields(
                survey.id,
                {
                    "insight_report": report.to_dict(),
                    "updated_at": datetime.now().isoformat(),
                },
            )
        except SurveyStoreError as e:
            raise SurveyAnalysisManagerError(
                "insight report could not be saved",
                code=ErrorCode.SURVEY_OPERATION_FAILED,
            ) from e
        if not written:
            raise SurveyAnalysisManagerError(
                "survey is gone", code=ErrorCode.SURVEY_NOT_FOUND
            )
        logger.info("Insight report saved for survey: %s", survey.id)
        return report

    # =========================================================================
    # 統計要約生成（SurveyBatchServiceに委譲）
    # =========================================================================

    def _generate_statistical_summary(
        self, results_csv: str, template: Any
    ) -> Dict[str, Any]:
        """CSV結果から統計要約を生成する。DataFrame操作はService層に委譲。"""
        questions = [
            {
                "id": q.id,
                "text": q.text,
                "question_type": q.question_type,
                "options": q.options if hasattr(q, "options") else [],
                "allow_multiple": q.allow_multiple
                if hasattr(q, "allow_multiple")
                else False,
            }
            for q in template.questions
        ]
        return self.batch_service.generate_statistical_summary(results_csv, questions)
