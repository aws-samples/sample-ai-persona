"""
Custom persona data (an organization asset) and how a survey points to it (U7).

``CustomPersonaData`` is the registration record of one imported persona file
(``CustomPersonaData`` table, entities.md). The file itself lives in S3 under a
key made from the ID, never from the name (BR4.3, NFR1.82). It has no owner;
``shared_group_ids`` is its usage scope and an empty scope means admin only
(BR5.1). Names are unique in the table: a reservation item whose ``id`` is
``name#{name}`` is written in the same transaction as the record (BR4.4).

``PersonaDataRef`` is the ``datasource`` string of a survey: ``nemotron`` or
``custom:{id}`` (BR4.3).
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence
import uuid

from .errors import CodedError, ErrorCode
from .uploaded_file import is_uuid

#: Longest name after stripping (BR4.4).
NAME_MAX_LENGTH = 100
#: ``id`` prefix of a name reservation item (BR4.4, NFR1.74).
NAME_RESERVATION_PREFIX = "name#"
#: S3 prefix of the imported files.
CUSTOM_FILE_PREFIX = "persona-dataset/custom/"
#: ``datasource`` value of the built-in dataset.
NEMOTRON = "nemotron"
#: ``datasource`` prefix of a custom persona data reference.
CUSTOM_PREFIX = "custom:"


def normalize_name(name: Optional[str]) -> str:
    """Strip ``name`` and check its length (BR4.4).

    Raises:
        CodedError: ``CUSTOM_PERSONA_DATA_NAME_INVALID`` when it is blank or
            longer than :data:`NAME_MAX_LENGTH` characters.
    """
    stripped = (name or "").strip()
    if not 1 <= len(stripped) <= NAME_MAX_LENGTH:
        raise CodedError(
            f"name has {len(stripped)} characters (1-{NAME_MAX_LENGTH})",
            code=ErrorCode.CUSTOM_PERSONA_DATA_NAME_INVALID,
            context={"max_length": NAME_MAX_LENGTH},
        )
    return stripped


def reservation_id(name: str) -> str:
    """The ``id`` of the name reservation item (case-sensitive, stripped)."""
    return f"{NAME_RESERVATION_PREFIX}{name.strip()}"


def is_name_reservation(item_id: Optional[str]) -> bool:
    """Whether ``item_id`` is a name reservation, not a record (NFR1.74)."""
    return bool(item_id) and str(item_id).startswith(NAME_RESERVATION_PREFIX)


def custom_file_key(data_id: str) -> str:
    """The S3 key of the imported file of ``data_id`` (BR4.3)."""
    return f"{CUSTOM_FILE_PREFIX}{data_id}.parquet"


@dataclass(frozen=True)
class ExtraColumn:
    """A column outside the standard ones (the ``extra_columns`` of meta.json).

    Attributes:
        csv_column: Column name in the imported file.
        label: Label shown to the persona; the column name when empty.
        description: What the values mean.
    """

    csv_column: str
    label: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, str]:
        """The shape the persona prompt reads (empty label left out)."""
        data = {"csv_column": self.csv_column}
        if self.label:
            data["label"] = self.label
        data["description"] = self.description
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExtraColumn":
        """Read one entry (``name`` is accepted for ``csv_column``)."""
        return cls(
            csv_column=str(data.get("csv_column") or data.get("name") or ""),
            label=str(data.get("label") or ""),
            description=str(data.get("description") or ""),
        )


@dataclass(frozen=True)
class CustomPersonaData:
    """The registration record of imported persona data (entities.md).

    Attributes:
        id: UUID, new for every registration.
        name: Display name, unique in the table.
        s3_key: ``persona-dataset/custom/{id}.parquet``.
        columns: Column names after the import (display only).
        row_count: Rows imported.
        registered_by: ``sub`` of the admin (display only, never a decision).
        registered_at: When it was registered.
        standard_mapping: Standard column -> file column.
        extra_columns: Columns outside the standard ones.
        shared_group_ids: Usage scope; empty means admin only.
        created_at: Same as ``registered_at`` (the common lookup column).
    """

    id: str
    name: str
    s3_key: str
    columns: tuple[str, ...]
    row_count: int
    registered_by: str
    registered_at: datetime
    standard_mapping: Mapping[str, str] = field(default_factory=dict)
    extra_columns: tuple[ExtraColumn, ...] = ()
    shared_group_ids: frozenset[str] = frozenset()
    created_at: Optional[datetime] = None

    @classmethod
    def create_new(
        cls,
        *,
        name: str,
        columns: Sequence[str],
        row_count: int,
        registered_by: str,
        standard_mapping: Optional[Mapping[str, str]] = None,
        extra_columns: Sequence[ExtraColumn] = (),
        shared_group_ids: frozenset[str] = frozenset(),
        now: Optional[datetime] = None,
    ) -> "CustomPersonaData":
        """A new record with a new ID (BR4.3, BR4.5).

        Raises:
            CodedError: ``CUSTOM_PERSONA_DATA_NAME_INVALID``.
        """
        data_id = str(uuid.uuid4())
        at = now or datetime.now()
        return cls(
            id=data_id,
            name=normalize_name(name),
            s3_key=custom_file_key(data_id),
            columns=tuple(str(c) for c in columns),
            row_count=int(row_count),
            registered_by=registered_by,
            registered_at=at,
            standard_mapping=dict(standard_mapping or {}),
            extra_columns=tuple(extra_columns),
            shared_group_ids=frozenset(shared_group_ids),
            created_at=at,
        )

    def renamed(self, name: str) -> "CustomPersonaData":
        """Return a copy with a new name (BR4.4).

        Raises:
            CodedError: ``CUSTOM_PERSONA_DATA_NAME_INVALID``.
        """
        return replace(self, name=normalize_name(name))

    def extra_column_dicts(self) -> list[dict[str, str]]:
        """``extra_columns`` in the shape ``build_persona_system_prompt`` reads."""
        return [column.to_dict() for column in self.extra_columns]

    def to_dict(self) -> dict[str, Any]:
        """The item to store (an empty scope is not stored, contract C8)."""
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "s3_key": self.s3_key,
            "columns": list(self.columns),
            "row_count": self.row_count,
            "registered_by": self.registered_by,
            "registered_at": self.registered_at.isoformat(),
            "standard_mapping": dict(self.standard_mapping),
            "extra_columns": self.extra_column_dicts(),
            "created_at": (self.created_at or self.registered_at).isoformat(),
        }
        if self.shared_group_ids:
            data["shared_group_ids"] = sorted(self.shared_group_ids)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CustomPersonaData":
        """Read a stored item (DynamoDB sets and numbers accepted)."""
        registered_at = datetime.fromisoformat(str(data["registered_at"]))
        created = data.get("created_at")
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            s3_key=str(data.get("s3_key") or custom_file_key(str(data["id"]))),
            columns=tuple(str(c) for c in data.get("columns") or ()),
            row_count=int(data.get("row_count") or 0),
            registered_by=str(data.get("registered_by") or ""),
            registered_at=registered_at,
            standard_mapping={
                str(k): str(v) for k, v in (data.get("standard_mapping") or {}).items()
            },
            extra_columns=tuple(
                ExtraColumn.from_dict(c) for c in data.get("extra_columns") or ()
            ),
            shared_group_ids=frozenset(data.get("shared_group_ids") or ()),
            created_at=datetime.fromisoformat(str(created))
            if created
            else registered_at,
        )


@dataclass(frozen=True)
class PersonaDataListing:
    """What the persona data screen lists for one user (BR4.1, BR4.2).

    Attributes:
        custom: The custom data the user may use (every record for admins).
        manageable: The user may import, delete and change scopes.
        scope_labels: Record id -> names of its scope's departments (admins).
    """

    custom: tuple[CustomPersonaData, ...]
    manageable: bool
    scope_labels: Mapping[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class PersonaDataRef:
    """Which persona data a survey uses: ``nemotron`` or ``custom:{id}`` (BR4.3)."""

    custom_id: Optional[str] = None

    @classmethod
    def parse(cls, value: Optional[str]) -> "PersonaDataRef":
        """Read a ``datasource`` value; empty means ``nemotron``.

        Raises:
            ValueError: Anything other than ``nemotron`` or ``custom:{uuid}``
                (old ``custom:{name}`` values included).
        """
        if not value or value == NEMOTRON:
            return cls()
        if value.startswith(CUSTOM_PREFIX):
            data_id = value[len(CUSTOM_PREFIX) :]
            if is_uuid(data_id):
                return cls(custom_id=data_id)
        raise ValueError("datasource must be 'nemotron' or 'custom:{id}'")

    @classmethod
    def custom(cls, data_id: str) -> "PersonaDataRef":
        """The reference to custom persona data ``data_id``."""
        return cls.parse(f"{CUSTOM_PREFIX}{data_id}")

    @property
    def is_nemotron(self) -> bool:
        """Whether this is the built-in dataset."""
        return self.custom_id is None

    @property
    def value(self) -> str:
        """The ``datasource`` string."""
        return (
            NEMOTRON if self.custom_id is None else f"{CUSTOM_PREFIX}{self.custom_id}"
        )
