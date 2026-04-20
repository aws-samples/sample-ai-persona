"""
SurveyManager
マスアンケート機能のビジネスロジックを管理するマネージャークラス。
テンプレート管理、アンケート実行制御、結果取得・分析を担当する。
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.models.survey import (
    InsightReport,
    PersonaStatistics,
    Survey,
    VisualAnalysisData,
)
from src.models.survey_template import Question, SurveyTemplate, TemplateImage
from src.services.ai_service import AIService
from src.services.database_service import DatabaseService
from src.services.survey_service import SurveyService

logger = logging.getLogger(__name__)


class SurveyManagerError(Exception):
    """SurveyManager層の基底例外"""

    pass


class SurveyValidationError(SurveyManagerError):
    """バリデーションエラー"""

    pass


class SurveyExecutionError(SurveyManagerError):
    """アンケート実行エラー"""

    pass


class SurveyManager:
    """マスアンケート機能のビジネスロジックを管理するマネージャークラス"""

    def __init__(
        self,
        database_service: DatabaseService,
        survey_service: SurveyService,
        ai_service: Optional[AIService] = None,
    ) -> None:
        self.db = database_service
        self.survey_service = survey_service
        self.ai_service = ai_service

    # =========================================================================
    # テンプレート管理
    # =========================================================================

    def create_template(
        self,
        name: str,
        questions: List[Question],
        images: Optional[List[TemplateImage]] = None,
    ) -> SurveyTemplate:
        """
        アンケートテンプレートを作成して保存する。

        Args:
            name: テンプレート名
            questions: 質問リスト
            images: テンプレート画像リスト（最大1枚）

        Returns:
            SurveyTemplate: 作成されたテンプレート

        Raises:
            SurveyValidationError: バリデーションエラー
        """
        self._validate_template_name(name)
        self._validate_questions(questions)
        if images:
            self._validate_images(images)

        template = SurveyTemplate.create_new(
            name=name.strip(), questions=questions, images=images or []
        )
        self.db.save_survey_template(template)
        logger.info(f"Template created: {template.id} ({template.name})")
        return template

    def get_template(self, template_id: str) -> Optional[SurveyTemplate]:
        """テンプレートをIDで取得する。"""
        return self.db.get_survey_template(template_id)

    def get_all_templates(self) -> List[SurveyTemplate]:
        """全テンプレートを取得する。"""
        return self.db.get_all_survey_templates()

    def update_template(
        self,
        template_id: str,
        name: str,
        questions: List[Question],
        images: Optional[List[TemplateImage]] = None,
    ) -> SurveyTemplate:
        """
        既存テンプレートを更新する。

        Args:
            template_id: テンプレートID
            name: 新しいテンプレート名
            questions: 新しい質問リスト
            images: テンプレート画像リスト（最大3枚）

        Returns:
            SurveyTemplate: 更新されたテンプレート

        Raises:
            SurveyValidationError: バリデーションエラー
            SurveyManagerError: テンプレートが見つからない場合
        """
        self._validate_template_name(name)
        self._validate_questions(questions)
        if images:
            self._validate_images(images)

        existing = self.db.get_survey_template(template_id)
        if existing is None:
            raise SurveyManagerError(f"テンプレートが見つかりません: {template_id}")

        updated = SurveyTemplate(
            id=existing.id,
            name=name.strip(),
            questions=questions,
            created_at=existing.created_at,
            updated_at=datetime.now(),
            images=images or [],
        )
        self.db.update_survey_template(updated)
        logger.info(f"Template updated: {updated.id} ({updated.name})")
        return updated

    def delete_template(self, template_id: str) -> bool:
        """テンプレートを削除する。"""
        result = self.db.delete_survey_template(template_id)
        if result:
            logger.info(f"Template deleted: {template_id}")
        return result

    # =========================================================================
    # バリデーション
    # =========================================================================

    @staticmethod
    def _validate_template_name(name: str) -> None:
        """テンプレート名のバリデーション。"""
        if not name or not name.strip():
            raise SurveyValidationError("テンプレート名は空白のみでは登録できません")

    @staticmethod
    def _validate_questions(questions: List[Question]) -> None:
        """質問リストのバリデーション。"""
        if not questions:
            raise SurveyValidationError("質問が1つも含まれていません")
        for q in questions:
            if q.question_type == "multiple_choice" and len(q.options) < 2:
                raise SurveyValidationError(
                    f"選択式質問「{q.text}」には2つ以上の選択肢が必要です"
                )

    @staticmethod
    def _validate_images(images: List[TemplateImage]) -> None:
        """画像リストのバリデーション。"""
        if len(images) > 1:
            raise SurveyValidationError("画像は1枚まで添付できます")
        for img in images:
            if not img.name or not img.name.strip():
                raise SurveyValidationError("画像には名前を設定してください")

    # =========================================================================
    # アンケートAI生成（Issue #23）
    # =========================================================================

    _MAX_AI_MESSAGES = 40  # 会話履歴の最大件数（ユーザー+AI合計）
    _MAX_AI_MESSAGE_LENGTH = 2000  # 1メッセージあたりの最大文字数

    def _validate_ai_messages(self, messages: List[Dict[str, str]]) -> None:
        if not isinstance(messages, list) or not messages:
            raise SurveyValidationError("会話履歴が空です")
        if len(messages) > self._MAX_AI_MESSAGES:
            raise SurveyValidationError(
                f"会話履歴が長すぎます（最大 {self._MAX_AI_MESSAGES} 件）"
            )
        for m in messages:
            if not isinstance(m, dict):
                raise SurveyValidationError("会話履歴の形式が不正です")
            if m.get("role") not in ("user", "assistant"):
                raise SurveyValidationError("会話履歴に不正な role が含まれています")
            content = m.get("content")
            if not isinstance(content, str) or not content.strip():
                raise SurveyValidationError("会話履歴に空のメッセージが含まれています")
            if len(content) > self._MAX_AI_MESSAGE_LENGTH:
                raise SurveyValidationError(
                    f"1メッセージは{self._MAX_AI_MESSAGE_LENGTH}文字以内にしてください"
                )

    def generate_ai_chat_response(self, messages: List[Dict[str, str]]) -> str:
        """AIチャットヒアリングの1ターンを処理してassistant発言を返す。"""
        if self.ai_service is None:
            raise SurveyManagerError("AIService が利用できません")
        self._validate_ai_messages(messages)
        if messages[-1].get("role") != "user":
            raise SurveyValidationError(
                "最後のメッセージはユーザー発言である必要があります"
            )
        return self.ai_service.chat_for_survey(messages)

    def generate_ai_questions_draft(
        self, messages: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        """会話履歴からアンケート設問ドラフトを生成して返す。

        Returns:
            {"summary": str, "questions": [Question.to_dict(), ...]}
        """
        if self.ai_service is None:
            raise SurveyManagerError("AIService が利用できません")
        self._validate_ai_messages(messages)

        raw = self.ai_service.generate_survey_questions_draft(messages)
        questions = [self._build_question_from_ai(q) for q in raw.get("questions", [])]
        if not questions:
            raise SurveyManagerError("AIが有効な設問を生成できませんでした")
        # 既存ルールに沿うかチェック（選択式は2つ以上の選択肢必須）
        self._validate_questions(questions)
        return {
            "summary": raw.get("summary", ""),
            "template_name": raw.get("template_name", ""),
            "questions": [q.to_dict() for q in questions],
        }

    @staticmethod
    def _build_question_from_ai(data: Dict[str, Any]) -> Question:
        """AI生成のJSON 1件を Question に変換する。不正値は安全側に丸める。"""
        qtype = str(data.get("question_type", "")).strip()
        text = str(data.get("text", "")).strip()
        if not text:
            raise SurveyValidationError("AI生成の設問に質問文がありません")
        if qtype == "multiple_choice":
            options = [
                str(o).strip() for o in data.get("options", []) if str(o).strip()
            ]
            allow_multiple = bool(data.get("allow_multiple", False))
            try:
                max_selections = int(data.get("max_selections", 0) or 0)
            except (TypeError, ValueError):
                max_selections = 0
            if max_selections < 0 or max_selections > len(options):
                max_selections = 0
            return Question.create_multiple_choice(
                text=text,
                options=options,
                allow_multiple=allow_multiple,
                max_selections=max_selections if allow_multiple else 0,
            )
        if qtype == "free_text":
            return Question.create_free_text(text=text)
        if qtype == "scale_rating":
            return Question.create_scale_rating(text=text)
        raise SurveyValidationError(f"AI生成の設問タイプが不正です: {qtype}")

    @staticmethod
    def _validate_persona_count(count: int, has_images: bool = False) -> None:
        """
        ペルソナ数のバリデーション。
        
        Args:
            count: ペルソナ数
            has_images: 画像が含まれる場合True
        
        Raises:
            SurveyValidationError: バリデーションエラー
        """
        if not isinstance(count, int) or count < 100:
            raise SurveyValidationError(
                "対象ペルソナ数は100以上で指定してください"
            )
        
        # 画像付きの場合は1000人まで
        if has_images and count > 1000:
            raise SurveyValidationError(
                "画像付きアンケートの場合、対象ペルソナ数は1000人までです"
            )
        
        # 画像なしの場合は10000人まで
        if not has_images and count > 10000:
            raise SurveyValidationError(
                "対象ペルソナ数は10000人までです"
            )

    # =========================================================================
    # アンケート実行
    # =========================================================================

    @staticmethod
    def generate_default_survey_name(template_name: str) -> str:
        """デフォルトのアンケート名を生成する（テンプレート名 + 日付）。"""
        date_str = datetime.now().strftime("%Y%m%d")
        return f"{template_name} {date_str}"

    def start_survey(
        self,
        template_id: str,
        name: Optional[str] = None,
        description: str = "",
        persona_count: int = 100,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Survey:
        """
        アンケートを開始する（互換性のため残す：作成+実行を一括で行う）。
        """
        survey = self.create_survey(
            template_id=template_id,
            name=name,
            description=description,
            persona_count=persona_count,
            filters=filters,
        )
        self.execute_survey(survey.id, filters)
        result = self.db.get_survey(survey.id)
        assert result is not None
        return result

    def create_survey(
        self,
        template_id: str,
        name: Optional[str] = None,
        description: str = "",
        persona_count: int = 100,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Survey:
        """
        アンケートレコードを作成する（実行はしない）。

        バリデーション・DB保存のみ行い、Surveyオブジェクトを返す。
        実際の実行は execute_survey() で行う。

        Args:
            template_id: テンプレートID
            name: アンケート名（Noneの場合はデフォルト名を生成）
            description: アンケートの説明
            persona_count: 対象ペルソナ数
            filters: ペルソナ属性フィルタ

        Returns:
            Survey: 作成されたアンケート（status=pending）

        Raises:
            SurveyValidationError: バリデーションエラー
            SurveyManagerError: テンプレートが見つからない場合
        """
        template = self.db.get_survey_template(template_id)
        if template is None:
            raise SurveyManagerError(f"テンプレートが見つかりません: {template_id}")

        # 画像の有無を確認してバリデーション
        has_images = bool(template.images)
        self._validate_persona_count(persona_count, has_images)

        if not name or not name.strip():
            name = self.generate_default_survey_name(template.name)

        survey = Survey.create_new(
            name=name,
            description=description,
            template_id=template_id,
            persona_count=persona_count,
            filters=filters,
        )
        self.db.save_survey(survey)
        logger.info(f"Survey created: {survey.id} ({survey.name})")
        return survey

    def execute_survey(
        self,
        survey_id: str,
        filters: Optional[Dict[str, Any]] = None,
        datasource: str = "nemotron",
    ) -> None:
        """
        アンケートを実行する（バックグラウンド実行用）。

        create_survey() で作成済みのアンケートに対して、
        ペルソナ取得・バッチ推論・結果保存を行う。

        Args:
            survey_id: アンケートID
            filters: ペルソナ属性フィルタ

        Raises:
            SurveyExecutionError: 実行エラー
        """
        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyManagerError(f"アンケートが見つかりません: {survey_id}")

        template = self.db.get_survey_template(survey.template_id)
        if template is None:
            raise SurveyManagerError(
                f"テンプレートが見つかりません: {survey.template_id}"
            )

        # ステータスを running に更新
        survey.status = "running"
        survey.updated_at = datetime.now()
        self.db.update_survey(survey)

        try:
            # ペルソナデータの取得・フィルタリング・サンプリング
            # DuckDB SQL内でフィルタ+サンプリングを1クエリで実行（メモリ効率改善）
            sampled = self.survey_service.filter_and_sample_personas(
                filters or {}, survey.persona_count, datasource=datasource
            )

            # プロンプト構築とバッチ推論
            prompts = self.survey_service.build_persona_prompts(sampled, template, datasource=datasource)
            results = self.survey_service.execute_batch_inference(prompts)

            # CSV結果をS3に保存
            s3_path = self.survey_service.save_results_to_s3(
                batch_results=results,
                personas_df=sampled,
                template=template,
                survey_id=survey.id,
            )

            # ステータスを completed に更新
            survey.status = "completed"
            survey.s3_result_path = s3_path
            survey.persona_count = len(sampled)
            survey.updated_at = datetime.now()
            self.db.update_survey(survey)
            logger.info(f"Survey completed: {survey.id}")

        except Exception as e:
            # エラー時はステータスを error に更新
            survey.status = "error"
            survey.error_message = str(e)
            survey.updated_at = datetime.now()
            self.db.update_survey(survey)
            logger.error(f"Survey execution failed: {survey.id} - {e}")
            raise SurveyExecutionError(
                f"アンケート実行中にエラーが発生しました: {e}"
            ) from e

    # =========================================================================
    # 結果取得・分析
    # =========================================================================

    def get_survey(self, survey_id: str) -> Optional[Survey]:
        """アンケートをIDで取得する。"""
        return self.db.get_survey(survey_id)

    def get_all_surveys(self) -> List[Survey]:
        """全アンケートをcreated_at降順で取得する。"""
        surveys = self.db.get_all_surveys()
        surveys.sort(key=lambda s: s.created_at, reverse=True)
        return surveys

    def delete_survey(self, survey_id: str) -> None:
        """
        アンケートを削除する。

        Args:
            survey_id: アンケートID

        Raises:
            SurveyManagerError: アンケートが見つからない場合
        """
        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyManagerError(f"Survey not found: {survey_id}")

        self.db.delete_survey(survey_id)
        logger.info(f"Survey deleted: {survey_id}")

    def get_download_url(self, survey_id: str, expiration: int = 300) -> str:
        """
        CSV結果ファイルの署名付きダウンロードURLを生成

        Args:
            survey_id: アンケートID
            expiration: URL有効期限（秒）デフォルト5分

        Returns:
            str: 署名付きダウンロードURL

        Raises:
            SurveyManagerError: アンケートが見つからない、または結果がない場合
        """
        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyManagerError(f"アンケートが見つかりません: {survey_id}")
        if not survey.s3_result_path:
            raise SurveyManagerError(
                f"アンケート結果がまだ生成されていません: {survey_id}"
            )

        s3_service = self.survey_service.s3_service
        return s3_service.generate_presigned_url(survey.s3_result_path, expiration)

    def download_results_csv(self, survey_id: str) -> bytes:
        """
        S3からCSVデータを取得しバイト列で返す。

        Args:
            survey_id: アンケートID

        Returns:
            bytes: CSVデータのバイト列

        Raises:
            SurveyManagerError: アンケートが見つからない、または結果がない場合
        """
        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyManagerError(f"アンケートが見つかりません: {survey_id}")
        if not survey.s3_result_path:
            raise SurveyManagerError(
                f"アンケート結果がまだ生成されていません: {survey_id}"
            )
        return self.survey_service.load_results_from_s3(survey.s3_result_path)

    def get_visual_analysis(self, survey_id: str) -> VisualAnalysisData:
        """
        CSVデータをパースし、選択式質問の回答分布とスケール評価の分布・平均値を計算する。

        Args:
            survey_id: アンケートID

        Returns:
            VisualAnalysisData: ビジュアル分析データ

        Raises:
            SurveyManagerError: アンケートが見つからない、または結果がない場合
        """
        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyManagerError(f"アンケートが見つかりません: {survey_id}")
        if not survey.s3_result_path:
            raise SurveyManagerError(
                f"アンケート結果がまだ生成されていません: {survey_id}"
            )

        template = self.db.get_survey_template(survey.template_id)
        if template is None:
            raise SurveyManagerError(
                f"テンプレートが見つかりません: {survey.template_id}"
            )

        csv_bytes = self.survey_service.load_results_from_s3(survey.s3_result_path)
        rows = SurveyService.parse_results_csv(csv_bytes)

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
                        pass
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
    def _get_age_bracket(age_str: str) -> str | None:
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
        """選択式質問の属性別クロス集計（性別・年齢層）を計算する。"""
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

    def generate_insight_report(self, survey_id: str) -> InsightReport:
        """
        SurveyServiceを呼び出してインサイトレポートを生成しDynamoDBに保存する。

        Args:
            survey_id: アンケートID

        Returns:
            InsightReport: 生成されたインサイトレポート

        Raises:
            SurveyManagerError: アンケートが見つからない、または結果がない場合
            SurveyExecutionError: レポート生成に失敗した場合
        """
        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyManagerError(f"アンケートが見つかりません: {survey_id}")
        if not survey.s3_result_path:
            raise SurveyManagerError(
                f"アンケート結果がまだ生成されていません: {survey_id}"
            )

        template = self.db.get_survey_template(survey.template_id)
        if template is None:
            raise SurveyManagerError(
                f"テンプレートが見つかりません: {survey.template_id}"
            )

        try:
            csv_bytes = self.survey_service.load_results_from_s3(survey.s3_result_path)
            csv_text = csv_bytes.decode("utf-8-sig")

            report = self.survey_service.generate_insights(csv_text, template)
            # survey_idを設定
            report = InsightReport(
                id=report.id,
                survey_id=survey_id,
                content=report.content,
                created_at=report.created_at,
            )

            # Surveyにレポートを紐付けて保存
            survey.insight_report = report
            survey.updated_at = datetime.now()
            self.db.update_survey(survey)

            logger.info(f"Insight report generated for survey: {survey_id}")
            return report

        except SurveyManagerError:
            raise
        except Exception as e:
            logger.error(f"Failed to generate insight report: {e}")
            raise SurveyExecutionError(
                f"レポート生成に失敗しました。再試行してください: {e}"
            ) from e

    def generate_insight_report_streaming(self, survey_id: str) -> Any:
        """
        インサイトレポートをストリーミング生成する。

        Args:
            survey_id: アンケートID

        Yields:
            str: テキストチャンク
        """
        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyManagerError(f"アンケートが見つかりません: {survey_id}")
        if not survey.s3_result_path:
            raise SurveyManagerError(
                f"アンケート結果がまだ生成されていません: {survey_id}"
            )

        template = self.db.get_survey_template(survey.template_id)
        if template is None:
            raise SurveyManagerError(
                f"テンプレートが見つかりません: {survey.template_id}"
            )

        csv_bytes = self.survey_service.load_results_from_s3(survey.s3_result_path)
        csv_text = csv_bytes.decode("utf-8-sig")

        yield from self.survey_service.generate_insights_streaming(csv_text, template)

    def save_insight_report(self, survey_id: str, content: str) -> InsightReport:
        """
        ストリーミング生成済みのレポートを保存する。

        Args:
            survey_id: アンケートID
            content: レポート内容（Markdown）

        Returns:
            InsightReport: 保存されたレポート
        """
        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyManagerError(f"アンケートが見つかりません: {survey_id}")

        report = InsightReport.create_new(survey_id=survey_id, content=content)
        survey.insight_report = report
        survey.updated_at = datetime.now()
        self.db.update_survey(survey)

        logger.info(f"Insight report saved for survey: {survey_id}")
        return report

    def get_persona_statistics(self, survey_id: str) -> PersonaStatistics:
        """
        CSVデータからペルソナの統計情報を集計する。

        Args:
            survey_id: アンケートID

        Returns:
            PersonaStatistics: ペルソナ統計データ

        Raises:
            SurveyManagerError: アンケートが見つからない、または結果がない場合
        """
        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyManagerError(f"アンケートが見つかりません: {survey_id}")
        if not survey.s3_result_path:
            raise SurveyManagerError(
                f"アンケート結果がまだ生成されていません: {survey_id}"
            )

        csv_bytes = self.survey_service.load_results_from_s3(survey.s3_result_path)
        rows = SurveyService.parse_results_csv(csv_bytes)

        return self._compute_persona_statistics(rows)

    def _compute_persona_statistics(
        self, rows: List[Dict[str, Any]]
    ) -> PersonaStatistics:
        """回答データからペルソナ統計を計算する。"""
        from collections import Counter

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

        # 年齢統計
        age_stats: Dict[str, Any] = {}
        if age_values:
            age_stats = {
                "min": min(age_values),
                "max": max(age_values),
                "average": round(sum(age_values) / len(age_values), 1),
            }

        # 年代順にソート
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
