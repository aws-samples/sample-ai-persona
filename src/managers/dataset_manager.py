"""Dataset Manager: business logic for datasets.

Rules:

- Every method (the settings screen) first checks
  ``settings:manage``; others get ``SETTINGS_NOT_FOUND`` (the same 404 as a
  missing page) and nothing is read or written.
- An edit writes only the changed columns, so the usage scope stays.
- The usage scope changes with ``ADD`` / ``DELETE``; only added
  departments must exist. ``asset.scope_changed`` is logged.
- Persona-dedicated datasets are not organization assets; the settings
  screen neither lists nor changes them.

Members see the datasets they may use through ``PersonaManager``.
"""

import logging
import uuid
from datetime import datetime
from typing import Any, List, Optional

from ..models.authz import Permission, Principal, ResourceType
from ..models.dataset import Dataset, DatasetColumn
from ..models.errors import CodedError, ErrorCode
from ..models.persona_binding import dataset_target_key
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
from .shared.file_utils import analyze_csv_schema
from .shared.structured_log import log_event

logger = logging.getLogger(__name__)

_DATASET = ResourceType.DATASET


class DatasetManagerError(CodedError):
    """Base exception for dataset manager related errors."""

    pass


class DatasetManager:
    """Dataset management Manager (settings screen, admin only)"""

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
            departments: The per-task department copy (scope choices).
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
        Parse the schema from a CSV file

        Returns:
            (columns, row_count)
        """
        self._require_manage(principal)
        return self._analyze_schema(file_content, sample_rows)

    def _analyze_schema(
        self, file_content: bytes, sample_rows: int = 100
    ) -> tuple[List[DatasetColumn], int]:
        # Empty or duplicate headers are renamed by DuckDB read_csv_auto and then
        # disagree with the query's column names, so they are rejected at upload.
        # Encoding errors are raised as they are (UnicodeDecodeError is also a kind
        # of ValueError, so it is separated first)
        try:
            return analyze_csv_schema(file_content, sample_rows)
        except UnicodeDecodeError:
            raise
        except ValueError as e:
            raise DatasetManagerError(
                "csv header is invalid", code=ErrorCode.FILE_CSV_HEADER_INVALID
            ) from e

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
        Upload a CSV file and create a dataset (the usage scope starts as admin only)
        """
        self._require_manage(principal)
        # Parse the schema (when columns are not given)
        if columns is None:
            columns, row_count = self._analyze_schema(file_content)
        else:
            _, row_count = self._analyze_schema(file_content)

        # Upload to S3. The key is normalized to ASCII based on the dataset id
        # (the original file name may contain Japanese or spaces, so it is not put in
        # the key; the display name is kept in Dataset.name / metadata).
        file_id = str(uuid.uuid4())
        s3_key = f"datasets/{file_id}.csv"

        if self.s3_service:
            s3_path = self.s3_service.upload_file(file_content, s3_key)
        else:
            # Fall back to local storage
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
        """Organization-asset datasets and their usage scope (persona-dedicated ones excluded)"""
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
        """Get an organization-asset dataset by ID (None for persona-dedicated)"""
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
        """Write only the changed columns (the usage scope is kept). None if not found"""
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
        """Delete a dataset (persona-dedicated ones are not targets)"""
        self._require_manage(principal)
        dataset = self._org_dataset(dataset_id)
        if not dataset:
            return False

        # Delete the file from S3
        if self.s3_service and dataset.s3_path.startswith("s3://"):
            try:
                self.s3_service.delete_file(dataset.s3_path)
            except Exception as e:
                logger.warning("Failed to delete S3 file: %s", type(e).__name__)

        # Delete from the DB
        self._datasets.delete(dataset_id)
        logger.info("Dataset deleted: %s", dataset_id)

        # Delete the persona bindings (leftovers are skipped on read because the dataset itself is gone)
        try:
            self._bindings.delete_for_target(dataset_id, dataset_target_key(dataset_id))
        except PersonaBindingStoreError as e:
            logger.warning(
                "Failed to delete bindings of dataset %s: %s",
                dataset_id,
                type(e).__name__,
            )
        return True

    # --- Usage scope ---

    def scope_dialog(self, principal: Principal, dataset_id: str) -> AssetScopeDialog:
        """What the usage-scope dialog shows.

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
        """Change the usage scope with ``ADD`` / ``DELETE``.

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

    # The five binding methods (save, list per persona, delete, bulk set, preview)
    # were moved to PersonaManager, because the binding decision is made together with
    # the persona's read/write decision.
