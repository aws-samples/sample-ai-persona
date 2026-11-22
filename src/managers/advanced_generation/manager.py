"""The advanced generation (beta) of personas from one table (U1, W4〜W6).

``start_generation`` checks the request and writes the owner's lock, the
generation and the file's in-use mark in one transaction (BR7.5, BR6.4);
the Router then hands ``run`` to its own thread pool. ``run`` goes through
the stages in order and never raises (functional-spec.md §3)::

    preparing -> planning -> running -> synthesizing -> verifying -> done
                     (any stage) -> failed

- One sandbox session per generation, always stopped at the end (BR6.1).
  The table goes in through a 15-minute presigned GET made here (BR6.2);
  a refused download gets one new URL. The URL is never logged or stored.
- Every transition is conditioned on the current state (BR7.1); every stage
  writes a ``stage`` event and a heartbeat runs every 30 seconds (BR7.3).
- The whole generation has 60 minutes; no model stage starts with less than
  10 minutes left (reliability-design.md §2, NFR5.6).
- The end releases the lock and the mark, writes ``finished_at`` (results
  readable 24 hours) and returns the slot, each step on its own (NFR5.15).
- Free slots are a ``BoundedSemaphore`` of ``ADVANCED_GENERATION_MAX_CONCURRENT``
  (scalability-design.md §1); a full task answers ``ADVANCED_GENERATION_BUSY``.

Failures keep the error code and safe values only, never row data or
exception text (NFR3.6); logs go through the allowed-field list (NFR6.1).

U5 (the generation's controls, ADR-009): requests travel through the store,
so whichever task receives them, the task running the generation takes them.

- ``confirm_plan`` stops the run in ``awaiting_plan_approval`` after the plan;
  approve, replan (no limit) or cancel; 15 minutes per card, 60 in total, and
  the waits move the 60-minute deadline back (BR29).
- A watch thread reads the generation every ``control_poll_seconds`` (and
  writes the heartbeat); the run checks at its boundaries and stops on a
  cancel, or quietly when someone else ended it (BR31.2, BR32.5). Right
  before the results it reads once more, and ``done`` loses to a pending
  cancel; a lost ``done`` removes the candidates and the record again.
- Every end (done, failed, cancelled) releases the lock and the marks before
  its event is written and before the session stops, so the same files can
  start again at once; a start also frees a lock left by an ended generation.
- Readers (the stream, the page, the start, the requests) end a generation
  whose heartbeat is 5 minutes old, or whose plan wait ran out (BR32.1, BR32.2).
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

from ...config import config
from ...models.advanced_generation import (
    PERSONA_COUNT_MAX,
    PERSONA_COUNT_MIN,
    PURPOSE_MAX_CHARS,
    AdvancedGeneration,
    ControlAction,
    ControlRequest,
    EventType,
    GenerationEvent,
    GenerationStatus,
    ResultPartName,
)
from ...models.analysis import (
    AnalysisMethod,
    AnalysisPlan,
    AnalysisRecord,
    DocumentMode,
    DocumentReport,
    Evidence,
    PrepareReport,
    Segment,
)
from ...models.authz import Principal
from ...models.errors import CodedError, ErrorCode, ErrorKind
from ...models.persona_candidate import MAX_CANDIDATE_ITEM_BYTES, PersonaCandidate
from ...models.source_file import SourceFile, SourceKind
from ...services.s3_service import S3ServiceError
from ...services.service_factory import service_factory
from ...services.stores.advanced_generation_store import (
    AdvancedGenerationStore,
    AdvancedGenerationStoreError,
)
from ...services.stores.persona_candidate_store import PersonaCandidateStore
from ..components.access_policy import AccessPolicy
from ..components.advanced_generation_policy import AdvancedGenerationPolicy
from ..components.analysis_record_archive import AnalysisRecordArchive
from ..components.source_file_catalog import SourceFileCatalog
from ..shared.advanced_generation_log import log_advanced_event
from . import documents, prompts
from .analysis_policy import PLAN_ATTEMPTS, AnalysisPolicy
from .analysis_verifier import AnalysisVerifier, VerificationResult
from .results import build_analysis_run, build_candidates, build_record

if TYPE_CHECKING:
    from ...services.agent_service import AgentService
    from ...services.s3_service import S3Service
    from ...services.sandbox_service import SandboxService, SandboxSession
    from ...services.stores.app_settings_store import AppSettingsStore
    from ...services.stores.source_file_store import SourceFileStore

logger = logging.getLogger(__name__)

#: ``cause`` of a sandbox failure (the same value SandboxService uses).
_CAUSE_SANDBOX = "code_interpreter"
#: Library error codes -> what the user is told (functional-spec.md §5).
_LIBRARY_CODES = {
    "SOURCE_UNREADABLE": ErrorCode.GENERATION_SOURCE_UNREADABLE,
    "TOO_MANY_BAD_ROWS": ErrorCode.GENERATION_SOURCE_UNREADABLE,
    "OUT_OF_MEMORY": ErrorCode.GENERATION_CAPACITY,
    # U4 (BR24.4, BR24.6)
    "DOCUMENT_NO_TEXT": ErrorCode.DOCUMENT_NO_TEXT,
    "DOCUMENT_ENCRYPTED": ErrorCode.DOCUMENT_ENCRYPTED,
}
_RESELECT_CODES = frozenset({"TOO_FEW_COLUMNS", "EMPTY_CLUSTER"})
#: Values of ``error_context`` that may be kept and shown (never text).
_SAFE_CONTEXT_KEYS = frozenset(
    {
        "cause",
        "http_status",
        "max_chars",
        "limit",
        "value",
        # U3: counts and line numbers, integers only (NFR3.20)
        "rows_read",
        "rows_excluded",
        "columns_count",
        "row_examples",
        "required_rows",
        "persona_count",
        # U4: the document's place and the character counts (NFR3.22)
        "source_index",
        "total_chars",
    }
)
_COUNT_KEYS = frozenset(
    {
        "rows_read",
        "rows_excluded",
        "columns_count",
        "required_rows",
        "persona_count",
        "source_index",
        "total_chars",
        "max_chars",
    }
)
#: Seconds between two "根拠の抽出 n / M" notices when 10% is not reached.
_EXTRACTION_NOTICE_SECONDS = 30
#: Room kept under the 2MB of the result parts for the item keys (NFR5.38).
_PARTS_SLACK_BYTES = 16 * 1024
#: At most this many line numbers are kept in ``row_examples`` (BR16.2).
_ROW_EXAMPLES_MAX = 5
#: The two time limits, told apart in ``error_context`` (NFR1.6, NFR5.6, NFR5.7).
_LIMIT_GENERATION = "generation_minutes"
_LIMIT_PREPARE = "prepare_minutes"
#: U5: one plan card (BR29.5) and every card together (BR29.7).
_LIMIT_PLAN_WAIT = "plan_wait_minutes"
_LIMIT_PLAN_WAIT_TOTAL = "plan_wait_total_minutes"
#: U5: attributes that mean nothing once the generation has ended.
_ENDED_REMOVES = ("control_request", "awaiting_since")
#: U5: the states a cancel may be written in (BR31.1).
_RUNNING_STATES = tuple(s for s in GenerationStatus if not s.is_terminal)
#: U5: the actions the plan card sends (a cancel has its own route).
_PLAN_DECISIONS = frozenset({ControlAction.APPROVE.value, ControlAction.REPLAN.value})
_EVENT_TEXT_MAX = 150
#: Wait before the one re-send of a failed copy of the source (NFR5.30).
_REFRESH_RETRY_SECONDS = 1.0


class AdvancedGenerationError(CodedError):
    """A refused or failed advanced generation request."""


class _StopRun(Exception):
    """Internal: the run cannot go on (a transition was lost); no failure write."""


class _Cancelled(Exception):
    """Internal: the user cancelled; the run moves to ``cancelled`` (U5 BR31.2)."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fail(code: ErrorCode, message: str, **context: Any) -> AdvancedGenerationError:
    return AdvancedGenerationError(message, code=code, context=context)


def _summary(**values: Any) -> str:
    return json.dumps(values, ensure_ascii=False, default=str)


