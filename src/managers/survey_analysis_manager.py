"""
SurveyAnalysisManager
ビジュアル分析 + ペルソナ統計 + インサイトレポート生成を担当するマネージャークラス。
"""

import logging
import time
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..models.survey import InsightReport, PersonaStatistics, VisualAnalysisData
from ..models.survey_template import SurveyTemplate
from ..prompts.survey_prompts import INSIGHT_REPORT_SYSTEM_PROMPT, build_insight_prompt
from ..services.ai_service import AIService
from ..services.database_service import DatabaseService
from ..services.s3_service import S3Service
from ..services.service_factory import service_factory
from ..services.survey_batch_service import SurveyBatchService
from .shared.file_utils import parse_results_csv

logger = logging.getLogger(__name__)


class SurveyAnalysisManagerError(Exception):
    """SurveyAnalysisManager層の基底例外"""

    pass


class SurveyAnalysisManager:
    """ビジュアル分析 + ペルソナ統計 + インサイトレポート生成"""

    _CACHE_TTL_SECONDS: int = 300

    def __init__(
        self,
        database_service: Optional[DatabaseService] = None,
        s3_service: Optional[S3Service] = None,
        ai_service: Optional[AIService] = None,
        survey_batch_service: Optional[SurveyBatchService] = None,
    ) -> None:
        self.db = database_service or service_factory.get_database_service()
        self.s3_service: S3Service = s3_service or service_factory.get_s3_service()
        self.ai_service = ai_service or service_factory.get_ai_service()
        self.batch_service = (
            survey_batch_service or service_factory.get_survey_batch_service()
        )
        self._csv_cache: Dict[str, tuple[float, bytes]] = {}

    # =========================================================================
    # 内部ヘルパー
    # =========================================================================

    def _load_results_csv(self, s3_path: str) -> bytes:
        """S3から結果CSVを取得する（TTLキャッシュ付き）。"""
        now = time.monotonic()
        cached = self._csv_cache.get(s3_path)
        if cached is not None:
            cached_at, data = cached
            if now - cached_at < self._CACHE_TTL_SECONDS:
                return data
        data = self.s3_service.download_file(s3_path)
        self._csv_cache[s3_path] = (now, data)
        return data

    def _get_survey_with_result(self, survey_id: str) -> Any:
        """アンケートを取得し結果が存在することを確認する。"""
        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyAnalysisManagerError(f"アンケートが見つかりません: {survey_id}")
        if not survey.s3_result_path:
            raise SurveyAnalysisManagerError(
                f"アンケート結果がまだ生成されていません: {survey_id}"
            )
        return survey

    def _get_template(self, template_id: str) -> SurveyTemplate:
        """テンプレートを取得する。"""
        template = self.db.get_survey_template(template_id)
        if template is None:
            raise SurveyAnalysisManagerError(
                f"テンプレートが見つかりません: {template_id}"
            )
        return template

    # =========================================================================
    # ビジュアル分析
    # =========================================================================

    def get_visual_analysis(self, survey_id: str) -> VisualAnalysisData:
        """CSVデータをパースし、選択式/スケール評価の分布を計算する。"""
        survey = self._get_survey_with_result(survey_id)
        template = self._get_template(survey.template_id)

        csv_bytes = self._load_results_csv(survey.s3_result_path)
        rows = parse_results_csv(csv_bytes)
        return self._compute_visual_analysis(rows, template)

    def _compute_visual_analysis(
        self, rows: List[Dict[str, Any]], template: SurveyTemplate
    ) -> VisualAnalysisData:
        """回答データからビジュアル分析データを計算する。"""
        multiple_choice_charts: List[Dict[str, Any]] = []
        scale_rating_charts: List[Dict[str, Any]] = []

        for q in template.questions:
            answer_key = f"{q.id}_answer"
            answers = [r[answer_key] for r in rows if answer_key in r and r[answer_key]]

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
            if not ans:
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
            if not ans:
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

    def get_persona_statistics(self, survey_id: str) -> PersonaStatistics:
        """CSVデータからペルソナの統計情報を集計する。"""
        survey = self._get_survey_with_result(survey_id)

        csv_bytes = self._load_results_csv(survey.s3_result_path)
        rows = parse_results_csv(csv_bytes)
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
                    pass

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
    # インサイトレポート
    # =========================================================================

    def generate_insight_report(self, survey_id: str) -> InsightReport:
        """インサイトレポートを生成しDynamoDBに保存する。"""
        survey = self._get_survey_with_result(survey_id)
        template = self._get_template(survey.template_id)

        try:
            csv_bytes = self._load_results_csv(survey.s3_result_path)
            csv_text = csv_bytes.decode("utf-8-sig")

            summary = self._generate_statistical_summary(csv_text, template)
            prompt = build_insight_prompt(summary, template)
            content = self.ai_service.invoke_model(prompt, max_tokens=8000)

            report = InsightReport.create_new(survey_id=survey_id, content=content)

            survey.insight_report = report
            survey.updated_at = datetime.now()
            self.db.update_survey(survey)

            logger.info(f"Insight report generated for survey: {survey_id}")
            return report

        except SurveyAnalysisManagerError:
            raise
        except Exception as e:
            logger.error(f"Failed to generate insight report: {e}")
            raise SurveyAnalysisManagerError(
                f"レポート生成に失敗しました。再試行してください: {e}"
            ) from e

    def generate_insight_report_streaming(self, survey_id: str) -> Any:
        """インサイトレポートをストリーミング生成する。"""
        survey = self._get_survey_with_result(survey_id)
        template = self._get_template(survey.template_id)

        csv_bytes = self._load_results_csv(survey.s3_result_path)
        csv_text = csv_bytes.decode("utf-8-sig")

        summary = self._generate_statistical_summary(csv_text, template)
        prompt = build_insight_prompt(summary, template)
        converse_messages = [{"role": "user", "content": [{"text": prompt}]}]

        yield from self.ai_service.generate_standard_report_streaming(
            system_prompt=INSIGHT_REPORT_SYSTEM_PROMPT,
            converse_messages=converse_messages,
        )

    def save_insight_report(self, survey_id: str, content: str) -> InsightReport:
        """ストリーミング生成済みのレポートを保存する。"""
        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyAnalysisManagerError(f"アンケートが見つかりません: {survey_id}")

        report = InsightReport.create_new(survey_id=survey_id, content=content)
        survey.insight_report = report
        survey.updated_at = datetime.now()
        self.db.update_survey(survey)

        logger.info(f"Insight report saved for survey: {survey_id}")
        return report

    # =========================================================================
    # 統計要約生成（SurveyBatchServiceに委譲）
    # =========================================================================

    def _generate_statistical_summary(
        self, results_csv: str, template: SurveyTemplate
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
