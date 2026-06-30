"""
SurveyExecutionManager
アンケート実行制御を担当するマネージャークラス。
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..models.survey import Survey
from ..services.database_service import DatabaseService
from ..services.s3_service import S3Service
from ..services.service_factory import service_factory
from ..services.survey_batch_service import SurveyBatchService

logger = logging.getLogger(__name__)


class SurveyExecutionManagerError(Exception):
    """SurveyExecutionManager層の基底例外"""

    pass


class SurveyExecutionValidationError(SurveyExecutionManagerError):
    """バリデーションエラー"""

    pass


class SurveyExecutionError(SurveyExecutionManagerError):
    """アンケート実行エラー"""

    pass


class SurveyExecutionManager:
    """アンケート実行制御"""

    def __init__(
        self,
        database_service: Optional[DatabaseService] = None,
        survey_batch_service: Optional[SurveyBatchService] = None,
        s3_service: Optional[S3Service] = None,
    ) -> None:
        self.db = database_service or service_factory.get_database_service()
        self.batch_service = (
            survey_batch_service or service_factory.get_survey_batch_service()
        )
        self.s3_service: S3Service = s3_service or service_factory.get_s3_service()

    # =========================================================================
    # アンケートCRUD
    # =========================================================================

    def create_survey(
        self,
        template_id: str,
        name: Optional[str] = None,
        description: str = "",
        persona_count: int = 100,
        filters: Optional[Dict[str, Any]] = None,
        datasource: Optional[str] = None,
    ) -> Survey:
        """アンケートレコードを作成する（実行はしない）。"""
        template = self.db.get_survey_template(template_id)
        if template is None:
            raise SurveyExecutionManagerError(
                f"テンプレートが見つかりません: {template_id}"
            )

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
            datasource=datasource,
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
        """アンケートを実行する（バッチ推論）。"""
        import base64
        import json

        from ..prompts.survey_prompts import (
            SURVEY_QUESTION_FORMAT_TEMPLATE,
            build_persona_system_prompt,
        )
        from ..services.survey_batch_service import CUSTOM_DATASET_PREFIX
        from .shared.file_utils import compress_image_for_batch

        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyExecutionManagerError(
                f"アンケートが見つかりません: {survey_id}"
            )

        template = self.db.get_survey_template(survey.template_id)
        if template is None:
            raise SurveyExecutionManagerError(
                f"テンプレートが見つかりません: {survey.template_id}"
            )

        survey.status = "running"
        survey.updated_at = datetime.now()
        self.db.update_survey(survey)

        try:
            # ペルソナ取得・フィルタリング・サンプリング
            self._ensure_parquet_uri(datasource)
            sampled = self.batch_service.filter_and_sample_personas(
                filters or {}, survey.persona_count, datasource=datasource
            )

            # extra_columns取得（カスタムデータセットの場合）
            extra_columns = None
            if datasource.startswith("custom:"):
                name = datasource.split(":", 1)[1]
                meta_key = f"{CUSTOM_DATASET_PREFIX}{name}.meta.json"
                try:
                    bucket = self.s3_service.bucket_name
                    raw = self.s3_service.download_file(f"s3://{bucket}/{meta_key}")
                    metadata = json.loads(raw.decode("utf-8"))
                    extra_columns = metadata.get("extra_columns")
                except Exception:
                    pass

            # 質問テキスト構築
            questions_text = self._format_questions_for_prompt(template.questions)

            # 画像の事前圧縮・base64化
            shared_image_contents: List[Dict[str, Any]] = []
            if template.images:
                for img in template.images:
                    try:
                        if img.file_path.startswith("s3://"):
                            raw_bytes = self.s3_service.download_file(img.file_path)
                        else:
                            with open(img.file_path, "rb") as f:
                                raw_bytes = f.read()
                        compressed, media_type = compress_image_for_batch(raw_bytes)
                        b64 = base64.b64encode(compressed).decode("utf-8")
                        shared_image_contents.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64,
                                },
                            }
                        )
                    except Exception as e:
                        logger.warning(f"Failed to load image {img.name}: {e}")

            # 各ペルソナのシステムプロンプト + ユーザーメッセージ構築
            system_prompts: List[str] = []
            user_messages: List[List[Dict[str, Any]]] = []

            for row in sampled.iter_rows(named=True):
                system_prompts.append(
                    build_persona_system_prompt(row, extra_columns=extra_columns)
                )
                user_content: List[Dict[str, Any]] = []
                if shared_image_contents:
                    image_desc_parts = [f"- {img.name}" for img in template.images]
                    user_content.append(
                        {
                            "type": "text",
                            "text": "以下の画像が添付されています:\n"
                            + "\n".join(image_desc_parts)
                            + "\n",
                        }
                    )
                    user_content.extend(shared_image_contents)
                user_message = SURVEY_QUESTION_FORMAT_TEMPLATE.format(
                    questions_text=questions_text
                )
                user_content.append({"type": "text", "text": user_message})
                user_messages.append(user_content)

            # Structured Output スキーマ
            output_schema = {
                "type": "object",
                "properties": {
                    "answers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question_id": {"type": "string"},
                                "answer": {"type": "string"},
                            },
                            "required": ["question_id", "answer"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["answers"],
                "additionalProperties": False,
            }

            # バッチ推論
            prompts = self.batch_service.build_batch_prompts(
                sampled, system_prompts, user_messages, output_schema
            )
            results = self.batch_service.execute_batch_inference(prompts)

            # 結果パース + CSV保存
            s3_path = self._save_results_to_s3(results, sampled, template, survey.id)

            survey.status = "completed"
            survey.s3_result_path = s3_path
            survey.persona_count = len(sampled)
            survey.updated_at = datetime.now()
            self.db.update_survey(survey)
            logger.info(f"Survey completed: {survey.id}")

        except Exception as e:
            survey.status = "error"
            survey.error_message = str(e)
            survey.updated_at = datetime.now()
            self.db.update_survey(survey)
            logger.error(f"Survey execution failed: {survey.id} - {e}")
            raise SurveyExecutionError(
                f"アンケート実行中にエラーが発生しました: {e}"
            ) from e

    def _ensure_parquet_uri(self, datasource: str) -> None:
        """nemotron使用時、batch_serviceにS3 URIをセットする。"""
        if not datasource.startswith("custom:"):
            if not self.batch_service.has_parquet_uri():
                from ..services.survey_batch_service import PARQUET_S3_KEY

                bucket = self.s3_service.bucket_name
                try:
                    self.s3_service.s3_client.head_object(
                        Bucket=bucket, Key=PARQUET_S3_KEY
                    )
                    self.batch_service.set_parquet_s3_uri(
                        f"s3://{bucket}/{PARQUET_S3_KEY}"
                    )
                except Exception:
                    raise SurveyExecutionError(
                        "Nemotronデータセットがまだダウンロードされていません。"
                    )

    def _save_results_to_s3(
        self,
        batch_results: List[Dict[str, Any]],
        personas_df: Any,
        template: Any,
        survey_id: str,
    ) -> str:
        """バッチ推論結果をCSV化してS3に保存する。"""

        from .shared.file_utils import build_results_csv_bytes

        attribute_headers = [
            "persona_id",
            "sex",
            "age",
            "occupation",
            "country",
            "region",
            "prefecture",
            "marital_status",
        ]
        question_headers: List[str] = []
        for q in template.questions:
            question_headers.extend([f"{q.id}_text", f"{q.id}_answer"])

        all_headers = attribute_headers + question_headers

        persona_index: Dict[str, Dict[str, Any]] = {}
        for row in personas_df.iter_rows(named=True):
            rid = str(row.get("uuid", ""))
            persona_index[rid] = row

        rows: List[List[str]] = []
        for result in batch_results:
            record_id = result.get("recordId", "")
            persona_row = persona_index.get(record_id)

            row_data: List[str] = []
            if persona_row is not None:
                row_data.append(record_id)
                for col in [
                    "sex",
                    "age",
                    "occupation",
                    "country",
                    "region",
                    "prefecture",
                    "marital_status",
                ]:
                    val = persona_row.get(col, "")
                    row_data.append(str(val) if val is not None else "")
            else:
                row_data = [record_id] + [""] * 7

            answers = self._parse_batch_result_answers(result, template.questions)
            answer_map = {a["question_id"]: a["answer"] for a in answers}

            for q in template.questions:
                row_data.append(q.text)
                row_data.append(str(answer_map.get(q.id, "")))

            rows.append(row_data)

        csv_bytes = build_results_csv_bytes(all_headers, rows)

        s3_key = f"survey-results/{survey_id}/results.csv"
        s3_path = self.s3_service.upload_file(csv_bytes, s3_key)

        logger.info(f"Survey results saved to {s3_path}")
        return s3_path

    def _parse_batch_result_answers(
        self, result: Dict[str, Any], questions: List[Any]
    ) -> List[Dict[str, str]]:
        """バッチ推論結果から回答データをパース・バリデーションする。"""
        import json

        try:
            model_output = result.get("modelOutput", {})
            if isinstance(model_output, dict):
                content = model_output.get("content", [])
                if isinstance(content, list) and len(content) > 0:
                    text = content[0].get("text", "")
                else:
                    text = str(model_output)
            else:
                text = str(model_output)

            data = json.loads(text)
            if "answers" not in data or not isinstance(data["answers"], list):
                return []

            question_map = {q.id: q for q in questions}
            validated_answers = []

            for answer_obj in data["answers"]:
                question_id = answer_obj.get("question_id", "")
                answer = answer_obj.get("answer", "")

                question = question_map.get(question_id)
                if not question:
                    validated_answers.append(
                        {"question_id": question_id, "answer": answer}
                    )
                    continue

                validated_answers.append(
                    {
                        "question_id": question_id,
                        "answer": self._validate_answer(answer, question),
                    }
                )

            return validated_answers
        except Exception as e:
            logger.warning(f"Failed to parse batch result: {e}")
            return []

    @staticmethod
    def _validate_answer(answer: str, question: Any) -> str:
        """回答バリデーション。無効な場合は空文字列を返す。"""
        if not answer or not answer.strip():
            return ""
        answer = answer.strip()

        if question.question_type == "multiple_choice":
            if question.allow_multiple:
                selected = [s.strip() for s in answer.split("|") if s.strip()]
                valid_selected = [s for s in selected if s in question.options]
                if not valid_selected:
                    return ""
                if (
                    question.max_selections > 0
                    and len(valid_selected) > question.max_selections
                ):
                    valid_selected = valid_selected[: question.max_selections]
                return "|".join(valid_selected)
            else:
                return answer if answer in question.options else ""

        elif question.question_type == "scale_rating":
            try:
                value = int(answer)
                if question.scale_min <= value <= question.scale_max:
                    return str(value)
                return ""
            except ValueError:
                return ""

        return answer

    @staticmethod
    def _format_questions_for_prompt(questions: List[Any]) -> str:
        """質問リストをプロンプト用テキストに変換する。"""
        lines: List[str] = []
        for i, q in enumerate(questions, 1):
            lines.append(f"質問{i} (ID: {q.id}): {q.text}")
            if q.question_type == "multiple_choice":
                if q.allow_multiple:
                    max_note = (
                        f"（最大{q.max_selections}個まで）"
                        if q.max_selections > 0
                        else ""
                    )
                    lines.append(f"  タイプ: 選択式・複数回答{max_note}")
                    lines.append("  【必ず以下の選択肢から選んでください】")
                else:
                    lines.append("  タイプ: 選択式・単一回答")
                    lines.append("  【必ず以下の選択肢から1つ選んでください】")
                for j, opt in enumerate(q.options, 1):
                    lines.append(f"  {j}. {opt}")
                if q.allow_multiple:
                    lines.append(
                        f"  【回答例】{q.options[0]}|{q.options[1] if len(q.options) > 1 else q.options[0]}"
                    )
                    lines.append(
                        "  【注意】選択肢の文言をそのまま使用し、説明や理由は含めないでください"
                    )
                else:
                    lines.append(f"  【回答例】{q.options[0]}")
                    lines.append(
                        "  【注意】選択肢の文言をそのまま使用し、説明や理由は含めないでください"
                    )
            elif q.question_type == "free_text":
                lines.append(
                    "  タイプ: 自由記述（あなた自身の経験・価値観・こだわりを具体的に盛り込んで200文字以内で回答してください。一般的・抽象的な回答は避けてください）"
                )
            elif q.question_type == "scale_rating":
                lines.append(
                    f"  タイプ: スケール評価（{q.scale_min}〜{q.scale_max}の整数で回答してください）"
                )
            lines.append("")
        return "\n".join(lines)

    def start_survey(
        self,
        template_id: str,
        name: Optional[str] = None,
        description: str = "",
        persona_count: int = 100,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Survey:
        """アンケートを作成+実行する（互換用）。"""
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

    def get_survey(self, survey_id: str) -> Optional[Survey]:
        """アンケートをIDで取得する。"""
        return self.db.get_survey(survey_id)

    def get_all_surveys(self) -> List[Survey]:
        """全アンケートをcreated_at降順で取得する。"""
        surveys = self.db.get_all_surveys()
        surveys.sort(key=lambda s: s.created_at, reverse=True)
        return surveys

    def delete_survey(self, survey_id: str) -> None:
        """アンケートを削除する。"""
        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyExecutionManagerError(f"Survey not found: {survey_id}")
        self.db.delete_survey(survey_id)
        logger.info(f"Survey deleted: {survey_id}")

    def get_download_url(self, survey_id: str, expiration: int = 300) -> str:
        """CSV結果ファイルの署名付きダウンロードURLを生成する。"""
        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyExecutionManagerError(
                f"アンケートが見つかりません: {survey_id}"
            )
        if not survey.s3_result_path:
            raise SurveyExecutionManagerError(
                f"アンケート結果がまだ生成されていません: {survey_id}"
            )
        return self.s3_service.generate_presigned_url(survey.s3_result_path, expiration)

    def download_results_csv(self, survey_id: str) -> bytes:
        """S3からCSVデータを取得しバイト列で返す。"""
        survey = self.db.get_survey(survey_id)
        if survey is None:
            raise SurveyExecutionManagerError(
                f"アンケートが見つかりません: {survey_id}"
            )
        if not survey.s3_result_path:
            raise SurveyExecutionManagerError(
                f"アンケート結果がまだ生成されていません: {survey_id}"
            )
        return self.s3_service.download_file(survey.s3_result_path)

    # =========================================================================
    # フィルタ正規化（Router層から移動）
    # =========================================================================

    @staticmethod
    def normalize_filters(raw: dict) -> Optional[dict]:
        """フィルタJSONから空値を除去して返す。空なら None。"""
        cleaned: Dict[str, Any] = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                range_cleaned: Dict[str, int] = {}
                for kk, vv in v.items():
                    if vv is not None and vv != "":
                        try:
                            range_cleaned[kk] = int(float(vv))
                        except (ValueError, TypeError):
                            pass
                if range_cleaned:
                    cleaned[k] = range_cleaned
            elif isinstance(v, list):
                if len(v) > 0:
                    cleaned[k] = v
            elif isinstance(v, str) and v:
                cleaned[k] = v
        return cleaned or None

    # =========================================================================
    # バリデーション
    # =========================================================================

    @staticmethod
    def _validate_persona_count(count: int, has_images: bool = False) -> None:
        """ペルソナ数のバリデーション。"""
        if not isinstance(count, int) or count < 100:
            raise SurveyExecutionValidationError(
                "対象ペルソナ数は100以上で指定してください"
            )
        if has_images and count > 1000:
            raise SurveyExecutionValidationError(
                "画像付きアンケートの場合、対象ペルソナ数は1000人までです"
            )
        if not has_images and count > 10000:
            raise SurveyExecutionValidationError("対象ペルソナ数は10000人までです")

    @staticmethod
    def generate_default_survey_name(template_name: str) -> str:
        """デフォルトのアンケート名を生成する（テンプレート名 + 日付）。"""
        date_str = datetime.now().strftime("%Y%m%d")
        return f"{template_name} {date_str}"
