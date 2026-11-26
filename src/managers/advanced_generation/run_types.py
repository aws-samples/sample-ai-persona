"""The data of the advanced generation: one run's state and what the screen reads."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Optional

from ...config import config
from ...models.advanced_generation import (
    AdvancedGeneration,
    ControlRequest,
    GenerationEvent,
    GenerationStatus,
    TableReading,
)
from ...models.analysis import (
    AnalysisPlan,
    DocumentMode,
    DocumentReport,
    Evidence,
    JoinPlan,
    PrepareReport,
    Segment,
)
from ...models.errors import CodedError, ErrorCode
from ...models.persona_candidate import PersonaCandidate
from ...models.source_file import SourceFile
from .detailed_analysis import RunCodeBudget

if TYPE_CHECKING:
    from ...services.sandbox_service import SandboxSession


@dataclass(frozen=True)
class GenerationProgress:
    """What the progress stream reads at one moment."""

    generation: AdvancedGeneration
    events: tuple[GenerationEvent, ...]
    #: The stored failure values and, for a document's failure, the owner's
    #: file name found again from ``source_index``.
    failure_context: dict[str, Any] = field(default_factory=dict)

    @property
    def is_finished(self) -> bool:
        return self.generation.status.is_terminal


@dataclass(frozen=True)
class AdvancedGenerationResult:
    """The finished result shown in the result section."""

    generation: AdvancedGeneration
    prepare_reports: tuple[PrepareReport, ...]
    segments: tuple[Segment, ...]
    evidence: tuple[Evidence, ...]
    candidates: tuple[PersonaCandidate, ...]
    unverified_segment_ids: tuple[str, ...]
    #: One report per document, and the display name of each source file.
    document_reports: tuple[DocumentReport, ...] = ()
    source_names: dict[str, str] = field(default_factory=dict)


class NoTools:
    """Stands for the analysis tools when there is no table (documents only)."""

    calls: tuple[Any, ...] = ()
    sandbox_error: Optional[CodedError] = None

    @staticmethod
    def tool_call_ids() -> list[str]:
        return []

    @staticmethod
    def output_of(_call_id: str) -> None:
        return None


@dataclass
class Table:
    """One prepared table of the run, named in the order chosen."""

    alias: str
    source: SourceFile
    report: PrepareReport
    profile: dict[str, Any]


@dataclass
class Run:
    """The state of one ``run`` (one thread)."""

    generation: AdvancedGeneration
    started_monotonic: float
    status: GenerationStatus = GenerationStatus.PREPARING
    session: Optional["SandboxSession"] = None
    #: The table (none when the generation uses documents alone).
    source: Optional[SourceFile] = None
    #: Every source file, in ``source_file_ids`` order.
    sources: list[SourceFile] = field(default_factory=list)
    report: Optional[PrepareReport] = None
    #: ``(source_index, file, report)`` of each prepared document.
    documents: list[tuple[int, SourceFile, DocumentReport]] = field(
        default_factory=list
    )
    document_mode: Optional[DocumentMode] = None
    #: Full text of each document, in ``documents`` order (``full_text`` only).
    document_texts: list[str] = field(default_factory=list)
    #: Document evidence that passed the quote check.
    document_evidence: list[Evidence] = field(default_factory=list)
    evidence_stats: dict[str, int] = field(default_factory=dict)
    evidence_tokens: dict[str, int] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    plan: Optional[AnalysisPlan] = None
    tools: Any = None
    segments: list[Segment] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    stage_started: dict[str, float] = field(default_factory=dict)
    durations_ms: dict[str, int] = field(default_factory=dict)
    error_code: Optional[ErrorCode] = None
    cause: Optional[str] = None
    #: DuckDB's memory limit the preparation reported (for the log only).
    memory_limit: Optional[str] = None
    #: Seconds already waited for plan approvals and replans.
    waited_seconds: int = 0
    replan_count: int = 0
    #: When the current plan card was shown (``awaiting_plan_approval``).
    awaiting_since: Optional[datetime] = None
    #: The watch saw a cancel request ...
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    cancel_request: Optional[ControlRequest] = None
    #: ... or that a reader ended the generation.
    ended_elsewhere: threading.Event = field(default_factory=threading.Event)
    #: Set before the run writes its own end, so the watch ignores that end.
    ending: threading.Event = field(default_factory=threading.Event)
    #: Consecutive failures of the watch's read and of its heartbeat.
    read_failures: int = 0
    beat_failures: int = 0
    #: The end (lock, marks, ``finished_at``) is already written.
    finished: bool = False
    #: Every table, in the order chosen (``source_1`` to ``source_3``).
    tables: list[Table] = field(default_factory=list)
    #: The join plans and the axis people each matched.
    joins: list[tuple[JoinPlan, int]] = field(default_factory=list)
    #: The run_code budget (the gate the tools ask).
    budget: Optional[RunCodeBudget] = None
    #: Why detailed analysis was not offered after all
    detailed_fallback: Optional[str] = None
    #: How each table reads (proposed, then as the user confirmed).
    reading: tuple[TableReading, ...] = ()

    @property
    def deadline(self) -> datetime:
        """60 minutes from the start, moved back by the plan waits."""
        return self.generation.created_at + timedelta(
            seconds=config.ADVANCED_GENERATION_TIMEOUT_SECONDS + self.waited_seconds
        )


@dataclass(frozen=True)
class ElapsedClock:
    """Seconds since the generation started, and whether they still grow."""

    seconds: int
    running: bool
