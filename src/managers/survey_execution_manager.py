"""
SurveyExecutionManager
アンケート実行制御を担当するマネージャークラス。

v1.0.0 U7 (``functional-design/rules.md``):

- BR2.1: starting a survey checks, in order, ``ai:execute``, the template's
  ``ensure_readable`` and the persona data (Nemotron downloaded, or custom
  data the user may use); then its size (350KB). Nothing is written and no
  batch job is queued unless every check passes.
- BR1.1 / BR2.7: the runner owns the survey; it starts unshared and carries a
  snapshot of the template (name, questions, image references) and the
  custom data's name.
- BR2.2: the background run uses the snapshot and the datasource only; it
  does not decide again. Missing custom data (record or file) or an
  unreadable snapshot image stops it with an error code; a survey deleted
  meanwhile is not written back, and a CSV already put is removed.
- BR1.2 / BR1.3: lists are tabbed; detail, download and images need read.
- BR2.8: result images are only the snapshot's, signed after the read check.
- BR1.8: delete removes the body and its share items, then the CSV (three
  tries, then ``survey.cleanup_failed``) and the cached CSV.

Logs: ``survey.dependency_missing``, ``survey.execution_failed`` and
``survey.cleanup_failed`` (IDs and codes only, NFR3.13).
"""

import base64
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Protocol

from ..models.authz import Permission, Principal, ResourceType
from ..models.custom_persona_data import PersonaDataRef
from ..models.errors import CodedError, ErrorCode, LookupUnavailableError
from ..models.sharing import ListTab, TabbedList
from ..models.survey import Survey, SurveyDetail, TemplateSnapshot
from ..services.custom_persona_data_store import (
    CustomPersonaDataStore,
    CustomPersonaDataStoreError,
)
from ..services.database_service import DatabaseError, DatabaseService
from ..services.s3_service import S3Service
from ..services.service_factory import service_factory
from ..services.survey_batch_service import SurveyBatchService
from .components.access_policy import AccessPolicy
from .components.owner_directory import OwnerDirectory
from .components.resource_lookup import ResourceLookup
from .components.resource_sharing import SEARCH_LIMIT, ResourceSharing
from .components.sharing_runtime import access_parts_from, sharing_parts_from
from .shared.cleanup import run_cleanup
from .shared.result_csv_cache import shared_result_cache
from .shared.structured_log import log_event

logger = logging.getLogger(__name__)

_SURVEY = ResourceType.SURVEY
_TEMPLATE = ResourceType.SURVEY_TEMPLATE
#: Largest survey body written, snapshot and report included (NFR2.25).
MAX_SURVEY_BYTES = 350 * 1024
#: Owner label of a survey without owner (before v1.0.0).
OWNER_UNKNOWN = "所有者不明"
#: Most rows a search returns.
SEARCH_RESULT_LIMIT = 100


class SurveyExecutionManagerError(CodedError):
    """SurveyExecutionManager層の基底例外"""

    pass


class SurveyExecutionValidationError(SurveyExecutionManagerError):
    """バリデーションエラー"""

    pass


class SurveyExecutionError(SurveyExecutionManagerError):
    """アンケート実行エラー"""

    pass


class _SurveyGone(Exception):
    """The survey was deleted while it ran (not written back, BR2.2 (c))."""


class ResultCache(Protocol):
    """What deleting needs from ``ResultCsvCache`` (shared with the analysis)."""

    def evict(self, survey_id: str) -> None: ...


@dataclass(frozen=True)
class SurveyStart:
    """A survey that passed every start check (BR2.1).

    Attributes:
        survey: The saved survey (``pending``).
        run: Runs it (batch inference); the caller schedules it in the
            background. It records failures on the survey and never raises.
    """

    survey: Survey
    run: Callable[[], None]


