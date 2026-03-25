"""
Dataset data model for external data integration.
"""

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Dict, Any
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

    @classmethod
    def create_new(
        cls,
        name: str,
        description: str,
        s3_path: str,
        columns: List[DatasetColumn],
        row_count: int = 0,
        notes: str = "",
    ) -> "Dataset":
        now = datetime.now()
        return cls(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            s3_path=s3_path,
            columns=columns,
            row_count=row_count,
            created_at=now,
            updated_at=now,
            notes=notes,
        )

    def to_dict(self) -> Dict[str, Any]:
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
        )


@dataclass
class PersonaDatasetBinding:
    """ペルソナとデータセットの紐付け情報"""

    id: str
    persona_id: str
    dataset_id: str
    binding_keys: Dict[str, str]  # {"user_id": "U12345"}
    created_at: datetime

    @classmethod
    def create_new(
        cls, persona_id: str, dataset_id: str, binding_keys: Dict[str, str]
    ) -> "PersonaDatasetBinding":
        return cls(
            id=str(uuid.uuid4()),
            persona_id=persona_id,
            dataset_id=dataset_id,
            binding_keys=binding_keys,
            created_at=datetime.now(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "persona_id": self.persona_id,
            "dataset_id": self.dataset_id,
            "binding_keys": self.binding_keys,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PersonaDatasetBinding":
        return cls(
            id=data["id"],
            persona_id=data["persona_id"],
            dataset_id=data["dataset_id"],
            binding_keys=data.get("binding_keys", {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )
