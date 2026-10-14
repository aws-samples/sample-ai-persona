"""Bindings of a persona to organization assets (U5, ADR-007, entities.md).

The two former binding tables (datasets, knowledge bases) are merged into one
``PersonaBindings`` table: PK ``persona_id``, SK ``target_key``. A dataset
binding's key is ``DATASET#{dataset_id}``; the knowledge-base binding uses the
fixed key ``KB`` so a persona has at most one and replacing it is a single
write (BR2.9). The reverse index ``TargetIndex`` is keyed by ``target_id``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

#: Sort key of the one knowledge-base binding of a persona (BR2.9).
KB_TARGET_KEY = "KB"
_DATASET_PREFIX = "DATASET#"


class BindingTargetType(StrEnum):
    """What a binding points at."""

    DATASET = "DATASET"
    KB = "KB"


def dataset_target_key(dataset_id: str) -> str:
    """Sort key of the binding to ``dataset_id``."""
    return f"{_DATASET_PREFIX}{dataset_id}"


@dataclass(frozen=True)
class PersonaBinding:
    """One binding (entities.md PersonaBinding).

    Attributes:
        persona_id: The persona.
        target_type: ``DATASET`` or ``KB``.
        target_id: ``Dataset.id`` or ``KnowledgeBase.id``.
        created_at: When it was written.
        binding_keys: For datasets, the column/value that narrows rows.
        metadata_filters: For knowledge bases, the search filters.
        dedicated: For datasets, whether the dataset belongs to this persona
            (made from a behaviour CSV on save, BR6.1). Display only; the
            dataset's ``owner_persona_id`` stays the source of truth.
    """

    persona_id: str
    target_type: BindingTargetType
    target_id: str
    created_at: datetime
    binding_keys: dict[str, str] = field(default_factory=dict)
    metadata_filters: dict[str, str] = field(default_factory=dict)
    dedicated: bool = False

    @classmethod
    def for_dataset(
        cls,
        persona_id: str,
        dataset_id: str,
        *,
        binding_keys: Mapping[str, str] | None = None,
        dedicated: bool = False,
        now: datetime | None = None,
    ) -> "PersonaBinding":
        """A binding to a dataset."""
        return cls(
            persona_id=persona_id,
            target_type=BindingTargetType.DATASET,
            target_id=dataset_id,
            created_at=now or datetime.now(),
            binding_keys=dict(binding_keys or {}),
            dedicated=dedicated,
        )

    @classmethod
    def for_kb(
        cls,
        persona_id: str,
        kb_id: str,
        *,
        metadata_filters: Mapping[str, str] | None = None,
        now: datetime | None = None,
    ) -> "PersonaBinding":
        """The knowledge-base binding (fixed key ``KB``)."""
        return cls(
            persona_id=persona_id,
            target_type=BindingTargetType.KB,
            target_id=kb_id,
            created_at=now or datetime.now(),
            metadata_filters=dict(metadata_filters or {}),
        )

    @property
    def target_key(self) -> str:
        """The sort key: ``DATASET#{id}`` or ``KB``."""
        if self.target_type is BindingTargetType.KB:
            return KB_TARGET_KEY
        return dataset_target_key(self.target_id)

    @property
    def is_dataset(self) -> bool:
        return self.target_type is BindingTargetType.DATASET

    def to_dict(self) -> dict[str, Any]:
        """Serialize without ``None`` values or empty maps."""
        data: dict[str, Any] = {
            "persona_id": self.persona_id,
            "target_key": self.target_key,
            "target_type": self.target_type.value,
            "target_id": self.target_id,
            "created_at": self.created_at.isoformat(),
        }
        if self.binding_keys:
            data["binding_keys"] = dict(self.binding_keys)
        if self.metadata_filters:
            data["metadata_filters"] = dict(self.metadata_filters)
        if self.dedicated:
            data["dedicated"] = True
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PersonaBinding":
        """Read a stored item.

        Raises:
            ValueError: An unknown ``target_type``.
        """
        target_type = BindingTargetType(str(data["target_type"]))
        return cls(
            persona_id=str(data["persona_id"]),
            target_type=target_type,
            target_id=str(data["target_id"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            binding_keys={
                str(k): str(v) for k, v in (data.get("binding_keys") or {}).items()
            },
            metadata_filters={
                str(k): str(v) for k, v in (data.get("metadata_filters") or {}).items()
            },
            dedicated=bool(data.get("dedicated", False)),
        )
