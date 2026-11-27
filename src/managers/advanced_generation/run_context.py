"""The run context: what every stage of an advanced generation shares.

It holds the Services (built on first use), the clock, the sleep, the free
slots and the intervals, and the helpers every stage calls: entering a stage,
the deadline and the time limits, the events, the logs, the checkpoint, the
sandbox's values and the quote check.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

from ...config import config
from ...models.advanced_generation import (
    AdvancedGeneration,
    ControlRequest,
    EventType,
    GenerationStatus,
)
from ...models.analysis import Evidence
from ...models.errors import CodedError, ErrorCode
from ...services.service_factory import service_factory
from ...services.stores.persona_candidate_store import PersonaCandidateStore
from ..components.analysis_record_archive import AnalysisRecordArchive
from ..components.source_file_catalog import SourceFileCatalog
from ..shared.advanced_generation_log import log_advanced_event
from .errors import AdvancedGenerationError, Cancelled, StopRun, fail
from .run_types import Run

if TYPE_CHECKING:
    from ...services.agent_service import AgentService
    from ...services.s3_service import S3Service
    from ...services.sandbox_service import SandboxService
    from ...services.stores.advanced_generation_store import AdvancedGenerationStore
    from ...services.stores.source_file_store import SourceFileStore

logger = logging.getLogger(__name__)

#: ``cause`` of a sandbox failure (the same value SandboxService uses).
CAUSE_SANDBOX = "code_interpreter"
#: Library error codes -> what the user is told.
_LIBRARY_CODES = {
    "SOURCE_UNREADABLE": ErrorCode.GENERATION_SOURCE_UNREADABLE,
    "TOO_MANY_BAD_ROWS": ErrorCode.GENERATION_SOURCE_UNREADABLE,
    "OUT_OF_MEMORY": ErrorCode.GENERATION_CAPACITY,
    # Document failures
    "DOCUMENT_NO_TEXT": ErrorCode.DOCUMENT_NO_TEXT,
    "DOCUMENT_ENCRYPTED": ErrorCode.DOCUMENT_ENCRYPTED,
}
#: Values of ``error_context`` that may be kept and shown (never text).
_SAFE_CONTEXT_KEYS = frozenset(
    {
        "cause",
        "http_status",
        "max_chars",
        "limit",
        "value",
        # Counts and line numbers, integers only
        "rows_read",
        "rows_excluded",
        "columns_count",
        "row_examples",
        "required_rows",
        "persona_count",
        # The document's place and the character counts
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
#: At most this many line numbers are kept in ``row_examples``.
_ROW_EXAMPLES_MAX = 5
#: The whole generation's time limit, told apart in ``error_context``.
_LIMIT_GENERATION = "generation_minutes"
#: One plan card and every card together.
_LIMIT_PLAN_WAIT = "plan_wait_minutes"
_LIMIT_PLAN_WAIT_TOTAL = "plan_wait_total_minutes"
#: Attributes that mean nothing once the generation has ended.
ENDED_REMOVES = ("control_request", "awaiting_since")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _summary(**values: Any) -> str:
    return json.dumps(values, ensure_ascii=False, default=str)


def safe_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Only short, known values survive into the stored failure."""
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


