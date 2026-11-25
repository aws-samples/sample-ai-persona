"""One advanced generation (beta) and its items (contract C5, entities.md).

The ``AdvancedGenerations`` table holds, under one partition per generation,
the generation itself, its progress events and the parts of a large result;
a separate partition per owner holds the lock that allows one running
generation per user (BR7.5). This module defines the plain values; the keys
and conditional writes live in ``AdvancedGenerationStore``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence

from ._advanced_common import drop_none, epoch, iso, parse_dt, parse_optional_dt
from .analysis import AnalysisPlan, AnalysisRun, DocumentMode

#: Version of the stored items (contract C5).
SCHEMA_VERSION = 1
#: Personas per generation (BR5.5).
PERSONA_COUNT_MIN = 1
PERSONA_COUNT_MAX = 10
#: Longest instruction (entities.md AdvancedGeneration.purpose).
PURPOSE_MAX_CHARS = 2000
#: Source files per generation (U1 uses one, BR5.8; the shape allows ten).
SOURCE_FILES_MAX = 10
#: Longest event summary (no row data or text goes in, NFR3).
EVENT_SUMMARY_MAX_CHARS = 500


class GenerationStatus(StrEnum):
    """States of a generation (functional-spec.md §3, U5 functional-spec.md §1).

    U5: ``awaiting_plan_approval`` sits between ``planning`` and ``running``
    when the user asked to confirm the plan; every running state and the wait
    may move to ``cancelled``.
    """

    PREPARING = "preparing"
    PLANNING = "planning"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    RUNNING = "running"
    SYNTHESIZING = "synthesizing"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Whether no further transition may happen."""
        return self in _TERMINAL


_TERMINAL = frozenset(
    {GenerationStatus.DONE, GenerationStatus.FAILED, GenerationStatus.CANCELLED}
)


class EventType(StrEnum):
    """Kinds of stored progress events (contract C3 / C5)."""

    STAGE = "stage"
    PROGRESS = "progress"
    PLAN = "plan"
    RESULT = "result"
    ERROR = "error"
    #: U5 BR31.2: the generation was cancelled (no payload)
    CANCELLED = "cancelled"


class ControlAction(StrEnum):
    """What a control request asks of a running generation (ADR-009)."""

    CANCEL = "cancel"
    APPROVE = "approve"
    REPLAN = "replan"


#: How a table reads: one row per person, or several rows per person.
READING_KINDS = ("people", "history")


