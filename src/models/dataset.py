"""
Dataset data model for external data integration.
"""

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid


@dataclass
class DatasetColumn:
    """データセットのカラム情報"""

    name: str
    data_type: str  # string, integer, float, date, datetime, boolean
    description: str = ""


@dataclass
class Dataset:
    """外部データセット（CSVファイル）の情報"""

    id: str
    name: str
    description: str
    s3_path: str
    columns: List[DatasetColumn]
    row_count: int
    created_at: datetime
    updated_at: datetime
    notes: str = ""
    # U5（Q3 A）: ペルソナ専用のデータセットなら持ち主のペルソナ ID。組織資産は None。
    # 作成後は変えない
    owner_persona_id: Optional[str] = None

    @classmethod
    def create_new(
        cls,
        name: str,
        description: str,
        s3_path: str,
        columns: List[DatasetColumn],
        row_count: int = 0,
        notes: str = "",
        owner_persona_id: Optional[str] = None,
        dataset_id: Optional[str] = None,
    ) -> "Dataset":
        now = datetime.now()
        return cls(
            id=dataset_id or str(uuid.uuid4()),
            name=name,
            description=description,
            s3_path=s3_path,
            columns=columns,
            row_count=row_count,
            created_at=now,
            updated_at=now,
            notes=notes,
            owner_persona_id=owner_persona_id,
        )

    def is_dedicated_to(self, persona_id: str) -> bool:
        """Whether this dataset belongs to ``persona_id`` (BR2.4, BR6.1)."""
        return bool(self.owner_persona_id) and self.owner_persona_id == persona_id

    def to_dict(self) -> Dict[str, Any]:
        data = self._base_dict()
        if self.owner_persona_id:
            data["owner_persona_id"] = self.owner_persona_id
        return data

    def _base_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "s3_path": self.s3_path,
            "columns": [asdict(col) for col in self.columns],
            "row_count": int(self.row_count),
            "created_at": self.created_at.isoformat()
            if isinstance(self.created_at, datetime)
            else self.created_at,
            "updated_at": self.updated_at.isoformat()
            if isinstance(self.updated_at, datetime)
            else self.updated_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Dataset":
        columns = [DatasetColumn(**col) for col in data.get("columns", [])]
        return cls(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            s3_path=data["s3_path"],
            columns=columns,
            row_count=data.get("row_count", 0),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            notes=data.get("notes", ""),
            owner_persona_id=data.get("owner_persona_id") or None,
        )


# ペルソナとデータセットの紐付けは U5 から src/models/persona_binding.py の
# PersonaBinding（PersonaBindings テーブル）が持つ。
