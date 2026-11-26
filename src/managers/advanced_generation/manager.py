"""The advanced generation (beta) of personas from tables and documents.

Up to three tables (``source_1`` ...), prepared one at a time; with the
AI detailed analysis (decided at the start) the join keys are found,
``run_code`` is given with a budget and a change of the
library or a Parquet fails the generation.

``start_generation`` checks the request and writes the owner's lock, the
generation and the file's in-use mark in one transaction;
the Router then hands ``run`` to its own thread pool. ``run`` goes through
the stages in order and never raises::

    preparing -> planning -> running -> synthesizing -> verifying -> done
                     (any stage) -> failed

- One sandbox session per generation, always stopped at the end.
  The table goes in through a 15-minute presigned GET made here;
  a refused download gets one new URL. The URL is never logged or stored.
- Every transition is conditioned on the current state; every stage
  writes a ``stage`` event and a heartbeat runs every 30 seconds.
- The whole generation has 60 minutes; no model stage starts with less than
  10 minutes left.
- The end releases the lock and the mark, writes ``finished_at`` (results
  readable 24 hours) and returns the slot, each step on its own.
- Free slots are a ``BoundedSemaphore`` of ``ADVANCED_GENERATION_MAX_CONCURRENT``;
  a full task answers ``ADVANCED_GENERATION_BUSY``.

Failures keep the error code and safe values only, never row data or
exception text; logs go through the allowed-field list.

Generation controls: requests travel through the store,
so whichever task receives them, the task running the generation takes them.

- ``confirm_plan`` stops the run in ``awaiting_plan_approval`` after the plan;
  approve, replan (no limit) or cancel; 15 minutes per card, 60 in total, and
  the waits move the 60-minute deadline back.
- A watch thread reads the generation every ``control_poll_seconds`` (and
  writes the heartbeat); the run checks at its boundaries and stops on a
  cancel, or quietly when someone else ended it. Right
  before the results it reads once more, and ``done`` loses to a pending
  cancel; a lost ``done`` removes the candidates and the record again.
- Every end (done, failed, cancelled) releases the lock and the marks before
  its event is written and before the session stops, so the same files can
  start again at once; a start also frees a lock left by an ended generation.
- Readers (the stream, the page, the start, the requests) end a generation
  whose heartbeat is 5 minutes old, or whose plan wait ran out.

This module is the entry: the public methods, the owner check, the start and
the order of the stages. The stages live in ``preparation``,
``analysis_stage`` and ``finishing``, the screen's requests and reads in
``controls``, and what they share in ``run_context``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

from ...config import config
from ...models.advanced_generation import (
    PERSONA_COUNT_MAX,
    PERSONA_COUNT_MIN,
    PURPOSE_MAX_CHARS,
    AdvancedGeneration,
    ControlAction,
    EventType,
    GenerationStatus,
)
from ...models.analysis import AnalysisRecord
from ...models.authz import Principal
from ...models.errors import CodedError, ErrorCode
from ...models.source_file import SourceKind
from ...services.service_factory import service_factory
from ...services.stores.advanced_generation_store import AdvancedGenerationStoreError
from ...services.stores.persona_candidate_store import PersonaCandidateStore
from ..components.access_policy import AccessPolicy
from ..components.advanced_generation_policy import AdvancedGenerationPolicy
from . import prompts
from .analysis_stage import AnalysisStage
from .controls import ControlDesk
from .errors import AdvancedGenerationError, Cancelled, StopRun, fail
from .finishing import FinishingStage
from .persona_check import check_personas
from .preparation import PreparationStage
from .run_context import RunContext
from .run_types import AdvancedGenerationResult, ElapsedClock, GenerationProgress, Run

if TYPE_CHECKING:
    from ...services.agent_service import AgentService
    from ...services.s3_service import S3Service
    from ...services.sandbox_service import SandboxService
    from ...services.stores.advanced_generation_store import AdvancedGenerationStore
    from ...services.stores.app_settings_store import AppSettingsStore
    from ...services.stores.source_file_store import SourceFileStore

logger = logging.getLogger(__name__)

#: The actions the plan card sends (a cancel has its own route).
_PLAN_DECISIONS = frozenset({ControlAction.APPROVE.value, ControlAction.REPLAN.value})


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
        store, so the tab strip never builds the sandbox or the agents.
        """
        self._advanced_generation_policy = AdvancedGenerationPolicy(
            settings_store or service_factory.get_app_settings_store()
        )
        self._context = RunContext(
            generation_store=generation_store,
            source_file_store=source_file_store,
            candidate_store=candidate_store,
            s3_service=s3_service,
            agent_service=agent_service,
            sandbox=sandbox,
            tool_factory=tool_factory,
            clock=clock,
            sleep=sleep,
            max_concurrent=max_concurrent,
            heartbeat_seconds=heartbeat_seconds,
            poll_seconds=poll_seconds,
            control_poll_seconds=control_poll_seconds,
        )
        self._preparation = PreparationStage(self._context)
        self._analysis = AnalysisStage(self._context)
        self._finishing = FinishingStage(self._context)
        self._controls = ControlDesk(self._context, self._finishing)

    # --- availability and start -------------------------------------------------

    def is_available(self, principal: Principal) -> bool:
        """Whether the tab and the page are shown to ``principal``."""
        return self._advanced_generation_policy.is_available(principal)

    def detailed_analysis_offered(self, principal: Principal) -> bool:
        """Whether the detailed settings show "AI による詳細分析"."""
        return self._advanced_generation_policy.availability(
            principal, in_process=self._context.in_process()
        ).detailed

    def start_generation(
        self,
        principal: Principal,
        *,
        source_file_ids: Sequence[str],
        purpose: str,
        persona_count: int,
        confirm_plan: bool = False,
        use_detailed_analysis: bool = False,
    ) -> str:
        """Check the request and write the start transaction.

        ``confirm_plan`` stops the run after the plan until the user
        approves it.

        The order is: release conditions, input, the free slot, the lock.
        A refused transaction gives the slot back.

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
        # Documents alone, or up to three tables
        found = self._context.catalog.ready_sources(principal.sub, ids)
        # Decided now and kept; a box sent without the
        # administrator's permission is quietly off
        detailed = (
            bool(use_detailed_analysis)
            and any(s.kind is SourceKind.TABLE for s in found)
            and self.detailed_analysis_offered(principal)
        )
        self._judge_lock_holder(principal)  # a stalled one lets go
        if not self._context.slots.acquire(blocking=False):
            raise fail(ErrorCode.ADVANCED_GENERATION_BUSY, "no free generation slot")
        try:
            return self._write_start(
                principal,
                ids,
                purpose,
                int(persona_count),
                bool(confirm_plan),
                detailed,
            )
        except BaseException:
            self._finishing.release_slot()
            raise

    def _judge_lock_holder(self, principal: Principal) -> None:
        """Judge the generation the owner's lock points at.

        A stalled one is ended (which releases its lock and marks) so the new
        start can take them; one already ended has its lock and marks released
        (conditioned on its ID); a live one keeps them and the start is refused
        by the transaction as before. Only an aid: a failed read lets the
        transaction decide.
        """
        try:
            lock = self._context.store.get_lock(principal.sub)
            if lock is None or not lock.is_held(self._context.clock()):
                return
            generation = self._context.store.get(lock.generation_id)
        except CodedError as e:
            logger.warning("lock holder unreadable (%s)", e.code.value)
            return
        if generation is None or generation.owner_sub != principal.sub:
            return
        if generation.status.is_terminal:
            # An ended generation whose holds were not released
            # (yet) never blocks the next start; only its own holds go
            self._context.store.release_holds(
                generation.generation_id,
                generation.owner_sub,
                generation.source_file_ids,
            )
            return
        self._controls.judge_reader(generation)

    @staticmethod
    def _check_input(purpose: str, persona_count: int) -> None:
        if not PERSONA_COUNT_MIN <= int(persona_count) <= PERSONA_COUNT_MAX:
            raise fail(
                ErrorCode.GENERATION_PERSONA_COUNT_INVALID,
                "persona count out of range",
            )
        if len(purpose) > PURPOSE_MAX_CHARS:
            raise fail(
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
        use_detailed_analysis: bool = False,
    ) -> str:
        now = self._context.clock()
        generation = AdvancedGeneration.create_new(
            owner_sub=principal.sub,
            source_file_ids=ids,
            purpose=purpose,
            persona_count=persona_count,
            now=now,
            ttl_seconds=config.ADVANCED_GENERATION_ITEM_TTL_SECONDS,
            confirm_plan=confirm_plan,
            use_detailed_analysis=use_detailed_analysis,
        )
        lock_until = now + timedelta(seconds=config.ADVANCED_GENERATION_LOCK_SECONDS)
        try:
            # "Used" is the start; the same write moves the expiry
            self._context.store.create_with_lock(
                generation,
                lock_until=lock_until,
                in_use_until=lock_until,
                now=now,
                source_expires_at=self._context.catalog.new_expiry(now),
            )
        except AdvancedGenerationStoreError as e:
            raise AdvancedGenerationError("start refused", code=e.code) from e
        return generation.generation_id

    def abort_start(self, generation_id: str) -> None:
        """Undo a start whose run could not be handed to the pool.

        Each end step stands alone; the slot goes back last, exactly once,
        even when the read raises (that error still reaches the caller).
        """
        try:
            generation = self._context.store.get(generation_id)
            if generation is not None:
                try:
                    self._context.store.transition(
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
                self._finishing.finish(generation)
        finally:
            self._finishing.release_slot()

    # --- the run ----------------------------------------------------------------

    def run(self, generation_id: str) -> None:
        """Run one started generation to its end. Never raises (a pool thread)."""
        try:
            generation = self._context.store.get(generation_id)
        except CodedError:
            logger.error("advanced generation unreadable at run start")
            generation = None
        if generation is None or generation.status is not GenerationStatus.PREPARING:
            self._finishing.release_slot()
            return
        state = Run(
            generation=generation,
            started_monotonic=time.monotonic(),
            waited_seconds=generation.waited_seconds,
            replan_count=generation.replan_count,
        )
        stop_watch = self._finishing.start_watch(state)
        try:
            self._run_stages(state)
        except Cancelled:
            self._finishing.record_cancel(state)
        except StopRun:
            # A lost transition means someone else ended it
            state.ended_elsewhere.set()
            logger.warning("advanced generation stopped: a transition was lost")
        except CodedError as e:
            self._finishing.record_failure(state, e.code, e.context)
        except Exception as e:  # noqa: BLE001 - the thread must end cleanly
            logger.error("advanced generation failed (%s)", type(e).__name__)
            self._finishing.record_failure(
                state, ErrorCode.GENERATION_OPERATION_FAILED, {}
            )
        finally:
            # Each end step stands alone; the slot goes back last and once.
            try:
                stop_watch.set()
                if not state.ended_elsewhere.is_set() and not state.finished:
                    # The side that ended it released the lock and marks
                    self._finishing.finish(state.generation)
                    state.finished = True
                # The session (an external call) stops after the
                # lock and the marks are gone, so a restart is never refused
                self._finishing.stop_session(state)
                try:
                    self._finishing.log_finished(state)
                except Exception as e:  # noqa: BLE001 - each end step stands alone
                    logger.error(
                        "advanced generation end not logged (%s)", type(e).__name__
                    )
            finally:
                self._finishing.release_slot()

    def _run_stages(self, state: Run) -> None:
        context = self._context
        context.enter(state, GenerationStatus.PREPARING, first=True)
        self._preparation.prepare(state)
        context.enter(
            state,
            GenerationStatus.PLANNING,
            # Decided right after the preparation
            updates={
                "document_mode": (
                    state.document_mode.value if state.document_mode else None
                )
            },
        )
        self._analysis.plan(state)
        if state.generation.confirm_plan:
            # Wait for the user; ends in ``running`` (or raises)
            self._analysis.await_plan_decision(state)
        else:
            context.enter(
                state,
                GenerationStatus.RUNNING,
                updates={
                    "plan": state.plan.to_dict() if state.plan else None,
                    **self._analysis.reading_update(state.reading),
                },
            )
        self._analysis.analyze(state)
        context.enter(state, GenerationStatus.SYNTHESIZING)
        personas = self._analysis.synthesize(state)
        problem = self._persona_problem(state, personas)
        if problem is not None:
            # A persona that is not complete, or a layer left out,
            # is written once more; a second time fails the generation
            logger.info("advanced generation personas rejected: %s", problem)
            context.event(
                state,
                EventType.PROGRESS,
                message="ペルソナに足りないところがあったため、書き直します",
            )
            personas = self._analysis.synthesize(state)
            problem = self._persona_problem(state, personas)
            if problem is not None:
                logger.info("advanced generation personas rejected again: %s", problem)
                raise fail(
                    ErrorCode.GENERATION_VERIFICATION_FAILED, "persona incomplete"
                )
        context.enter(state, GenerationStatus.VERIFYING)
        context.event(
            state, EventType.PROGRESS, message="数字を数え直して検証しています"
        )
        verification = self._finishing.verify(state, personas)
        dropped = len(verification.unverified_segment_ids)
        context.event(
            state,
            EventType.PROGRESS,
            message="検証できました"
            + (f"（数字が合わず外した層 {dropped} 件）" if dropped else ""),
        )
        self._finishing.write_results(state, personas, verification)

    @staticmethod
    def _persona_problem(
        state: Run, personas: Sequence[prompts.AdvancedPersonaOutput]
    ) -> Optional[str]:
        return check_personas(
            personas,
            [s.id for s in state.segments],
            has_table=state.source is not None,
        )

    # --- Reads (owner only, no release check) ------------------

    def _owned(self, principal: Principal, generation_id: str) -> AdvancedGeneration:
        generation = self._context.store.get(generation_id)
        if generation is None or not AccessPolicy.is_owner(
            principal, generation.owner_sub
        ):
            raise fail(ErrorCode.GENERATION_NOT_FOUND, "generation not found")
        window = timedelta(seconds=config.ADVANCED_GENERATION_RESULT_READABLE_SECONDS)
        if (
            generation.finished_at is not None
            and self._context.clock() > generation.finished_at + window
        ):
            raise fail(ErrorCode.GENERATION_EXPIRED, "generation expired")
        return generation

    # --- Requests from the screen ---------------------

    def request_control(
        self,
        principal: Principal,
        generation_id: str,
        action: str,
        revised_purpose: Optional[str] = None,
    ) -> None:
        """Write a cancel, an approve or a replan for the owner's generation.

        The request goes through the store, so the task running the
        generation takes it whichever task received it.

        Raises:
            AdvancedGenerationError: ``GENERATION_NOT_FOUND`` (someone else's
                or missing), ``GENERATION_EXPIRED``, ``GENERATION_PURPOSE_TOO_LONG``
                (replan), ``GENERATION_NOT_AWAITING_PLAN`` (approve / replan
                while not waiting, or with a request pending) or
                ``GENERATION_ALREADY_FINISHED`` (cancel of an ended one).
        """
        request = self._controls.control_request(action, revised_purpose)
        self._controls.write_control(self._owned(principal, generation_id), request)

    def decide_plan(
        self,
        principal: Principal,
        generation_id: str,
        action: str,
        revised_purpose: Optional[str] = None,
        data_reading: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> None:
        """Approve the plan or replan it; nothing else.

        A cancel has its own request (``request_control`` with ``cancel``);
        here it is refused like any other action, and nothing is written.

        Raises:
            AdvancedGenerationError: ``REQUEST_INVALID`` for any action but
                ``approve`` and ``replan``; otherwise as ``request_control``.
        """
        if action not in _PLAN_DECISIONS:
            raise fail(ErrorCode.REQUEST_INVALID, "not a plan decision")
        generation = self._owned(principal, generation_id)
        self._controls.decide_plan(generation, action, revised_purpose, data_reading)

    def running_generation_id(self, principal: Principal) -> Optional[str]:
        """The owner's generation that still runs, to reopen its progress.

        It is found through the owner's lock (one running generation per user).
        Only a display aid: an unreadable lock reads as "none".
        """
        try:
            lock = self._context.store.get_lock(principal.sub)
            if lock is None or not lock.is_held(self._context.clock()):
                return None
            generation = self._context.store.get(lock.generation_id)
        except CodedError as e:
            logger.warning("running generation unreadable (%s)", e.code.value)
            return None
        if generation is None or not AccessPolicy.is_owner(
            principal, generation.owner_sub
        ):
            return None
        # The page reopening a stalled generation ends it
        generation = self._controls.judge_reader(generation)
        if generation.status.is_terminal:
            return None
        return generation.generation_id

    def elapsed_clock(self, principal: Principal, generation_id: str) -> ElapsedClock:
        """Elapsed time from ``created_at``; a finished one stops at ``finished_at``.

        Raises:
            AdvancedGenerationError: ``GENERATION_NOT_FOUND`` or
                ``GENERATION_EXPIRED`` (as ``stream_events``).
        """
        return self._controls.elapsed_clock(self._owned(principal, generation_id))

    def stream_events(
        self,
        principal: Principal,
        generation_id: str,
        *,
        after_sequence: Optional[int] = None,
    ) -> GenerationProgress:
        """The generation and its events after ``after_sequence``.

        Raises:
            AdvancedGenerationError: ``GENERATION_NOT_FOUND`` (someone else's
                or missing), ``GENERATION_EXPIRED`` (24 hours after the end).
        """
        return self._controls.stream_events(
            principal,
            self._owned(principal, generation_id),
            generation_id,
            after_sequence=after_sequence,
        )

    def get_result(
        self, principal: Principal, generation_id: str
    ) -> AdvancedGenerationResult:
        """The finished result for the result section.

        Raises:
            AdvancedGenerationError: ``GENERATION_NOT_FOUND`` (also while it is
                not done) or ``GENERATION_EXPIRED``.
        """
        return self._controls.get_result(
            principal, self._owned(principal, generation_id)
        )

    def get_record(self, principal: Principal, generation_id: str) -> AnalysisRecord:
        """The analysis record of the owner's finished generation.

        Raises:
            AdvancedGenerationError: ``GENERATION_NOT_FOUND`` or
                ``GENERATION_EXPIRED``.
        """
        return self._controls.get_record(
            principal, self._owned(principal, generation_id)
        )