def _safe_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Only short, known values survive into the stored failure (NFR3.6)."""
    safe: dict[str, Any] = {}
    for key, value in context.items():
        if key not in _SAFE_CONTEXT_KEYS:
            continue
        if key == "row_examples":
            # A short list of line numbers: positive integers only.
            if (
                isinstance(value, list)
                and 0 < len(value) <= _ROW_EXAMPLES_MAX
                and all(
                    isinstance(n, int) and not isinstance(n, bool) and n >= 1
                    for n in value
                )
            ):
                safe[key] = list(value)
            continue
        if key in _COUNT_KEYS:
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                safe[key] = value
            continue
        if isinstance(value, (bool, int)) or (
            isinstance(value, str) and len(value) <= 40
        ):
            safe[key] = value
    return safe


@dataclass(frozen=True)
class GenerationProgress:
    """What the progress stream reads at one moment (C3)."""

    generation: AdvancedGeneration
    events: tuple[GenerationEvent, ...]
    #: The stored failure values and, for a document's failure, the owner's
    #: file name found again from ``source_index`` (U4 BR24.4, NFR3.22).
    failure_context: dict[str, Any] = field(default_factory=dict)

    @property
    def is_finished(self) -> bool:
        return self.generation.status.is_terminal


@dataclass(frozen=True)
class AdvancedGenerationResult:
    """The finished result shown in section D (BR8.4)."""

    generation: AdvancedGeneration
    prepare_reports: tuple[PrepareReport, ...]
    segments: tuple[Segment, ...]
    evidence: tuple[Evidence, ...]
    candidates: tuple[PersonaCandidate, ...]
    unverified_segment_ids: tuple[str, ...]
    #: U4: one report per document, and the display name of each source file.
    document_reports: tuple[DocumentReport, ...] = ()
    source_names: dict[str, str] = field(default_factory=dict)


class _NoTools:
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
class _Run:
    """The state of one ``run`` (one thread)."""

    generation: AdvancedGeneration
    started_monotonic: float
    status: GenerationStatus = GenerationStatus.PREPARING
    session: Optional["SandboxSession"] = None
    #: The table (none when the generation uses documents alone).
    source: Optional[SourceFile] = None
    #: Every source file, in ``source_file_ids`` order (U4).
    sources: list[SourceFile] = field(default_factory=list)
    report: Optional[PrepareReport] = None
    #: U4: ``(source_index, file, report)`` of each prepared document.
    documents: list[tuple[int, SourceFile, DocumentReport]] = field(
        default_factory=list
    )
    document_mode: Optional[DocumentMode] = None
    #: Full text of each document, in ``documents`` order (``full_text`` only).
    document_texts: list[str] = field(default_factory=list)
    #: Document evidence that passed the quote check (BR27.1).
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
    #: U5: seconds already waited for plan approvals (BR29.6) and replans.
    waited_seconds: int = 0
    replan_count: int = 0
    #: U5: when the current plan card was shown (``awaiting_plan_approval``).
    awaiting_since: Optional[datetime] = None
    #: U5: the watch saw a cancel request (BR31.2) ...
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    cancel_request: Optional[ControlRequest] = None
    #: ... or that a reader ended the generation (BR32.5).
    ended_elsewhere: threading.Event = field(default_factory=threading.Event)
    #: Set before the run writes its own end, so the watch ignores that end.
    ending: threading.Event = field(default_factory=threading.Event)
    #: Consecutive failures of the watch's read and of its heartbeat (NFR5.51).
    read_failures: int = 0
    beat_failures: int = 0
    #: The end (lock, marks, ``finished_at``) is already written.
    finished: bool = False

    @property
    def deadline(self) -> datetime:
        """60 minutes from the start, moved back by the plan waits (BR29.6)."""
        return self.generation.created_at + timedelta(
            seconds=config.ADVANCED_GENERATION_TIMEOUT_SECONDS + self.waited_seconds
        )


@dataclass(frozen=True)
class ElapsedClock:
    """Seconds since the generation started, and whether they still grow (AC9.1.1)."""

    seconds: int
    running: bool


class AdvancedPersonaGenerationManager:
    """Start, run, follow and read the user's advanced generations."""

    def __init__(
        self,
        *,
        settings_store: Optional["AppSettingsStore"] = None,
        generation_store: Optional["AdvancedGenerationStore"] = None,
        source_file_store: Optional["SourceFileStore"] = None,
        candidate_store: Optional[PersonaCandidateStore] = None,
        s3_service: Optional["S3Service"] = None,
        agent_service: Optional["AgentService"] = None,
        sandbox: Optional["SandboxService"] = None,
        tool_factory: Optional[Callable[..., Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        sleep: Callable[[float], None] = time.sleep,
        max_concurrent: Optional[int] = None,
        heartbeat_seconds: Optional[float] = None,
        poll_seconds: Optional[float] = None,
        control_poll_seconds: Optional[float] = None,
    ) -> None:
        """Every Service is optional; omitted ones come from the factory on first use.

        The availability check (``is_available``) needs only the settings
        store, so the tab strip never builds the sandbox or the agents (ADR-011).
        """
        self._advanced_generation_policy = AdvancedGenerationPolicy(
            settings_store or service_factory.get_app_settings_store()
        )
        self._generation_store = generation_store
        self._source_file_store = source_file_store
        self._candidate_store = candidate_store
        self._s3_service = s3_service
        self._agent_service = agent_service
        self._sandbox_service = sandbox
        self._tool_factory = tool_factory or service_factory.create_analysis_tools
        self._clock = clock or _utc_now
        self._sleep = sleep
        self._slots = threading.BoundedSemaphore(
            max_concurrent or config.ADVANCED_GENERATION_MAX_CONCURRENT
        )
        self._heartbeat_seconds = (
            heartbeat_seconds or config.ADVANCED_GENERATION_HEARTBEAT_SECONDS
        )
        self._poll_seconds = poll_seconds
        #: U5: how often the watch and the plan wait read the generation
        self._control_poll_seconds = float(
            control_poll_seconds
            if control_poll_seconds is not None
            else config.ADVANCED_GENERATION_CONTROL_POLL_SECONDS
        )

    # --- lazy dependencies (ADR-011) --------------------------------------------

    @property
    def _store(self) -> "AdvancedGenerationStore":
        if self._generation_store is None:
            self._generation_store = service_factory.get_advanced_generation_store()
        return self._generation_store

    @property
    def _catalog(self) -> SourceFileCatalog:
        if self._source_file_store is None:
            self._source_file_store = service_factory.get_source_file_store()
        return SourceFileCatalog(
            self._source_file_store,
            error_type=AdvancedGenerationError,
            clock=self._clock,
        )

    @property
    def _candidates(self) -> PersonaCandidateStore:
        if self._candidate_store is None:
            self._candidate_store = service_factory.get_persona_candidate_store()
        return self._candidate_store

    @property
    def _s3(self) -> "S3Service":
        if self._s3_service is None:
            self._s3_service = service_factory.get_s3_service()
        return self._s3_service

    @property
    def _archive(self) -> AnalysisRecordArchive:
        return AnalysisRecordArchive(self._s3, error_type=AdvancedGenerationError)

    @property
    def _agents(self) -> "AgentService":
        if self._agent_service is None:
            self._agent_service = service_factory.get_agent_service()
        return self._agent_service

    @property
    def _sandbox(self) -> "SandboxService":
        if self._sandbox_service is None:
            self._sandbox_service = service_factory.get_sandbox_service()
        return self._sandbox_service

    # --- availability and start -------------------------------------------------

    def is_available(self, principal: Principal) -> bool:
        """Whether the tab and the page are shown to ``principal`` (BR1.1)."""
        return self._advanced_generation_policy.is_available(principal)

    def start_generation(
        self,
        principal: Principal,
        *,
        source_file_ids: Sequence[str],
        purpose: str,
        persona_count: int,
        confirm_plan: bool = False,
    ) -> str:
        """Check the request and write the start transaction (W4, BR7.5, BR6.4).

        U5: ``confirm_plan`` stops the run after the plan until the user
        approves it (BR29.1).

        The order is: release conditions, input, the free slot, the lock
        (scalability-design.md §1). A refused transaction gives the slot back.

        Returns:
            The generation ID; the caller hands :meth:`run` to its pool and
            calls :meth:`abort_start` when that fails.

        Raises:
            AccessDeniedError: The user lacks ``ai:execute``.
            AdvancedGenerationError: ``ADVANCED_GENERATION_UNAVAILABLE``,
                ``GENERATION_PERSONA_COUNT_INVALID``,
                ``GENERATION_PURPOSE_TOO_LONG``, ``UPLOAD_SOURCE_INVALID`` (not
                exactly one table), ``GENERATION_SOURCE_NOT_FOUND``,
                ``ADVANCED_GENERATION_BUSY`` or
                ``ADVANCED_GENERATION_ALREADY_RUNNING``.
        """
        self._advanced_generation_policy.ensure_available(
            principal, AdvancedGenerationError
        )
        ids = list(dict.fromkeys(i for i in source_file_ids if i))
        self._check_input(purpose, persona_count)
        # U4 BR23.1: documents alone, or one table with documents
        self._catalog.ready_sources(principal.sub, ids)
        self._judge_lock_holder(principal)  # U5 BR30.1: a stalled one lets go
        if not self._slots.acquire(blocking=False):
            raise _fail(ErrorCode.ADVANCED_GENERATION_BUSY, "no free generation slot")
        try:
            return self._write_start(
                principal, ids, purpose, int(persona_count), bool(confirm_plan)
            )
        except BaseException:
            self._release_slot()
            raise

    def _judge_lock_holder(self, principal: Principal) -> None:
        """Judge the generation the owner's lock points at (BR30.1, NFR5.47).

        A stalled one is ended (which releases its lock and marks) so the new
        start can take them; one already ended has its lock and marks released
        (conditioned on its ID); a live one keeps them and the start is refused
        by the transaction as before. Only an aid: a failed read lets the
        transaction decide.
        """
        try:
            lock = self._store.get_lock(principal.sub)
            if lock is None or not lock.is_held(self._clock()):
                return
            generation = self._store.get(lock.generation_id)
        except CodedError as e:
            logger.warning("lock holder unreadable (%s)", e.code.value)
            return
        if generation is None or generation.owner_sub != principal.sub:
            return
        if generation.status.is_terminal:
            # review fix: an ended generation whose holds were not released
            # (yet) never blocks the next start; only its own holds go
            self._store.release_holds(
                generation.generation_id,
                generation.owner_sub,
                generation.source_file_ids,
            )
            return
        self._judge_reader(generation)

    @staticmethod
    def _check_input(purpose: str, persona_count: int) -> None:
        if not PERSONA_COUNT_MIN <= int(persona_count) <= PERSONA_COUNT_MAX:
            raise _fail(
                ErrorCode.GENERATION_PERSONA_COUNT_INVALID,
                "persona count out of range",
            )
        if len(purpose) > PURPOSE_MAX_CHARS:
            raise _fail(
                ErrorCode.GENERATION_PURPOSE_TOO_LONG,
                "purpose too long",
                max_chars=PURPOSE_MAX_CHARS,
            )

    def _write_start(
        self,
        principal: Principal,
        ids: list[str],
        purpose: str,
        persona_count: int,
        confirm_plan: bool,
    ) -> str:
        now = self._clock()
        generation = AdvancedGeneration.create_new(
            owner_sub=principal.sub,
            source_file_ids=ids,
            purpose=purpose,
            persona_count=persona_count,
            now=now,
            ttl_seconds=config.ADVANCED_GENERATION_ITEM_TTL_SECONDS,
            confirm_plan=confirm_plan,
        )
        lock_until = now + timedelta(seconds=config.ADVANCED_GENERATION_LOCK_SECONDS)
        try:
            # U2 BR13.3: "used" is the start; the same write moves the expiry
            self._store.create_with_lock(
                generation,
                lock_until=lock_until,
                in_use_until=lock_until,
                now=now,
                source_expires_at=self._catalog.new_expiry(now),
            )
        except AdvancedGenerationStoreError as e:
            raise AdvancedGenerationError("start refused", code=e.code) from e
        return generation.generation_id

    def abort_start(self, generation_id: str) -> None:
        """Undo a start whose run could not be handed to the pool (§2).

        Each end step stands alone; the slot goes back last, exactly once,
        even when the read raises (that error still reaches the caller).
        """
        try:
            generation = self._store.get(generation_id)
            if generation is not None:
                try:
                    self._store.transition(
                        generation_id,
                        generation.status,
                        GenerationStatus.FAILED,
                        updates={
                            "error_code": ErrorCode.ADVANCED_GENERATION_BUSY.value
                        },
                    )
                except Exception as e:  # noqa: BLE001 - each end step stands alone
                    logger.error(
                        "advanced generation abort not written (%s)", type(e).__name__
                    )
                self._finish(generation)
        finally:
            self._release_slot()

    # --- the run ----------------------------------------------------------------

    def run(self, generation_id: str) -> None:
        """Run one started generation to its end. Never raises (a pool thread)."""
        try:
            generation = self._store.get(generation_id)
        except CodedError:
            logger.error("advanced generation unreadable at run start")
            generation = None
        if generation is None or generation.status is not GenerationStatus.PREPARING:
            self._release_slot()
            return
        state = _Run(
            generation=generation,
            started_monotonic=time.monotonic(),
            waited_seconds=generation.waited_seconds,
            replan_count=generation.replan_count,
        )
        stop_watch = self._start_watch(state)
        try:
            self._run_stages(state)
        except _Cancelled:
            self._record_cancel(state)
        except _StopRun:
            # U5 BR32.5: a lost transition means someone else ended it
            state.ended_elsewhere.set()
            logger.warning("advanced generation stopped: a transition was lost")
        except CodedError as e:
            self._record_failure(state, e.code, e.context)
        except Exception as e:  # noqa: BLE001 - the thread must end cleanly
            logger.error("advanced generation failed (%s)", type(e).__name__)
            self._record_failure(state, ErrorCode.GENERATION_OPERATION_FAILED, {})
        finally:
            # Each end step stands alone; the slot goes back last and once (§2).
            try:
                stop_watch.set()
                if not state.ended_elsewhere.is_set() and not state.finished:
                    # NFR5.46: the side that ended it released the lock and marks
                    self._finish(state.generation)
                    state.finished = True
                # review fix: the session (an external call) stops after the
                # lock and the marks are gone, so a restart is never refused
                self._stop_session(state)
                try:
                    self._log_finished(state)
                except Exception as e:  # noqa: BLE001 - each end step stands alone
                    logger.error(
                        "advanced generation end not logged (%s)", type(e).__name__
                    )
            finally:
                self._release_slot()

    def _run_stages(self, state: _Run) -> None:
        self._enter(state, GenerationStatus.PREPARING, first=True)
        self._prepare(state)
        self._enter(
            state,
            GenerationStatus.PLANNING,
            # U4 BR25.2: decided right after the preparation
            updates={
                "document_mode": (
                    state.document_mode.value if state.document_mode else None
                )
            },
        )
        self._plan(state)
        if state.generation.confirm_plan:
            # U5 BR29.1: wait for the user; ends in ``running`` (or raises)
            self._await_plan_decision(state)
        else:
            self._enter(
                state,
                GenerationStatus.RUNNING,
                updates={"plan": state.plan.to_dict() if state.plan else None},
            )
        self._analyze(state)
        self._enter(state, GenerationStatus.SYNTHESIZING)
        personas = self._synthesize(state)
        self._enter(state, GenerationStatus.VERIFYING)
        verification = self._verify(state, personas)
        self._write_results(state, personas, verification)

    def _enter(
        self,
        state: _Run,
        stage: GenerationStatus,
        *,
        first: bool = False,
        moved: bool = False,
        updates: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Move to ``stage`` (conditioned on the current one) and say so (BR7.1).

        ``moved``: the store already moved it (U5: the plan wait's own writes).
        """
        self._checkpoint(state)
        self._check_deadline(state)
        if not first and not moved:
            clean = {k: v for k, v in (updates or {}).items() if v is not None}
            if not self._store.transition(
                state.generation.generation_id, state.status, stage, updates=clean
            ):
                raise _StopRun()
        self._close_stage(state)
        state.status = stage
        state.stage_started[stage.value] = time.monotonic()
        now = self._clock()
        self._event(state, EventType.STAGE, status=stage.value, started_at=now)
        log_advanced_event(
            "advanced_generation.stage",
            generation_id=state.generation.generation_id,
            stage=stage.value,
            elapsed_ms=int((time.monotonic() - state.started_monotonic) * 1000),
        )

    @staticmethod
    def _close_stage(state: _Run) -> None:
        started = state.stage_started.get(state.status.value)
        if started is not None:
            state.durations_ms[state.status.value] = int(
                (time.monotonic() - started) * 1000
            )

    @staticmethod
    def _timeout(message: str, limit: str, seconds: int) -> AdvancedGenerationError:
        """``GENERATION_TIMEOUT`` naming which limit ran out, in minutes."""
        return _fail(
            ErrorCode.GENERATION_TIMEOUT, message, limit=limit, value=seconds // 60
        )

    def _generation_timeout(self, message: str) -> AdvancedGenerationError:
        return self._timeout(
            message, _LIMIT_GENERATION, config.ADVANCED_GENERATION_TIMEOUT_SECONDS
        )

    def _check_deadline(self, state: _Run) -> None:
        if self._clock() > state.deadline:
            raise self._generation_timeout("generation over 60 minutes")

    def _before_model(self, state: _Run) -> None:
        """No model stage starts with under 10 minutes left (§2), nor after a cancel."""
        self._checkpoint(state)
        remaining = state.deadline - self._clock()
        if remaining < timedelta(
            seconds=config.ADVANCED_GENERATION_MODEL_MIN_REMAINING_SECONDS
        ):
            raise self._generation_timeout("too little time for the model")

    # --- preparing --------------------------------------------------------------

    def _prepare(self, state: _Run) -> None:
        """Bring every file in, prepare the table, then each document (BR25.4).

        U4: every chosen file is refreshed and brought in (the U2 note that
        only the first was refreshed is fixed here). The 20 minutes of the
        preparation count the table and the documents together (NFR1.22).
        """
        generation = state.generation
        sources = [
            self._catalog.get_owned(
                generation.owner_sub,
                source_file_id,
                not_found_code=ErrorCode.GENERATION_SOURCE_NOT_FOUND,
            )
            for source_file_id in generation.source_file_ids
        ]
        state.sources = [self._refresh_source(state, s) for s in sources]
        state.source = next(
            (s for s in state.sources if s.kind is SourceKind.TABLE), None
        )
        sandbox = self._sandbox
        state.session = sandbox.start_session(
            generation.generation_id, config.ADVANCED_GENERATION_SESSION_TIMEOUT_SECONDS
        )
        # U5 BR32.4: a reader that ends the generation stops the session by its ID
        if not self._store.transition(
            generation.generation_id,
            GenerationStatus.PREPARING,
            GenerationStatus.PREPARING,
            updates={"session_id": state.session.session_id},
        ):
            raise _StopRun()
        for source in state.sources:
            self._bring_in(state, source)
        prepare_started = self._clock()
        if state.source is not None:
            self._prepare_table(state, state.source, prepare_started)
        position = 0
        for source_index, source in enumerate(state.sources):
            if source.kind is not SourceKind.DOCUMENT:
                continue
            position += 1
            report = self._prepare_document(
                state, position, source_index, source, prepare_started
            )
            state.documents.append((source_index, source, report))
        if state.documents:
            self._decide_document_mode(state)

    def _prepare_table(
        self, state: _Run, source: SourceFile, prepare_started: datetime
    ) -> None:
        """The table's preparation and its checks (U1, U3: BR16, BR18)."""
        assert state.session is not None
        started = time.monotonic()
        try:
            state.report = self._run_prepare(state, source, prepare_started)
        except CodedError as e:
            self._log_prepared(state, started, e.code, e.context)
            raise
        self._log_prepared(state, started, None, {})
        self._check_enough_rows(state)
        profile = self._sandbox.call(
            state.session, "profile_dataset", {"source_id": source.source_file_id}
        )
        state.profile = dict(self._library_value(profile))

    def _check_enough_rows(self, state: _Run) -> None:
        """Stop when the table has fewer rows than persona count x 10 (BR18.1).

        Runs after the bad-row check (which raised already if it applied) and
        before any model call. The count is the rows read, before any narrowing
        by the user's instruction.
        """
        assert state.report is not None
        persona_count = state.generation.persona_count
        required = persona_count * config.ADVANCED_MIN_ROWS_PER_PERSONA
        if state.report.rows_read < required:
            raise _fail(
                ErrorCode.GENERATION_INSUFFICIENT_DATA,
                "too few rows for the persona count",
                rows_read=state.report.rows_read,
                required_rows=required,
                persona_count=persona_count,
            )

    def _log_prepared(
        self,
        state: _Run,
        started: float,
        error_code: Optional[ErrorCode],
        context: Mapping[str, Any],
    ) -> None:
        """One line at the end of the preparation: counts and time only (NFR6.11)."""
        fields: dict[str, Any] = {
            "generation_id": state.generation.generation_id,
            "status": "failed" if error_code else "done",
            "error_code": error_code.value if error_code else None,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        report = state.report
        if report is not None:
            fields["rows_read"] = report.rows_read
            fields["rows_excluded"] = report.rows_excluded
            counts = (report.excluded_reasons or {}).get("counts")
            if isinstance(counts, Mapping):
                fields["excluded_reasons"] = {
                    str(k): v
                    for k, v in counts.items()
                    if isinstance(v, int) and not isinstance(v, bool)
                }
            fields["memory_limit"] = state.memory_limit
        else:
            safe = _safe_context(context)
            for key in ("rows_read", "rows_excluded"):
                if key in safe:
                    fields[key] = safe[key]
        try:
            log_advanced_event("advanced_generation.prepared", **fields)
        except Exception as e:  # noqa: BLE001 - a log line never fails a run
            logger.error("preparation not logged (%s)", type(e).__name__)

    def _refresh_source(self, state: _Run, source: SourceFile) -> SourceFile:
        """Copy the object onto its key so S3 keeps it 9 more days (BR13.4).

        Runs before the session starts. A transient S3 failure is sent again
        once after 1 second (NFR5.30); an object gone from S3 or a checksum
        that differs from the record is ``GENERATION_SOURCE_UNREADABLE``. A
        record without a checksum gets the copy's (security-design.md §5).

        Returns:
            The file, with the filled checksum when there was none.
        """
        started = time.monotonic()
        checksum: Optional[str] = None
        for attempt in range(2):
            try:
                checksum = self._s3.refresh_object(source.s3_key)
                break
            except S3ServiceError as e:
                if attempt == 0 and e.code.kind is ErrorKind.TRANSIENT:
                    self._sleep(_REFRESH_RETRY_SECONDS)
                    continue
                code = (
                    ErrorCode.GENERATION_SOURCE_UNREADABLE
                    if e.code is ErrorCode.S3_OBJECT_NOT_FOUND
                    else e.code
                )
                self._log_refresh(state, source, started, code)
                raise _fail(code, "source object not refreshed") from e
        if checksum and source.checksum and checksum != source.checksum:
            code = ErrorCode.GENERATION_SOURCE_UNREADABLE
            self._log_refresh(state, source, started, code)
            raise _fail(code, "source checksum changed")
        if checksum and not source.checksum:
            self._catalog.fill_checksum(source.source_file_id, checksum)
            source = source.update(checksum=checksum)
        self._log_refresh(state, source, started, None)
        return source

    @staticmethod
    def _log_refresh(
        state: _Run,
        source: SourceFile,
        started: float,
        error_code: Optional[ErrorCode],
    ) -> None:
        log_advanced_event(
            "advanced_generation.source_refreshed",
            generation_id=state.generation.generation_id,
            source_file_id=source.source_file_id,
            status="failed" if error_code else "done",
            size_bytes=source.size_bytes,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_code=error_code.value if error_code else None,
        )

    def _bring_in(self, state: _Run, source: SourceFile) -> None:
        """Hand the file in with a fresh presigned GET; one new URL on refusal."""
        outcome: Mapping[str, Any] = {}
        for _ in range(2):
            url = self._s3.generate_presigned_url(
                self._s3.path_of(source.s3_key),
                config.ADVANCED_GENERATION_DOWNLOAD_URL_SECONDS,
            )
            results = self._sandbox.put_files(
                state.session,  # type: ignore[arg-type]
                [
                    {
                        "source_file_id": source.source_file_id,
                        "s3_key": source.s3_key,
                        "download_url": url,
                    }
                ],
            )
            outcome = results[0] if results else {}
            if outcome.get("ok"):
                return
        context: dict[str, Any] = {"cause": _CAUSE_SANDBOX}
        if outcome.get("http_status") is not None:
            context["http_status"] = int(outcome["http_status"])
        raise AdvancedGenerationError(
            "file not brought in",
            code=ErrorCode.GENERATION_SANDBOX_UNAVAILABLE,
            context=context,
        )

    def _run_prepare(
        self, state: _Run, source: SourceFile, prepare_started: datetime
    ) -> PrepareReport:
        metadata = source.table_metadata
        assert metadata is not None and state.session is not None
        args = {
            "source_id": source.source_file_id,
            "metadata": {
                "encoding": metadata.encoding.value,
                "delimiter": metadata.delimiter,
            },
            "max_excluded_percent": config.ADVANCED_SOURCE_MAX_EXCLUDED_PERCENT,
        }
        command_id = self._sandbox.start_command(state.session, "prepare", args)
        last: dict[str, Optional[int]] = {"rows": None}

        def on_progress(progress: Mapping[str, Any]) -> bool:
            rows = progress.get("rows")
            if not isinstance(rows, int) or rows == last["rows"]:
                return False
            last["rows"] = rows
            self._event(
                state,
                EventType.PROGRESS,
                message=f"{rows} 行を読み込みました",
                detail={"rows": rows},
            )
            return True

        status = self._wait_command(state, command_id, prepare_started, on_progress)
        value = self._library_value(status.get("result") or {"ok": False})
        limit_text = value.get("memory_limit")
        if isinstance(limit_text, str) and re.fullmatch(r"\d{1,6}[KMGT]?B", limit_text):
            state.memory_limit = limit_text
        try:
            report = PrepareReport.from_dict(value)
        except (KeyError, TypeError, ValueError):
            raise _fail(
                ErrorCode.GENERATION_SANDBOX_UNAVAILABLE,
                "bad preparation report",
                cause=_CAUSE_SANDBOX,
            ) from None
        self._event(
            state,
            EventType.PROGRESS,
            message=(
                f"{report.rows_read} 行を読み込みました（{report.rows_excluded} 行を除外）"
            ),
            detail={"rows": report.rows_read, "rows_excluded": report.rows_excluded},
        )
        return report

    def _wait_command(
        self,
        state: _Run,
        command_id: str,
        prepare_started: datetime,
        on_progress: Callable[[Mapping[str, Any]], bool],
    ) -> Mapping[str, Any]:
        """Poll a started command until it ends, within the 20 and 60 minutes.

        ``on_progress`` gets the progress file and says whether it wrote an
        event; without one for 60 seconds a "still going" notice is written
        (BR17.2). The 20 minutes count from ``prepare_started``, the start of
        the whole preparation (table and documents, NFR1.22).
        """
        assert state.session is not None
        sandbox = self._sandbox
        limit = timedelta(seconds=config.ADVANCED_GENERATION_PREPARE_TIMEOUT_SECONDS)
        notice = timedelta(seconds=config.ADVANCED_GENERATION_PREPARE_NOTICE_SECONDS)
        started = prepare_started
        last_event_at = self._clock()
        while True:
            status = sandbox.poll_command(state.session, command_id)
            if on_progress(status.get("progress") or {}):
                last_event_at = self._clock()
            if status.get("done"):
                return status
            if state.cancel_requested.is_set() or state.ended_elsewhere.is_set():
                # U5 BR31.2: stop the command, then end the run
                self._cancel_quietly(state, command_id)
                self._checkpoint(state)
            now = self._clock()
            if now - last_event_at >= notice:
                # The rows stand still while the table is written (BR17.2): say
                # that it goes on, so the screen hears from us within 5 minutes.
                last_event_at = now
                minutes = max(int((now - started).total_seconds() // 60), 1)
                self._event(
                    state,
                    EventType.PROGRESS,
                    message=f"前準備を続けています（{minutes} 分経過）",
                    detail={"minutes": minutes},
                )
            if now > state.deadline:
                self._cancel_quietly(state, command_id)
                raise self._generation_timeout("generation over 60 minutes")
            if now - started > limit:
                self._cancel_quietly(state, command_id)
                raise self._timeout(
                    "preparation over 20 minutes",
                    _LIMIT_PREPARE,
                    config.ADVANCED_GENERATION_PREPARE_TIMEOUT_SECONDS,
                )
            self._sleep(
                self._poll_seconds
                if self._poll_seconds is not None
                else config.ADVANCED_GENERATION_PREPARE_POLL_SECONDS
            )

    # --- U4: documents (BR24, BR25, BR26, BR27.1) --------------------------------

    def _prepare_document(
        self,
        state: _Run,
        position: int,
        source_index: int,
        source: SourceFile,
        prepare_started: datetime,
    ) -> DocumentReport:
        """Prepare one document in the sandbox and say what it holds (BR24.5)."""
        assert state.session is not None
        metadata = source.document_metadata
        fmt = source.source_format
        if metadata is None or fmt is None:
            raise _fail(ErrorCode.GENERATION_SOURCE_NOT_FOUND, "not a ready document")
        args: dict[str, Any] = {
            "source_id": source.source_file_id,
            "source_index": source_index,
            "format": fmt.value,
            "chunk_chars": int(config.ADVANCED_DOCUMENT_CHUNK_CHARS),
            "overlap_chars": int(config.ADVANCED_DOCUMENT_CHUNK_OVERLAP_CHARS),
        }
        if metadata.encoding is not None:
            args["encoding"] = metadata.encoding.value
        started = time.monotonic()
        last: dict[str, Optional[int]] = {"pages": None}

        def on_progress(progress: Mapping[str, Any]) -> bool:
            pages = progress.get("pages")
            if not isinstance(pages, int) or pages == last["pages"]:
                return False
            last["pages"] = pages
            self._event(
                state,
                EventType.PROGRESS,
                message=f"{position} 件目の文書: {pages} ページを読み込みました",
                detail={"pages": pages},
            )
            return True

        try:
            command_id = self._sandbox.start_command(
                state.session, "prepare_document", args
            )
            status = self._wait_command(state, command_id, prepare_started, on_progress)
            value = self._library_value(status.get("result") or {"ok": False})
            try:
                report = DocumentReport.from_dict(value)
            except (KeyError, TypeError, ValueError):
                raise _fail(
                    ErrorCode.GENERATION_SANDBOX_UNAVAILABLE,
                    "bad document report",
                    cause=_CAUSE_SANDBOX,
                ) from None
        except CodedError as e:
            self._log_document(state, source_index, fmt.value, started, None, e.code)
            raise
        self._log_document(state, source_index, fmt.value, started, report, None)
        self._event(
            state,
            EventType.PROGRESS,
            message=documents.prepared_message(position, report),
            detail={
                "document_index": source_index,
                "pages": report.pages,
                "chars": report.chars,
                "chunks": report.chunks,
                "no_text_pages": len(report.no_text_pages),
            },
        )
        return report

    @staticmethod
    def _log_document(
        state: _Run,
        source_index: int,
        fmt: str,
        started: float,
        report: Optional[DocumentReport],
        error_code: Optional[ErrorCode],
    ) -> None:
        """One line per document: counts and time only, never names (NFR6.13)."""
        try:
            log_advanced_event(
                "advanced_generation.document_prepared",
                generation_id=state.generation.generation_id,
                status="failed" if error_code else "done",
                error_code=error_code.value if error_code else None,
                document_index=source_index,
                format=fmt,
                pages=report.pages if report else None,
                chars=report.chars if report else None,
                chunks=report.chunks if report else None,
                no_text_pages=len(report.no_text_pages) if report else None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as e:  # noqa: BLE001 - a log line never fails a run
            logger.error("document preparation not logged (%s)", type(e).__name__)

    def _decide_document_mode(self, state: _Run) -> None:
        """The total limit, then full text or extraction (BR25.1, BR25.2)."""
        total = sum(report.chars for _, _, report in state.documents)
        chunks = sum(report.chunks for _, _, report in state.documents)
        limit = int(config.ADVANCED_DOCUMENT_MAX_TOTAL_CHARS)
        if total > limit:
            raise _fail(
                ErrorCode.GENERATION_SOURCE_TOO_LARGE,
                "documents over the total limit",
                total_chars=total,
                max_chars=limit,
            )
        full_text = total <= int(config.PERSONA_SOURCE_MAX_CHARS)
        state.document_mode = (
            DocumentMode.FULL_TEXT if full_text else DocumentMode.EXTRACTION
        )
        self._event(
            state,
            EventType.PROGRESS,
            message=documents.mode_message(total, chunks, full_text),
            detail={"mode": state.document_mode.value, "chunks": chunks},
        )
        try:
            log_advanced_event(
                "advanced_generation.document_mode",
                generation_id=state.generation.generation_id,
                mode=state.document_mode.value,
                total_chars=total,
                chunks=chunks,
            )
        except Exception as e:  # noqa: BLE001 - a log line never fails a run
            logger.error("document mode not logged (%s)", type(e).__name__)
        if full_text:
            overlap = int(config.ADVANCED_DOCUMENT_CHUNK_OVERLAP_CHARS)
            state.document_texts = [
                documents.join_chunks(
                    [
                        self._read_chunk(state, source.source_file_id, i)[0]
                        for i in range(report.chunks)
                    ],
                    overlap,
                )
                for _, source, report in state.documents
            ]
        else:
            self._extract_evidence(state)

    def _read_chunk(
        self, state: _Run, source_id: str, index: int
    ) -> tuple[str, Mapping[str, Any]]:
        """One chunk, taken out of the session a piece at a time (NFR5.39)."""
        assert state.session is not None
        parts: list[str] = []
        offset: Optional[int] = 0
        info: Mapping[str, Any] = {}
        while offset is not None:
            value = self._library_value(
                self._sandbox.call(
                    state.session,
                    "document_chunk",
                    {
                        "source_id": source_id,
                        "index": index,
                        "offset": offset,
                        "max_chars": int(config.ADVANCED_DOCUMENT_TRANSFER_CHARS),
                    },
                ),
                strict=True,
            )
            parts.append(str(value.get("text") or ""))
            info = value
            next_offset = value.get("next_offset")
            offset = next_offset if isinstance(next_offset, int) else None
        return "".join(parts), info

    def _extract_evidence(self, state: _Run) -> None:
        """Evidence out of every chunk, a few at once (BR26.1〜BR26.3, NFR1.16).

        Each task takes one chunk out of the session, asks the light model and
        returns its evidence; the chunk's text goes when the task ends. One
        failure stops the rest without waiting for them (BR26.2, NFR1.18).
        """
        started = time.monotonic()
        tasks = [
            (source_index, source.source_file_id, chunk)
            for source_index, source, report in state.documents
            for chunk in range(report.chunks)
        ]
        total = len(tasks)
        step = max(1, math.ceil(total / 10))
        tokens = {"input_tokens": 0, "output_tokens": 0}
        found: list[tuple[int, int, list[Evidence]]] = []
        # Set by the first failure: later tasks do not start and running ones
        # do not retry (BR26.2), so no call goes on after the run has failed.
        stopped = threading.Event()
        first_failure: list[BaseException] = []
        executor = ThreadPoolExecutor(
            max_workers=max(1, int(config.ADVANCED_EVIDENCE_MAX_CONCURRENT)),
            thread_name_prefix="advanced-evidence",
        )
        try:
            futures: list[Future[tuple[int, int, list[Evidence], dict[str, int]]]] = [
                executor.submit(
                    self._extract_chunk, state, stopped, first_failure, *task
                )
                for task in tasks
            ]
            done = 0
            last_notice = self._clock()
            for future in as_completed(futures):
                source_index, chunk, evidence, usage = future.result()
                found.append((source_index, chunk, evidence))
                for key in tokens:
                    tokens[key] += int(usage.get(key, 0))
                done += 1
                now = self._clock()
                if (
                    done % step == 0
                    or done == total
                    or (now - last_notice).total_seconds() >= _EXTRACTION_NOTICE_SECONDS
                ):
                    last_notice = now
                    self._event(
                        state,
                        EventType.PROGRESS,
                        message=f"根拠の抽出 {done} / {total}",
                        detail={"done": done, "total": total},
                    )
        except BaseException as e:
            stopped.set()
            executor.shutdown(wait=False, cancel_futures=True)
            # A task that gave up because of the stop may finish first; the
            # failure that caused the stop is the one the run reports.
            if first_failure and first_failure[0] is not e:
                raise first_failure[0] from None
            raise
        executor.shutdown(wait=True)
        extracted = [
            e for _, _, evidence in sorted(found, key=lambda f: f[:2]) for e in evidence
        ]
        state.evidence_tokens = tokens
        state.document_evidence = self._check_quotes(state, extracted)
        stats = state.evidence_stats
        try:
            log_advanced_event(
                "advanced_generation.evidence_extracted",
                generation_id=state.generation.generation_id,
                chunks=total,
                extracted=stats.get("extracted", 0),
                discarded_unverified=stats.get("discarded_unverified", 0),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                evidence_input_tokens=tokens["input_tokens"],
                evidence_output_tokens=tokens["output_tokens"],
            )
        except Exception as e:  # noqa: BLE001 - a log line never fails a run
            logger.error("evidence extraction not logged (%s)", type(e).__name__)

    def _extract_chunk(
        self,
        state: _Run,
        stopped: threading.Event,
        first_failure: list[BaseException],
        source_index: int,
        source_id: str,
        chunk: int,
    ) -> tuple[int, int, list[Evidence], dict[str, int]]:
        """One task of the extraction: the time check, the chunk, the model.

        A failure here sets ``stopped`` before it is raised, so the next task a
        worker picks up gives up at once (BR26.2, NFR1.18).
        """
        if stopped.is_set():
            raise _fail(
                ErrorCode.GENERATION_OPERATION_FAILED, "extraction already stopped"
            )
        try:
            self._before_model(state)  # BR26.5: before every call
            text, info = self._read_chunk(state, source_id, chunk)
            # One agent per call: a Strands agent is never shared by two threads.
            agent = self._agents.create_advanced_evidence_agent(
                prompts.EVIDENCE_INSTRUCTIONS
            )
            output, usage = self._agents.run_evidence_extraction(
                agent,
                prompts.evidence_prompt(
                    state.generation.purpose,
                    int(config.ADVANCED_EVIDENCE_PER_CHUNK_MAX),
                    int(config.ADVANCED_EVIDENCE_QUOTE_MAX_CHARS),
                ),
                text,
                prompts.EvidenceExtraction,
                document_index=source_index,
                chunk_index=chunk,
                may_retry=lambda: not stopped.is_set(),
            )
        except BaseException as e:
            # list.append is atomic: the first item is the earliest failure.
            if not stopped.is_set():
                first_failure.append(e)
            stopped.set()
            raise
        evidence = documents.evidence_from_extraction(
            output if isinstance(output, prompts.EvidenceExtraction) else None,
            source_index=source_index,
            source_id=source_id,
            chunk_index=chunk,
            info=info,
            max_items=int(config.ADVANCED_EVIDENCE_PER_CHUNK_MAX),
            max_quote_chars=int(config.ADVANCED_EVIDENCE_QUOTE_MAX_CHARS),
        )
        return source_index, chunk, evidence, usage

    def _check_quotes(
        self, state: _Run, evidence: Sequence[Evidence]
    ) -> list[Evidence]:
        """Keep the evidence whose quote is in its document (BR27.1).

        Quotes go to the sandbox per document, ``ADVANCED_EVIDENCE_VERIFY_BATCH``
        at a time. The dropped ones are counted in ``evidence_stats``.
        """
        assert state.session is not None
        batch = max(1, int(config.ADVANCED_EVIDENCE_VERIFY_BATCH))
        kept_ids: set[str] = set()
        by_source: dict[str, list[Evidence]] = {}
        for item in evidence:
            by_source.setdefault(item.source_id, []).append(item)
        for source_id, items in by_source.items():
            for start in range(0, len(items), batch):
                group = items[start : start + batch]
                value = self._library_value(
                    self._sandbox.call(
                        state.session,
                        "verify_quotes",
                        {
                            "source_id": source_id,
                            "quotes": [e.quote or "" for e in group],
                            "min_chars": int(config.ADVANCED_EVIDENCE_QUOTE_MIN_CHARS),
                        },
                    ),
                    strict=True,
                )
                found = value.get("found")
                if not isinstance(found, list) or len(found) != len(group):
                    raise _fail(
                        ErrorCode.GENERATION_SANDBOX_UNAVAILABLE,
                        "bad quote check",
                        cause=_CAUSE_SANDBOX,
                    )
                kept_ids |= {e.id for e, ok in zip(group, found) if ok is True}
        kept = [e for e in evidence if e.id in kept_ids]
        discarded = len(evidence) - len(kept)
        stats = state.evidence_stats
        stats["extracted"] = stats.get("extracted", 0) + len(evidence)
        stats["discarded_unverified"] = stats.get("discarded_unverified", 0) + discarded
        if discarded:
            self._event(
                state,
                EventType.PROGRESS,
                message=f"文書と照合できない根拠 {discarded} 件を除きました",
                detail={"discarded_unverified": discarded},
            )
        return kept

    @staticmethod
    def _library_value(
        result: Mapping[str, Any], *, strict: bool = False
    ) -> Mapping[str, Any]:
        """The value of a C9 result, or the user-facing code of its error.

        ``strict`` (U4 NFR5.39): a value the Service had to cut cannot be used
        (a chunk's text, the quote check) and is ``GENERATION_OPERATION_FAILED``.
        """
        if result.get("ok") and isinstance(result.get("value"), Mapping):
            value: Mapping[str, Any] = result["value"]
            if strict and (result.get("truncated") or value.get("truncated")):
                raise _fail(
                    ErrorCode.GENERATION_OPERATION_FAILED, "sandbox output was cut"
                )
            return value
        error = result.get("error") or {}
        code = _LIBRARY_CODES.get(str(error.get("code")))
        if code is not None:
            context = error.get("context")
            raise _fail(
                code,
                "the sandbox refused the source",
                **(_safe_context(context) if isinstance(context, Mapping) else {}),
            )
        raw = str(error.get("code"))
        # Library codes are fixed upper-case names; anything else is not logged.
        logger.warning(
            "sandbox function failed: library_code=%s",
            raw if raw.replace("_", "").isalpha() and raw.isupper() else "other",
        )
        raise _fail(
            ErrorCode.GENERATION_SANDBOX_UNAVAILABLE,
            "sandbox function failed",
            cause=_CAUSE_SANDBOX,
        )

    def _cancel_quietly(self, state: _Run, command_id: str) -> None:
        try:
            self._sandbox.cancel_command(state.session, command_id)  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001 - the session is stopped anyway
            logger.warning("preparation not cancelled (%s)", type(e).__name__)

    # --- planning, running, synthesizing ----------------------------------------

    def _columns(self, state: _Run) -> list[str]:
        return [str(c.get("name")) for c in state.profile.get("columns") or []]

    def _profile_text(self, state: _Run) -> str:
        return json.dumps(state.profile, ensure_ascii=False, default=str)

    def _full_texts(self, state: _Run) -> list[str]:
        """The whole document texts for the prompts (``full_text`` only, BR25.3)."""
        return (
            list(state.document_texts)
            if state.document_mode is DocumentMode.FULL_TEXT
            else []
        )

    def _document_evidence_text(self, state: _Run) -> str:
        return json.dumps(
            [e.to_dict() for e in state.document_evidence], ensure_ascii=False
        )

    def _plan(self, state: _Run) -> None:
        generation = state.generation
        guide = AnalysisPolicy.guide_method(generation.purpose, state.profile)
        instructions = prompts.PLANNING_INSTRUCTIONS + (
            prompts.DOCUMENT_PLANNING_NOTE if state.documents else ""
        )
        agent = self._agents.create_advanced_planning_agent(instructions)
        data = {"purpose": generation.purpose, "profile": self._profile_text(state)}
        if state.document_mode is DocumentMode.EXTRACTION:
            # BR26.3: the checked evidence only, never the text
            data["document_evidence"] = self._document_evidence_text(state)
        plan: Optional[AnalysisPlan] = None
        for attempt in range(1, PLAN_ATTEMPTS + 1):
            self._before_model(state)
            raw = self._agents.run_advanced_planning(
                agent,
                prompts.planning_prompt(generation.persona_count, attempt),
                data,
                documents=self._full_texts(state),
            )
            self._checkpoint(state)  # U5: a cancel during the call
            plan = AnalysisPolicy.settle_plan(
                raw, guide=guide, attempt=attempt, columns=self._columns(state)
            )
            if plan is not None:
                break
        assert plan is not None
        state.plan = plan
        if not generation.confirm_plan:
            # U5: a plan to confirm is announced once its card can be shown
            self._plan_event(state)

    def _plan_event(self, state: _Run) -> None:
        assert state.plan is not None
        self._event(
            state,
            EventType.PLAN,
            method=state.plan.method.value,
            interpreted_purpose=state.plan.interpreted_purpose[:_EVENT_TEXT_MAX],
        )

    # --- U5: the plan confirmation (BR29) ---------------------------------------

    def _await_plan_decision(self, state: _Run) -> None:
        """Show the plan and wait for approve, replan or cancel (W5-1).

        The thread keeps its slot and the lock while it waits (NFR5.41) and
        reads the generation every ``control_poll_seconds`` (NFR1.24). A
        replan makes the plan again with the new instruction and waits again,
        with no limit on the count (BR29.4). One card waits 15 minutes and all
        the cards 60 minutes together (BR29.5, BR29.7).

        Raises:
            _Cancelled: The user cancelled while the card was shown.
            _StopRun: A reader ended the generation.
            AdvancedGenerationError: ``GENERATION_TIMEOUT`` (a plan wait).
        """
        while True:
            self._enter_awaiting(state)
            request = self._wait_for_request(state)
            if request.action is ControlAction.APPROVE:
                if self._take_approve(state, request):
                    return
            elif request.action is ControlAction.REPLAN:
                if self._take_replan(state, request):
                    self._plan(state)
            # a request taken by no one (a cancel wrote over it) is read again

    def _enter_awaiting(self, state: _Run) -> None:
        """``planning -> awaiting_plan_approval`` with the longer lock (NFR5.43)."""
        if state.status is GenerationStatus.AWAITING_PLAN_APPROVAL:
            return
        assert state.plan is not None
        generation = state.generation
        now = self._clock()
        # From the generation's own deadline, so it never shrinks (NFR5.43)
        until = generation.created_at + timedelta(
            seconds=config.ADVANCED_GENERATION_LOCK_SECONDS
            + state.waited_seconds
            + config.ADVANCED_GENERATION_PLAN_WAIT_SECONDS
        )
        if not self._store.enter_awaiting(
            generation.generation_id,
            generation.owner_sub,
            generation.source_file_ids,
            now=now,
            until=until,
            updates={"plan": state.plan.to_dict()},
        ):
            raise _StopRun()
        state.awaiting_since = now
        self._enter(state, GenerationStatus.AWAITING_PLAN_APPROVAL, moved=True)
        self._plan_event(state)

    def _wait_for_request(self, state: _Run) -> ControlRequest:
        """Read the generation until a request arrives or a wait runs out."""
        generation_id = state.generation.generation_id
        assert state.awaiting_since is not None
        while True:
            self._checkpoint(state)
            try:
                current = self._store.get(generation_id)
            except CodedError as e:
                logger.warning("plan wait read failed (%s)", e.code.value)
                current = state.generation
            if current is None or current.status is not state.status:
                state.ended_elsewhere.set()
                raise _StopRun()
            request = current.control_request
            if request is not None and request.action is ControlAction.CANCEL:
                state.cancel_request = request
                raise _Cancelled()
            if request is not None:
                return request
            self._check_plan_wait(state, self._clock())
            state.cancel_requested.wait(self._control_poll_seconds)

    @staticmethod
    def _plan_wait_limit(
        awaiting_since: datetime, waited_seconds: int, now: datetime
    ) -> tuple[Optional[tuple[str, int]], int]:
        """The plan wait that ran out, if any, and all the seconds waited.

        One card waits ``ADVANCED_GENERATION_PLAN_WAIT_SECONDS`` (BR29.5); all
        the cards ``ADVANCED_GENERATION_PLAN_WAIT_TOTAL_SECONDS`` (BR29.7).
        """
        card = int((now - awaiting_since).total_seconds())
        total = waited_seconds + max(0, card)
        if card > config.ADVANCED_GENERATION_PLAN_WAIT_SECONDS:
            return (
                _LIMIT_PLAN_WAIT,
                config.ADVANCED_GENERATION_PLAN_WAIT_SECONDS,
            ), total
        if total > config.ADVANCED_GENERATION_PLAN_WAIT_TOTAL_SECONDS:
            return (
                _LIMIT_PLAN_WAIT_TOTAL,
                config.ADVANCED_GENERATION_PLAN_WAIT_TOTAL_SECONDS,
            ), total
        return None, total

    def _check_plan_wait(self, state: _Run, now: datetime) -> None:
        assert state.awaiting_since is not None
        limit, total = self._plan_wait_limit(
            state.awaiting_since, state.waited_seconds, now
        )
        if limit is None:
            return
        self._log_plan_wait_expired(state.generation.generation_id, limit[0], total)
        raise self._timeout("plan wait ran out", limit[0], limit[1])

    def _waited_now(self, state: _Run) -> int:
        """All the waits, the current card's included (BR29.6)."""
        assert state.awaiting_since is not None
        current = int((self._clock() - state.awaiting_since).total_seconds())
        return state.waited_seconds + max(0, current)

    def _take_approve(self, state: _Run, request: ControlRequest) -> bool:
        waited = self._waited_now(state)
        if not self._store.take_control_request(
            state.generation.generation_id,
            GenerationStatus.AWAITING_PLAN_APPROVAL,
            GenerationStatus.RUNNING,
            request,
            updates={"waited_seconds": waited},
            removes=("awaiting_since",),
        ):
            return False
        self._taken(state, request, waited)
        self._enter(state, GenerationStatus.RUNNING, moved=True)
        return True

    def _take_replan(self, state: _Run, request: ControlRequest) -> bool:
        """Put the new instruction in and go back to ``planning`` (BR29.4)."""
        generation = state.generation
        waited = self._waited_now(state)
        purpose = request.revised_purpose or ""
        original = (
            generation.original_purpose
            if generation.original_purpose is not None
            else generation.purpose
        )
        replans = state.replan_count + 1
        if not self._store.take_control_request(
            generation.generation_id,
            GenerationStatus.AWAITING_PLAN_APPROVAL,
            GenerationStatus.PLANNING,
            request,
            updates={
                "waited_seconds": waited,
                "purpose": purpose,
                "original_purpose": original,
                "replan_count": replans,
            },
            removes=("awaiting_since",),
        ):
            return False
        state.generation = generation.update(
            purpose=purpose, original_purpose=original, replan_count=replans
        )
        state.replan_count = replans
        self._taken(state, request, waited)
        self._enter(state, GenerationStatus.PLANNING, moved=True)
        return True

    def _taken(self, state: _Run, request: ControlRequest, waited: int) -> None:
        state.waited_seconds = waited
        state.awaiting_since = None
        state.generation = state.generation.update(waited_seconds=waited)
        self._log_control_taken(state, request)

    def _analyze(self, state: _Run) -> None:
        assert state.plan is not None
        if state.source is None:
            # U4: documents alone have no table to analyse
            state.tools = _NoTools()
            return
        self._before_model(state)
        generation = state.generation
        tools = self._tool_factory(
            state.session,
            AnalysisPolicy.tool_policy(),
            source_id=state.source.source_file_id,
            columns=self._columns(state),
            persona_count=generation.persona_count,
            sandbox=self._sandbox,
        )
        state.tools = tools
        agent = self._agents.create_advanced_analysis_agent(
            prompts.ANALYSIS_INSTRUCTIONS, tools.tools()
        )
        output = self._agents.run_advanced_analysis(
            agent,
            prompts.analysis_prompt(state.plan, generation.persona_count),
            {
                "plan": json.dumps(state.plan.to_dict(), ensure_ascii=False),
                "profile": self._profile_text(state),
            },
            source_id=state.source.source_file_id,
        )
        self._checkpoint(state)  # U5: a cancel during the call
        if tools.sandbox_error is not None:
            raise tools.sandbox_error
        self._check_cluster_calls(state)
        state.segments, state.evidence = self._parse_analysis(output)
        if not state.segments:
            raise _fail(ErrorCode.GENERATION_ANALYSIS_FAILED, "no segment")

    @staticmethod
    def _check_cluster_calls(state: _Run) -> None:
        """A cluster plan whose clustering failed twice cannot go on (BR5.3)."""
        if state.plan is None or state.plan.method is not AnalysisMethod.CLUSTER:
            return
        calls = [c for c in state.tools.calls if c.tool == "cluster_segments"]
        failed = [c for c in calls if not c.ok and c.error_code in _RESELECT_CODES]
        if len(failed) >= 2 and not any(c.ok for c in calls):
            raise _fail(ErrorCode.GENERATION_ANALYSIS_FAILED, "clustering failed twice")

    @staticmethod
    def _parse_analysis(
        output: Mapping[str, Any],
    ) -> tuple[list[Segment], list[Evidence]]:
        try:
            segments = [Segment.from_dict(s) for s in output.get("segments") or []]
            evidence = [Evidence.from_dict(e) for e in output.get("evidence") or []]
        except (KeyError, TypeError, ValueError):
            raise _fail(
                ErrorCode.GENERATION_ANALYSIS_FAILED, "bad analysis output"
            ) from None
        return segments, evidence

    def _assign_to_segments(self, state: _Run) -> None:
        """Extraction with a table: each evidence gets 0 or 1 segment (BR28.1)."""
        if not state.document_evidence or not state.segments:
            return
        self._before_model(state)
        agent = self._agents.create_advanced_synthesis_agent(
            prompts.ASSIGNMENT_INSTRUCTIONS
        )
        output = self._agents.run_advanced_synthesis(
            agent,
            prompts.assignment_prompt(),
            {
                "segments": json.dumps(
                    [s.to_dict() for s in state.segments], ensure_ascii=False
                ),
                "document_evidence": json.dumps(
                    [
                        {
                            "id": e.id,
                            "kind": e.kind.value,
                            "claim": e.claim,
                            "quote": e.quote,
                            "speaker_attributes": e.speaker_attributes or {},
                        }
                        for e in state.document_evidence
                    ],
                    ensure_ascii=False,
                ),
            },
            prompts.EvidenceAssignmentOutput,
        )
        self._checkpoint(state)  # U5: a cancel during the call
        state.document_evidence = documents.apply_assignments(
            state.document_evidence,
            list(output.assignments),
            segment_ids={s.id for s in state.segments},
        )

    def _synthesize(self, state: _Run) -> list[prompts.AdvancedPersonaOutput]:
        generation = state.generation
        has_documents = bool(state.documents)
        full_text = state.document_mode is DocumentMode.FULL_TEXT
        if state.document_mode is DocumentMode.EXTRACTION:
            self._assign_to_segments(state)
        self._before_model(state)
        agent = self._agents.create_advanced_synthesis_agent(
            prompts.SYNTHESIS_INSTRUCTIONS
            + (prompts.DOCUMENT_SYNTHESIS_NOTE if has_documents else "")
        )
        data = {
            "purpose": generation.purpose,
            "segments": json.dumps(
                [s.to_dict() for s in state.segments], ensure_ascii=False
            ),
            # BR28.2: evidence without a segment reaches every persona
            "evidence": json.dumps(
                [e.to_dict() for e in [*state.evidence, *state.document_evidence]],
                ensure_ascii=False,
            ),
        }
        report = state.report
        if report is not None:
            data["prepare_report"] = json.dumps(
                {
                    "rows_read": report.rows_read,
                    "rows_excluded": report.rows_excluded,
                    "columns": [c.to_dict() for c in report.columns],
                },
                ensure_ascii=False,
            )
        output = self._agents.run_advanced_synthesis(
            agent,
            prompts.synthesis_prompt(
                generation.persona_count,
                has_table=state.source is not None,
                has_documents=has_documents,
                full_text=full_text,
            ),
            data,
            prompts.AdvancedPersonaListOutput,
            documents=self._full_texts(state),
        )
        self._checkpoint(state)  # U5: a cancel during the call
        personas = list(output.personas)
        if len(personas) != generation.persona_count:
            raise _fail(
                ErrorCode.GENERATION_VERIFICATION_FAILED, "wrong number of personas"
            )
        renamed: dict[str, str] = {}
        if full_text:
            evidence, renamed = documents.evidence_from_synthesis(
                list(output.document_evidence),
                documents=[(i, s.source_file_id) for i, s, _ in state.documents],
                segment_ids={s.id for s in state.segments},
                max_quote_chars=int(config.ADVANCED_EVIDENCE_QUOTE_MAX_CHARS),
            )
            state.document_evidence = self._check_quotes(state, evidence)
        # NFR4.4: only the generation's own evidence IDs are kept
        known = {e.id for e in [*state.evidence, *state.document_evidence]}
        return [
            p.model_copy(
                update={
                    "evidence_ids": list(
                        dict.fromkeys(
                            renamed.get(i, i)
                            for i in p.evidence_ids
                            if renamed.get(i, i) in known
                        )
                    )
                }
            )
            for p in personas
        ]

    # --- verifying and the result -----------------------------------------------

    def _verify(
        self, state: _Run, personas: Sequence[prompts.AdvancedPersonaOutput]
    ) -> VerificationResult:
        assert state.plan is not None
        if state.report is None:
            # U4 BR27.2: documents alone rest on their checked quotes
            result = AnalysisVerifier.verify_documents(
                persona_evidence_ids=[p.evidence_ids for p in personas],
                evidence=state.document_evidence,
            )
            if not result.passed:
                logger.info(
                    "advanced generation verification failed: %s", result.reason
                )
                raise _fail(
                    ErrorCode.GENERATION_VERIFICATION_FAILED, "verification failed"
                )
            return result
        outputs = {
            cid: state.tools.output_of(cid) for cid in state.tools.tool_call_ids()
        }
        result = AnalysisVerifier.verify(
            method=state.plan.method,
            segments=state.segments,
            evidence=state.evidence,
            persona_segment_ids=[p.segment_id for p in personas],
            population=state.report.rows_read,
            outputs=outputs,
        )
        if not result.passed:
            logger.info("advanced generation verification failed: %s", result.reason)
            raise _fail(ErrorCode.GENERATION_VERIFICATION_FAILED, "verification failed")
        return result

    def _write_results(
        self,
        state: _Run,
        personas: Sequence[prompts.AdvancedPersonaOutput],
        verification: VerificationResult,
    ) -> None:
        """Record, candidates and result parts first, then ``done`` (W5-6, W5-7).

        U5 BR31.2 (review fix): the generation is read once more right before
        anything is written, and ``done`` is conditioned on no pending
        request. When ``done`` still loses (a reader ended it, or a cancel
        came in while writing), what was written is removed again and the run
        ends as the store says: cancelled, or quietly.
        """
        self._checkpoint(state)  # U5 BR31.2: a cancelled run keeps nothing
        generation = state.generation
        assert state.plan is not None
        now = self._clock()
        sha = self._sandbox.library_sha256
        unverified = verification.unverified_segment_ids
        run = build_analysis_run(
            generation=generation,
            plan=state.plan,
            report=state.report,
            sources=state.sources,
            tool_call_ids=state.tools.tool_call_ids(),
            library_sha256=sha,
            unverified_segment_ids=unverified,
            document_mode=state.document_mode,
        )
        record = build_record(
            generation=generation,
            plan=state.plan,
            report=state.report,
            sources=state.sources,
            calls=state.tools.calls,
            library_sha256=sha,
            now=now,
            document_mode=state.document_mode,
        )
        candidates = build_candidates(
            generation=generation,
            plan=state.plan,
            segments=state.segments,
            personas=personas,
            run=run,
            unverified_segment_ids=unverified,
            now=now,
        )
        if any(
            PersonaCandidateStore.item_size(c) > MAX_CANDIDATE_ITEM_BYTES
            for c in candidates
        ):
            raise _fail(ErrorCode.GENERATION_RESULT_TOO_LARGE, "candidate too large")
        done_updates: dict[str, Any] = {
            "analysis_run": run.to_dict(),
            "analysis_record_id": generation.generation_id,
            "candidate_ids": [c.candidate_id for c in candidates],
        }
        if state.documents:
            done_updates["evidence_stats"] = {
                "extracted": state.evidence_stats.get("extracted", 0),
                "discarded_unverified": state.evidence_stats.get(
                    "discarded_unverified", 0
                ),
            }
        self._check_item_size(state, done_updates)
        parts: dict[ResultPartName, list[dict[str, Any]]] = {
            ResultPartName.PREPARE_REPORTS: (
                [state.report.to_dict()] if state.report else []
            ),
            ResultPartName.DOCUMENT_REPORTS: [
                r.to_dict() for _, _, r in state.documents
            ],
            ResultPartName.SEGMENTS: [s.to_dict() for s in state.segments],
        }
        parts[ResultPartName.EVIDENCE] = [
            e.to_dict() for e in self._trimmed_evidence(state, personas, parts)
        ]
        self._judge_before_results(state)
        self._archive.deposit(record)
        self._candidates.put_all(candidates)
        part_counts = self._store.put_result_parts(
            generation.generation_id,
            parts,
            expires_at=generation.expires_at,
            max_part_bytes=config.ADVANCED_GENERATION_PART_MAX_BYTES,
            max_total_bytes=config.ADVANCED_GENERATION_PARTS_TOTAL_MAX_BYTES,
        )
        state.ending.set()
        if not self._store.transition(
            generation.generation_id,
            GenerationStatus.VERIFYING,
            GenerationStatus.DONE,
            updates={"part_counts": part_counts, **done_updates},
            removes=_ENDED_REMOVES,
            unless_requested=True,
        ):
            self._discard_results(state, candidates)
            self._judge_before_results(state)
            raise _StopRun()  # moved in a way the read cannot tell: end quietly
        self._close_stage(state)
        state.status = GenerationStatus.DONE
        # review fix (as the cancel, AC9.3.2): the lock and the marks go
        # before the result shows
        self._finish(state.generation)
        state.finished = True
        self._event(
            state,
            EventType.RESULT,
            persona_count=len(candidates),
            unverified=len(verification.unverified_segment_ids),
        )

    @staticmethod
    def _trimmed_evidence(
        state: _Run,
        personas: Sequence[prompts.AdvancedPersonaOutput],
        other_parts: Mapping[ResultPartName, Sequence[Mapping[str, Any]]],
    ) -> list[Evidence]:
        """All evidence, less what no persona cites when over 2MB (BR27.3, NFR5.38)."""

        def size(items: Sequence[Mapping[str, Any]]) -> int:
            return sum(
                len(json.dumps(i, ensure_ascii=False).encode("utf-8")) for i in items
            )

        budget = (
            int(config.ADVANCED_GENERATION_PARTS_TOTAL_MAX_BYTES)
            - sum(size(items) for items in other_parts.values())
            - _PARTS_SLACK_BYTES
        )
        return documents.trim_evidence(
            [*state.evidence, *state.document_evidence],
            cited={i for p in personas for i in p.evidence_ids},
            budget=budget,
            size_of=lambda item: size([item.to_dict()]),
        )

    @staticmethod
    def _check_item_size(state: _Run, done_updates: Mapping[str, Any]) -> None:
        """The generation item stays within its limit once ``done`` is written.

        entities.md: one item is up to 64KB; large results go to ResultPart.
        Measured before the candidates and the result parts are written.
        """
        assert state.plan is not None
        item = {
            **state.generation.to_dict(),
            "plan": state.plan.to_dict(),
            # the slice counts are not known yet; they are a few digits each
            "part_counts": {name.value: 0 for name in ResultPartName},
            "status": GenerationStatus.DONE.value,
            **done_updates,
        }
        if AdvancedGenerationStore.item_size(item) > int(
            config.ADVANCED_GENERATION_ITEM_MAX_BYTES
        ):
            raise _fail(ErrorCode.GENERATION_RESULT_TOO_LARGE, "generation too large")

    def _judge_before_results(self, state: _Run) -> None:
        """Read the generation once more; stop when it ended or a cancel waits.

        The watch reads only every ``control_poll_seconds``, so a reader's end
        or a cancel written after the last checkpoint is seen here (BR31.2).
        A failed read lets the conditioned ``done`` decide.

        Raises:
            _StopRun: Someone else ended it (BR32.5).
            _Cancelled: A cancel is pending.
        """
        try:
            current = self._store.get(state.generation.generation_id)
        except CodedError as e:
            logger.warning("advanced generation unreadable (%s)", e.code.value)
            return
        if current is None or current.status.is_terminal:
            state.ended_elsewhere.set()
            raise _StopRun()
        request = current.control_request
        if request is not None and request.action is ControlAction.CANCEL:
            state.cancel_request = request
            state.cancel_requested.set()
            raise _Cancelled()

    def _discard_results(
        self, state: _Run, candidates: Sequence[PersonaCandidate]
    ) -> None:
        """Remove the candidates and the pending record of a lost ``done``.

        The existing paths remove them: a candidate is claimed and deleted
        with the claim, the record as one persona's copy (its key is the
        generation ID). Each step stands alone and a failure is logged; the
        pending record's lifecycle and the candidates' TTL are the last net.
        The result parts stay: nothing reads them unless the run is ``done``.
        """
        owner = state.generation.owner_sub
        token = f"discard-{state.generation.generation_id}"
        now = self._clock()
        for candidate in candidates:
            try:
                self._candidates.claim(candidate.candidate_id, owner, token, now)
                self._candidates.delete(candidate.candidate_id, token)
            except Exception as e:  # noqa: BLE001 - each removal stands alone
                logger.error(
                    "advanced generation candidate not removed (%s)", type(e).__name__
                )
        try:
            self._archive.discard_copy(owner, state.generation.generation_id)
        except Exception as e:  # noqa: BLE001 - the lifecycle removes it later
            logger.error(
                "advanced generation record not removed (%s)", type(e).__name__
            )

    # --- failure and the end ----------------------------------------------------

    def _record_failure(
        self, state: _Run, code: ErrorCode, context: Mapping[str, Any]
    ) -> None:
        """Move the current stage to ``failed`` with the code only (W5)."""
        state.error_code = code
        safe = _safe_context(context)
        state.cause = safe.get("cause")
        updates: dict[str, Any] = {"error_code": code.value}
        if safe:
            updates["error_context"] = safe
        state.ending.set()
        try:
            moved = self._store.transition(
                state.generation.generation_id,
                state.status,
                GenerationStatus.FAILED,
                updates=updates,
                removes=_ENDED_REMOVES,
            )
            if moved:
                self._close_stage(state)
                # review fix (as the cancel, AC9.3.2): the lock and the marks
                # go before 失敗しました shows, so a restart is not refused
                self._finish(state.generation)
                state.finished = True
                self._event(
                    state, EventType.ERROR, error_code=code.value, kind=code.kind.value
                )
            else:
                # U5 BR32.5: a reader (or the requester) ended it first
                state.ended_elsewhere.set()
        except CodedError as e:
            logger.error("advanced generation failure not written (%s)", e.code.value)
        logger.info(
            "advanced generation failed: stage=%s error_code=%s",
            state.status.value,
            code.value,
        )

    def _stop_session(self, state: _Run) -> None:
        if state.session is None:
            return
        try:
            self._sandbox.stop_session(state.session)
        except Exception as e:  # noqa: BLE001 - the session's lifetime ends it
            logger.warning(
                "sandbox session not stopped (%s): session_id=%s",
                type(e).__name__,
                state.session.session_id,
            )

    def _finish(self, generation: AdvancedGeneration) -> None:
        now = self._clock()
        try:
            self._store.finish(
                generation.generation_id,
                generation.owner_sub,
                generation.source_file_ids,
                now=now,
                expires_at=int(now.timestamp())
                + int(config.ADVANCED_GENERATION_RESULT_READABLE_SECONDS),
            )
        except Exception as e:  # noqa: BLE001 - each end step stands alone
            logger.error("advanced generation end not written (%s)", type(e).__name__)

    def _release_slot(self) -> None:
        try:
            self._slots.release()
        except ValueError:
            logger.error("advanced generation slot released twice")

    def _start_watch(self, state: _Run) -> threading.Event:
        """The heartbeat thread, widened to read the generation (U5 NFR1.23).

        Every ``control_poll_seconds`` it reads the generation; every
        ``heartbeat_seconds`` it writes the heartbeat. Each read and each
        write is wrapped on its own: a failure is a warning with the count of
        failures in a row, and the next round goes on (NFR5.51).
        """
        stop = threading.Event()
        generation_id = state.generation.generation_id
        interval = min(self._control_poll_seconds, float(self._heartbeat_seconds))

        def watch() -> None:
            last_beat = time.monotonic()
            last_read = 0.0
            while not stop.wait(interval):
                if time.monotonic() - last_beat >= self._heartbeat_seconds:
                    last_beat = time.monotonic()
                    self._beat(state, generation_id)
                if time.monotonic() - last_read >= self._control_poll_seconds:
                    last_read = time.monotonic()
                    self._read_controls(state, generation_id)

        threading.Thread(
            target=watch, name="advanced-generation-watch", daemon=True
        ).start()
        return stop

    def _read_controls(self, state: _Run, generation_id: str) -> None:
        """One read of the watch: a cancel, or an end written by someone else."""
        try:
            current = self._store.get(generation_id)
            state.read_failures = 0
        except Exception as e:  # noqa: BLE001 - the watch never dies (NFR5.51)
            state.read_failures += 1
            logger.warning(
                "advanced generation watch read failed (%s): failures_in_a_row=%d",
                type(e).__name__,
                state.read_failures,
            )
            return
        if current is None or current.status.is_terminal:
            if not state.ending.is_set():
                state.ended_elsewhere.set()  # BR32.5: a reader ended it
            return
        request = current.control_request
        if request is not None and request.action is ControlAction.CANCEL:
            state.cancel_request = request
            state.cancel_requested.set()

    def _beat(self, state: _Run, generation_id: str) -> None:
        try:
            self._store.heartbeat(generation_id, now=self._clock())
            state.beat_failures = 0
        except Exception as e:  # noqa: BLE001 - a missed beat is logged
            state.beat_failures += 1
            logger.warning(
                "heartbeat not written (%s): failures_in_a_row=%d",
                type(e).__name__,
                state.beat_failures,
            )

    @staticmethod
    def _checkpoint(state: _Run) -> None:
        """Stop here when a reader ended the run or the user cancelled (BR31.2).

        Called at the stage boundaries, before and after the model calls, in
        the preparation's wait and before each extraction chunk.
        """
        if state.ended_elsewhere.is_set():
            raise _StopRun()
        if state.cancel_requested.is_set():
            raise _Cancelled()

    def _record_cancel(self, state: _Run) -> None:
        """Move the current stage to ``cancelled`` and say so (BR31.2, BR32.4).

        Nothing of the result is kept: the candidates and the record are only
        written at the very end (``_write_results``), after the last check.
        """
        state.ending.set()
        try:
            moved = self._store.transition(
                state.generation.generation_id,
                state.status,
                GenerationStatus.CANCELLED,
                removes=_ENDED_REMOVES,
            )
        except CodedError as e:
            logger.error("advanced generation cancel not written (%s)", e.code.value)
            return
        if not moved:
            state.ended_elsewhere.set()
            return
        self._close_stage(state)
        stage = state.status
        state.status = GenerationStatus.CANCELLED
        # AC9.3.2: the lock and the marks go before the screen hears of it, so
        # the same files can start again as soon as 中止しました shows
        self._finish(state.generation)
        state.finished = True
        self._event(state, EventType.CANCELLED)
        if state.cancel_request is not None:
            self._log_control_taken(state, state.cancel_request, stage=stage)

    def _event(self, state: _Run, event_type: EventType, **values: Any) -> None:
        self._append_event(state.generation, event_type, **values)

    def _append_event(
        self, generation: AdvancedGeneration, event_type: EventType, **values: Any
    ) -> None:
        try:
            self._store.append_event(
                generation.generation_id,
                event_type,
                summary=_summary(**values),
                now=self._clock(),
                expires_at=generation.expires_at,
            )
        except CodedError as e:
            logger.warning("progress event not written (%s)", e.code.value)

    def _log_finished(self, state: _Run) -> None:
        report = state.report
        log_advanced_event(
            "advanced_generation.finished",
            generation_id=state.generation.generation_id,
            status="failed" if state.error_code else state.status.value,
            stage=state.status.value,
            error_code=state.error_code.value if state.error_code else None,
            cause=state.cause,
            method=state.plan.method.value if state.plan else None,
            rows_read=report.rows_read if report else None,
            rows_excluded=report.rows_excluded if report else None,
            persona_count=state.generation.persona_count,
            durations_ms=dict(state.durations_ms),
            total_ms=int((time.monotonic() - state.started_monotonic) * 1000),
            tool_calls=len(state.tools.calls) if state.tools is not None else 0,
            # U5 NFR6.17
            replan_count=state.replan_count,
            waited_seconds=state.waited_seconds,
        )

    # --- U5: control logs (observability-design.md NFR6.16) ---------------------

    @staticmethod
    def _log(event: str, **fields: Any) -> None:
        try:
            log_advanced_event(event, **fields)
        except Exception as e:  # noqa: BLE001 - a log line never fails a run
            logger.error("%s not logged (%s)", event, type(e).__name__)

    def _log_control_taken(
        self,
        state: _Run,
        request: ControlRequest,
        *,
        stage: Optional[GenerationStatus] = None,
    ) -> None:
        latency = (self._clock() - request.requested_at).total_seconds()
        self._log(
            "advanced_generation.control_taken",
            generation_id=state.generation.generation_id,
            action=request.action.value,
            stage=(stage or state.status).value,
            replan_count=state.replan_count,
            waited_seconds=state.waited_seconds,
            latency_ms=max(0, int(latency * 1000)),
        )

    def _log_plan_wait_expired(
        self, generation_id: str, limit: str, waited_seconds: int
    ) -> None:
        self._log(
            "advanced_generation.plan_wait_expired",
            generation_id=generation_id,
            limit=limit,
            waited_seconds=waited_seconds,
        )

    # --- reads (owner only, no release check: BR1.1 exception) ------------------

    def _owned(self, principal: Principal, generation_id: str) -> AdvancedGeneration:
        generation = self._store.get(generation_id)
        if generation is None or not AccessPolicy.is_owner(
            principal, generation.owner_sub
        ):
            raise _fail(ErrorCode.GENERATION_NOT_FOUND, "generation not found")
        window = timedelta(seconds=config.ADVANCED_GENERATION_RESULT_READABLE_SECONDS)
        if (
            generation.finished_at is not None
            and self._clock() > generation.finished_at + window
        ):
            raise _fail(ErrorCode.GENERATION_EXPIRED, "generation expired")
        return generation

    # --- U5: requests from the screen (BR29.3, BR29.4, BR31.1) -------------------

    def request_control(
        self,
        principal: Principal,
        generation_id: str,
        action: str,
        revised_purpose: Optional[str] = None,
    ) -> None:
        """Write a cancel, an approve or a replan for the owner's generation.

        The request goes through the store, so the task running the
        generation takes it whichever task received it (ADR-009).

        Raises:
            AdvancedGenerationError: ``GENERATION_NOT_FOUND`` (someone else's
                or missing), ``GENERATION_EXPIRED``, ``GENERATION_PURPOSE_TOO_LONG``
                (replan), ``GENERATION_NOT_AWAITING_PLAN`` (approve / replan
                while not waiting, or with a request pending) or
                ``GENERATION_ALREADY_FINISHED`` (cancel of an ended one).
        """
        request = self._control_request(action, revised_purpose)
        self._write_control(self._owned(principal, generation_id), request)

    def decide_plan(
        self,
        principal: Principal,
        generation_id: str,
        action: str,
        revised_purpose: Optional[str] = None,
    ) -> None:
        """Approve the plan or replan it; nothing else (W5-1, BR29.3, BR29.4).

        A cancel has its own request (``request_control`` with ``cancel``);
        here it is refused like any other action, and nothing is written.

        Raises:
            AdvancedGenerationError: ``REQUEST_INVALID`` for any action but
                ``approve`` and ``replan``; otherwise as ``request_control``.
        """
        if action not in _PLAN_DECISIONS:
            raise _fail(ErrorCode.REQUEST_INVALID, "not a plan decision")
        request = self._control_request(action, revised_purpose)
        self._write_control(self._owned(principal, generation_id), request)

    def _control_request(
        self, action: str, revised_purpose: Optional[str]
    ) -> ControlRequest:
        """The request for ``action``, its instruction checked (BR29.4)."""
        try:
            kind = ControlAction(action)
        except ValueError:
            raise _fail(
                ErrorCode.GENERATION_NOT_AWAITING_PLAN, "unknown control action"
            ) from None
        revised: Optional[str] = None
        if kind is ControlAction.REPLAN:
            revised = revised_purpose or ""
            if len(revised) > PURPOSE_MAX_CHARS:
                raise _fail(
                    ErrorCode.GENERATION_PURPOSE_TOO_LONG,
                    "revised purpose too long",
                    max_chars=PURPOSE_MAX_CHARS,
                )
        return ControlRequest(
            action=kind, requested_at=self._clock(), revised_purpose=revised
        )

    def _write_control(
        self, generation: AdvancedGeneration, request: ControlRequest
    ) -> None:
        """Write ``request`` for the owner's ``generation`` and log it."""
        if request.action is ControlAction.CANCEL:
            self._request_cancel(generation, request)
        else:
            self._request_plan_decision(generation, request)
        self._log(
            "advanced_generation.control_requested",
            generation_id=generation.generation_id,
            action=request.action.value,
        )

    def _request_cancel(
        self, generation: AdvancedGeneration, request: ControlRequest
    ) -> None:
        if not generation.status.is_terminal and self._is_stalled(generation):
            # BR31.3: no thread runs it any more; the requester ends it
            if self._end_by_reader(
                generation,
                GenerationStatus.CANCELLED,
                field="heartbeat_at",
                value=generation.heartbeat_at,
            ):
                return
            # it moved since the read: a live thread takes the request instead
            generation = self._store.get(generation.generation_id) or generation
        else:
            generation = self._judge_reader(generation)  # BR31.1: a plan wait out
        if generation.status.is_terminal or not self._store.put_control_request(
            generation.generation_id,
            request,
            allowed_statuses=_RUNNING_STATES,
            require_empty=False,
        ):
            raise _fail(ErrorCode.GENERATION_ALREADY_FINISHED, "generation ended")

    def _request_plan_decision(
        self, generation: AdvancedGeneration, request: ControlRequest
    ) -> None:
        generation = self._judge_reader(generation)  # BR29.3: judged first
        if (
            generation.status is not GenerationStatus.AWAITING_PLAN_APPROVAL
            or generation.control_request is not None
            or not self._store.put_control_request(
                generation.generation_id,
                request,
                allowed_statuses=(GenerationStatus.AWAITING_PLAN_APPROVAL,),
                require_empty=True,
            )
        ):
            raise _fail(
                ErrorCode.GENERATION_NOT_AWAITING_PLAN, "generation is not waiting"
            )

    # --- U5: the reader's judgement (BR32, ADR-009) -----------------------------

    def _judge_reader(self, generation: AdvancedGeneration) -> AdvancedGeneration:
        """End a generation whose thread is gone or whose plan wait ran out.

        Run on the SSE re-reads, the page, the start and the control requests
        (NFR5.47). It only reads, unless one of these holds (BR32.1, BR32.2):

        - no heartbeat for 5 minutes (the plan wait included):
          ``GENERATION_STALLED``, conditioned on ``heartbeat_at``;
        - the card waited 15 minutes, or all the cards 60:
          ``GENERATION_TIMEOUT``, conditioned on ``awaiting_since``.

        Returns:
            The generation as it is after the judgement.
        """
        if generation.status.is_terminal:
            return generation
        now = self._clock()
        ended = False
        if self._is_stalled(generation):
            ended = self._end_by_reader(
                generation,
                GenerationStatus.FAILED,
                field="heartbeat_at",
                value=generation.heartbeat_at,
                code=ErrorCode.GENERATION_STALLED,
            )
        elif (
            generation.status is GenerationStatus.AWAITING_PLAN_APPROVAL
            and generation.awaiting_since is not None
        ):
            limit, total = self._plan_wait_limit(
                generation.awaiting_since, generation.waited_seconds, now
            )
            if limit is not None:
                ended = self._end_by_reader(
                    generation,
                    GenerationStatus.FAILED,
                    field="awaiting_since",
                    value=generation.awaiting_since,
                    code=ErrorCode.GENERATION_TIMEOUT,
                    context={"limit": limit[0], "value": limit[1] // 60},
                )
                if ended:
                    self._log_plan_wait_expired(
                        generation.generation_id, limit[0], total
                    )
        if not ended:
            return generation
        try:
            return self._store.get(generation.generation_id) or generation
        except CodedError as e:
            logger.warning("advanced generation unreadable (%s)", e.code.value)
            return generation

    def _is_stalled(self, generation: AdvancedGeneration) -> bool:
        """No heartbeat for more than 5 minutes (BR32.1); the start counts as one."""
        beat = generation.heartbeat_at or generation.created_at
        return self._clock() - beat > timedelta(
            seconds=config.ADVANCED_GENERATION_STALL_SECONDS
        )

    def _end_by_reader(
        self,
        generation: AdvancedGeneration,
        status: GenerationStatus,
        *,
        field: str,
        value: Optional[datetime],
        code: Optional[ErrorCode] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """End a generation no thread looks after any more (BR32.4).

        The move is conditioned on the state and ``field`` being what was read
        (NFR5.44); only the side that moved it writes the event, releases the
        lock and the marks, and stops the session by its ID. Each of those
        steps stands alone (NFR5.45).

        Returns:
            Whether this reader ended it.
        """
        updates: dict[str, Any] = {}
        if code is not None:
            updates["error_code"] = code.value
            safe = _safe_context(context or {})
            if safe:
                updates["error_context"] = safe
        try:
            moved = self._store.end_if_unchanged(
                generation.generation_id,
                generation.status,
                status,
                field=field,
                value=value,
                updates=updates,
                removes=_ENDED_REMOVES,
            )
        except CodedError as e:
            logger.warning("advanced generation reader end failed (%s)", e.code.value)
            return False
        if not moved:
            return False
        if code is not None:
            self._append_event(
                generation, EventType.ERROR, error_code=code.value, kind=code.kind.value
            )
        else:
            self._append_event(generation, EventType.CANCELLED)
        self._finish(generation)
        if generation.session_id:
            try:
                self._sandbox.stop_session_by_id(generation.session_id)
            except Exception as e:  # noqa: BLE001 - the session's lifetime ends it
                logger.warning(
                    "sandbox session not stopped (%s): session_id=%s",
                    type(e).__name__,
                    generation.session_id,
                )
        self._log(
            "advanced_generation.ended_by_reader",
            generation_id=generation.generation_id,
            stage=generation.status.value,
            error_code=code.value if code is not None else None,
            status=status.value,
            judged_by="reader",
        )
        return True

    def running_generation_id(self, principal: Principal) -> Optional[str]:
        """The owner's generation that still runs, to reopen its progress (W1-5).

        It is found through the owner's lock (one running generation per user,
        BR7.5). Only a display aid: an unreadable lock reads as "none".
        """
        try:
            lock = self._store.get_lock(principal.sub)
            if lock is None or not lock.is_held(self._clock()):
                return None
            generation = self._store.get(lock.generation_id)
        except CodedError as e:
            logger.warning("running generation unreadable (%s)", e.code.value)
            return None
        if generation is None or not AccessPolicy.is_owner(
            principal, generation.owner_sub
        ):
            return None
        # U5 NFR5.47: the page reopening a stalled generation ends it
        generation = self._judge_reader(generation)
        if generation.status.is_terminal:
            return None
        return generation.generation_id

    def elapsed_clock(self, principal: Principal, generation_id: str) -> ElapsedClock:
        """Elapsed time from ``created_at``; a finished one stops at ``finished_at``.

        Raises:
            AdvancedGenerationError: ``GENERATION_NOT_FOUND`` or
                ``GENERATION_EXPIRED`` (as ``stream_events``).
        """
        generation = self._owned(principal, generation_id)
        finished = generation.status.is_terminal
        # U5 W5-1.3: the plan waits are not counted; a shown card stops the clock
        waiting = (
            generation.status is GenerationStatus.AWAITING_PLAN_APPROVAL
            and generation.awaiting_since is not None
        )
        if finished and generation.finished_at is not None:
            end = generation.finished_at
        elif waiting and generation.awaiting_since is not None:
            end = generation.awaiting_since
        else:
            end = self._clock()
        seconds = max(
            0,
            int((end - generation.created_at).total_seconds())
            - generation.waited_seconds,
        )
        return ElapsedClock(seconds=seconds, running=not finished and not waiting)

    def stream_events(
        self,
        principal: Principal,
        generation_id: str,
        *,
        after_sequence: Optional[int] = None,
    ) -> GenerationProgress:
        """The generation and its events after ``after_sequence`` (C3, BR7.2).

        Raises:
            AdvancedGenerationError: ``GENERATION_NOT_FOUND`` (someone else's
                or missing), ``GENERATION_EXPIRED`` (24 hours after the end).
        """
        # U5 NFR5.47: every re-read of the stream also judges the generation
        generation = self._judge_reader(self._owned(principal, generation_id))
        events = self._store.read_events(generation_id, after_sequence=after_sequence)
        return GenerationProgress(
            generation=generation,
            events=tuple(events),
            failure_context=self._failure_context(principal, generation),
        )

    def _failure_context(
        self, principal: Principal, generation: AdvancedGeneration
    ) -> dict[str, Any]:
        """The stored values, plus the owner's file name for ``source_index``.

        U4 BR24.4: the name is looked up again in the owner's own files; it is
        never stored with the failure. A file gone since shows no name.
        """
        context = dict(generation.error_context or {})
        index = context.get("source_index")
        if (
            generation.status is GenerationStatus.FAILED
            and isinstance(index, int)
            and not isinstance(index, bool)
            and 0 <= index < len(generation.source_file_ids)
        ):
            names = self._display_names(principal, [generation.source_file_ids[index]])
            name = names.get(generation.source_file_ids[index])
            if name:
                context["filename"] = name
        return context

    def _display_names(
        self, principal: Principal, source_file_ids: Sequence[str]
    ) -> dict[str, str]:
        """Display names of the owner's files that are still there."""
        names: dict[str, str] = {}
        for source_file_id in source_file_ids:
            try:
                found = self._catalog.get_owned(
                    principal.sub,
                    source_file_id,
                    not_found_code=ErrorCode.GENERATION_SOURCE_NOT_FOUND,
                )
            except CodedError:
                continue
            names[source_file_id] = found.display_name
        return names

    def get_result(
        self, principal: Principal, generation_id: str
    ) -> AdvancedGenerationResult:
        """The finished result for section D (BR8.4).

        Raises:
            AdvancedGenerationError: ``GENERATION_NOT_FOUND`` (also while it is
                not done) or ``GENERATION_EXPIRED``.
        """
        generation = self._owned(principal, generation_id)
        if generation.status is not GenerationStatus.DONE:
            raise _fail(ErrorCode.GENERATION_NOT_FOUND, "generation has no result")
        parts = {
            name: self._store.read_result_parts(generation_id, name)
            for name in ResultPartName
        }
        candidates = [
            c
            for c in (self._candidates.get(i) for i in generation.candidate_ids or ())
            if c is not None and c.owner_sub == principal.sub
        ]
        params = generation.analysis_run.params if generation.analysis_run else {}
        return AdvancedGenerationResult(
            generation=generation,
            prepare_reports=tuple(
                PrepareReport.from_dict(r)
                for r in parts[ResultPartName.PREPARE_REPORTS]
            ),
            segments=tuple(
                Segment.from_dict(s) for s in parts[ResultPartName.SEGMENTS]
            ),
            evidence=tuple(
                Evidence.from_dict(e) for e in parts[ResultPartName.EVIDENCE]
            ),
            candidates=tuple(candidates),
            unverified_segment_ids=tuple(params.get("unverified_segment_ids") or ()),
            document_reports=tuple(
                DocumentReport.from_dict(r)
                for r in parts[ResultPartName.DOCUMENT_REPORTS]
            ),
            source_names=self._display_names(principal, generation.source_file_ids),
        )

    def get_record(self, principal: Principal, generation_id: str) -> AnalysisRecord:
        """The analysis record of the owner's finished generation (BR9.1, BR9.3).

        Raises:
            AdvancedGenerationError: ``GENERATION_NOT_FOUND`` or
                ``GENERATION_EXPIRED``.
        """
        generation = self._owned(principal, generation_id)
        if generation.analysis_record_id is None:
            raise _fail(ErrorCode.GENERATION_NOT_FOUND, "no analysis record")
        return self._archive.read_pending(
            principal.sub,
            generation.analysis_record_id,
            finished_at=generation.finished_at,
            now=self._clock(),
        )