class RunContext:
    """The Services, the clock, the slots and the helpers every stage shares."""

    def __init__(
        self,
        *,
        generation_store: Optional["AdvancedGenerationStore"],
        source_file_store: Optional["SourceFileStore"],
        candidate_store: Optional[PersonaCandidateStore],
        s3_service: Optional["S3Service"],
        agent_service: Optional["AgentService"],
        sandbox: Optional["SandboxService"],
        tool_factory: Optional[Callable[..., Any]],
        clock: Optional[Callable[[], datetime]],
        sleep: Callable[[float], None],
        max_concurrent: Optional[int],
        heartbeat_seconds: Optional[float],
        poll_seconds: Optional[float],
        control_poll_seconds: Optional[float],
    ) -> None:
        """Omitted Services come from the factory on first use."""
        self._generation_store = generation_store
        self._source_file_store = source_file_store
        self._candidate_store = candidate_store
        self._s3_service = s3_service
        self._agent_service = agent_service
        self._sandbox_service = sandbox
        self.tool_factory = tool_factory or service_factory.create_analysis_tools
        self.clock = clock or _utc_now
        self.sleep = sleep
        self.slots = threading.BoundedSemaphore(
            max_concurrent or config.ADVANCED_GENERATION_MAX_CONCURRENT
        )
        self.heartbeat_seconds = (
            heartbeat_seconds or config.ADVANCED_GENERATION_HEARTBEAT_SECONDS
        )
        self.poll_seconds = poll_seconds
        #: How often the watch and the plan wait read the generation
        self.control_poll_seconds = float(
            control_poll_seconds
            if control_poll_seconds is not None
            else config.ADVANCED_GENERATION_CONTROL_POLL_SECONDS
        )

    # --- lazy dependencies --------------------------------------------

    @property
    def store(self) -> "AdvancedGenerationStore":
        if self._generation_store is None:
            self._generation_store = service_factory.get_advanced_generation_store()
        return self._generation_store

    @property
    def catalog(self) -> SourceFileCatalog:
        if self._source_file_store is None:
            self._source_file_store = service_factory.get_source_file_store()
        return SourceFileCatalog(
            self._source_file_store,
            error_type=AdvancedGenerationError,
            clock=self.clock,
        )

    @property
    def candidates(self) -> PersonaCandidateStore:
        if self._candidate_store is None:
            self._candidate_store = service_factory.get_persona_candidate_store()
        return self._candidate_store

    @property
    def s3(self) -> "S3Service":
        if self._s3_service is None:
            self._s3_service = service_factory.get_s3_service()
        return self._s3_service

    @property
    def archive(self) -> AnalysisRecordArchive:
        return AnalysisRecordArchive(self.s3, error_type=AdvancedGenerationError)

    @property
    def agents(self) -> "AgentService":
        if self._agent_service is None:
            self._agent_service = service_factory.get_agent_service()
        return self._agent_service

    @property
    def sandbox(self) -> "SandboxService":
        if self._sandbox_service is None:
            self._sandbox_service = service_factory.get_sandbox_service()
        return self._sandbox_service

    def in_process(self) -> bool:
        """The sandbox runs in this process (only an injected one can)."""
        return getattr(self._sandbox_service, "kind", "code_interpreter") == (
            "in_process"
        )

    # --- stages, deadline and time limits ---------------------------------------

    def enter(
        self,
        state: Run,
        stage: GenerationStatus,
        *,
        first: bool = False,
        moved: bool = False,
        updates: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Move to ``stage`` (conditioned on the current one) and say so.

        ``moved``: the store already moved it (the plan wait's own writes).
        Then the run takes the stage first, so a cancel or the deadline found
        by the checks is recorded from the stage the store is at.
        """
        if moved:
            self.close_stage(state)
            state.status = stage
        self.checkpoint(state)
        self.check_deadline(state)
        if not first and not moved:
            clean = {k: v for k, v in (updates or {}).items() if v is not None}
            if not self.store.transition(
                state.generation.generation_id, state.status, stage, updates=clean
            ):
                raise StopRun()
        if not moved:
            self.close_stage(state)
            state.status = stage
        state.stage_started[stage.value] = time.monotonic()
        now = self.clock()
        self.event(state, EventType.STAGE, status=stage.value, started_at=now)
        log_advanced_event(
            "advanced_generation.stage",
            generation_id=state.generation.generation_id,
            stage=stage.value,
            elapsed_ms=int((time.monotonic() - state.started_monotonic) * 1000),
        )

    @staticmethod
    def close_stage(state: Run) -> None:
        started = state.stage_started.get(state.status.value)
        if started is not None:
            state.durations_ms[state.status.value] = int(
                (time.monotonic() - started) * 1000
            )

    @staticmethod
    def timeout(message: str, limit: str, seconds: int) -> AdvancedGenerationError:
        """``GENERATION_TIMEOUT`` naming which limit ran out, in minutes."""
        return fail(
            ErrorCode.GENERATION_TIMEOUT, message, limit=limit, value=seconds // 60
        )

    def generation_timeout(self, message: str) -> AdvancedGenerationError:
        return self.timeout(
            message, _LIMIT_GENERATION, config.ADVANCED_GENERATION_TIMEOUT_SECONDS
        )

    def check_deadline(self, state: Run) -> None:
        if self.clock() > state.deadline:
            raise self.generation_timeout("generation over 60 minutes")

    def before_model(self, state: Run) -> None:
        """No model stage starts with under 10 minutes left, nor after a cancel."""
        self.checkpoint(state)
        remaining = state.deadline - self.clock()
        if remaining < timedelta(
            seconds=config.ADVANCED_GENERATION_MODEL_MIN_REMAINING_SECONDS
        ):
            raise self.generation_timeout("too little time for the model")

    @staticmethod
    def use_guide_table(state: Run, alias: str) -> None:
        guide = next(t for t in state.tables if t.alias == alias)
        state.source, state.report, state.profile = (
            guide.source,
            guide.report,
            guide.profile,
        )

    @staticmethod
    def plan_wait_limit(
        awaiting_since: datetime, waited_seconds: int, now: datetime
    ) -> tuple[Optional[tuple[str, int]], int]:
        """The plan wait that ran out, if any, and all the seconds waited.

        One card waits ``ADVANCED_GENERATION_PLAN_WAIT_SECONDS``; all
        the cards ``ADVANCED_GENERATION_PLAN_WAIT_TOTAL_SECONDS``.
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

    @staticmethod
    def checkpoint(state: Run) -> None:
        """Stop here when a reader ended the run or the user cancelled.

        Called at the stage boundaries, before and after the model calls, in
        the preparation's wait and before each extraction chunk.
        """
        if state.ended_elsewhere.is_set():
            raise StopRun()
        if state.cancel_requested.is_set():
            raise Cancelled()

    # --- events and logs ----------------------------------------------------------

    def event(self, state: Run, event_type: EventType, **values: Any) -> None:
        self.append_event(state.generation, event_type, **values)

    def append_event(
        self, generation: AdvancedGeneration, event_type: EventType, **values: Any
    ) -> None:
        try:
            self.store.append_event(
                generation.generation_id,
                event_type,
                summary=_summary(**values),
                now=self.clock(),
                expires_at=generation.expires_at,
            )
        except CodedError as e:
            logger.warning("progress event not written (%s)", e.code.value)

    @staticmethod
    def log(event: str, **fields: Any) -> None:
        try:
            log_advanced_event(event, **fields)
        except Exception as e:  # noqa: BLE001 - a log line never fails a run
            logger.error("%s not logged (%s)", event, type(e).__name__)

    def log_control_taken(
        self,
        state: Run,
        request: ControlRequest,
        *,
        stage: Optional[GenerationStatus] = None,
    ) -> None:
        latency = (self.clock() - request.requested_at).total_seconds()
        self.log(
            "advanced_generation.control_taken",
            generation_id=state.generation.generation_id,
            action=request.action.value,
            stage=(stage or state.status).value,
            replan_count=state.replan_count,
            waited_seconds=state.waited_seconds,
            latency_ms=max(0, int(latency * 1000)),
        )

    def log_plan_wait_expired(
        self, generation_id: str, limit: str, waited_seconds: int
    ) -> None:
        self.log(
            "advanced_generation.plan_wait_expired",
            generation_id=generation_id,
            limit=limit,
            waited_seconds=waited_seconds,
        )

    # --- the sandbox's values and the quote check --------------------------------

    def check_quotes(self, state: Run, evidence: Sequence[Evidence]) -> list[Evidence]:
        """Keep the evidence whose quote is in its document.

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
                value = self.library_value(
                    self.sandbox.call(
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
                    raise fail(
                        ErrorCode.GENERATION_SANDBOX_UNAVAILABLE,
                        "bad quote check",
                        cause=CAUSE_SANDBOX,
                    )
                kept_ids |= {e.id for e, ok in zip(group, found) if ok is True}
        kept = [e for e in evidence if e.id in kept_ids]
        discarded = len(evidence) - len(kept)
        stats = state.evidence_stats
        stats["extracted"] = stats.get("extracted", 0) + len(evidence)
        stats["discarded_unverified"] = stats.get("discarded_unverified", 0) + discarded
        if discarded:
            self.event(
                state,
                EventType.PROGRESS,
                message=f"文書と照合できない根拠 {discarded} 件を除きました",
                detail={"discarded_unverified": discarded},
            )
        return kept

    @staticmethod
    def library_value(
        result: Mapping[str, Any], *, strict: bool = False
    ) -> Mapping[str, Any]:
        """The value of a sandbox result, or the user-facing code of its error.

        ``strict``: a value the Service had to cut cannot be used
        (a chunk's text, the quote check) and is ``GENERATION_OPERATION_FAILED``.
        """
        if result.get("ok") and isinstance(result.get("value"), Mapping):
            value: Mapping[str, Any] = result["value"]
            if strict and (result.get("truncated") or value.get("truncated")):
                raise fail(
                    ErrorCode.GENERATION_OPERATION_FAILED, "sandbox output was cut"
                )
            return value
        error = result.get("error") or {}
        code = _LIBRARY_CODES.get(str(error.get("code")))
        if code is not None:
            context = error.get("context")
            raise fail(
                code,
                "the sandbox refused the source",
                **(safe_context(context) if isinstance(context, Mapping) else {}),
            )
        raw = str(error.get("code"))
        # Library codes are fixed upper-case names; anything else is not logged.
        logger.warning(
            "sandbox function failed: library_code=%s",
            raw if raw.replace("_", "").isalpha() and raw.isupper() else "other",
        )
        raise fail(
            ErrorCode.GENERATION_SANDBOX_UNAVAILABLE,
            "sandbox function failed",
            cause=CAUSE_SANDBOX,
        )
