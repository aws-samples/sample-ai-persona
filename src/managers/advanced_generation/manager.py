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
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

from ...config import config
from ...models.advanced_generation import (
    PERSONA_COUNT_MAX,
    PERSONA_COUNT_MIN,
    PURPOSE_MAX_CHARS,
    AdvancedGeneration,
    EventType,
    GenerationEvent,
    GenerationStatus,
    ResultPartName,
)
from ...models.analysis import (
    AnalysisMethod,
    AnalysisPlan,
    AnalysisRecord,
    Evidence,
    PrepareReport,
    Segment,
)
from ...models.authz import Principal
from ...models.errors import CodedError, ErrorCode
from ...models.persona_candidate import MAX_CANDIDATE_ITEM_BYTES, PersonaCandidate
from ...models.source_file import SourceFile
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
from . import prompts
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
}
_RESELECT_CODES = frozenset({"TOO_FEW_COLUMNS", "EMPTY_CLUSTER"})
#: Values of ``error_context`` that may be kept and shown (never text).
_SAFE_CONTEXT_KEYS = frozenset({"cause", "http_status", "max_chars", "limit", "value"})
#: The two time limits, told apart in ``error_context`` (NFR1.6, NFR5.6, NFR5.7).
_LIMIT_GENERATION = "generation_minutes"
_LIMIT_PREPARE = "prepare_minutes"
_EVENT_TEXT_MAX = 150


class AdvancedGenerationError(CodedError):
    """A refused or failed advanced generation request."""


class _StopRun(Exception):
    """Internal: the run cannot go on (a transition was lost); no failure write."""


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


