"""
Dataset Manager - データセットのビジネスロジック
"""

import csv
import io
import logging
import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any

from ..models.dataset import Dataset, DatasetColumn, PersonaDatasetBinding
from ..services.service_factory import service_factory

logger = logging.getLogger(__name__)


class DatasetManager:
    """データセット管理マネージャー"""

    def __init__(self):
        self.db_service = service_factory.get_database_service()
        self.s3_service = service_factory.get_s3_service()

    def analyze_schema(
        self, file_content: bytes, sample_rows: int = 100
    ) -> tuple[List[DatasetColumn], int]:
        """
        CSVファイルからスキーマを解析

        Returns:
            (columns, row_count)
        """
        text = file_content.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text))

        # ヘッダー取得
        headers = next(reader, [])
        if not headers:
            return [], 0

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
        import re

        return bool(re.match(r"^\d{4}-\d{2}-\d{2}", v))

    def upload_csv(
        self,
        file_content: bytes,
        filename: str,
        name: str,
        description: str = "",
        notes: str = "",
        columns: Optional[List[DatasetColumn]] = None,
    ) -> Dataset:
        """
        CSVファイルをアップロードしてデータセットを作成
        """
        # スキーマ解析（columnsが指定されていない場合）
        if columns is None:
            columns, row_count = self.analyze_schema(file_content)
        else:
            _, row_count = self.analyze_schema(file_content)

        # S3にアップロード
        file_id = str(uuid.uuid4())
        s3_key = f"datasets/{file_id}_{filename}"

        if self.s3_service:
            s3_path = self.s3_service.upload_file(file_content, s3_key)
        else:
            # ローカルストレージにフォールバック
            from pathlib import Path

            local_dir = Path("datasets")
            local_dir.mkdir(exist_ok=True)
            local_path = local_dir / f"{file_id}_{filename}"
            local_path.write_bytes(file_content)
            s3_path = f"local://{local_path}"

        # データセット作成
        dataset = Dataset.create_new(
            name=name,
            description=description,
            s3_path=s3_path,
            columns=columns,
            row_count=row_count,
            notes=notes,
        )

        # DB保存
        self.db_service.save_dataset(dataset)
        logger.info(f"Dataset created: {dataset.id} ({name})")

        return dataset

    def get_datasets(self) -> List[Dataset]:
        """全データセットを取得"""
        return self.db_service.get_all_datasets()

    def get_dataset(self, dataset_id: str) -> Optional[Dataset]:
        """IDでデータセットを取得"""
        return self.db_service.get_dataset(dataset_id)

    def update_dataset(
        self,
        dataset_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        notes: Optional[str] = None,
        columns: Optional[List[DatasetColumn]] = None,
    ) -> Optional[Dataset]:
        """データセットを更新"""
        dataset = self.db_service.get_dataset(dataset_id)
        if not dataset:
            return None

        if name is not None:
            dataset.name = name
        if description is not None:
            dataset.description = description
        if notes is not None:
            dataset.notes = notes
        if columns is not None:
            dataset.columns = columns
        dataset.updated_at = datetime.now()

        self.db_service.save_dataset(dataset)
        return dataset

    def delete_dataset(self, dataset_id: str) -> bool:
        """データセットを削除"""
        dataset = self.db_service.get_dataset(dataset_id)
        if not dataset:
            return False

        # S3からファイル削除
        if self.s3_service and dataset.s3_path.startswith("s3://"):
            try:
                self.s3_service.delete_file(dataset.s3_path)
            except Exception as e:
                logger.warning(f"Failed to delete S3 file: {e}")

        # DB削除
        self.db_service.delete_dataset(dataset_id)
        logger.info(f"Dataset deleted: {dataset_id}")
        return True

    # ==================== Binding Operations ====================

    def save_binding(self, binding: PersonaDatasetBinding) -> PersonaDatasetBinding:
        """紐付けを保存"""
        return self.db_service.save_binding(binding)

    def get_bindings_by_persona(self, persona_id: str) -> List[PersonaDatasetBinding]:
        """ペルソナの紐付けを取得"""
        return self.db_service.get_bindings_by_persona(persona_id)

    def delete_binding(self, binding_id: str) -> bool:
        """紐付けを削除"""
        return self.db_service.delete_binding(binding_id)

    def set_persona_bindings(
        self, persona_id: str, bindings_data: List[Dict[str, Any]]
    ) -> List[PersonaDatasetBinding]:
        """
        ペルソナの紐付けを一括設定（既存を削除して新規作成）

        bindings_data: [{"dataset_id": "...", "binding_keys": {"user_id": "..."}}]
        """
        # 既存削除
        self.db_service.delete_bindings_by_persona(persona_id)

        # 新規作成
        bindings = []
        for data in bindings_data:
            binding = PersonaDatasetBinding.create_new(
                persona_id=persona_id,
                dataset_id=data["dataset_id"],
                binding_keys=data.get("binding_keys", {}),
            )
            self.db_service.save_binding(binding)
            bindings.append(binding)

        return bindings
