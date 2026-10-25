"""
Dataset Manager - データセットのビジネスロジック

v1.0.0 U7 (``functional-design/rules.md``):

- BR3.1 / BR3.3: every method (the settings screen) first checks
  ``settings:manage``; others get ``SETTINGS_NOT_FOUND`` (the same 404 as a
  missing page) and nothing is read or written.
- BR1.5: an edit writes only the changed columns, so the usage scope stays.
- BR5.2: the usage scope changes with ``ADD`` / ``DELETE``; only added
  departments must exist. ``asset.scope_changed`` is logged.
- BR5.6: persona-dedicated datasets are not organization assets; the settings
  screen neither lists nor changes them.

Members see the datasets they may use through ``PersonaManager`` (U5).
"""

import csv
import io
import logging
import re
import uuid
from datetime import datetime
from typing import Any, List, Optional

from ..models.authz import Permission, Principal, ResourceType
from ..models.dataset import Dataset, DatasetColumn
from ..models.errors import CodedError, ErrorCode
from ..models.sharing import AssetScopeChange, AssetScopeDialog, ScopedAssets
from ..services.stores.dataset_store import DatasetStore
from ..services.stores.persona_binding_store import (
    PersonaBindingStore,
    PersonaBindingStoreError,
)
from ..services.service_factory import service_factory
from .components.access_policy import AccessPolicy
from .components.department_directory import DepartmentDirectory
from .components.sharing_runtime import sharing_parts_from
from .shared.asset_scope import scope_dialog, scope_labels
from .shared.file_utils import normalize_csv_headers
from .shared.structured_log import log_event

logger = logging.getLogger(__name__)

_DATASET = ResourceType.DATASET


class DatasetManagerError(CodedError):
    """Base exception for dataset manager related errors."""

    pass


