"""The finishing stage: verify and write the result, or record the failure or the cancel, then end.

It also runs the watch that reads the generation and writes the heartbeat
while the run goes on.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Mapping, Optional, Sequence

from ...config import config
from ...models.advanced_generation import (
    AdvancedGeneration,
    ControlAction,
    EventType,
    GenerationStatus,
    ResultPartName,
)
from ...models.analysis import TABLE_ALIASES, EmittedResult, Evidence
from ...models.errors import CodedError, ErrorCode
from ...models.persona_candidate import MAX_CANDIDATE_ITEM_BYTES, PersonaCandidate
from ...services.stores.advanced_generation_store import AdvancedGenerationStore
from ...services.stores.persona_candidate_store import PersonaCandidateStore
from ..shared.advanced_generation_log import log_advanced_event
from . import documents, prompts
from .analysis_policy import AnalysisPolicy
from .analysis_verifier import AnalysisVerifier, VerificationResult
from .call_results import call_result, table_summary
from .errors import Cancelled, StopRun, fail
from .results import build_analysis_run, build_candidates, build_record
from .run_context import ENDED_REMOVES, RunContext, safe_context
from .run_types import Run

logger = logging.getLogger(__name__)

#: Room kept under the 2MB of the result parts for the item keys.
_PARTS_SLACK_BYTES = 16 * 1024
#: The share of a history's rows one ID may hold before the record points it
#: out (a value such as "guest" may stand for many people).
_ONE_ID_ROWS_SHARE = 0.2


class FinishingStage:
    """Verification, the result, failure and cancel records, the end and the watch."""

    def __init__(self, context: RunContext) -> None:
        self._context = context

    # --- verifying and the result -----------------------------------------------

    def verify(
        self, state: Run, personas: Sequence[prompts.AdvancedPersonaOutput]
    ) -> VerificationResult:
        assert state.plan is not None
        if state.report is None:
            # Documents alone rest on their checked quotes
            result = AnalysisVerifier.verify_documents(
                persona_evidence_ids=[p.evidence_ids for p in personas],
                evidence=state.document_evidence,
            )
            if not result.passed:
                logger.info(
                    "advanced generation verification failed: %s", result.reason
                )
                raise fail(
                    ErrorCode.GENERATION_VERIFICATION_FAILED, "verification failed"
                )
            return result
        outputs = {
            cid: state.tools.output_of(cid) for cid in state.tools.tool_call_ids()
        }
        # The population of each table (the first one is the
        # default of a segment without a source), and the recounted results
        populations = {t.alias: t.report.rows_read for t in state.tables} or {
            TABLE_ALIASES[0]: state.report.rows_read
        }
        emitted = state.tools.emitted_results() if state.budget is not None else {}
        result = AnalysisVerifier.verify(
            method=state.plan.method,
            segments=state.segments,
            evidence=state.evidence,
            persona_segment_ids=[p.segment_id for p in personas],
            populations=populations,
            outputs=outputs,
            emitted=emitted,
        )
        if not result.passed:
            logger.info("advanced generation verification failed: %s", result.reason)
            raise fail(ErrorCode.GENERATION_VERIFICATION_FAILED, "verification failed")
        return result

    def write_results(
        self,
        state: Run,
        personas: Sequence[prompts.AdvancedPersonaOutput],
        verification: VerificationResult,
    ) -> None:
        """Record, candidates and result parts first, then ``done``.

        The generation is read once more right before
        anything is written, and ``done`` is conditioned on no pending
        request. When ``done`` still loses (a reader ended it, or a cancel
        came in while writing), what was written is removed again and the run
        ends as the store says: cancelled, or quietly.
        """
        self._context.checkpoint(state)  # a cancelled run keeps nothing
        generation = state.generation
        assert state.plan is not None
        now = self._context.clock()
        sha = self._context.sandbox.library_sha256
        unverified = verification.unverified_segment_ids
        ai_code = [
            (r.tool_call_id, r.code, r.sha256, self._join_title(state, r.result))
            for r in getattr(state.tools, "code_runs", [])
            if r.ok
        ]
        run = build_analysis_run(
            generation=generation,
            plan=state.plan,
            report=state.report,
            sources=state.sources,
            tool_call_ids=state.tools.tool_call_ids(),
            library_sha256=sha,
            unverified_segment_ids=unverified,
            document_mode=state.document_mode,
            ai_code_sha256=[s for _, _, s, _ in ai_code],
            extra_params=self._extra_run_params(state),
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
            ai_code=ai_code,
            call_results=self._call_results(state),
            table_summaries=[
                table_summary(t.source.display_name, t.report, t.profile)
                for t in state.tables
            ],
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
            raise fail(ErrorCode.GENERATION_RESULT_TOO_LARGE, "candidate too large")
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
            # Every table; the Parquet hash stays with the run
            ResultPartName.PREPARE_REPORTS: (
                [t.report.without_hash().to_dict() for t in state.tables]
                if state.tables
                else ([state.report.without_hash().to_dict()] if state.report else [])
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
        try:
            self._context.archive.deposit(record)
            self._context.candidates.put_all(candidates)
            part_counts = self._context.store.put_result_parts(
                generation.generation_id,
                parts,
                expires_at=generation.expires_at,
                max_part_bytes=config.ADVANCED_GENERATION_PART_MAX_BYTES,
                max_total_bytes=config.ADVANCED_GENERATION_PARTS_TOTAL_MAX_BYTES,
            )
        except Exception:
            # A failed run keeps nothing: what was written before the failure
            # goes (the end itself is not written yet, so nothing reads them).
            self._discard_results(state, candidates)
            raise
        state.ending.set()
        if not self._context.store.transition(
            generation.generation_id,
            GenerationStatus.VERIFYING,
            GenerationStatus.DONE,
            updates={"part_counts": part_counts, **done_updates},
            removes=ENDED_REMOVES,
            unless_requested=True,
        ):
            self._discard_results(state, candidates)
            self._judge_before_results(state)
            raise StopRun()  # moved in a way the read cannot tell: end quietly
        self._context.close_stage(state)
        state.status = GenerationStatus.DONE
        # The lock and the marks go before the result shows
        self.finish(state.generation)
        state.finished = True
        self._context.event(
            state,
            EventType.RESULT,
            persona_count=len(candidates),
            unverified=len(verification.unverified_segment_ids),
        )

    @staticmethod
    def _join_title(state: Run, result: Optional[EmittedResult]) -> Optional[str]:
        """The record's line for the tables a result used.

        A join with join_tables gives its columns and match rates; code that
        read several tables without it (a merge of its own) gives the tables.
        """
        if result is None:
            return None
        names = {t.alias: t.source.display_name for t in state.tables}
        joined = result.population_source.startswith("join:")
        if (not joined or not result.joined_columns) and len(result.tables_read) > 1:
            used = ", ".join(names.get(a, "CSV") for a in result.tables_read)
            return f"使った表: {used}"
        if not joined:
            return None
        rates = "、".join(
            f"{names.get(plan.table, '結合した CSV')} の一致 {plan.match_rate:.0%}"
            for plan, _ in state.joins
        )
        columns = ", ".join(result.joined_columns) or "（なし）"
        return f"結合した列: {columns}" + (f"（{rates}）" if rates else "")

    @staticmethod
    def _call_results(state: Run) -> dict[str, dict[str, Any]]:
        """What each tool call found, for the record (counted aggregates only)."""
        emitted = state.tools.emitted_results() if state.budget is not None else {}
        history = {r.alias: r.person_id for r in state.reading if r.kind == "history"}
        skip = {c for r in state.reading for c in r.skip}
        out: dict[str, dict[str, Any]] = {}
        for call in state.tools.calls:
            found = call_result(
                call,
                output=state.tools.output_of(call.tool_call_id),
                emitted=emitted.get(call.tool_call_id),
                segments=state.segments,
                history_tables=history,
                skip=skip,
            )
            if found is not None:
                out[call.tool_call_id] = found
        return out

    @staticmethod
    def _adopted_people_results(state: Run) -> list[tuple[Any, EmittedResult]]:
        """The kept segments resting on the people of a history (``people:``)."""
        if state.budget is None:
            return []
        emitted = state.tools.emitted_results()
        out = []
        for segment in state.segments:
            result = emitted.get(segment.tool_call_id or "")
            if result is not None and result.from_history:
                out.append((segment, result))
        return out

    @staticmethod
    def _all_rest_on_people(state: Run) -> bool:
        adopted = {id(s) for s, _ in FinishingStage._adopted_people_results(state)}
        return bool(state.segments) and all(id(s) in adopted for s in state.segments)

    @staticmethod
    def _people_from_history(state: Run) -> Optional[dict[str, Any]]:
        """How the people of a history were counted, for the record.

        The rows without an ID are left out of the population and shown as a
        count (never refused); one ID holding a large share of the rows is
        pointed out, since a value such as "guest" may stand for no one.
        """
        adopted = FinishingStage._adopted_people_results(state)
        if not adopted:
            return None
        result = adopted[0][1]
        alias = result.population_source.removeprefix("people:")
        table = next((t for t in state.tables if t.alias == alias), None)
        reading = next((r for r in state.reading if r.alias == alias), None)
        source_rows = int(result.source_rows or 0)
        without = int(result.rows_without_id or 0)
        with_id = source_rows - without
        largest = int(result.largest_id_rows or 0)
        return {
            "table": table.source.display_name if table is not None else "CSV",
            # The check accepts people: only with a confirmed person column
            "id_column": (
                reading.person_id
                if reading is not None and reading.person_id
                else "人を特定する列"
            ),
            "people": int(result.population),
            "source_rows": source_rows,
            "rows_without_id": without,
            "one_id_holds_many_rows": (
                result.population > 1
                and with_id > 0
                and largest / with_id >= _ONE_ID_ROWS_SHARE
            ),
        }

    @staticmethod
    def _extra_run_params(state: Run) -> dict[str, Any]:
        """``detailed_analysis`` and ``join`` of AnalysisRun.params."""
        params: dict[str, Any] = {}
        budget = state.budget
        if budget is not None:
            params["detailed_analysis"] = {
                "used": True,
                "fell_back": budget.disabled_reason is not None,
                "failures": budget.failures,
            }
        elif state.detailed_fallback is not None:
            # Asked for, but not offered (no hash to compare)
            params["detailed_analysis"] = {
                "used": False,
                "fell_back": True,
                "failures": 0,
                "reason": state.detailed_fallback,
            }
        people = FinishingStage._people_from_history(state)
        if people is not None:
            params["people_from_history"] = people
        if AnalysisPolicy.readings_count_rows(state.reading) and (
            people is None or not FinishingStage._all_rest_on_people(state)
        ):
            # The counts are of rows (orders, events), not people
            params["rows_are_not_people"] = True
        if state.joins and budget is not None:
            plan_only: dict[str, Any] = {
                "axis": state.joins[0][0].axis,
                "tables": [
                    {
                        "table": plan.table,
                        "key_column": plan.key_column,
                        "match_rate": plan.match_rate,
                        "matched_people": matched,
                    }
                    for plan, matched in state.joins
                ],
            }
            # "join" only when a joined result was adopted (a
            # kept segment rests on it); otherwise only the plan is recorded
            emitted = state.tools.emitted_results()
            joined: Optional[list[str]] = None
            for segment in state.segments:
                result = emitted.get(segment.tool_call_id or "")
                if result is not None and result.population_source.startswith("join:"):
                    joined = list(result.joined_columns)
            if joined is None:
                params["join_plan"] = plan_only
            else:
                params["join"] = {**plan_only, "joined_columns": joined}
        return params

    @staticmethod
    def _trimmed_evidence(
        state: Run,
        personas: Sequence[prompts.AdvancedPersonaOutput],
        other_parts: Mapping[ResultPartName, Sequence[Mapping[str, Any]]],
    ) -> list[Evidence]:
        """All evidence, less what no persona cites when over 2MB."""

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
    def _check_item_size(state: Run, done_updates: Mapping[str, Any]) -> None:
        """The generation item stays within its limit once ``done`` is written.

        One item is up to 64KB; large results go to ResultPart.
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
            raise fail(ErrorCode.GENERATION_RESULT_TOO_LARGE, "generation too large")

    def _judge_before_results(self, state: Run) -> None:
        """Read the generation once more; stop when it ended or a cancel waits.

        The watch reads only every ``control_poll_seconds``, so a reader's end
        or a cancel written after the last checkpoint is seen here.
        A failed read lets the conditioned ``done`` decide.

        Raises:
            StopRun: Someone else ended it.
            Cancelled: A cancel is pending.
        """
        try:
            current = self._context.store.get(state.generation.generation_id)
        except CodedError as e:
            logger.warning("advanced generation unreadable (%s)", e.code.value)
            return
        if current is None or current.status.is_terminal:
            state.ended_elsewhere.set()
            raise StopRun()
        request = current.control_request
        if request is not None and request.action is ControlAction.CANCEL:
            state.cancel_request = request
            state.cancel_requested.set()
            raise Cancelled()

    def _discard_results(
        self, state: Run, candidates: Sequence[PersonaCandidate]
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
        now = self._context.clock()
        for candidate in candidates:
            try:
                self._context.candidates.claim(
                    candidate.candidate_id, owner, token, now
                )
                self._context.candidates.delete(candidate.candidate_id, token)
            except Exception as e:  # noqa: BLE001 - each removal stands alone
                logger.error(
                    "advanced generation candidate not removed (%s)", type(e).__name__
                )
        try:
            self._context.archive.discard_copy(owner, state.generation.generation_id)
        except Exception as e:  # noqa: BLE001 - the lifecycle removes it later
            logger.error(
                "advanced generation record not removed (%s)", type(e).__name__
            )

    # --- failure and the end ----------------------------------------------------

    def record_failure(
        self, state: Run, code: ErrorCode, context: Mapping[str, Any]
    ) -> None:
        """Move the current stage to ``failed`` with the code only."""
        state.error_code = code
        safe = safe_context(context)
        state.cause = safe.get("cause")
        updates: dict[str, Any] = {"error_code": code.value}
        if safe:
            updates["error_context"] = safe
        state.ending.set()
        try:
            moved = self._context.store.transition(
                state.generation.generation_id,
                state.status,
                GenerationStatus.FAILED,
                updates=updates,
                removes=ENDED_REMOVES,
            )
            if moved:
                self._context.close_stage(state)
                # The lock and the marks go before 失敗しました shows,
                # so a restart is not refused
                self.finish(state.generation)
                state.finished = True
                self._context.event(
                    state, EventType.ERROR, error_code=code.value, kind=code.kind.value
                )
            else:
                # A reader (or the requester) ended it first
                state.ended_elsewhere.set()
        except CodedError as e:
            logger.error("advanced generation failure not written (%s)", e.code.value)
        logger.info(
            "advanced generation failed: stage=%s error_code=%s",
            state.status.value,
            code.value,
        )

    def stop_session(self, state: Run) -> None:
        if state.session is None:
            return
        try:
            self._context.sandbox.stop_session(state.session)
        except Exception as e:  # noqa: BLE001 - the session's lifetime ends it
            logger.warning(
                "sandbox session not stopped (%s): session_id=%s",
                type(e).__name__,
                state.session.session_id,
            )

    def finish(self, generation: AdvancedGeneration) -> None:
        now = self._context.clock()
        try:
            self._context.store.finish(
                generation.generation_id,
                generation.owner_sub,
                generation.source_file_ids,
                now=now,
                expires_at=int(now.timestamp())
                + int(config.ADVANCED_GENERATION_RESULT_READABLE_SECONDS),
            )
        except Exception as e:  # noqa: BLE001 - each end step stands alone
            logger.error("advanced generation end not written (%s)", type(e).__name__)

    def release_slot(self) -> None:
        try:
            self._context.slots.release()
        except ValueError:
            logger.error("advanced generation slot released twice")

    def start_watch(self, state: Run) -> threading.Event:
        """The heartbeat thread, widened to read the generation.

        Every ``control_poll_seconds`` it reads the generation; every
        ``heartbeat_seconds`` it writes the heartbeat. Each read and each
        write is wrapped on its own: a failure is a warning with the count of
        failures in a row, and the next round goes on.
        """
        stop = threading.Event()
        generation_id = state.generation.generation_id
        interval = min(
            self._context.control_poll_seconds, float(self._context.heartbeat_seconds)
        )

        def watch() -> None:
            last_beat = time.monotonic()
            last_read = 0.0
            while not stop.wait(interval):
                if time.monotonic() - last_beat >= self._context.heartbeat_seconds:
                    last_beat = time.monotonic()
                    self._beat(state, generation_id)
                if time.monotonic() - last_read >= self._context.control_poll_seconds:
                    last_read = time.monotonic()
                    self._read_controls(state, generation_id)

        threading.Thread(
            target=watch, name="advanced-generation-watch", daemon=True
        ).start()
        return stop

    def _read_controls(self, state: Run, generation_id: str) -> None:
        """One read of the watch: a cancel, or an end written by someone else."""
        try:
            current = self._context.store.get(generation_id)
            state.read_failures = 0
        except Exception as e:  # noqa: BLE001 - the watch never dies
            state.read_failures += 1
            logger.warning(
                "advanced generation watch read failed (%s): failures_in_a_row=%d",
                type(e).__name__,
                state.read_failures,
            )
            return
        if current is None or current.status.is_terminal:
            if not state.ending.is_set():
                state.ended_elsewhere.set()  # a reader ended it
            return
        request = current.control_request
        if request is not None and request.action is ControlAction.CANCEL:
            state.cancel_request = request
            state.cancel_requested.set()

    def _beat(self, state: Run, generation_id: str) -> None:
        try:
            self._context.store.heartbeat(generation_id, now=self._context.clock())
            state.beat_failures = 0
        except Exception as e:  # noqa: BLE001 - a missed beat is logged
            state.beat_failures += 1
            logger.warning(
                "heartbeat not written (%s): failures_in_a_row=%d",
                type(e).__name__,
                state.beat_failures,
            )

    def record_cancel(self, state: Run) -> None:
        """Move the current stage to ``cancelled`` and say so.

        Nothing of the result is kept: the candidates and the record are only
        written at the very end (``write_results``), after the last check.
        """
        state.ending.set()
        try:
            moved = self._context.store.transition(
                state.generation.generation_id,
                state.status,
                GenerationStatus.CANCELLED,
                removes=ENDED_REMOVES,
            )
        except CodedError as e:
            logger.error("advanced generation cancel not written (%s)", e.code.value)
            return
        if not moved:
            state.ended_elsewhere.set()
            return
        self._context.close_stage(state)
        stage = state.status
        state.status = GenerationStatus.CANCELLED
        # The lock and the marks go before the screen hears of it, so
        # the same files can start again as soon as 中止しました shows
        self.finish(state.generation)
        state.finished = True
        self._context.event(state, EventType.CANCELLED)
        if state.cancel_request is not None:
            self._context.log_control_taken(state, state.cancel_request, stage=stage)

    def log_finished(self, state: Run) -> None:
        report = state.report
        budget = state.budget
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
            # Replan and wait counts
            replan_count=state.replan_count,
            waited_seconds=state.waited_seconds,
            # Counts only, never the code or its output
            detailed_analysis_used=state.generation.use_detailed_analysis,
            run_code_calls=budget.calls if budget is not None else 0,
            run_code_failures=budget.failures if budget is not None else 0,
            run_code_disabled=(
                budget.disabled_reason is not None or budget.verify_failed
                if budget is not None
                else state.detailed_fallback is not None
            ),
            table_count=len(state.tables),
            joined_tables=len(state.joins),
            join_match_rate=(
                min(plan.match_rate for plan, _ in state.joins) if state.joins else None
            ),
        )
