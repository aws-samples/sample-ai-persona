"""
SurveyDatasetManager
データセット管理 + DWH連携 + フィルタ操作を担当するマネージャークラス。

v1.0.0 U7 (``functional-design/rules.md``):

- BR4.1: managing persona data (Nemotron download, custom import, delete,
  rename, usage scope, DWH extraction, the temporary files of the import)
  needs ``settings:manage``; others get ``CUSTOM_PERSONA_DATA_NOT_FOUND`` (the
  same 404 as a missing page). Reading is for everyone.
- BR4.2 / BR5.3: custom data is listed, shown and used only when
  ``can_use_asset`` holds (its scope meets the user's departments, or
  ``settings:manage``); otherwise it reads as missing. Nemotron needs no scope.
- BR4.3〜BR4.5: an import gets a new ID; the file goes to
  ``persona-dataset/custom/{id}.parquet`` first, then the record and its name
  reservation in one transaction (a failure removes the file). No
  ``meta.json`` is written. A DWH import finds a free name (`` (2)``…).
- BR4.6: delete removes the record and its reservation, then the file (three
  tries, then ``survey.cleanup_failed``).
- BR5.2: the scope changes with ``ADD`` / ``DELETE``; only added departments
  must exist (deleted groups can be removed). ``asset.scope_changed`` is logged.
"""

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from ..models.authz import (
    AccessTarget,
    Department,
    Permission,
    Principal,
    ResourceType,
)
from ..models.custom_persona_data import (
    CustomPersonaData,
    ExtraColumn,
    PersonaDataListing,
    PersonaDataRef,
    normalize_name,
)
from ..models.errors import CodedError, ErrorCode, LookupUnavailableError
from ..models.sharing import AssetScopeChange, AssetScopeDialog
from ..prompts.survey_prompts import (
    DWH_SEGMENT_SYSTEM_PROMPT,
    STANDARD_COLUMNS,
    build_column_mapping_prompt,
    build_dataset_name_prompt,
    build_persona_system_prompt,
)
from ..services.ai_service import AIService
from ..services.agent_service import AgentService
from ..services.custom_persona_data_store import (
    CustomPersonaDataConditionFailed,
    CustomPersonaDataNameTaken,
    CustomPersonaDataStore,
    CustomPersonaDataStoreError,
)
from ..services.s3_service import S3Service
from ..services.service_factory import service_factory
from ..services.survey_batch_service import PARQUET_S3_KEY, SurveyBatchService
from .components.access_policy import AccessPolicy
from .components.department_directory import DepartmentDirectory
from .components.resource_lookup import ResourceLookup
from .components.sharing_runtime import access_parts_from, sharing_parts_from
from .shared.asset_scope import scope_dialog, scope_labels
from .shared.cleanup import run_cleanup
from .shared.file_utils import (
    parse_csv_columns_with_samples,
    parse_csv_first_row_with_mapping,
)
from .shared.structured_log import log_event

logger = logging.getLogger(__name__)

_CUSTOM = ResourceType.CUSTOM_PERSONA_DATA
#: Most names tried when a DWH import's name is taken (BR4.4).
MAX_NAME_ATTEMPTS = 10


class SurveyDatasetManagerError(CodedError):
    """SurveyDatasetManager層の基底例外"""

    pass


class SurveyDatasetValidationError(SurveyDatasetManagerError):
    """バリデーションエラー"""

    pass


def _not_found(message: str) -> SurveyDatasetManagerError:
    return SurveyDatasetManagerError(
        message, code=ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND
    )


class SurveyDatasetManager:
    """データセット管理 + DWH連携 + フィルタ操作"""

    MIN_SEGMENT_ROWS = 100
    MAX_SEGMENT_ROWS = 10000

    def __init__(
        self,
        survey_batch_service: Optional[SurveyBatchService] = None,
        agent_service: Optional[AgentService] = None,
        ai_service: Optional[AIService] = None,
        s3_service: Optional[S3Service] = None,
        custom_persona_data_store: Optional[CustomPersonaDataStore] = None,
        resource_lookup: Optional[ResourceLookup] = None,
        departments: Optional[DepartmentDirectory] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the manager.

        Args:
            survey_batch_service: Parquet conversion, queries, Nemotron download.
            agent_service: Column mapping and DWH extraction agents.
            ai_service: Dataset name generation.
            s3_service: Persona data files and the import's temporary files.
            custom_persona_data_store: Custom persona data records.
            resource_lookup: The per-task ``ResourceLookup`` (decisions).
            departments: The per-task department copy (scope choices, C4).
            sleep: Wait between cleanup attempts (tests).
        """
        self.batch_service = (
            survey_batch_service or service_factory.get_survey_batch_service()
        )
        self.agent_service = agent_service or service_factory.get_agent_service()
        self.ai_service = ai_service or service_factory.get_ai_service()
        self.s3_service: S3Service = s3_service or service_factory.get_s3_service()
        self._store = custom_persona_data_store
        self._lookup = resource_lookup
        self._departments = departments
        self._sleep = sleep

    # --- parts (built lazily) -------------------------------------------------------

    @property
    def _records(self) -> CustomPersonaDataStore:
        if self._store is None:
            self._store = service_factory.get_custom_persona_data_store()
        return self._store

    @property
    def _resource_lookup(self) -> ResourceLookup:
        if self._lookup is None:
            self._lookup = access_parts_from(service_factory).lookup
        return self._lookup

    @property
    def _department_directory(self) -> DepartmentDirectory:
        if self._departments is None:
            self._departments = sharing_parts_from(service_factory).departments
        return self._departments

    @staticmethod
    def _require_manage(principal: Principal) -> None:
        AccessPolicy.require(
            principal,
            Permission.SETTINGS_MANAGE,
            hidden_as=ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND,
            resource_type=_CUSTOM,
        )

    def ensure_manageable(self, principal: Principal) -> None:
        """Refuse a user who may not manage persona data (admin only, BR4.1).

        For the routers to check before work they run in the background or
        stream (Nemotron download, DWH extraction), whose failures would not
        reach the response.

        Raises:
            CodedError: ``CUSTOM_PERSONA_DATA_NOT_FOUND`` (hidden as missing).
        """
        self._require_manage(principal)

    def import_scope_choices(self, principal: Principal) -> list[Department]:
        """The departments an import may be scoped to (admin: all, BR5.2).

        Raises:
            CodedError: ``CUSTOM_PERSONA_DATA_NOT_FOUND`` (not an admin) or
                ``LOOKUP_UNAVAILABLE``.
        """
        self._require_manage(principal)
        return self._department_list()

    # =========================================================================
    # Nemotron データセット
    # =========================================================================

    def check_nemotron_status(self) -> Dict[str, Any]:
        """Nemotronデータセットの存在状況を返す（利用者のデータを持たない）。"""
        bucket = self.s3_service.bucket_name
        try:
            resp = self.s3_service.s3_client.head_object(
                Bucket=bucket, Key=PARQUET_S3_KEY
            )
            size_mb = round(resp["ContentLength"] / (1024 * 1024), 1)
            return {"exists": True, "size_mb": size_mb}
        except self.s3_service.s3_client.exceptions.ClientError:
            return {"exists": False, "size_mb": 0}

    def download_nemotron_dataset(self, principal: Principal) -> Dict[str, Any]:
        """Nemotronデータセットをダウンロード・S3配置する（admin、BR4.1）。"""
        self._require_manage(principal)
        parquet_bytes = self.batch_service.download_nemotron_dataset()

        self.s3_service.upload_file(parquet_bytes, PARQUET_S3_KEY)
        s3_uri = f"s3://{self.s3_service.bucket_name}/{PARQUET_S3_KEY}"
        self.batch_service.set_parquet_s3_uri(s3_uri)
        logger.info("Nemotron parquet uploaded")

        return self.check_nemotron_status()

    def _ensure_parquet_uri(self) -> None:
        """S3上にParquetが存在するか確認し、batch_serviceにURIをセットする。"""
        bucket = self.s3_service.bucket_name
        try:
            self.s3_service.s3_client.head_object(Bucket=bucket, Key=PARQUET_S3_KEY)
            s3_uri = f"s3://{bucket}/{PARQUET_S3_KEY}"
            self.batch_service.set_parquet_s3_uri(s3_uri)
        except self.s3_service.s3_client.exceptions.ClientError:
            raise SurveyDatasetManagerError(
                "nemotron parquet uri is not configured",
                code=ErrorCode.SURVEY_DATASET_NOT_DOWNLOADED,
            )

    # =========================================================================
    # カスタムペルソナデータの参照（BR4.2、BR5.3）
    # =========================================================================

    def list_persona_data(self, principal: Principal) -> PersonaDataListing:
        """The custom data the user may use; every record for admins.

        Raises:
            CodedError: ``LOOKUP_UNAVAILABLE`` when the table cannot be read
                (never an empty list instead, NFR1.76).
        """
        try:
            records = self._records.list_all()
        except CustomPersonaDataStoreError as e:
            raise LookupUnavailableError("custom persona data unreadable") from e
        usable = [
            r for r in records if AccessPolicy.can_use_asset(principal, self._target(r))
        ]
        usable.sort(key=lambda r: r.registered_at, reverse=True)
        manageable = self._can_manage(principal)
        labels: Dict[str, List[str]] = {}
        if manageable and usable:
            departments = self._department_list()
            labels = {
                r.id: scope_labels(sorted(r.shared_group_ids), departments)
                for r in usable
            }
        return PersonaDataListing(
            custom=tuple(usable), manageable=manageable, scope_labels=labels
        )

    def get_custom_persona_data(
        self, principal: Principal, data_id: str
    ) -> CustomPersonaData:
        """One record the user may use (BR4.2).

        Raises:
            CodedError: ``CUSTOM_PERSONA_DATA_NOT_FOUND`` (missing or out of scope).
        """
        target = self._resource_lookup.get(_CUSTOM, data_id)
        if not AccessPolicy.can_use_asset(principal, target):
            raise _not_found("custom persona data is not usable")
        record = self._read(data_id)
        if record is None:
            raise _not_found("custom persona data is gone")
        return record

    def _usable_datasource(self, principal: Principal, datasource: str) -> str:
        """The datasource value after its decision (BR2.12, BR4.2)."""
        try:
            ref = PersonaDataRef.parse(datasource)
        except ValueError as e:
            raise _not_found("datasource is not usable") from e
        if ref.custom_id is not None:
            target = self._resource_lookup.get(_CUSTOM, ref.custom_id)
            if not AccessPolicy.can_use_asset(principal, target):
                raise _not_found("custom persona data is not usable")
        elif not self.batch_service.has_parquet_uri():
            self._ensure_parquet_uri()
        return ref.value

    def _read(self, data_id: str) -> Optional[CustomPersonaData]:
        try:
            return self._records.get(data_id)
        except CustomPersonaDataStoreError as e:
            raise LookupUnavailableError("custom persona data unreadable") from e

    @staticmethod
    def _target(record: CustomPersonaData) -> AccessTarget:
        return AccessTarget(
            _CUSTOM,
            record.id,
            shared_group_ids=record.shared_group_ids,
            display_name=record.name,
        )

    @staticmethod
    def _can_manage(principal: Principal) -> bool:
        try:
            AccessPolicy.require(principal, Permission.SETTINGS_MANAGE)
        except CodedError:
            return False
        return True

    def _department_list(self) -> list[Department]:
        return list(self._department_directory.list_all())

    # =========================================================================
    # 取り込み・削除・名前の変更（admin、BR4.3〜BR4.6）
    # =========================================================================

    def upload_custom_dataset(
        self,
        principal: Principal,
        csv_bytes: bytes,
        name: str,
        column_mapping: Optional[Dict[str, str]] = None,
        extra_columns: Optional[List[Dict[str, str]]] = None,
        shared_group_ids: frozenset[str] = frozenset(),
        avoid_name_conflict: bool = False,
    ) -> CustomPersonaData:
        """Import a CSV as new custom persona data (BR4.3〜BR4.5).

        Args:
            principal: The admin.
            csv_bytes: The CSV.
            name: Its name (1〜100 characters after stripping).
            column_mapping: Standard column -> CSV column.
            extra_columns: Columns outside the standard ones.
            shared_group_ids: The scope chosen in the dialog (empty = admin only).
            avoid_name_conflict: For a DWH import's generated name: try
                ``{name} (2)`` … up to ten names.

        Raises:
            CodedError: ``CUSTOM_PERSONA_DATA_NOT_FOUND`` (not an admin),
                ``CUSTOM_PERSONA_DATA_NAME_INVALID`` / ``_NAME_TAKEN``,
                ``SHARE_SCOPE_INVALID``, ``SURVEY_DATASET_TOO_FEW_ROWS`` or
                ``SURVEY_OPERATION_FAILED``.
        """
        self._require_manage(principal)
        base = normalize_name(name)
        self._check_departments(frozenset(shared_group_ids))
        df, parquet_bytes = self.batch_service.convert_csv_to_parquet(
            csv_bytes, column_mapping
        )
        # バッチ推論の最小レコード数を下回るデータセットは、アンケート実行時に
        # 必ず失敗する。DWH抽出経路と同じ下限をここでも適用し、使えない
        # データセットを作らせない（Issue #118）。
        self._validate_dataset_row_count(len(df))
        record = CustomPersonaData.create_new(
            name=base,
            columns=list(df.columns),
            row_count=len(df),
            registered_by=principal.sub,
            standard_mapping=column_mapping or {},
            extra_columns=[ExtraColumn.from_dict(c) for c in extra_columns or []],
            shared_group_ids=frozenset(shared_group_ids),
        )
        self.s3_service.upload_file(parquet_bytes, record.s3_key)
        try:
            record = self._register(record, avoid_name_conflict)
        except CodedError:
            self._remove_file(record)
            raise
        logger.info("Custom persona data registered: %s (%d rows)", record.id, len(df))
        return record

    def _register(
        self, record: CustomPersonaData, avoid_name_conflict: bool
    ) -> CustomPersonaData:
        attempts = MAX_NAME_ATTEMPTS if avoid_name_conflict else 1
        base = record.name
        for attempt in range(1, attempts + 1):
            candidate = (
                record if attempt == 1 else record.renamed(f"{base} ({attempt})")
            )
            try:
                self._records.put_new(candidate)
                return candidate
            except CustomPersonaDataNameTaken:
                continue
            except CustomPersonaDataStoreError as e:
                raise SurveyDatasetManagerError(
                    "the record could not be written",
                    code=ErrorCode.SURVEY_OPERATION_FAILED,
                ) from e
        raise SurveyDatasetValidationError(
            "the name is taken", code=ErrorCode.CUSTOM_PERSONA_DATA_NAME_TAKEN
        )

    def _remove_file(self, record: CustomPersonaData) -> None:
        path = self.s3_service.path_of(record.s3_key)
        self._cleanup(
            record.id, "custom_file", lambda: self.s3_service.delete_file(path)
        )

    def delete_custom_dataset(self, principal: Principal, data_id: str) -> None:
        """Delete the record and its reservation, then the file (BR4.6).

        Surveys made from it stay (they show the name they were run with).

        Raises:
            CodedError: ``CUSTOM_PERSONA_DATA_NOT_FOUND`` or
                ``SURVEY_OPERATION_FAILED``.
        """
        self._require_manage(principal)
        record = self._read(data_id)
        if record is None:
            raise _not_found("nothing to delete")
        try:
            deleted = self._records.delete(record.id, record.name)
        except CustomPersonaDataStoreError as e:
            raise SurveyDatasetManagerError(
                "the record could not be deleted",
                code=ErrorCode.SURVEY_OPERATION_FAILED,
            ) from e
        if not deleted:
            raise _not_found("nothing to delete")
        self._remove_file(record)
        self.batch_service.invalidate_datasource(PersonaDataRef.custom(record.id).value)
        logger.info("Custom persona data deleted: %s", record.id)

    def rename_custom_dataset(
        self, principal: Principal, data_id: str, name: str
    ) -> CustomPersonaData:
        """Rename one record; the reservation moves with it (BR4.4).

        Raises:
            CodedError: ``CUSTOM_PERSONA_DATA_NOT_FOUND`` /
                ``CUSTOM_PERSONA_DATA_NAME_INVALID`` / ``_NAME_TAKEN``.
        """
        self._require_manage(principal)
        new_name = normalize_name(name)
        record = self._read(data_id)
        if record is None:
            raise _not_found("nothing to rename")
        if new_name == record.name:
            return record
        try:
            self._records.rename(record.id, old_name=record.name, new_name=new_name)
        except CustomPersonaDataNameTaken as e:
            raise SurveyDatasetValidationError(
                "the name is taken", code=ErrorCode.CUSTOM_PERSONA_DATA_NAME_TAKEN
            ) from e
        except CustomPersonaDataConditionFailed as e:
            raise _not_found("the record is gone or was renamed") from e
        except CustomPersonaDataStoreError as e:
            raise SurveyDatasetManagerError(
                "rename failed", code=ErrorCode.SURVEY_OPERATION_FAILED
            ) from e
        return record.renamed(new_name)

    # =========================================================================
    # 利用範囲（admin、BR5.2）
    # =========================================================================

    def scope_dialog(self, principal: Principal, data_id: str) -> AssetScopeDialog:
        """What the usage-scope dialog shows (M10).

        Raises:
            CodedError: ``CUSTOM_PERSONA_DATA_NOT_FOUND``.
        """
        self._require_manage(principal)
        record = self._read(data_id)
        if record is None:
            raise _not_found("no such record")
        return scope_dialog(
            _CUSTOM,
            record.id,
            record.name,
            record.shared_group_ids,
            self._department_list(),
        )

    def update_scope(
        self, principal: Principal, data_id: str, change: AssetScopeChange
    ) -> CustomPersonaData:
        """Change the scope with ``ADD`` / ``DELETE`` (BR5.2).

        Raises:
            CodedError: ``CUSTOM_PERSONA_DATA_NOT_FOUND`` or ``SHARE_SCOPE_INVALID``
                (an added department does not exist).
        """
        self._require_manage(principal)
        self._check_departments(change.add)
        try:
            self._records.update_scope(data_id, add=change.add, remove=change.remove)
        except CustomPersonaDataConditionFailed as e:
            raise _not_found("the record is gone") from e
        except CustomPersonaDataStoreError as e:
            raise SurveyDatasetManagerError(
                "scope update failed", code=ErrorCode.SURVEY_OPERATION_FAILED
            ) from e
        log_event(
            "asset.scope_changed",
            resource_type=_CUSTOM.value,
            resource_id=data_id,
            added=sorted(change.add),
            removed=sorted(change.remove),
            actor_sub=principal.sub,
        )
        record = self._read(data_id)
        if record is None:
            raise _not_found("the record is gone")
        return record

    def _check_departments(self, groups: frozenset[str]) -> None:
        if not groups:
            return
        existing = {d.group_id for d in self._department_list()}
        if not groups <= existing:
            raise SurveyDatasetValidationError(
                "a department that does not exist was sent",
                code=ErrorCode.SHARE_SCOPE_INVALID,
            )

    def _cleanup(self, data_id: str, step: str, action: Callable[[], object]) -> None:
        run_cleanup(
            action,
            resource_type=_CUSTOM.value,
            resource_id=data_id,
            step=step,
            sleep=self._sleep,
        )

    # =========================================================================
    # DWH連携セグメント抽出（admin）
    # =========================================================================

    def extract_segment_from_dwh(
        self, principal: Principal, condition: str, event_queue: Any
    ) -> Dict[str, Any]:
        """データ分析エージェント連携でDWHから顧客セグメントを抽出する。"""
        from ..config import config
        from ..services.data_agent_service import (
            DataAgentService,
            create_data_agent_tool,
        )

        self._require_manage(principal)
        if not condition or not condition.strip():
            raise SurveyDatasetValidationError(
                "extraction condition is blank",
                code=ErrorCode.SEGMENT_CONDITION_REQUIRED,
            )
        if not config.DATA_AGENT_RUNTIME_ARN:
            raise SurveyDatasetManagerError(
                "DATA_AGENT_RUNTIME_ARN is not set",
                code=ErrorCode.DATA_AGENT_NOT_CONFIGURED,
            )

        logger.info("DWH セグメント抽出開始")
        event_queue.put(
            {"type": "status", "content": "データ分析エージェントに接続中..."}
        )

        csv_urls: List[str] = []

        class _CsvUrlCapture:
            def __init__(self, inner: Any, url_list: List[str]) -> None:
                self._inner = inner
                self._url_list = url_list

            def put(self, item: Any) -> None:
                if isinstance(item, dict) and item.get("type") == "csv_url":
                    url = item.get("url", "")
                    if url:
                        self._url_list.append(url)
                self._inner.put(item)

            def get(self, *args: Any, **kwargs: Any) -> Any:
                return self._inner.get(*args, **kwargs)

            def get_nowait(self) -> Any:
                return self._inner.get_nowait()

            def empty(self) -> bool:
                return bool(self._inner.empty())

        capture_queue = _CsvUrlCapture(event_queue, csv_urls)

        ask_data_agent = create_data_agent_tool(
            runtime_arn=config.DATA_AGENT_RUNTIME_ARN,
            region=config.DATA_AGENT_REGION,
            event_queue=capture_queue,  # type: ignore[arg-type]
        )

        user_prompt = (
            f"以下の条件でDWHから顧客セグメントデータを抽出してCSVエクスポートしてください:\n\n"
            f"条件: {condition}\n\n"
            f"最終出力: ask_data_agent に「CSVで出力してください」と依頼し、"
            f"ダウンロードURLを取得してください。"
        )

        event_queue.put({"type": "status", "content": "データ抽出中..."})
        self.agent_service.run_segment_extraction_agent(
            system_prompt=DWH_SEGMENT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tools=[ask_data_agent],
        )

        if not csv_urls:
            raise SurveyDatasetManagerError(
                "data agent returned no csv export url",
                code=ErrorCode.SEGMENT_CSV_URL_MISSING,
            )

        csv_url = csv_urls[-1]
        event_queue.put({"type": "status", "content": "CSVデータをダウンロード中..."})
        data_agent_svc = DataAgentService(
            config.DATA_AGENT_RUNTIME_ARN, config.DATA_AGENT_REGION
        )
        csv_bytes = data_agent_svc.download_csv(csv_url)

        parse_result = parse_csv_columns_with_samples(csv_bytes, STANDARD_COLUMNS)
        row_count = parse_result["row_count"]
        self._validate_segment_row_count(row_count)

        suggested_name = self._generate_dataset_name(condition)

        logger.info(
            "DWH セグメント抽出完了: %d件, %dカラム",
            row_count,
            len(parse_result["columns"]),
        )
        return {
            "csv_bytes": csv_bytes,
            "row_count": row_count,
            "columns": parse_result["columns"],
            "samples": parse_result["samples"],
            "auto_mapping": parse_result["auto_mapping"],
            "suggested_name": suggested_name,
        }

    def _generate_dataset_name(self, condition: str) -> str:
        """抽出条件からデータセット名を自動生成する。"""
        if self.ai_service:
            prompt = build_dataset_name_prompt(condition)
            try:
                name = self.ai_service.invoke_model(prompt).strip()
                name = name.strip("「」『』\"'")
                if 1 <= len(name) <= 30:
                    return name
            except Exception as e:
                logger.warning(
                    "データセット名自動生成失敗（フォールバック使用）: %s",
                    type(e).__name__,
                )
        clean = condition.strip().replace("\n", " ")
        return clean[:20] if len(clean) > 20 else clean

    def _validate_segment_row_count(self, row_count: int) -> None:
        """抽出件数バリデーション。"""
        if row_count < self.MIN_SEGMENT_ROWS:
            raise SurveyDatasetValidationError(
                f"row count {row_count} below minimum {self.MIN_SEGMENT_ROWS}",
                code=ErrorCode.SEGMENT_ROW_COUNT_TOO_LOW,
                context={"row_count": row_count, "min_rows": self.MIN_SEGMENT_ROWS},
            )
        if row_count > self.MAX_SEGMENT_ROWS:
            raise SurveyDatasetValidationError(
                f"row count {row_count} exceeds maximum {self.MAX_SEGMENT_ROWS}",
                code=ErrorCode.SEGMENT_ROW_COUNT_TOO_HIGH,
                context={"row_count": row_count, "max_rows": self.MAX_SEGMENT_ROWS},
            )

    def _validate_dataset_row_count(self, row_count: int) -> None:
        """アップロードされたデータセットの件数バリデーション。

        DWH抽出（:meth:`_validate_segment_row_count`）と下限は同じだが、
        こちらは「フィルタでは救えない、ファイル自体が足りない」状況なので
        別のコードで文言を分ける。
        """
        if row_count < self.MIN_SEGMENT_ROWS:
            raise SurveyDatasetValidationError(
                f"dataset row count {row_count} below minimum {self.MIN_SEGMENT_ROWS}",
                code=ErrorCode.SURVEY_DATASET_TOO_FEW_ROWS,
                context={"row_count": row_count, "min_rows": self.MIN_SEGMENT_ROWS},
            )

    # =========================================================================
    # カラムマッピング提案（admin）
    # =========================================================================

    def suggest_column_mapping(
        self, principal: Principal, columns: List[str], samples: Dict[str, List[str]]
    ) -> Dict[str, Any]:
        """LLMを使ったカラムマッピング提案。"""
        self._require_manage(principal)
        if not self.agent_service:
            return {"mapping": {}, "extra_columns": []}

        prompt = build_column_mapping_prompt(columns, samples, STANDARD_COLUMNS)

        try:
            result = self.agent_service.suggest_column_mapping_with_llm(prompt)

            valid_mapping = {
                k: v
                for k, v in result.get("mapping", {}).items()
                if k in STANDARD_COLUMNS and v in columns
            }
            valid_extra = [
                e
                for e in result.get("extra_columns", [])
                if e.get("csv_column") in columns
                and e.get("csv_column") not in valid_mapping.values()
            ]
            return {
                "mapping": valid_mapping,
                "extra_columns": valid_extra,
            }
        except Exception as e:
            logger.warning("LLMマッピング提案失敗: %s", type(e).__name__)

        return {"mapping": {}, "extra_columns": []}

    # =========================================================================
    # 一時ファイル操作（admin。temp_key の扱いは今のまま、BR4.7）
    # =========================================================================

    def upload_temp_file(self, principal: Principal, content: bytes, key: str) -> None:
        """一時ファイルをS3にアップロードする。"""
        self._require_manage(principal)
        self.s3_service.upload_file(content, key)

    def download_temp_file(self, principal: Principal, key: str) -> bytes:
        """一時ファイルをS3からダウンロードする。"""
        self._require_manage(principal)
        bucket = self.s3_service.bucket_name
        return self.s3_service.download_file(f"s3://{bucket}/{key}")

    def delete_temp_file(self, principal: Principal, key: str) -> None:
        """一時ファイルをS3から削除する。"""
        self._require_manage(principal)
        self.s3_service.s3_client.delete_object(
            Bucket=self.s3_service.bucket_name, Key=key
        )

    # =========================================================================
    # CSV解析・プレビュー（admin）
    # =========================================================================

    def parse_csv_columns(self, principal: Principal, content: bytes) -> Dict[str, Any]:
        """CSVファイルのカラムを解析する。"""
        self._require_manage(principal)
        return parse_csv_columns_with_samples(content, STANDARD_COLUMNS)

    def get_standard_columns(self) -> Dict[str, Dict[str, Any]]:
        """標準カラム定義を取得する（利用者のデータを持たない）。"""
        return STANDARD_COLUMNS

    def preview_system_prompt(
        self,
        principal: Principal,
        csv_bytes: bytes,
        column_mapping: Dict[str, str],
        extra_columns: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """マッピング設定に基づくシステムプロンプトのプレビュー（取り込みの途中）。"""
        self._require_manage(principal)
        row = parse_csv_first_row_with_mapping(csv_bytes, column_mapping)
        return build_persona_system_prompt(row, extra_columns)

    # =========================================================================
    # フィルタ・プレビュー統計（データソースを判定してから読む、BR2.12）
    # =========================================================================

    def get_available_filter_values(
        self, principal: Principal, datasource: str
    ) -> Dict[str, Dict[str, Any]]:
        """利用可能なフィルター値を取得する。"""
        source = self._usable_datasource(principal, datasource)
        return self.batch_service.get_available_filter_values(source)

    def get_filtered_count(
        self,
        principal: Principal,
        filters: Optional[Dict[str, Any]] = None,
        datasource: str = "nemotron",
    ) -> int:
        """フィルター条件に合致するレコード数を取得する。"""
        source = self._usable_datasource(principal, datasource)
        return self.batch_service.get_filtered_count(filters, source)

    def get_preview_stats(
        self,
        principal: Principal,
        filters: Optional[Dict[str, Any]] = None,
        datasource: str = "nemotron",
    ) -> Dict[str, Any]:
        """プレビュー用の統計情報を取得する。"""
        source = self._usable_datasource(principal, datasource)
        return self.batch_service.get_preview_stats(filters, source)

    def get_datasource_count(self, principal: Principal, datasource: str) -> int:
        """データソースの総レコード数を取得する。"""
        source = self._usable_datasource(principal, datasource)
        return self.batch_service.get_total_count(source)
