"""Generated personas waiting to be saved (U5, ADR-008, entities.md).

A generation writes one :class:`PersonaCandidate` per persona to the
``PersonaCandidates`` table, outside the application process, so that the save
request may land on another task (NFR9.27). Only the user who generated a
candidate may save it (BR1.3); the claim marks (``claimed_at`` /
``claim_token``) stop a double submit, and ``saved_at`` stops a second save
when the candidate could not be deleted after a successful save.

The types here are plain data: the conditional writes live in
``PersonaCandidateStore`` (Service layer), the rules in
``PersonaGenerationManager``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .dataset import DatasetColumn
from .persona import Persona

#: A claim older than this is treated as released (a crashed request, BR1.3).
CLAIM_TIMEOUT_SECONDS = 300
#: Largest item a candidate may serialize to (BR1.6, NFR2.12).
MAX_CANDIDATE_ITEM_BYTES = 350 * 1024
#: Key prefix of the temporary behaviour CSVs (the lifecycle rule's filter).
CANDIDATE_TEMP_PREFIX = "candidates"

#: Persona fields copied into ``persona_payload``. Identity, ownership, sharing,
#: avatar and the generation record are not part of the payload.
_PAYLOAD_FIELDS = (
    "name",
    "age",
    "occupation",
    "background",
    "values",
    "pain_points",
    "goals",
    "gender",
    "country",
    "city",
    "tags",
)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _epoch(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


@dataclass(frozen=True)
class BehaviorDatasetCandidate:
    """One behaviour CSV fetched for a DWH generation (BR1.2).

    The CSV itself stays in S3 under :attr:`temp_s3_key`; only its shape is
    kept here.

    Attributes:
        temp_id: ID the screen sends back when the user selects it.
        name: ``{persona name}_{data type}`` (numbered when repeated).
        data_type_label: Label of the data type.
        temp_s3_key: ``candidates/{owner_sub}/{candidate_id}/{temp_id}.csv``.
        columns: Columns of the CSV.
        row_count: Rows of the CSV (at least one).
        binding_key_column: Column that narrows rows to this persona.
        binding_key_value: Value of that column.
    """

    temp_id: str
    name: str
    data_type_label: str
    temp_s3_key: str
    columns: tuple[DatasetColumn, ...]
    row_count: int
    binding_key_column: str | None = None
    binding_key_value: str | None = None

    def __post_init__(self) -> None:
        if self.row_count < 1:
            raise ValueError("a behaviour dataset candidate needs at least one row")

    @staticmethod
    def temp_key(owner_sub: str, candidate_id: str, temp_id: str) -> str:
        """The temporary S3 key of one CSV (BR1.2, BR1.8)."""
        return f"{CANDIDATE_TEMP_PREFIX}/{owner_sub}/{candidate_id}/{temp_id}.csv"

    @property
    def binding_keys(self) -> dict[str, str]:
        """The binding keys of the dataset made from this CSV (may be empty)."""
        if self.binding_key_column and self.binding_key_value:
            return {self.binding_key_column: self.binding_key_value}
        return {}

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "temp_id": self.temp_id,
            "name": self.name,
            "data_type_label": self.data_type_label,
            "temp_s3_key": self.temp_s3_key,
            "columns": [
                {"name": c.name, "data_type": c.data_type, "description": c.description}
                for c in self.columns
            ],
            "row_count": int(self.row_count),
        }
        if self.binding_key_column is not None:
            data["binding_key_column"] = self.binding_key_column
        if self.binding_key_value is not None:
            data["binding_key_value"] = self.binding_key_value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BehaviorDatasetCandidate":
        return cls(
            temp_id=str(data["temp_id"]),
            name=str(data["name"]),
            data_type_label=str(data.get("data_type_label", "")),
            temp_s3_key=str(data["temp_s3_key"]),
            columns=tuple(
                DatasetColumn(
                    name=str(c["name"]),
                    data_type=str(c.get("data_type", "string")),
                    description=str(c.get("description", "")),
                )
                for c in data.get("columns", [])
            ),
            row_count=int(data["row_count"]),
            binding_key_column=data.get("binding_key_column"),
            binding_key_value=data.get("binding_key_value"),
        )


@dataclass(frozen=True)
class PersonaCandidate:
    """A generated persona that is not saved yet (entities.md PersonaCandidate).

    Attributes:
        candidate_id: ID shown to the screen; never reused as the saved
            persona's ID (BR1.5).
        owner_sub: The user who generated it.
        generation_id: Groups the candidates of one generation.
        persona_payload: The generated fields (see ``_PAYLOAD_FIELDS``).
        created_at: When it was generated.
        expires_at: Epoch seconds after which it cannot be saved (TTL).
        generation_log: The generation's thinking log.
        generation_context: The generation conditions (no file contents).
        behavior_datasets: Behaviour CSVs fetched for it.
        claimed_at: Set while a save is in progress (BR1.3).
        claim_token: Token of the request that holds the claim.
        saved_at: Set when a saved candidate could not be deleted.
    """

    candidate_id: str
    owner_sub: str
    generation_id: str
    persona_payload: dict[str, Any]
    created_at: datetime
    expires_at: int
    generation_log: list[dict[str, str]] | None = None
    generation_context: dict[str, Any] | None = None
    behavior_datasets: tuple[BehaviorDatasetCandidate, ...] = field(
        default_factory=tuple
    )
    claimed_at: datetime | None = None
    claim_token: str | None = None
    saved_at: datetime | None = None

    @classmethod
    def from_persona(
        cls,
        persona: Persona,
        *,
        owner_sub: str,
        generation_id: str,
        ttl_seconds: int,
        now: datetime,
        generation_log: list[dict[str, str]] | None = None,
        generation_context: dict[str, Any] | None = None,
        behavior_datasets: tuple[BehaviorDatasetCandidate, ...] = (),
    ) -> "PersonaCandidate":
        """Make the candidate of one generated persona (BR1.1).

        The generated persona's ID becomes the candidate ID; the saved persona
        gets a new ID later (BR1.5).
        """
        payload = {
            key: value
            for key, value in persona.to_dict().items()
            if key in _PAYLOAD_FIELDS and value is not None
        }
        return cls(
            candidate_id=persona.id,
            owner_sub=owner_sub,
            generation_id=generation_id,
            persona_payload=payload,
            created_at=now,
            expires_at=_epoch(now) + int(ttl_seconds),
            generation_log=generation_log,
            generation_context=generation_context,
            behavior_datasets=tuple(behavior_datasets),
        )

    def is_expired(self, now: datetime) -> bool:
        """Whether ``now`` is at or past the expiry (BR1.8)."""
        return _epoch(now) >= self.expires_at

    def claim_is_active(self, now: datetime) -> bool:
        """Whether a claim is held and younger than five minutes (BR1.3)."""
        if self.claimed_at is None:
            return False
        return now - self.claimed_at < timedelta(seconds=CLAIM_TIMEOUT_SECONDS)

    def claimed(self, *, token: str, at: datetime) -> "PersonaCandidate":
        """A copy holding the claim of ``token``."""
        return replace(self, claimed_at=at, claim_token=token)

    def mark_saved(self, at: datetime) -> "PersonaCandidate":
        """A copy marked as saved."""
        return replace(self, saved_at=at)

    @property
    def persona_name(self) -> str:
        """The generated name (for results and logs shown to the owner)."""
        return str(self.persona_payload.get("name", ""))

    def behavior_dataset(self, temp_id: str) -> BehaviorDatasetCandidate | None:
        """The behaviour CSV with ``temp_id``, or ``None`` (BR1.5)."""
        return next((b for b in self.behavior_datasets if b.temp_id == temp_id), None)

    def to_persona(self) -> Persona:
        """A new, unsaved persona with the payload and the generation record.

        It has a fresh ID and no owner; ``PersonaManager.save_persona`` sets the
        owner (BR1.4, BR1.5).
        """
        p = self.persona_payload
        persona = Persona.create_new(
            name=str(p.get("name", "")),
            age=int(p.get("age", 0)),
            occupation=str(p.get("occupation", "")),
            background=str(p.get("background", "")),
            values=list(p.get("values", [])),
            pain_points=list(p.get("pain_points", [])),
            goals=list(p.get("goals", [])),
            gender=p.get("gender"),
            country=p.get("country"),
            city=p.get("city"),
            tags=list(p.get("tags", [])),
        )
        return replace(
            persona,
            generation_log=self.generation_log,
            generation_context=self.generation_context,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize without ``None`` values."""
        data: dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "owner_sub": self.owner_sub,
            "generation_id": self.generation_id,
            "persona_payload": dict(self.persona_payload),
            "created_at": _iso(self.created_at),
            "expires_at": int(self.expires_at),
        }
        if self.generation_log is not None:
            data["generation_log"] = list(self.generation_log)
        if self.generation_context is not None:
            data["generation_context"] = dict(self.generation_context)
        if self.behavior_datasets:
            data["behavior_datasets"] = [b.to_dict() for b in self.behavior_datasets]
        if self.claimed_at is not None:
            data["claimed_at"] = _iso(self.claimed_at)
        if self.claim_token is not None:
            data["claim_token"] = self.claim_token
        if self.saved_at is not None:
            data["saved_at"] = _iso(self.saved_at)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PersonaCandidate":
        created_at = _parse(data.get("created_at"))
        if created_at is None:
            raise ValueError("candidate has no created_at")
        return cls(
            candidate_id=str(data["candidate_id"]),
            owner_sub=str(data["owner_sub"]),
            generation_id=str(data["generation_id"]),
            persona_payload=dict(data.get("persona_payload") or {}),
            created_at=created_at,
            expires_at=int(data["expires_at"]),
            generation_log=data.get("generation_log"),
            generation_context=data.get("generation_context"),
            behavior_datasets=tuple(
                BehaviorDatasetCandidate.from_dict(b)
                for b in data.get("behavior_datasets") or []
            ),
            claimed_at=_parse(data.get("claimed_at")),
            claim_token=data.get("claim_token"),
            saved_at=_parse(data.get("saved_at")),
        )