@dataclass
class _Run:
    """The state of one ``run`` (one thread)."""

    generation: AdvancedGeneration
    started_monotonic: float
    status: GenerationStatus = GenerationStatus.PREPARING
    session: Optional["SandboxSession"] = None
    source: Optional[SourceFile] = None
    report: Optional[PrepareReport] = None
    profile: dict[str, Any] = field(default_factory=dict)
    plan: Optional[AnalysisPlan] = None
    tools: Any = None
    segments: list[Segment] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    stage_started: dict[str, float] = field(default_factory=dict)
    durations_ms: dict[str, int] = field(default_factory=dict)
    error_code: Optional[ErrorCode] = None
    cause: Optional[str] = None

    @property
    def deadline(self) -> datetime:
        return self.generation.created_at + timedelta(
            seconds=config.ADVANCED_GENERATION_TIMEOUT_SECONDS
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
            self._source_file_store, error_type=AdvancedGenerationError
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
    ) -> str:
        """Check the request and write the start transaction (W4, BR7.5, BR6.4).

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
        self._check_input(ids, purpose, persona_count)
        self._catalog.ready_table(principal.sub, ids[0])
        if not self._slots.acquire(blocking=False):
            raise _fail(ErrorCode.ADVANCED_GENERATION_BUSY, "no free generation slot")
        try:
            return self._write_start(principal, ids, purpose, int(persona_count))
        except BaseException:
            self._release_slot()
            raise

    @staticmethod
    def _check_input(ids: list[str], purpose: str, persona_count: int) -> None:
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
        if len(ids) != 1:
            raise _fail(
                ErrorCode.UPLOAD_SOURCE_INVALID, "exactly one table", reason="tables"
            )

    def _write_start(
        self, principal: Principal, ids: list[str], purpose: str, persona_count: int
    ) -> str:
        now = self._clock()
        generation = AdvancedGeneration.create_new(
            owner_sub=principal.sub,
            source_file_ids=ids,
            purpose=purpose,
            persona_count=persona_count,
            now=now,
            ttl_seconds=config.ADVANCED_GENERATION_ITEM_TTL_SECONDS,
        )
        lock_until = now + timedelta(seconds=config.ADVANCED_GENERATION_LOCK_SECONDS)
        try:
            self._store.create_with_lock(
                generation, lock_until=lock_until, in_use_until=lock_until, now=now
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
        state = _Run(generation=generation, started_monotonic=time.monotonic())
        stop_heartbeat = self._start_heartbeat(generation_id)
        try:
            self._run_stages(state)
        except _StopRun:
            logger.warning("advanced generation stopped: a transition was lost")
        except CodedError as e:
            self._record_failure(state, e.code, e.context)
        except Exception as e:  # noqa: BLE001 - the thread must end cleanly
            logger.error("advanced generation failed (%s)", type(e).__name__)
            self._record_failure(state, ErrorCode.GENERATION_OPERATION_FAILED, {})
        finally:
            # Each end step stands alone; the slot goes back last and once (§2).
            try:
                stop_heartbeat.set()
                self._stop_session(state)
                self._finish(state.generation)
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
        self._enter(state, GenerationStatus.PLANNING)
        self._plan(state)
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
        updates: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Move to ``stage`` (conditioned on the current one) and say so (BR7.1)."""
        self._check_deadline(state)
        if not first:
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
        """No model stage starts with under 10 minutes left (§2)."""
        remaining = state.deadline - self._clock()
        if remaining < timedelta(
            seconds=config.ADVANCED_GENERATION_MODEL_MIN_REMAINING_SECONDS
        ):
            raise self._generation_timeout("too little time for the model")

    # --- preparing --------------------------------------------------------------

    def _prepare(self, state: _Run) -> None:
        generation = state.generation
        source = self._catalog.get_owned(
            generation.owner_sub,
            generation.source_file_ids[0],
            not_found_code=ErrorCode.GENERATION_SOURCE_NOT_FOUND,
        )
        state.source = source
        sandbox = self._sandbox
        state.session = sandbox.start_session(
            generation.generation_id, config.ADVANCED_GENERATION_SESSION_TIMEOUT_SECONDS
        )
        self._bring_in(state, source)
        state.report = self._run_prepare(state, source)
        profile = sandbox.call(
            state.session, "profile_dataset", {"source_id": source.source_file_id}
        )
        state.profile = dict(self._library_value(profile))

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

    def _run_prepare(self, state: _Run, source: SourceFile) -> PrepareReport:
        assert source.metadata is not None and state.session is not None
        sandbox = self._sandbox
        args = {
            "source_id": source.source_file_id,
            "metadata": {
                "encoding": source.metadata.encoding.value,
                "delimiter": source.metadata.delimiter,
            },
        }
        command_id = sandbox.start_command(state.session, "prepare", args)
        started = self._clock()
        limit = timedelta(seconds=config.ADVANCED_GENERATION_PREPARE_TIMEOUT_SECONDS)
        last_rows: Optional[int] = None
        while True:
            status = sandbox.poll_command(state.session, command_id)
            rows = (status.get("progress") or {}).get("rows")
            if isinstance(rows, int) and rows != last_rows:
                last_rows = rows
                self._event(
                    state,
                    EventType.PROGRESS,
                    message=f"{rows} 行を読み込みました",
                    detail={"rows": rows},
                )
            if status.get("done"):
                break
            now = self._clock()
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
        value = self._library_value(status.get("result") or {"ok": False})
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

    @staticmethod
    def _library_value(result: Mapping[str, Any]) -> Mapping[str, Any]:
        """The value of a C9 result, or the user-facing code of its error."""
        if result.get("ok") and isinstance(result.get("value"), Mapping):
            return result["value"]  # type: ignore[no-any-return]
        error = result.get("error") or {}
        code = _LIBRARY_CODES.get(str(error.get("code")))
        if code is not None:
            raise _fail(code, "preparation refused the table")
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

    def _plan(self, state: _Run) -> None:
        generation = state.generation
        guide = AnalysisPolicy.guide_method(generation.purpose, state.profile)
        agent = self._agents.create_advanced_planning_agent(
            prompts.PLANNING_INSTRUCTIONS
        )
        plan: Optional[AnalysisPlan] = None
        for attempt in range(1, PLAN_ATTEMPTS + 1):
            self._before_model(state)
            raw = self._agents.run_advanced_planning(
                agent,
                prompts.planning_prompt(generation.persona_count, attempt),
                {"purpose": generation.purpose, "profile": self._profile_text(state)},
            )
            plan = AnalysisPolicy.settle_plan(
                raw, guide=guide, attempt=attempt, columns=self._columns(state)
            )
            if plan is not None:
                break
        assert plan is not None
        state.plan = plan
        self._event(
            state,
            EventType.PLAN,
            method=plan.method.value,
            interpreted_purpose=plan.interpreted_purpose[:_EVENT_TEXT_MAX],
        )

    def _analyze(self, state: _Run) -> None:
        assert state.plan is not None and state.source is not None
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

    def _synthesize(self, state: _Run) -> list[prompts.AdvancedPersonaOutput]:
        assert state.report is not None
        self._before_model(state)
        generation = state.generation
        agent = self._agents.create_advanced_synthesis_agent(
            prompts.SYNTHESIS_INSTRUCTIONS
        )
        report = state.report
        output = self._agents.run_advanced_synthesis(
            agent,
            prompts.synthesis_prompt(generation.persona_count),
            {
                "purpose": generation.purpose,
                "segments": json.dumps(
                    [s.to_dict() for s in state.segments], ensure_ascii=False
                ),
                "evidence": json.dumps(
                    [e.to_dict() for e in state.evidence], ensure_ascii=False
                ),
                "prepare_report": json.dumps(
                    {
                        "rows_read": report.rows_read,
                        "rows_excluded": report.rows_excluded,
                        "columns": [c.to_dict() for c in report.columns],
                    },
                    ensure_ascii=False,
                ),
            },
            prompts.AdvancedPersonaListOutput,
        )
        personas = list(output.personas)
        if len(personas) != generation.persona_count:
            raise _fail(
                ErrorCode.GENERATION_VERIFICATION_FAILED, "wrong number of personas"
            )
        return personas

    # --- verifying and the result -----------------------------------------------

    def _verify(
        self, state: _Run, personas: Sequence[prompts.AdvancedPersonaOutput]
    ) -> VerificationResult:
        assert state.plan is not None and state.report is not None
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
        """Record, candidates and result parts first, then ``done`` (W5-6, W5-7)."""
        generation = state.generation
        assert state.plan is not None and state.report is not None
        assert state.source is not None
        now = self._clock()
        sha = self._sandbox.library_sha256
        unverified = verification.unverified_segment_ids
        run = build_analysis_run(
            generation=generation,
            plan=state.plan,
            report=state.report,
            source=state.source,
            tool_call_ids=state.tools.tool_call_ids(),
            library_sha256=sha,
            unverified_segment_ids=unverified,
        )
        self._archive.deposit(
            build_record(
                generation=generation,
                plan=state.plan,
                report=state.report,
                source=state.source,
                calls=state.tools.calls,
                library_sha256=sha,
                now=now,
            )
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
        self._check_item_size(state, done_updates)
        self._candidates.put_all(candidates)
        part_counts = self._store.put_result_parts(
            generation.generation_id,
            {
                ResultPartName.PREPARE_REPORTS: [state.report.to_dict()],
                ResultPartName.SEGMENTS: [s.to_dict() for s in state.segments],
                ResultPartName.EVIDENCE: [e.to_dict() for e in state.evidence],
            },
            expires_at=generation.expires_at,
            max_part_bytes=config.ADVANCED_GENERATION_PART_MAX_BYTES,
            max_total_bytes=config.ADVANCED_GENERATION_PARTS_TOTAL_MAX_BYTES,
        )
        if not self._store.transition(
            generation.generation_id,
            GenerationStatus.VERIFYING,
            GenerationStatus.DONE,
            updates={"part_counts": part_counts, **done_updates},
        ):
            raise _StopRun()
        self._close_stage(state)
        state.status = GenerationStatus.DONE
        self._event(
            state,
            EventType.RESULT,
            persona_count=len(candidates),
            unverified=len(verification.unverified_segment_ids),
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
        try:
            moved = self._store.transition(
                state.generation.generation_id,
                state.status,
                GenerationStatus.FAILED,
                updates=updates,
            )
            if moved:
                self._close_stage(state)
                self._event(
                    state, EventType.ERROR, error_code=code.value, kind=code.kind.value
                )
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

    def _start_heartbeat(self, generation_id: str) -> threading.Event:
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self._heartbeat_seconds):
                try:
                    self._store.heartbeat(generation_id, now=self._clock())
                except Exception as e:  # noqa: BLE001 - a missed beat is logged
                    logger.warning("heartbeat not written (%s)", type(e).__name__)

        threading.Thread(target=beat, daemon=True).start()
        return stop

    def _event(self, state: _Run, event_type: EventType, **values: Any) -> None:
        try:
            self._store.append_event(
                state.generation.generation_id,
                event_type,
                summary=_summary(**values),
                now=self._clock(),
                expires_at=state.generation.expires_at,
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
        if (
            generation is None
            or not AccessPolicy.is_owner(principal, generation.owner_sub)
            or generation.status.is_terminal
        ):
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
        end = (
            generation.finished_at
            if finished and generation.finished_at is not None
            else self._clock()
        )
        seconds = max(0, int((end - generation.created_at).total_seconds()))
        return ElapsedClock(seconds=seconds, running=not finished)

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
        generation = self._owned(principal, generation_id)
        events = self._store.read_events(generation_id, after_sequence=after_sequence)
        return GenerationProgress(generation=generation, events=tuple(events))

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
