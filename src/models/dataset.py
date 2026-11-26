"""
Dataset data model for external data integration.
"""

from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid


@dataclass
class DatasetColumn:
    """Column information of a dataset."""

    name: str
    data_type: str  # string, integer, float, date, datetime, boolean
    description: str = ""


@dataclass
class Dataset:
    """Information about an external dataset (CSV file)."""

    id: str
    name: str
    description: str
    s3_path: str
    columns: List[DatasetColumn]
    row_count: int
    created_at: datetime
    updated_at: datetime
    notes: str = ""
    # For a persona-dedicated dataset, the owning persona's ID; None for an
    # organization asset. Not changed after creation
    owner_persona_id: Optional[str] = None
    # Usage scope of an organization asset. When empty, only admins can use it. Not written for persona-dedicated datasets
    shared_group_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_org_asset(self) -> bool:
        """Whether this is an organization asset (not persona-dedicated)."""
        return not self.owner_persona_id

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
        """Whether this dataset belongs to ``persona_id``."""
        return bool(self.owner_persona_id) and self.owner_persona_id == persona_id

    def to_dict(self) -> Dict[str, Any]:
        data = self._base_dict()
        if self.owner_persona_id:
            data["owner_persona_id"] = self.owner_persona_id
        if self.shared_group_ids:
            data["shared_group_ids"] = sorted(self.shared_group_ids)
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
            shared_group_ids=frozenset(data.get("shared_group_ids") or ()),
        )


# The persona-to-dataset binding is held by PersonaBinding (PersonaBindings
# table) in src/models/persona_binding.py.