class DatasetManager:
    """データセット管理マネージャー（設定画面、admin だけ）"""

    def __init__(
        self,
        survey_batch_service: Any = None,
        dataset_store: Optional[DatasetStore] = None,
        s3_service: Any = None,
        departments: Optional[DepartmentDirectory] = None,
        binding_store: Optional[PersonaBindingStore] = None,
    ) -> None:
        """Create the manager.

        Args:
            survey_batch_service: Batch service (the singleton when omitted).
            dataset_store: Dataset records (the singleton when omitted).
            s3_service: The CSV files (``None`` without S3: local files).
            departments: The per-task department copy (scope choices, C4).
            binding_store: Persona bindings, removed with a deleted dataset
                (the singleton on first use when omitted).
        """
        self._datasets = dataset_store or service_factory.get_dataset_store()
        self.s3_service = (
            s3_service if s3_service is not None else service_factory.get_s3_service()
        )
        self.survey_batch_service = (
            survey_batch_service or service_factory.get_survey_batch_service()
        )
        self._departments = departments
        self._binding_store = binding_store

    @property
    def _bindings(self) -> PersonaBindingStore:
        if self._binding_store is None:
            self._binding_store = service_factory.get_persona_binding_store()
        return self._binding_store

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
            hidden_as=ErrorCode.SETTINGS_NOT_FOUND,
            resource_type=_DATASET,
        )

    def analyze_schema(
        self, principal: Principal, file_content: bytes, sample_rows: int = 100
    ) -> tuple[List[DatasetColumn], int]:
        """
        CSVファイルからスキーマを解析

        Returns:
            (columns, row_count)
        """
        self._require_manage(principal)
        return self._analyze_schema(file_content, sample_rows)

    def _analyze_schema(
        self, file_content: bytes, sample_rows: int = 100
    ) -> tuple[List[DatasetColumn], int]:
        text = file_content.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text))

        # ヘッダー取得
        headers = next(reader, [])
        if not headers:
            return [], 0
        # ヘッダー名を正規化・検証（空/重複は DuckDB read_csv_auto がリネームして
        # クエリ列名と乖離するため、アップロード時点で明確に弾く）。
        try:
            headers = normalize_csv_headers(headers)
        except ValueError as e:
            raise DatasetManagerError(
                "csv header is invalid", code=ErrorCode.FILE_CSV_HEADER_INVALID
            ) from e

        # サンプル行を読み取って型推定
        rows = []
        for i, row in enumerate(reader):
            if i >= sample_rows:
                break
            rows.append(row)

        # 残りの行数をカウント
        row_count = len(rows)
        for _ in reader:
            row_count += 1

        columns = []
        for idx, header in enumerate(headers):
            data_type = self._infer_type(
                [row[idx] if idx < len(row) else "" for row in rows]
            )
            columns.append(DatasetColumn(name=header, data_type=data_type))

        return columns, row_count

    def _infer_type(self, values: List[str]) -> str:
        """値のリストから型を推定"""
        non_empty = [v for v in values if v.strip()]
        if not non_empty:
            return "string"

        # 整数チェック
        if all(self._is_int(v) for v in non_empty):
            return "integer"

        # 浮動小数点チェック
        if all(self._is_float(v) for v in non_empty):
            return "float"

        # 日付チェック（YYYY-MM-DD形式）
        if all(self._is_date(v) for v in non_empty):
            return "date"

        return "string"

    def _is_int(self, v: str) -> bool:
        try:
            int(v.replace(",", ""))
            return True
        except ValueError:
            return False

    def _is_float(self, v: str) -> bool:
        try:
            float(v.replace(",", ""))
            return True
        except ValueError:
            return False

    def _is_date(self, v: str) -> bool:
        return bool(re.match(r"^\d{4}-\d{2}-\d{2}", v))

    def upload_csv(
        self,
        principal: Principal,
        file_content: bytes,
        filename: str,
        name: str,
        description: str = "",
        notes: str = "",
        columns: Optional[List[DatasetColumn]] = None,
    ) -> Dataset:
        """
        CSVファイルをアップロードしてデータセットを作成（利用範囲は admin だけ）
        """
        self._require_manage(principal)
        # スキーマ解析（columnsが指定されていない場合）
        if columns is None:
            columns, row_count = self._analyze_schema(file_content)
        else:
            _, row_count = self._analyze_schema(file_content)

        # S3にアップロード。key は dataset id ベースの ASCII に正規化する
        # （元ファイル名は日本語・空白を含みうるため key に埋めない。表示名は
        # Dataset.name / metadata が保持する）。
        file_id = str(uuid.uuid4())
        s3_key = f"datasets/{file_id}.csv"

        if self.s3_service:
            s3_path = self.s3_service.upload_file(file_content, s3_key)
        else:
            # ローカルストレージにフォールバック
            from pathlib import Path

            local_dir = Path("datasets")
            local_dir.mkdir(exist_ok=True)
            local_path = local_dir / f"{file_id}.csv"
            local_path.write_bytes(file_content)
            s3_path = f"local://{local_path}"

        dataset = Dataset.create_new(
            name=name,
            description=description,
            s3_path=s3_path,
            columns=columns,
            row_count=row_count,
            notes=notes,
        )
        self._datasets.put_new(dataset)
        logger.info("Dataset created: %s", dataset.id)
        return dataset

    def get_datasets(self, principal: Principal) -> ScopedAssets[Dataset]:
        """組織資産のデータセットと利用範囲（ペルソナ専用を除く、BR5.6）"""
        self._require_manage(principal)
        datasets = self._datasets.list_org_assets()
        departments = self._department_directory.list_all() if datasets else []
        return ScopedAssets(
            items=datasets,
            scope_labels={
                d.id: scope_labels(sorted(d.shared_group_ids), departments)
                for d in datasets
            },
        )

    def get_dataset(self, principal: Principal, dataset_id: str) -> Optional[Dataset]:
        """IDで組織資産のデータセットを取得（ペルソナ専用は None）"""
        self._require_manage(principal)
        return self._org_dataset(dataset_id)

    def _org_dataset(self, dataset_id: str) -> Optional[Dataset]:
        dataset = self._datasets.get(dataset_id, consistent=True)
        if dataset is None or not dataset.is_org_asset:
            return None
        return dataset

    def update_dataset(
        self,
        principal: Principal,
        dataset_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        notes: Optional[str] = None,
        columns: Optional[List[DatasetColumn]] = None,
    ) -> Optional[Dataset]:
        """変える列だけを書く（利用範囲は消さない、BR1.5）。見つからなければ None"""
        self._require_manage(principal)
        if self._org_dataset(dataset_id) is None:
            return None
        written = self._datasets.update_fields(
            dataset_id,
            updated_at=datetime.now(),
            name=name,
            description=description,
            notes=notes,
            columns=columns,
        )
        if not written:
            return None
        return self._org_dataset(dataset_id)

    def delete_dataset(self, principal: Principal, dataset_id: str) -> bool:
        """データセットを削除（ペルソナ専用は対象外）"""
        self._require_manage(principal)
        dataset = self._org_dataset(dataset_id)
        if not dataset:
            return False

        # S3からファイル削除
        if self.s3_service and dataset.s3_path.startswith("s3://"):
            try:
                self.s3_service.delete_file(dataset.s3_path)
            except Exception as e:
                logger.warning("Failed to delete S3 file: %s", type(e).__name__)

        # DB削除
        self._datasets.delete(dataset_id)
        logger.info("Dataset deleted: %s", dataset_id)

        # ペルソナとの紐付けを削除（残っても本体がないため読むときに飛ばされる）
        try:
            self._bindings.delete_for_target(dataset_id)
        except PersonaBindingStoreError as e:
            logger.warning(
                "Failed to delete bindings of dataset %s: %s",
                dataset_id,
                type(e).__name__,
            )
        return True

    # --- 利用範囲（BR5.2） ---

    def scope_dialog(self, principal: Principal, dataset_id: str) -> AssetScopeDialog:
        """What the usage-scope dialog shows (M10).

        Raises:
            CodedError: ``DATASET_NOT_FOUND`` (missing or persona-dedicated).
        """
        self._require_manage(principal)
        dataset = self._org_dataset(dataset_id)
        if dataset is None:
            raise DatasetManagerError(
                "no such dataset", code=ErrorCode.DATASET_NOT_FOUND
            )
        return scope_dialog(
            _DATASET,
            dataset.id,
            dataset.name,
            dataset.shared_group_ids,
            self._department_directory.list_all(),
        )

    def update_scope(
        self, principal: Principal, dataset_id: str, change: AssetScopeChange
    ) -> Dataset:
        """Change the usage scope with ``ADD`` / ``DELETE`` (BR5.2).

        Raises:
            CodedError: ``DATASET_NOT_FOUND`` (missing or persona-dedicated) or
                ``SHARE_SCOPE_INVALID`` (an added department does not exist).
        """
        self._require_manage(principal)
        existing = {d.group_id for d in self._department_directory.list_all()}
        if not change.add <= existing:
            raise DatasetManagerError(
                "a department that does not exist was sent",
                code=ErrorCode.SHARE_SCOPE_INVALID,
            )
        if not self._datasets.update_scope(
            dataset_id, add=change.add, remove=change.remove
        ):
            raise DatasetManagerError(
                "no such dataset", code=ErrorCode.DATASET_NOT_FOUND
            )
        log_event(
            "asset.scope_changed",
            resource_type=_DATASET.value,
            resource_id=dataset_id,
            added=sorted(change.add),
            removed=sorted(change.remove),
            actor_sub=principal.sub,
        )
        dataset = self._org_dataset(dataset_id)
        if dataset is None:
            raise DatasetManagerError(
                "no such dataset", code=ErrorCode.DATASET_NOT_FOUND
            )
        return dataset

    # 紐付けの 5 メソッド（保存・ペルソナごとの一覧・削除・一括設定・プレビュー）は
    # U5 で PersonaManager へ移した
    # （ADR-007。紐付けの判定はペルソナの読み書きの判定と一緒に行うため）。