class SurveyExecutionManager:
    """アンケート実行制御"""

    #: バッチ推論1ジョブあたりの最小レコード数（Bedrockのクォータ）。
    #: これを下回る入力でジョブを作ると Bedrock 側の検証で Failed になるため、
    #: 希望数だけでなく「実際に集まった件数」もこの値で検証する。
    MIN_BATCH_RECORDS = 100

    def __init__(
        self,
        database_service: Optional[DatabaseService] = None,
        survey_batch_service: Optional[SurveyBatchService] = None,
        s3_service: Optional[S3Service] = None,
        resource_sharing: Optional[ResourceSharing] = None,
        owners: Optional[OwnerDirectory] = None,
        resource_lookup: Optional[ResourceLookup] = None,
        custom_persona_data_store: Optional[CustomPersonaDataStore] = None,
        result_cache: Optional[ResultCache] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the manager.

        Args:
            database_service: Survey and template bodies.
            survey_batch_service: Persona sampling and batch inference.
            s3_service: Result CSVs, images and custom data files.
            resource_sharing: The per-task ``ResourceSharing`` (tabs, delete).
            owners: The owner-name copy (display only).
            resource_lookup: The per-task ``ResourceLookup`` (decisions).
            custom_persona_data_store: Custom persona data records.
            result_cache: The per-task cache of result CSVs (evicted on delete).
            sleep: Wait between cleanup attempts (tests).
        """
        self.db = database_service or service_factory.get_database_service()
        self.batch_service = (
            survey_batch_service or service_factory.get_survey_batch_service()
        )
        self.s3_service: S3Service = s3_service or service_factory.get_s3_service()
        self._sharing = resource_sharing
        self._owner_directory = owners
        self._lookup = resource_lookup
        self._custom_store = custom_persona_data_store
        self._cache = result_cache
        self._sleep = sleep

    # --- parts (built lazily) -------------------------------------------------------

    @property
    def _resource_sharing(self) -> ResourceSharing:
        if self._sharing is None:
            self._sharing = sharing_parts_from(service_factory).sharing
        return self._sharing

    @property
    def _owners(self) -> OwnerDirectory:
        if self._owner_directory is None:
            self._owner_directory = sharing_parts_from(service_factory).owners
        return self._owner_directory

    @property
    def _resource_lookup(self) -> ResourceLookup:
        if self._lookup is None:
            self._lookup = access_parts_from(service_factory).lookup
        return self._lookup

    @property
    def result_cache(self) -> ResultCache:
        """The per-process result CSV cache (shared with the analysis, BR1.8)."""
        if self._cache is None:
            self._cache = shared_result_cache()
        return self._cache

    @property
    def _custom_persona_data(self) -> CustomPersonaDataStore:
        if self._custom_store is None:
            self._custom_store = service_factory.get_custom_persona_data_store()
        return self._custom_store

    # =========================================================================
    # 開始（BR2.1、BR1.1、BR2.7）
    # =========================================================================

    def create_survey(
        self,
        principal: Principal,
        template_id: str,
        name: Optional[str] = None,
        description: str = "",
        persona_count: int = 100,
        filters: Optional[Dict[str, Any]] = None,
        datasource: Optional[str] = None,
    ) -> SurveyStart:
        """Check every start condition, then save a ``pending`` survey (BR2.1).

        Returns:
            The saved survey and the run the caller schedules.

        Raises:
            AccessDeniedError: The user lacks ``ai:execute``.
            CodedError: ``SURVEY_TEMPLATE_NOT_FOUND``,
                ``CUSTOM_PERSONA_DATA_NOT_FOUND``, ``LOOKUP_UNAVAILABLE``,
                ``SURVEY_DATASET_NOT_DOWNLOADED``, count validation codes,
                ``SURVEY_TEMPLATE_TOO_LARGE`` or ``SURVEY_OPERATION_FAILED``.
        """
        AccessPolicy.require(principal, Permission.AI_EXECUTE, resource_type=_SURVEY)
        template_target = self._resource_lookup.get(_TEMPLATE, template_id)
        AccessPolicy.ensure_readable(
            principal, template_target, ErrorCode.SURVEY_TEMPLATE_NOT_FOUND
        )
        ref, datasource_name = self._usable_datasource(principal, datasource)
        template = self._read_template(template_id)
        if template is None:
            raise SurveyExecutionManagerError(
                "survey template is gone", code=ErrorCode.SURVEY_TEMPLATE_NOT_FOUND
            )
        self._validate_persona_count(persona_count, bool(template.images))
        self._validate_available_personas(filters, ref.value)
        if not name or not name.strip():
            name = self.generate_default_survey_name(template.name)
        survey = Survey.create_new(
            name=name,
            description=description,
            template_id=template_id,
            persona_count=persona_count,
            filters=filters,
            datasource=ref.value,
            owner_sub=principal.sub,
            template_snapshot=TemplateSnapshot.from_template(template),
            datasource_name=datasource_name,
        )
        if self.db.item_size(survey.to_dict()) > MAX_SURVEY_BYTES:
            raise SurveyExecutionValidationError(
                "the survey body exceeds 350KB",
                code=ErrorCode.SURVEY_TEMPLATE_TOO_LARGE,
            )
        try:
            self.db.put_new_survey(survey)
        except DatabaseError as e:
            logger.error("Survey save failed: %s", type(e).__name__)
            raise SurveyExecutionError(
                "survey save failed", code=ErrorCode.SURVEY_OPERATION_FAILED
            ) from e
        logger.info("Survey created: %s", survey.id)
        return SurveyStart(survey=survey, run=lambda: self._execute_survey(survey.id))

    def _usable_datasource(
        self, principal: Principal, datasource: Optional[str]
    ) -> tuple[PersonaDataRef, Optional[str]]:
        """The persona data reference and the custom data's name (BR4.2)."""
        try:
            ref = PersonaDataRef.parse(datasource)
        except ValueError as e:
            raise SurveyExecutionValidationError(
                "datasource is not usable", code=ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND
            ) from e
        if ref.custom_id is None:
            return ref, None
        target = self._resource_lookup.get(
            ResourceType.CUSTOM_PERSONA_DATA, ref.custom_id
        )
        if not AccessPolicy.can_use_asset(principal, target):
            raise SurveyExecutionValidationError(
                "custom persona data is not usable",
                code=ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND,
            )
        return ref, target.display_name

    def _read_template(self, template_id: str) -> Any:
        try:
            return self.db.get_survey_template(template_id, consistent=True)
        except DatabaseError as e:
            raise LookupUnavailableError("template body unreadable") from e

    # =========================================================================
    # 裏の実行（BR2.2、NFR1.86）
    # =========================================================================

    def _execute_survey(self, survey_id: str) -> None:
        """Run one survey in the background; failures are stored as codes."""
        try:
            survey = self._read_survey(survey_id)
            if survey is None or not self._update(
                survey_id, {"status": "running", "updated_at": _now()}
            ):
                self._dependency_missing(survey_id, "survey")
                return
            self._run(survey)
        except _SurveyGone:
            self._dependency_missing(survey_id, "survey")
        except Exception as e:
            self._mark_survey_failed(survey_id, e)

    def _run(self, survey: Survey) -> None:
        ref = PersonaDataRef.parse(survey.datasource)
        extra_columns = self._extra_columns(survey.id, ref)
        if ref.is_nemotron:
            self._ensure_parquet_uri(ref.value)
        snapshot = survey.template_snapshot or self._legacy_snapshot(survey)
        image_contents = self._snapshot_images(survey.id, snapshot)
        sampled = self.batch_service.filter_and_sample_personas(
            survey.filters or {}, survey.persona_count, datasource=ref.value
        )
        # 作成から実行までの間にデータが差し替わることがあるため、ジョブを作る直前にも
        # 実際に集まった行数を確かめる（Bedrock 側で Failed になる前に止める）。
        if len(sampled) < self.MIN_BATCH_RECORDS:
            self._raise_too_few(len(sampled), survey.filters)
        prompts = self._build_prompts(sampled, snapshot, image_contents, extra_columns)
        results = self.batch_service.execute_batch_inference(prompts)
        if self._read_survey(survey.id) is None:
            raise _SurveyGone()
        s3_path = self._save_results_to_s3(results, sampled, snapshot, survey.id)
        completed = self._update(
            survey.id,
            {
                "status": "completed",
                "s3_result_path": s3_path,
                "persona_count": len(sampled),
                "updated_at": _now(),
            },
        )
        if not completed:
            self._cleanup(
                survey.id, "results", lambda: self.s3_service.delete_file(s3_path)
            )
            raise _SurveyGone()
        logger.info("Survey completed: %s", survey.id)

    def _extra_columns(
        self, survey_id: str, ref: PersonaDataRef
    ) -> Optional[List[Dict[str, str]]]:
        """The custom data's columns from its record, by ID (BR2.2 (a))."""
        if ref.custom_id is None:
            return None
        try:
            record = self._custom_persona_data.get(ref.custom_id)
            present = record is not None and self.s3_service.exists(
                self.s3_service.path_of(record.s3_key)
            )
        except CustomPersonaDataStoreError as e:
            raise SurveyExecutionError(
                "custom persona data unreadable",
                code=ErrorCode.SURVEY_EXECUTION_FAILED,
            ) from e
        if record is None or not present:
            self._dependency_missing(survey_id, "custom_persona_data")
            raise SurveyExecutionError(
                "custom persona data is gone",
                code=ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND,
            )
        return record.extra_column_dicts()

    def _legacy_snapshot(self, survey: Survey) -> TemplateSnapshot:
        """A survey saved without a snapshot reads its template (before v1.0.0)."""
        template = self._read_template(survey.template_id)
        if template is None:
            raise SurveyExecutionError(
                "survey template is gone", code=ErrorCode.SURVEY_TEMPLATE_NOT_FOUND
            )
        return TemplateSnapshot.from_template(template)

    def _snapshot_images(
        self, survey_id: str, snapshot: TemplateSnapshot
    ) -> List[Dict[str, Any]]:
        """The snapshot's images, compressed; any failure stops the run (BR2.2 (b))."""
        from .shared.file_utils import compress_image_for_batch

        contents: List[Dict[str, Any]] = []
        for image in snapshot.images:
            try:
                if image.file_path.startswith("s3://"):
                    raw = self.s3_service.download_file(image.file_path)
                else:
                    with open(image.file_path, "rb") as f:
                        raw = f.read()
                compressed, media_type = compress_image_for_batch(raw)
            except Exception as e:
                self._dependency_missing(survey_id, "image")
                raise SurveyExecutionError(
                    f"template image unreadable ({type(e).__name__})",
                    code=ErrorCode.SURVEY_TEMPLATE_IMAGE_UNAVAILABLE,
                ) from e
            contents.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(compressed).decode("utf-8"),
                    },
                }
            )
        return contents

    def _build_prompts(
        self,
        sampled: Any,
        snapshot: TemplateSnapshot,
        image_contents: List[Dict[str, Any]],
        extra_columns: Optional[List[Dict[str, str]]],
    ) -> Any:
        from ..prompts.survey_prompts import (
            SURVEY_QUESTION_FORMAT_TEMPLATE,
            build_persona_system_prompt,
        )

        questions_text = self._format_questions_for_prompt(list(snapshot.questions))
        system_prompts: List[str] = []
        user_messages: List[List[Dict[str, Any]]] = []
        for row in sampled.iter_rows(named=True):
            system_prompts.append(
                build_persona_system_prompt(row, extra_columns=extra_columns)
            )
            user_content: List[Dict[str, Any]] = []
            if image_contents:
                names = "\n".join(f"- {img.name}" for img in snapshot.images)
                user_content.append(
                    {
                        "type": "text",
                        "text": f"以下の画像が添付されています:\n{names}\n",
                    }
                )
                user_content.extend(image_contents)
            user_content.append(
                {
                    "type": "text",
                    "text": SURVEY_QUESTION_FORMAT_TEMPLATE.format(
                        questions_text=questions_text
                    ),
                }
            )
            user_messages.append(user_content)
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
        return self.batch_service.build_batch_prompts(
            sampled, system_prompts, user_messages, output_schema
        )

    def _mark_survey_failed(self, survey_id: str, exc: Exception) -> None:
        """Store the failure as an error code (never the exception text)."""
        code = getattr(exc, "code", None)
        if not isinstance(code, ErrorCode) or code is ErrorCode.UNKNOWN:
            code = ErrorCode.SURVEY_EXECUTION_FAILED
        context = getattr(exc, "context", None)
        fields: Dict[str, Any] = {
            "status": "error",
            "error_code": code.value,
            "updated_at": _now(),
        }
        remove: tuple[str, ...] = ()
        if isinstance(context, dict) and context:
            fields["error_context"] = dict(context)
        else:
            remove = ("error_context",)
        logger.error("Survey execution failed: %s", type(exc).__name__)
        log_event("survey.execution_failed", survey_id=survey_id, error_code=code.value)
        try:
            self.db.update_survey_fields(survey_id, fields, remove=remove)
        except DatabaseError:
            logger.error("Recording the survey failure failed", exc_info=True)

    def _dependency_missing(self, survey_id: str, missing: str) -> None:
        log_event(
            "survey.dependency_missing",
            level="WARNING",
            survey_id=survey_id,
            missing=missing,
        )

    def _read_survey(self, survey_id: str) -> Optional[Survey]:
        try:
            return self.db.get_survey(survey_id, consistent=True)
        except DatabaseError as e:
            raise LookupUnavailableError("survey body unreadable") from e

    def _update(self, survey_id: str, fields: Dict[str, Any]) -> bool:
        return self.db.update_survey_fields(survey_id, fields)

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
                        "nemotron parquet uri is not configured",
                        code=ErrorCode.SURVEY_DATASET_NOT_DOWNLOADED,
                    )

    def _save_results_to_s3(
        self,
        batch_results: List[Dict[str, Any]],
        personas_df: Any,
        template: Any,
        survey_id: str,
    ) -> str:
        """バッチ推論結果をCSV化してS3に保存する（キーはアンケートの ID、BR2.9）。"""

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
                for col in attribute_headers[1:]:
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
        return self.s3_service.upload_file(csv_bytes, s3_key)

    def _parse_batch_result_answers(
        self, result: Dict[str, Any], questions: List[Any]
    ) -> List[Dict[str, str]]:
        """バッチ推論結果から回答データをパース・バリデーションする。"""
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
            logger.warning("Failed to parse batch result: %s", type(e).__name__)
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

    # =========================================================================
    # 一覧（BR1.2）
    # =========================================================================

    def list_surveys(
        self,
        principal: Principal,
        tab: Optional[str],
        cursor: Optional[str],
        search: Optional[str] = None,
    ) -> TabbedList[Survey]:
        """One page of a tab, or a search inside the tab (BR1.2).

        Raises:
            CodedError: ``LOOKUP_UNAVAILABLE`` when the index or the bodies
                cannot be read.
        """
        sharing = self._resource_sharing
        active = sharing.effective_tab(principal, ListTab.parse(tab) or ListTab.MINE)
        query = (search or "").strip()
        if query:
            ids, truncated = self._tab_ids(principal, active)
            needle = query.lower()
            matched = [
                s
                for s in self._readable_bodies(principal, ids)
                if needle in s.name.lower()
            ][:SEARCH_RESULT_LIMIT]
            return self._listing(
                principal,
                active,
                matched,
                counts={active.value: None if truncated else len(ids)},
                search_truncated=truncated,
            )
        page = sharing.list_page(principal, _SURVEY, active, cursor)
        surveys = self._readable_bodies(principal, [t.resource_id for t in page.items])
        counts = self._counts(principal, {active: page.total}) if cursor is None else {}
        return self._listing(
            principal, active, surveys, next_cursor=page.next_cursor, counts=counts
        )

    def _readable_bodies(
        self, principal: Principal, survey_ids: List[str]
    ) -> List[Survey]:
        if not survey_ids:
            return []
        try:
            found = self.db.get_surveys_by_ids(survey_ids)
        except DatabaseError as e:
            raise LookupUnavailableError("survey bodies unreadable") from e
        bodies = [found[i] for i in dict.fromkeys(survey_ids) if i in found]
        return [
            s
            for s in bodies
            if self._resource_sharing.can_read_body(principal, _SURVEY, s.to_dict())
        ]

    def _tab_ids(self, principal: Principal, active: ListTab) -> tuple[List[str], bool]:
        sharing = self._resource_sharing
        if active is not ListTab.ALL:
            return sharing.collect_ids(principal, _SURVEY, active)
        ids: List[str] = []
        cursor: Optional[str] = None
        while len(ids) <= SEARCH_LIMIT:
            page = sharing.list_page(principal, _SURVEY, ListTab.ALL, cursor)
            ids += [t.resource_id for t in page.items]
            cursor = page.next_cursor
            if not cursor:
                break
        return ids[:SEARCH_LIMIT], len(ids) > SEARCH_LIMIT or bool(cursor)

    def _counts(
        self, principal: Principal, known: Dict[ListTab, Optional[int]]
    ) -> Dict[str, Optional[int]]:
        sharing = self._resource_sharing
        tabs = [ListTab.MINE, ListTab.SHARED]
        if sharing.effective_tab(principal, ListTab.ALL) is ListTab.ALL:
            tabs.append(ListTab.ALL)
        return {
            tab.value: known[tab]
            if known.get(tab) is not None
            else sharing.count(principal, _SURVEY, tab)
            for tab in tabs
        }

    def _listing(
        self,
        principal: Principal,
        active: ListTab,
        surveys: List[Survey],
        *,
        next_cursor: Optional[str] = None,
        counts: Dict[str, Optional[int]],
        search_truncated: bool = False,
    ) -> TabbedList[Survey]:
        sharing = self._resource_sharing
        owners: Dict[str, Optional[str]] = {}
        if active is not ListTab.MINE and surveys:
            labels = self._owners.labels({s.owner_sub for s in surveys if s.owner_sub})
            owners = {
                s.id: None
                if labels is None
                else (
                    labels.get(s.owner_sub, OWNER_UNKNOWN)
                    if s.owner_sub
                    else OWNER_UNKNOWN
                )
                for s in surveys
            }
        shared_with = {
            s.id: sharing.department_names(
                sorted(
                    s.shared_group_ids & principal.group_ids
                    if active is ListTab.SHARED
                    else s.shared_group_ids
                )
            )
            for s in surveys
        }
        return TabbedList(
            tab=active,
            items=surveys,
            next_cursor=next_cursor,
            counts=counts,
            show_all=sharing.effective_tab(principal, ListTab.ALL) is ListTab.ALL,
            owners=owners,
            shared_with=shared_with,
            manageable=frozenset(
                s.id
                for s in surveys
                if sharing.can_manage_body(principal, _SURVEY, s.to_dict())
            ),
            no_department=not principal.group_ids,
            search_truncated=search_truncated,
        )

    # =========================================================================
    # 読む（BR1.3、BR2.6〜BR2.10）
    # =========================================================================

    def get_survey(self, principal: Principal, survey_id: str) -> Survey:
        """A survey the user may read (BR1.3).

        Raises:
            CodedError: ``SURVEY_NOT_FOUND`` (unreadable or missing).
        """
        return self._readable_survey(principal, survey_id)

    def _readable_survey(self, principal: Principal, survey_id: str) -> Survey:
        target = self._resource_lookup.get(_SURVEY, survey_id)
        AccessPolicy.ensure_readable(principal, target, ErrorCode.SURVEY_NOT_FOUND)
        survey = self._read_survey(survey_id)
        if survey is None:
            raise SurveyExecutionManagerError(
                "survey is gone", code=ErrorCode.SURVEY_NOT_FOUND
            )
        return survey

    def get_survey_detail(self, principal: Principal, survey_id: str) -> SurveyDetail:
        """The survey with what the reader may do and see (BR2.7, BR2.10, BR2.11).

        Raises:
            CodedError: ``SURVEY_NOT_FOUND``.
        """
        survey = self._readable_survey(principal, survey_id)
        manageable = self._resource_sharing.can_manage_body(
            principal, _SURVEY, survey.to_dict()
        )
        if survey.template_snapshot is not None:
            return SurveyDetail(
                survey=survey,
                manageable=manageable,
                template_name=survey.template_snapshot.name,
                analysis_available=True,
            )
        template_target = self._resource_lookup.get(_TEMPLATE, survey.template_id)
        template = (
            self._read_template(survey.template_id)
            if AccessPolicy.can_read(principal, template_target)
            else None
        )
        return SurveyDetail(
            survey=survey,
            manageable=manageable,
            template_name=template.name if template is not None else None,
            analysis_available=template is not None,
        )

    def get_download_url(
        self, principal: Principal, survey_id: str, expiration: int = 300
    ) -> str:
        """A signed URL of the result CSV, after the read check (BR2.6).

        Raises:
            CodedError: ``SURVEY_NOT_FOUND`` / ``SURVEY_RESULT_NOT_READY``.
        """
        survey = self._readable_survey(principal, survey_id)
        return self.s3_service.generate_presigned_url(
            self._result_path(survey), expiration
        )

    def download_results_csv(self, principal: Principal, survey_id: str) -> bytes:
        """The result CSV, after the read check (BR2.6)."""
        survey = self._readable_survey(principal, survey_id)
        return self.s3_service.download_file(self._result_path(survey))

    @staticmethod
    def _result_path(survey: Survey) -> str:
        if survey.status != "completed" or not survey.s3_result_path:
            raise SurveyExecutionManagerError(
                "survey has no result yet", code=ErrorCode.SURVEY_RESULT_NOT_READY
            )
        return survey.s3_result_path

    def get_result_image_url(
        self, principal: Principal, survey_id: str, image_id: str
    ) -> str:
        """A signed URL of a snapshot image, after the read check (BR2.8).

        Raises:
            CodedError: ``SURVEY_NOT_FOUND`` (unreadable survey) or
                ``FILE_NOT_FOUND`` (not in the snapshot, or deleted).
        """
        survey = self._readable_survey(principal, survey_id)
        snapshot = survey.template_snapshot
        image = snapshot.image(image_id) if snapshot is not None else None
        if image is None or not image.file_path.startswith("s3://"):
            raise SurveyExecutionManagerError(
                "image is not in the snapshot", code=ErrorCode.FILE_NOT_FOUND
            )
        if not self.s3_service.exists(image.file_path):
            raise SurveyExecutionManagerError(
                "image file is gone", code=ErrorCode.FILE_NOT_FOUND
            )
        return self.s3_service.generate_presigned_url(image.file_path)

    # =========================================================================
    # 削除（BR1.8）
    # =========================================================================

    def delete_survey(self, principal: Principal, survey_id: str) -> None:
        """Delete the body and its shares, then the CSV and the cache (BR1.8).

        Raises:
            CodedError: ``SURVEY_NOT_FOUND`` / ``SURVEY_OPERATION_FAILED``.
            AccessDeniedError: Readable but not manageable.
        """
        deleted = self._resource_sharing.delete_with_shares(
            principal, _SURVEY, survey_id
        )
        if deleted is None:
            raise SurveyExecutionManagerError(
                "nothing to delete", code=ErrorCode.SURVEY_NOT_FOUND
            )
        result_path = deleted.get("s3_result_path")
        if result_path:
            self._cleanup(
                survey_id,
                "results",
                lambda: self.s3_service.delete_file(str(result_path)),
            )
        self.result_cache.evict(survey_id)
        logger.info("Survey deleted: %s", survey_id)

    def _cleanup(self, survey_id: str, step: str, action: Callable[[], object]) -> None:
        run_cleanup(
            action,
            resource_type=_SURVEY.value,
            resource_id=survey_id,
            step=step,
            sleep=self._sleep,
        )

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
                            continue  # 数値変換できない値はフィルタから除外
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
                f"target count {count} below minimum 100",
                code=ErrorCode.SURVEY_TARGET_COUNT_TOO_LOW,
                context={"min_count": 100},
            )
        if has_images and count > 1000:
            raise SurveyExecutionValidationError(
                f"target count {count} exceeds 1000 for image survey",
                code=ErrorCode.SURVEY_TARGET_COUNT_TOO_HIGH_WITH_IMAGES,
                context={"max_count": 1000},
            )
        if not has_images and count > 10000:
            raise SurveyExecutionValidationError(
                f"target count {count} exceeds 10000",
                code=ErrorCode.SURVEY_TARGET_COUNT_TOO_HIGH,
                context={"max_count": 10000},
            )

    def _validate_available_personas(
        self, filters: Optional[Dict[str, Any]], datasource: Optional[str]
    ) -> None:
        """フィルタ適用後に実際に集められるペルソナ数を検証する。

        ``persona_count`` は「希望数」でしかなく、サンプリングSQLの ``LIMIT`` は
        上限にすぎない。条件に合致する行が少なければそれだけしか集まらず、
        バッチ推論の最小レコード数を下回った入力で Bedrock に投げてしまう。
        ここで実件数を確かめ、ジョブを作る前（課金前）に弾く。
        """
        source = datasource or "nemotron"
        self._ensure_parquet_uri(source)
        try:
            available = self.batch_service.get_filtered_count(filters, source)
        except SurveyExecutionManagerError:
            # _ensure_parquet_uri 由来のコード付き例外はそのまま通す。
            raise
        except Exception as e:
            # Service層の例外・生の例外（DuckDB/S3障害）をここで自ドメインへ変換
            # する。素通りさせると Router の except を抜けて未処理500になり、
            # 例外文に載った S3 パスがそのまま応答に出る。
            logger.error("Failed to count available personas (%s)", type(e).__name__)
            raise SurveyExecutionError(
                f"available persona count failed ({type(e).__name__})",
                code=ErrorCode.SURVEY_OPERATION_FAILED,
            ) from e

        if available < self.MIN_BATCH_RECORDS:
            self._raise_too_few(available, filters)

    def _raise_too_few(self, available: int, filters: Optional[Dict[str, Any]]) -> None:
        """件数不足を、フィルタの有無に応じた案内で送出する。

        フィルタありなら「条件を緩める」で解決しうるが、フィルタなしで足りない
        のはデータセット自体が小さいということ。「条件を緩めて」と案内しても
        緩めるフィルタが無く、利用者が次に取れる行動にならない（Issue #118）。
        """
        if filters:
            raise SurveyExecutionValidationError(
                f"only {available} personas match the filters "
                f"(minimum {self.MIN_BATCH_RECORDS})",
                code=ErrorCode.SURVEY_AVAILABLE_PERSONAS_TOO_FEW,
                context={
                    "available_count": available,
                    "min_count": self.MIN_BATCH_RECORDS,
                },
            )
        raise SurveyExecutionValidationError(
            f"datasource has only {available} personas "
            f"(minimum {self.MIN_BATCH_RECORDS})",
            code=ErrorCode.SURVEY_DATASET_TOO_FEW_ROWS,
            context={
                "row_count": available,
                "min_rows": self.MIN_BATCH_RECORDS,
            },
        )

    @staticmethod
    def generate_default_survey_name(template_name: str) -> str:
        """デフォルトのアンケート名を生成する（テンプレート名 + 日付）。"""
        date_str = datetime.now().strftime("%Y%m%d")
        return f"{template_name} {date_str}"


def _now() -> str:
    return datetime.now().isoformat()