@dataclass(frozen=True)
class CandidateSaveResult:
    """Outcome of saving claimed candidates (BR1.9, WF2).

    ``PersonaManager.save_generated_personas`` returns it and
    ``PersonaGenerationManager.finish_candidates`` cleans up from it; the
    Router only hands it over.

    Attributes:
        saved_ids: Candidates saved as personas (deleted afterwards).
        failed_ids: Candidates that could not be saved (released afterwards).
        saved_persona_ids: IDs of the new personas, in saved order.
        failed_names: Names of the candidates that failed (shown to the owner).
        failed_datasets: Names of behaviour datasets that could not be made.
    """

    saved_ids: tuple[str, ...] = ()
    failed_ids: tuple[str, ...] = ()
    saved_persona_ids: tuple[str, ...] = ()
    failed_names: tuple[str, ...] = ()
    failed_datasets: tuple[str, ...] = ()

    @property
    def saved_count(self) -> int:
        return len(self.saved_ids)


@dataclass(frozen=True)
class CandidateClaim:
    """The candidates one save request holds (BR1.3).

    Attributes:
        token: The request's claim token; cleanup only touches items holding it.
        owner_sub: The user who claimed them.
        candidates: The claimed candidates, in the requested order.
    """

    token: str
    owner_sub: str
    candidates: tuple[PersonaCandidate, ...]

    @staticmethod
    def new_token() -> str:
        """A fresh claim token (unguessable per request)."""
        return secrets.token_urlsafe(16)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(c.candidate_id for c in self.candidates)

    def get(self, candidate_id: str) -> PersonaCandidate | None:
        return next(
            (c for c in self.candidates if c.candidate_id == candidate_id), None
        )
