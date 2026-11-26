"""The control desk: the screen's requests, the reads of a generation and the reader's judgement.

Every generation it is given has already passed the owner check in the
Manager; it never decides access itself.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, Sequence

from ...config import config
from ...models.advanced_generation import (
    PURPOSE_MAX_CHARS,
    AdvancedGeneration,
    ControlAction,
    ControlRequest,
    EventType,
    GenerationStatus,
    ResultPartName,
)
from ...models.analysis import (
    AnalysisRecord,
    DocumentReport,
    Evidence,
    PrepareReport,
    Segment,
)
from ...models.authz import Principal
from ...models.errors import CodedError, ErrorCode
from .errors import fail
from .finishing import FinishingStage
from .run_context import ENDED_REMOVES, RunContext, safe_context
from .run_types import AdvancedGenerationResult, ElapsedClock, GenerationProgress

logger = logging.getLogger(__name__)

#: The states a cancel may be written in.
_RUNNING_STATES = tuple(s for s in GenerationStatus if not s.is_terminal)


class ControlDesk:
    """Requests from the screen, the reads of a generation and the reader's judgement."""

    def __init__(self, context: RunContext, finishing: FinishingStage) -> None:
        self._context = context
        self._finishing = finishing

    # --- Requests from the screen ---------------------

    @staticmethod
    def _checked_reading(
        generation: AdvancedGeneration,
        choices: Optional[Mapping[str, Mapping[str, Any]]],
    ) -> Optional[dict[str, dict[str, Any]]]:
        """The user's table choices, each checked against the table's columns.

        Raises:
            AdvancedGenerationError: ``REQUEST_INVALID`` for a table the run
                does not have, a kind that is none of the two, or a column the
                table does not have.
        """
        if not choices:
            return None
        known = {r.alias: r for r in generation.data_reading or ()}
        out: dict[str, dict[str, Any]] = {}
        for alias, choice in choices.items():
            reading = known.get(alias)
            try:
                if reading is None:
                    raise ValueError("unknown table")
                chosen = reading.choose(
                    str(choice.get("kind")),
                    choice.get("person_id") or None,
                    choice.get("skip") or (),
                )
            except ValueError:
                raise fail(
                    ErrorCode.REQUEST_INVALID,
                    "invalid data reading",
                    reason="data_reading",
                ) from None
            out[alias] = {
                "kind": chosen.kind,
                "person_id": chosen.person_id,
                "skip": list(chosen.skip),
            }
        return out

    def decide_plan(
        self,
        generation: AdvancedGeneration,
        action: str,
        revised_purpose: Optional[str],
        data_reading: Optional[Mapping[str, Mapping[str, Any]]],
    ) -> None:
        """Write the approve or the replan for the owner's ``generation``."""
        request = self.control_request(
            action, revised_purpose, self._checked_reading(generation, data_reading)
        )
        self.write_control(generation, request)

    def control_request(
        self,
        action: str,
        revised_purpose: Optional[str],
        data_reading: Optional[dict[str, dict[str, Any]]] = None,
    ) -> ControlRequest:
        """The request for ``action``, its instruction checked."""
        try:
            kind = ControlAction(action)
        except ValueError:
            raise fail(
                ErrorCode.GENERATION_NOT_AWAITING_PLAN, "unknown control action"
            ) from None
        revised: Optional[str] = None
        if kind is ControlAction.REPLAN:
            revised = revised_purpose or ""
            if len(revised) > PURPOSE_MAX_CHARS:
                raise fail(
                    ErrorCode.GENERATION_PURPOSE_TOO_LONG,
                    "revised purpose too long",
                    max_chars=PURPOSE_MAX_CHARS,
                )
        return ControlRequest(
            action=kind,
            requested_at=self._context.clock(),
            revised_purpose=revised,
            data_reading=data_reading if kind is not ControlAction.CANCEL else None,
        )

    def write_control(
        self, generation: AdvancedGeneration, request: ControlRequest
    ) -> None:
        """Write ``request`` for the owner's ``generation`` and log it."""
        if request.action is ControlAction.CANCEL:
            self._request_cancel(generation, request)
        else:
            self._request_plan_decision(generation, request)
        self._context.log(
            "advanced_generation.control_requested",
            generation_id=generation.generation_id,
            action=request.action.value,
        )

    def _request_cancel(
        self, generation: AdvancedGeneration, request: ControlRequest
    ) -> None:
        if not generation.status.is_terminal and self._is_stalled(generation):
            # No thread runs it any more; the requester ends it
            if self._end_by_reader(
                generation,
                GenerationStatus.CANCELLED,
                field="heartbeat_at",
                value=generation.heartbeat_at,
            ):
                return
            # it moved since the read: a live thread takes the request instead
            generation = self._context.store.get(generation.generation_id) or generation
        else:
            generation = self.judge_reader(generation)  # a plan wait out
        if generation.status.is_terminal or not self._context.store.put_control_request(
            generation.generation_id,
            request,
            allowed_statuses=_RUNNING_STATES,
            require_empty=False,
        ):
            raise fail(ErrorCode.GENERATION_ALREADY_FINISHED, "generation ended")

    def _request_plan_decision(
        self, generation: AdvancedGeneration, request: ControlRequest
    ) -> None:
        generation = self.judge_reader(generation)  # judged first
        if (
            generation.status is not GenerationStatus.AWAITING_PLAN_APPROVAL
            or generation.control_request is not None
            or not self._context.store.put_control_request(
                generation.generation_id,
                request,
                allowed_statuses=(GenerationStatus.AWAITING_PLAN_APPROVAL,),
                require_empty=True,
            )
        ):
            raise fail(
                ErrorCode.GENERATION_NOT_AWAITING_PLAN, "generation is not waiting"
            )

    # --- The reader's judgement ---------------------

    def judge_reader(self, generation: AdvancedGeneration) -> AdvancedGeneration:
        """End a generation whose thread is gone or whose plan wait ran out.

        Run on the SSE re-reads, the page, the start and the control requests.
        It only reads, unless one of these holds:

        - no heartbeat for 5 minutes (the plan wait included):
          ``GENERATION_STALLED``, conditioned on ``heartbeat_at``;
        - the card waited 15 minutes, or all the cards 60:
          ``GENERATION_TIMEOUT``, conditioned on ``awaiting_since``.

        Returns:
            The generation as it is after the judgement.
        """
        if generation.status.is_terminal:
            return generation
        now = self._context.clock()
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
            limit, total = self._context.plan_wait_limit(
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
                    self._context.log_plan_wait_expired(
                        generation.generation_id, limit[0], total
                    )
        if not ended:
            return generation
        try:
            return self._context.store.get(generation.generation_id) or generation
        except CodedError as e:
            logger.warning("advanced generation unreadable (%s)", e.code.value)
            return generation

    def _is_stalled(self, generation: AdvancedGeneration) -> bool:
        """No heartbeat for more than 5 minutes; the start counts as one."""
        beat = generation.heartbeat_at or generation.created_at
        return self._context.clock() - beat > timedelta(
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
        """End a generation no thread looks after any more.

        The move is conditioned on the state and ``field`` being what was read;
        only the side that moved it writes the event, releases the
        lock and the marks, and stops the session by its ID. Each of those
        steps stands alone.

        Returns:
            Whether this reader ended it.
        """
        updates: dict[str, Any] = {}
        if code is not None:
            updates["error_code"] = code.value
            safe = safe_context(context or {})
            if safe:
                updates["error_context"] = safe
        try:
            moved = self._context.store.end_if_unchanged(
                generation.generation_id,
                generation.status,
                status,
                field=field,
                value=value,
                updates=updates,
                removes=ENDED_REMOVES,
            )
        except CodedError as e:
            logger.warning("advanced generation reader end failed (%s)", e.code.value)
            return False
        if not moved:
            return False
        if code is not None:
            self._context.append_event(
                generation, EventType.ERROR, error_code=code.value, kind=code.kind.value
            )
        else:
            self._context.append_event(generation, EventType.CANCELLED)
        self._finishing.finish(generation)
        if generation.session_id:
            try:
                self._context.sandbox.stop_session_by_id(generation.session_id)
            except Exception as e:  # noqa: BLE001 - the session's lifetime ends it
                logger.warning(
                    "sandbox session not stopped (%s): session_id=%s",
                    type(e).__name__,
                    generation.session_id,
                )
        self._context.log(
            "advanced_generation.ended_by_reader",
            generation_id=generation.generation_id,
            stage=generation.status.value,
            error_code=code.value if code is not None else None,
            status=status.value,
            judged_by="reader",
        )
        return True

    # --- Reads of the owner's generation ---------------------

    def elapsed_clock(self, generation: AdvancedGeneration) -> ElapsedClock:
        """Elapsed time from ``created_at``; a finished one stops at ``finished_at``."""
        finished = generation.status.is_terminal
        # The plan waits are not counted; a shown card stops the clock
        waiting = (
            generation.status is GenerationStatus.AWAITING_PLAN_APPROVAL
            and generation.awaiting_since is not None
        )
        if finished and generation.finished_at is not None:
            end = generation.finished_at
        elif waiting and generation.awaiting_since is not None:
            end = generation.awaiting_since
        else:
            end = self._context.clock()
        seconds = max(
            0,
            int((end - generation.created_at).total_seconds())
            - generation.waited_seconds,
        )
        return ElapsedClock(seconds=seconds, running=not finished and not waiting)

    def stream_events(
        self,
        principal: Principal,
        generation: AdvancedGeneration,
        generation_id: str,
        *,
        after_sequence: Optional[int] = None,
    ) -> GenerationProgress:
        """The judged generation and its events after ``after_sequence``."""
        # Every re-read of the stream also judges the generation
        generation = self.judge_reader(generation)
        events = self._context.store.read_events(
            generation_id, after_sequence=after_sequence
        )
        return GenerationProgress(
            generation=generation,
            events=tuple(events),
            failure_context=self._failure_context(principal, generation),
        )

    def _failure_context(
        self, principal: Principal, generation: AdvancedGeneration
    ) -> dict[str, Any]:
        """The stored values, plus the owner's file name for ``source_index``.

        The name is looked up again in the owner's own files; it is
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
                found = self._context.catalog.get_owned(
                    principal.sub,
                    source_file_id,
                    not_found_code=ErrorCode.GENERATION_SOURCE_NOT_FOUND,
                )
            except CodedError:
                continue
            names[source_file_id] = found.display_name
        return names

    def get_result(
        self, principal: Principal, generation: AdvancedGeneration
    ) -> AdvancedGenerationResult:
        """The finished result of the owner's ``generation``."""
        generation_id = generation.generation_id
        if generation.status is not GenerationStatus.DONE:
            raise fail(ErrorCode.GENERATION_NOT_FOUND, "generation has no result")
        parts = {
            name: self._context.store.read_result_parts(generation_id, name)
            for name in ResultPartName
        }
        candidates = [
            c
            for c in (
                self._context.candidates.get(i) for i in generation.candidate_ids or ()
            )
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

    def get_record(
        self, principal: Principal, generation: AdvancedGeneration
    ) -> AnalysisRecord:
        """The analysis record of the owner's finished ``generation``."""
        if generation.analysis_record_id is None:
            raise fail(ErrorCode.GENERATION_NOT_FOUND, "no analysis record")
        return self._context.archive.read_pending(
            principal.sub,
            generation.analysis_record_id,
            finished_at=generation.finished_at,
            now=self._context.clock(),
        )