@dataclass(frozen=True)
class TableReading:
    """How one prepared table is read (U6 addendum; shown on the plan card).

    The system proposes it from the column profile and the user may correct
    it before the analysis starts; what was confirmed is trusted afterwards.

    Attributes:
        alias: The table's alias in the run (``source_1``...).
        name: The file name shown to the user.
        kind: ``people`` (one row per person) or ``history`` (several rows
            per person, such as orders or events).
        person_id: The column that identifies a person, if any.
        skip: Columns that are no trait of a person (IDs, codes).
        columns: Every column of the table (what a choice may name).
        uncertain: The proposal could not be read from the names (no column
            that says who the person is); the card asks the user to check.
    """

    alias: str
    name: str
    kind: str
    person_id: str | None
    skip: tuple[str, ...]
    columns: tuple[str, ...]
    uncertain: bool = False

    def __post_init__(self) -> None:
        if self.kind not in READING_KINDS:
            raise ValueError("unknown table kind")
        object.__setattr__(self, "skip", tuple(self.skip))
        object.__setattr__(self, "columns", tuple(self.columns))
        names = set(self.columns)
        if self.person_id is not None and self.person_id not in names:
            raise ValueError("person_id is not a column")
        if any(c not in names for c in self.skip):
            raise ValueError("skip names a column the table does not have")

    def choose(
        self, kind: str, person_id: str | None, skip: Sequence[str]
    ) -> "TableReading":
        """The reading with the user's choice; a bad value is an error.

        The person's ID column is never a trait, so it is always in ``skip``.
        """
        left = [c for c in dict.fromkeys(skip) if c != person_id]
        if person_id is not None:
            left.insert(0, person_id)
        return replace(
            self, kind=kind, person_id=person_id, skip=tuple(left), uncertain=False
        )

    def to_dict(self) -> dict[str, Any]:
        return drop_none(
            {
                "alias": self.alias,
                "name": self.name,
                "kind": self.kind,
                "person_id": self.person_id,
                "skip": list(self.skip),
                "columns": list(self.columns),
                "uncertain": True if self.uncertain else None,
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TableReading":
        person = data.get("person_id")
        return cls(
            alias=str(data["alias"]),
            name=str(data.get("name", "")),
            kind=str(data["kind"]),
            person_id=str(person) if person is not None else None,
            skip=tuple(str(c) for c in data.get("skip") or []),
            columns=tuple(str(c) for c in data.get("columns") or []),
            uncertain=bool(data.get("uncertain")),
        )


@dataclass(frozen=True)
class ControlRequest:
    """One pending request on a generation (``control_request``, entities.md).

    Attributes:
        action: One of :class:`ControlAction`.
        requested_at: When the request was written.
        revised_purpose: The new instruction of a ``replan`` (may be empty).
        data_reading: The user's choices for the tables, ``{alias: {kind,
            person_id, skip}}`` (an approve or a replan; none = as proposed).
    """

    action: ControlAction
    requested_at: datetime
    revised_purpose: str | None = None
    data_reading: dict[str, dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", ControlAction(self.action))
        if (
            self.revised_purpose is not None
            and len(self.revised_purpose) > PURPOSE_MAX_CHARS
        ):
            raise ValueError("revised purpose is too long")

    def to_dict(self) -> dict[str, Any]:
        return drop_none(
            {
                "action": self.action.value,
                "requested_at": iso(self.requested_at),
                "revised_purpose": self.revised_purpose,
                "data_reading": self.data_reading,
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ControlRequest":
        revised = data.get("revised_purpose")
        reading = data.get("data_reading")
        return cls(
            action=ControlAction(str(data["action"])),
            requested_at=parse_dt(data["requested_at"]),
            revised_purpose=str(revised) if revised is not None else None,
            data_reading=(
                {str(k): dict(v) for k, v in dict(reading).items()}
                if isinstance(reading, Mapping)
                else None
            ),
        )


def _default_options() -> dict[str, Any]:
    # U6 BR35.2: ``use_detailed_analysis`` is decided at the start and kept
    return {"confirm_plan": False, "use_detailed_analysis": False}


class ResultPartName(StrEnum):
    """Lists of a large result written as separate items (contract C5)."""

    PREPARE_REPORTS = "prepare_reports"
    SEGMENTS = "segments"
    EVIDENCE = "evidence"
    #: U4: one DocumentReport per document (C8)
    DOCUMENT_REPORTS = "document_reports"


@dataclass(frozen=True)
class AdvancedGeneration:
    """One advanced generation (the ``GENERATION`` item of contract C5).

    Attributes:
        generation_id: UUID.
        owner_sub: The user who started it; every read checks it.
        status: One of :class:`GenerationStatus`.
        source_file_ids: The owner's ready tables (one in U1).
        purpose: The instruction (may be empty).
        persona_count: Personas to make (1〜10).
        created_at: Start time.
        expires_at: TTL in epoch seconds (48 hours from creation, rewritten to
            24 hours from finishing).
        plan: The analysis plan.
        part_counts: ``{name: n}`` of the stored result parts.
        analysis_run: Summary of the run.
        analysis_record_id: The analysis record in S3.
        candidate_ids: IDs in the existing candidate store.
        heartbeat_at: Last heartbeat of the running thread.
        error_code: ``ErrorCode`` value when failed.
        error_context: Values safe to show (limits); never row data or
            exception text.
        finished_at: When it ended.
        document_mode: ``full_text`` or ``extraction`` when documents are used
            (U4 BR25.2, decided right after the preparation).
        evidence_stats: ``{extracted, discarded_unverified}`` of the document
            evidence (U4 BR27.1).
        options: ``{confirm_plan, use_detailed_analysis}`` chosen at the start
            (U5 BR29.1, U6 BR35.2).
        control_request: The pending cancel / approve / replan (U5 BR29.3,
            BR31.1); removed once the run takes it.
        awaiting_since: When the current plan card was shown (only while
            ``awaiting_plan_approval``, U5 BR29.5).
        waited_seconds: Seconds spent waiting for plan approvals; they move
            the 60-minute deadline back (U5 BR29.6).
        replan_count: How many times the plan was made again (U5 BR29.4).
        original_purpose: The first instruction, kept at the first replan.
        session_id: The sandbox session, so a reader can stop it (U5 BR32.4).
            Never shown on the screen.
        data_reading: How each table is read, as proposed and then as the user
            confirmed it (U6 addendum).
    """

    generation_id: str
    owner_sub: str
    status: GenerationStatus
    source_file_ids: tuple[str, ...]
    purpose: str
    persona_count: int
    created_at: datetime
    expires_at: int
    plan: AnalysisPlan | None = None
    part_counts: dict[str, int] | None = None
    analysis_run: AnalysisRun | None = None
    analysis_record_id: str | None = None
    candidate_ids: tuple[str, ...] | None = None
    heartbeat_at: datetime | None = None
    error_code: str | None = None
    error_context: dict[str, Any] | None = None
    finished_at: datetime | None = None
    document_mode: DocumentMode | None = None
    evidence_stats: dict[str, int] | None = None
    options: dict[str, Any] = field(default_factory=_default_options)
    control_request: ControlRequest | None = None
    awaiting_since: datetime | None = None
    waited_seconds: int = 0
    replan_count: int = 0
    original_purpose: str | None = None
    session_id: str | None = None
    data_reading: tuple[TableReading, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", GenerationStatus(self.status))
        object.__setattr__(
            self, "options", {**_default_options(), **dict(self.options or {})}
        )
        if self.waited_seconds < 0 or self.replan_count < 0:
            raise ValueError("waited_seconds and replan_count are not negative")
        if (
            self.original_purpose is not None
            and len(self.original_purpose) > PURPOSE_MAX_CHARS
        ):
            raise ValueError("original purpose is too long")
        if self.document_mode is not None:
            object.__setattr__(self, "document_mode", DocumentMode(self.document_mode))
        object.__setattr__(self, "source_file_ids", tuple(self.source_file_ids))
        if self.candidate_ids is not None:
            object.__setattr__(self, "candidate_ids", tuple(self.candidate_ids))
        if not PERSONA_COUNT_MIN <= self.persona_count <= PERSONA_COUNT_MAX:
            raise ValueError("persona_count must be 1..10")
        if len(self.purpose) > PURPOSE_MAX_CHARS:
            raise ValueError("purpose is too long")
        if not 1 <= len(self.source_file_ids) <= SOURCE_FILES_MAX:
            raise ValueError("a generation uses 1..10 source files")

    @classmethod
    def create_new(
        cls,
        *,
        owner_sub: str,
        source_file_ids: Sequence[str],
        purpose: str,
        persona_count: int,
        now: datetime,
        ttl_seconds: int,
        generation_id: str | None = None,
        confirm_plan: bool = False,
        use_detailed_analysis: bool = False,
    ) -> "AdvancedGeneration":
        """A new generation in ``preparing`` that expires ``ttl_seconds`` later."""
        return cls(
            options={
                "confirm_plan": bool(confirm_plan),
                "use_detailed_analysis": bool(use_detailed_analysis),
            },
            generation_id=generation_id or str(uuid.uuid4()),
            owner_sub=owner_sub,
            status=GenerationStatus.PREPARING,
            source_file_ids=tuple(source_file_ids),
            purpose=purpose,
            persona_count=int(persona_count),
            created_at=now,
            expires_at=epoch(now) + int(ttl_seconds),
        )

    def update(self, **changes: Any) -> "AdvancedGeneration":
        """A copy with ``changes`` applied (the instance itself never changes)."""
        return replace(self, **changes)

    @property
    def confirm_plan(self) -> bool:
        """Whether the run stops for the user's plan approval (U5 BR29.1)."""
        return bool(self.options.get("confirm_plan"))

    @property
    def use_detailed_analysis(self) -> bool:
        """Whether AI-written code may run, decided at the start (U6 BR35.2)."""
        return bool(self.options.get("use_detailed_analysis"))

    def to_dict(self) -> dict[str, Any]:
        """Serialize the generation item without ``None`` values."""
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "generation_id": self.generation_id,
            "owner_sub": self.owner_sub,
            "status": self.status.value,
            "source_file_ids": list(self.source_file_ids),
            "purpose": self.purpose,
            "persona_count": int(self.persona_count),
            "created_at": iso(self.created_at),
            "expires_at": int(self.expires_at),
            "options": dict(self.options),
            "waited_seconds": int(self.waited_seconds),
            "replan_count": int(self.replan_count),
        }
        data.update(
            drop_none(
                {
                    "plan": self.plan.to_dict() if self.plan else None,
                    "part_counts": (
                        {k: int(v) for k, v in self.part_counts.items()}
                        if self.part_counts is not None
                        else None
                    ),
                    "analysis_run": (
                        self.analysis_run.to_dict() if self.analysis_run else None
                    ),
                    "analysis_record_id": self.analysis_record_id,
                    "candidate_ids": (
                        list(self.candidate_ids)
                        if self.candidate_ids is not None
                        else None
                    ),
                    "heartbeat_at": iso(self.heartbeat_at)
                    if self.heartbeat_at
                    else None,
                    "error_code": self.error_code,
                    "error_context": (
                        dict(self.error_context)
                        if self.error_context is not None
                        else None
                    ),
                    "finished_at": iso(self.finished_at) if self.finished_at else None,
                    "document_mode": (
                        self.document_mode.value if self.document_mode else None
                    ),
                    "evidence_stats": (
                        {k: int(v) for k, v in self.evidence_stats.items()}
                        if self.evidence_stats is not None
                        else None
                    ),
                    "control_request": (
                        self.control_request.to_dict() if self.control_request else None
                    ),
                    "awaiting_since": (
                        iso(self.awaiting_since) if self.awaiting_since else None
                    ),
                    "original_purpose": self.original_purpose,
                    "session_id": self.session_id,
                    "data_reading": (
                        [r.to_dict() for r in self.data_reading]
                        if self.data_reading is not None
                        else None
                    ),
                }
            )
        )
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdvancedGeneration":
        """Read an item written by :meth:`to_dict` (unknown fields are ignored)."""
        plan = data.get("plan")
        run = data.get("analysis_run")
        part_counts = data.get("part_counts")
        candidates = data.get("candidate_ids")
        error_context = data.get("error_context")
        mode = data.get("document_mode")
        stats = data.get("evidence_stats")
        options = data.get("options")
        request = data.get("control_request")
        original = data.get("original_purpose")
        session_id = data.get("session_id")
        reading = data.get("data_reading")
        return cls(
            data_reading=(
                tuple(TableReading.from_dict(r) for r in reading)
                if isinstance(reading, list)
                else None
            ),
            options=dict(options) if isinstance(options, Mapping) else {},
            control_request=(
                ControlRequest.from_dict(request)
                if isinstance(request, Mapping)
                else None
            ),
            awaiting_since=parse_optional_dt(data.get("awaiting_since")),
            waited_seconds=int(data.get("waited_seconds") or 0),
            replan_count=int(data.get("replan_count") or 0),
            original_purpose=str(original) if original is not None else None,
            session_id=str(session_id) if session_id is not None else None,
            generation_id=str(data["generation_id"]),
            owner_sub=str(data["owner_sub"]),
            status=GenerationStatus(str(data["status"])),
            source_file_ids=tuple(str(s) for s in data.get("source_file_ids", [])),
            purpose=str(data.get("purpose", "")),
            persona_count=int(data["persona_count"]),
            created_at=parse_dt(data["created_at"]),
            expires_at=int(data["expires_at"]),
            plan=AnalysisPlan.from_dict(plan) if plan else None,
            part_counts=(
                {str(k): int(v) for k, v in dict(part_counts).items()}
                if part_counts is not None
                else None
            ),
            analysis_run=AnalysisRun.from_dict(run) if run else None,
            analysis_record_id=data.get("analysis_record_id"),
            candidate_ids=(
                tuple(str(c) for c in candidates) if candidates is not None else None
            ),
            heartbeat_at=parse_optional_dt(data.get("heartbeat_at")),
            error_code=data.get("error_code"),
            error_context=dict(error_context) if error_context is not None else None,
            finished_at=parse_optional_dt(data.get("finished_at")),
            document_mode=DocumentMode(str(mode)) if mode else None,
            evidence_stats=(
                {str(k): int(v) for k, v in dict(stats).items()}
                if stats is not None
                else None
            ),
        )


@dataclass(frozen=True)
class GenerationEvent:
    """One stored progress event (contract C5 event_item; the SSE ``id``).

    Attributes:
        generation_id: The generation.
        sequence: 1, 2, 3 ... within the generation.
        event_type: One of :class:`EventType`.
        created_at: When it was written.
        summary: Short text or a JSON payload (500 characters; no row data).
    """

    generation_id: str
    sequence: int
    event_type: EventType
    created_at: datetime
    summary: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", EventType(self.event_type))
        if self.sequence < 1:
            raise ValueError("sequence starts at 1")

    @classmethod
    def create_new(
        cls,
        *,
        generation_id: str,
        sequence: int,
        event_type: EventType | str,
        now: datetime,
        summary: str | None = None,
    ) -> "GenerationEvent":
        """A new event with the summary cut to 500 characters."""
        return cls(
            generation_id=generation_id,
            sequence=int(sequence),
            event_type=EventType(event_type),
            created_at=now,
            summary=summary[:EVENT_SUMMARY_MAX_CHARS] if summary is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return drop_none(
            {
                "schema_version": SCHEMA_VERSION,
                "generation_id": self.generation_id,
                "sequence": int(self.sequence),
                "event_type": self.event_type.value,
                "summary": self.summary,
                "created_at": iso(self.created_at),
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GenerationEvent":
        return cls(
            generation_id=str(data["generation_id"]),
            sequence=int(data["sequence"]),
            event_type=EventType(str(data["event_type"])),
            created_at=parse_dt(data["created_at"]),
            summary=data.get("summary"),
        )


@dataclass(frozen=True)
class OwnerLock:
    """One running generation per user (contract C5 owner_lock_item, BR7.5).

    Attributes:
        owner_sub: The user.
        generation_id: The generation holding the lock.
        lock_until: The lock is free once this time has passed.
    """

    owner_sub: str
    generation_id: str
    lock_until: datetime

    def is_held(self, now: datetime) -> bool:
        return now < self.lock_until

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_sub": self.owner_sub,
            "generation_id": self.generation_id,
            "lock_until": iso(self.lock_until),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OwnerLock":
        return cls(
            owner_sub=str(data["owner_sub"]),
            generation_id=str(data["generation_id"]),
            lock_until=parse_dt(data["lock_until"]),
        )


@dataclass(frozen=True)
class ResultPart:
    """One slice of a large result list (contract C5 part_item).

    Attributes:
        name: One of :class:`ResultPartName`.
        index: 0, 1, 2 ... within the name.
        items: ``to_dict()`` values of the C8 models.
    """

    name: ResultPartName
    index: int
    items: tuple[dict[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", ResultPartName(self.name))
        object.__setattr__(self, "items", tuple(self.items))
        if self.index < 0:
            raise ValueError("index must not be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name.value,
            "index": int(self.index),
            "items": [dict(i) for i in self.items],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResultPart":
        return cls(
            name=ResultPartName(str(data["name"])),
            index=int(data["index"]),
            items=tuple(dict(i) for i in data.get("items", [])),
        )
