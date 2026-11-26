"""The analysis stage: make the plan, wait for its confirmation, analyse the tables and write the personas.

The AI detailed analysis (``run_code``) and its checks for a changed
library or Parquet belong here too.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, Sequence

from ...config import config
from ...models.advanced_generation import (
    ControlAction,
    ControlRequest,
    EventType,
    GenerationStatus,
    TableReading,
)
from ...models.analysis import (
    CHANGED_PROCESS,
    processes_left,
    AnalysisMethod,
    AnalysisPlan,
    AnalysisTool,
    DocumentMode,
    EmittedResult,
    Evidence,
    Segment,
)
from ...models.errors import CodedError, ErrorCode
from ...services.dataset_analysis.analysis_tool_service import AnalysisTable
from . import documents, prompts
from .analysis_policy import PLAN_ATTEMPTS, AnalysisPolicy
from .detailed_analysis import (
    REASON_MISSING_HASH,
    REASON_VERIFY_FAILED,
    RunCodeBudget,
    filter_evidence,
)
from .errors import Cancelled, StopRun, fail
from .run_context import RunContext
from .run_types import NoTools, Run
from .segment_features import settle_features

logger = logging.getLogger(__name__)

_RESELECT_CODES = frozenset({"TOO_FEW_COLUMNS", "EMPTY_CLUSTER"})
_EVENT_TEXT_MAX = 150

#: The progress notice when run_code is taken off.
_RUN_CODE_DISABLED_NOTICE = "AI による詳細分析をやめて、事前定義された関数で続けます"

#: What the screen's log calls a plan step and a tool (fixed names only; the
#: user's data never goes into a progress message).
_STEP_NAMES = {
    "filter": "対象を絞って集計",
    "compare": "群どうしの比較",
    "cluster": "似た人のグループ分け",
    "join": "CSV を結合",
    "rfm": "新しさ・頻度・金額で点数化",
    "compare_groups": "群の差を検定",
    "person_profile": "履歴を人ごとに集計",
}
#: Why a call did not give a result (fixed words; a look around is no failure).
_CALL_TROUBLE = "（うまくいかなかったため、やり直します）"
_CALL_TROUBLES = {
    "NO_RESULT": "（結果は出さずにデータを確かめました）",
    "RUN_CODE_FAILED": "（プログラムがエラーになったため、書き直します）",
    "RUN_CODE_TIMEOUT": "（時間内に終わらなかったため、やり直します）",
    "RESULT_INVALID": "（結果の形が合わなかったため、やり直します）",
    "EMIT_TWICE": "（結果を 2 回出したため、やり直します）",
}
_TOOL_NAMES = {
    "profile_dataset": "データの全体像を確認",
    "analyze_dataset": "条件を指定して集計",
    "cluster_segments": "似た人のグループ分け",
    "profile_segments": "層の特徴を数える",
    "run_code": "AI が書いたプログラムで分析",
    "load_playbook": "分析の定石を読み込み",
}


class AnalysisStage:
    """The plan and its confirmation, the analysis and the synthesis."""

    def __init__(self, context: RunContext) -> None:
        self._context = context

    # --- planning, running, synthesizing ----------------------------------------

    def _columns(self, state: Run) -> list[str]:
        """The guide table's columns a person can be told apart by (no IDs)."""
        skip = self._skipped(state)
        return [
            str(c.get("name"))
            for c in state.profile.get("columns") or []
            if str(c.get("name")) not in skip
        ]

    @staticmethod
    def _skipped(state: Run) -> set[str]:
        """Columns of the guide table that are no trait (what the user confirmed)."""
        guide = next((t.alias for t in state.tables if t.source is state.source), None)
        return {c for r in state.reading if r.alias == guide for c in r.skip}

    def _profile_text(self, state: Run) -> str:
        if len(state.tables) > 1:
            # Every table's profile under its name
            return json.dumps(
                {t.alias: t.profile for t in state.tables},
                ensure_ascii=False,
                default=str,
            )
        return json.dumps(state.profile, ensure_ascii=False, default=str)

    @staticmethod
    def _reading_data(state: Run) -> dict[str, str]:
        """The tables' reading for the prompts (names are the aliases, not files)."""
        if not state.reading:
            return {}
        return {
            "data_reading": json.dumps(
                [
                    {
                        "table": r.alias,
                        "kind": r.kind,
                        "person_id": r.person_id,
                        "skip": list(r.skip),
                    }
                    for r in state.reading
                ],
                ensure_ascii=False,
            )
        }

    def _table_data(self, state: Run) -> dict[str, str]:
        """The table names, the guide table and the join plans."""
        if len(state.tables) <= 1 and not state.joins:
            return {}
        data = {
            "tables": json.dumps(
                [{"name": t.alias, "rows": t.report.rows_read} for t in state.tables],
                ensure_ascii=False,
            ),
            "guide_table": next(
                t.alias for t in state.tables if t.source is state.source
            ),
        }
        if state.joins:
            data["join_plans"] = json.dumps(
                [plan.to_dict() for plan, _ in state.joins], ensure_ascii=False
            )
        return data

    def _full_texts(self, state: Run) -> list[str]:
        """The whole document texts for the prompts (``full_text`` only)."""
        return (
            list(state.document_texts)
            if state.document_mode is DocumentMode.FULL_TEXT
            else []
        )

    def _document_evidence_text(self, state: Run) -> str:
        return json.dumps(
            [e.to_dict() for e in state.document_evidence], ensure_ascii=False
        )

    def plan(self, state: Run) -> None:
        generation = state.generation
        guide_alias = next(
            (t.alias for t in state.tables if t.source is state.source), None
        )
        guide = AnalysisPolicy.guide_method(
            generation.purpose,
            state.profile,
            next((r for r in state.reading if r.alias == guide_alias), None),
        )
        detailed = self._detailed_in_use(state)
        # The step candidates from the words and the column shapes
        candidates = (
            AnalysisPolicy.guide_candidates(
                generation.purpose,
                [(t.alias, t.profile) for t in state.tables],
                [plan for plan, _ in state.joins],
                detailed,
                state.reading,
            )
            if state.source is not None
            else []
        )
        table_columns = {
            t.alias: [str(c.get("name")) for c in t.profile.get("columns") or []]
            for t in state.tables
        }
        instructions = (
            prompts.PLANNING_INSTRUCTIONS
            + prompts.READING_NOTE
            + (prompts.DOCUMENT_PLANNING_NOTE if state.documents else "")
        )
        agent = self._context.agents.create_advanced_planning_agent(instructions)
        data = {
            "purpose": generation.purpose,
            "profile": self._profile_text(state),
            **self._table_data(state),
            **self._reading_data(state),
        }
        if state.document_mode is DocumentMode.EXTRACTION:
            # The checked evidence only, never the text
            data["document_evidence"] = self._document_evidence_text(state)
        plan: Optional[AnalysisPlan] = None
        self._context.event(
            state, EventType.PROGRESS, message="分析の計画を作っています"
        )
        for attempt in range(1, PLAN_ATTEMPTS + 1):
            self._context.before_model(state)
            raw = self._context.agents.run_advanced_planning(
                agent,
                prompts.planning_prompt(
                    generation.persona_count, attempt, candidates=candidates
                ),
                data,
                documents=self._full_texts(state),
            )
            self._context.checkpoint(state)  # a cancel during the call
            plan = AnalysisPolicy.settle_plan(
                raw,
                guide=guide,
                attempt=attempt,
                columns=self._columns(state),
                detailed=detailed,
                tables=table_columns or None,
                joined=bool(state.joins),
            )
            if plan is not None:
                break
        assert plan is not None
        state.plan = plan
        names = " → ".join(
            _STEP_NAMES.get(step.kind.value, "分析") for step in plan.plan_steps()
        )
        self._context.event(
            state,
            EventType.PROGRESS,
            message=f"計画ができました（手順 {len(plan.plan_steps())} 件: {names}）",
        )
        if not generation.confirm_plan:
            # A plan to confirm is announced once its card can be shown
            self._plan_event(state)

    def _plan_event(self, state: Run) -> None:
        assert state.plan is not None
        self._context.event(
            state,
            EventType.PLAN,
            method=state.plan.method.value,
            interpreted_purpose=state.plan.interpreted_purpose[:_EVENT_TEXT_MAX],
        )

    # --- Plan confirmation ---------------------------------------

    def await_plan_decision(self, state: Run) -> None:
        """Show the plan and wait for approve, replan or cancel.

        The thread keeps its slot and the lock while it waits and
        reads the generation every ``control_poll_seconds``. A
        replan makes the plan again with the new instruction and waits again,
        with no limit on the count. One card waits 15 minutes and all
        the cards 60 minutes together.

        Raises:
            Cancelled: The user cancelled while the card was shown.
            StopRun: A reader ended the generation.
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
                    self.plan(state)
            # a request taken by no one (a cancel wrote over it) is read again

    def _enter_awaiting(self, state: Run) -> None:
        """``planning -> awaiting_plan_approval`` with the longer lock."""
        if state.status is GenerationStatus.AWAITING_PLAN_APPROVAL:
            return
        assert state.plan is not None
        generation = state.generation
        now = self._context.clock()
        # From the generation's own deadline, so it never shrinks
        until = generation.created_at + timedelta(
            seconds=config.ADVANCED_GENERATION_LOCK_SECONDS
            + state.waited_seconds
            + config.ADVANCED_GENERATION_PLAN_WAIT_SECONDS
        )
        if not self._context.store.enter_awaiting(
            generation.generation_id,
            generation.owner_sub,
            generation.source_file_ids,
            now=now,
            until=until,
            updates={
                "plan": state.plan.to_dict(),
                **self.reading_update(state.reading),
            },
        ):
            raise StopRun()
        state.awaiting_since = now
        self._context.enter(state, GenerationStatus.AWAITING_PLAN_APPROVAL, moved=True)
        self._plan_event(state)

    def _wait_for_request(self, state: Run) -> ControlRequest:
        """Read the generation until a request arrives or a wait runs out."""
        generation_id = state.generation.generation_id
        assert state.awaiting_since is not None
        while True:
            self._context.checkpoint(state)
            try:
                current = self._context.store.get(generation_id)
            except CodedError as e:
                logger.warning("plan wait read failed (%s)", e.code.value)
                current = state.generation
            if current is None or current.status is not state.status:
                state.ended_elsewhere.set()
                raise StopRun()
            request = current.control_request
            if request is not None and request.action is ControlAction.CANCEL:
                state.cancel_request = request
                raise Cancelled()
            if request is not None:
                return request
            self._check_plan_wait(state, self._context.clock())
            state.cancel_requested.wait(self._context.control_poll_seconds)

    def _check_plan_wait(self, state: Run, now: datetime) -> None:
        assert state.awaiting_since is not None
        limit, total = self._context.plan_wait_limit(
            state.awaiting_since, state.waited_seconds, now
        )
        if limit is None:
            return
        self._context.log_plan_wait_expired(
            state.generation.generation_id, limit[0], total
        )
        raise self._context.timeout("plan wait ran out", limit[0], limit[1])

    def _waited_now(self, state: Run) -> int:
        """All the waits, the current card's included."""
        assert state.awaiting_since is not None
        current = int((self._context.clock() - state.awaiting_since).total_seconds())
        return state.waited_seconds + max(0, current)

    @staticmethod
    def reading_update(readings: Sequence[TableReading]) -> dict[str, Any]:
        return {"data_reading": [r.to_dict() for r in readings]} if readings else {}

    @staticmethod
    def _chosen_reading(
        state: Run, request: ControlRequest
    ) -> tuple[TableReading, ...]:
        """The tables' reading with the user's choices of the request put in."""
        choices = request.data_reading or {}
        out: list[TableReading] = []
        for reading in state.reading:
            choice = choices.get(reading.alias)
            if choice is None:
                out.append(reading)
                continue
            try:
                out.append(
                    reading.choose(
                        str(choice.get("kind")),
                        choice.get("person_id"),
                        choice.get("skip") or (),
                    )
                )
            except ValueError:
                out.append(reading)  # checked when it was asked; never trust twice
        return tuple(out)

    def _use_reading(self, state: Run, chosen: Sequence[TableReading]) -> None:
        """Take the confirmed reading; the guide table follows a changed kind."""
        if tuple(chosen) == state.reading or not chosen:
            return
        state.reading = tuple(chosen)
        if state.tables:
            self._context.use_guide_table(
                state, AnalysisPolicy.guide_alias(state.reading)
            )

    def _take_approve(self, state: Run, request: ControlRequest) -> bool:
        waited = self._waited_now(state)
        chosen = self._chosen_reading(state, request)
        if not self._context.store.take_control_request(
            state.generation.generation_id,
            GenerationStatus.AWAITING_PLAN_APPROVAL,
            GenerationStatus.RUNNING,
            request,
            updates={"waited_seconds": waited, **self.reading_update(chosen)},
            removes=("awaiting_since",),
        ):
            return False
        self._use_reading(state, chosen)
        self._taken(state, request, waited)
        self._context.enter(state, GenerationStatus.RUNNING, moved=True)
        return True

    def _take_replan(self, state: Run, request: ControlRequest) -> bool:
        """Put the new instruction in and go back to ``planning``."""
        generation = state.generation
        waited = self._waited_now(state)
        purpose = request.revised_purpose or ""
        original = (
            generation.original_purpose
            if generation.original_purpose is not None
            else generation.purpose
        )
        replans = state.replan_count + 1
        chosen = self._chosen_reading(state, request)
        if not self._context.store.take_control_request(
            generation.generation_id,
            GenerationStatus.AWAITING_PLAN_APPROVAL,
            GenerationStatus.PLANNING,
            request,
            updates={
                "waited_seconds": waited,
                "purpose": purpose,
                "original_purpose": original,
                "replan_count": replans,
                **self.reading_update(chosen),
            },
            removes=("awaiting_since",),
        ):
            return False
        self._use_reading(state, chosen)
        state.generation = generation.update(
            purpose=purpose, original_purpose=original, replan_count=replans
        )
        state.replan_count = replans
        self._taken(state, request, waited)
        self._context.enter(state, GenerationStatus.PLANNING, moved=True)
        return True

    def _taken(self, state: Run, request: ControlRequest, waited: int) -> None:
        state.waited_seconds = waited
        state.awaiting_since = None
        state.generation = state.generation.update(waited_seconds=waited)
        self._context.log_control_taken(state, request)

    def analyze(self, state: Run) -> None:
        assert state.plan is not None
        if state.source is None:
            # Documents alone have no table to analyse
            state.tools = NoTools()
            return
        self._context.before_model(state)
        generation = state.generation
        aliases = tuple(t.alias for t in state.tables)
        # run_code only when decided at the start (never re-read)
        policy = AnalysisPolicy.tool_policy(
            detailed=generation.use_detailed_analysis,
            in_process=self._context.in_process(),
            sources=aliases,
        )
        detailed = AnalysisTool.RUN_CODE in policy.tools
        if detailed and not self._hashes_known(state):
            # Nothing to compare a change with -> fail closed,
            # the fixed functions go on (as in-process does)
            detailed = False
            state.detailed_fallback = REASON_MISSING_HASH
            policy = AnalysisPolicy.tool_policy(
                detailed=False, in_process=self._context.in_process(), sources=aliases
            )
            self._context.event(
                state, EventType.PROGRESS, message=_RUN_CODE_DISABLED_NOTICE
            )
            self._context.log(
                "advanced_generation.run_code_disabled",
                generation_id=generation.generation_id,
                status="disabled",
                reason=REASON_MISSING_HASH,
                failures=0,
            )
        extra: dict[str, Any] = {
            "tables": [
                AnalysisTable(
                    t.alias,
                    t.source.source_file_id,
                    tuple(str(c.get("name")) for c in t.profile.get("columns") or []),
                )
                for t in state.tables
            ]
        }
        if detailed:
            state.budget = self._new_budget(state)
            extra.update(
                gate=state.budget,
                expected_library_sha256=self._context.sandbox.library_sha256,
                expected_parquet_sha256=self._expected_parquet(state),
            )
        tools = self._context.tool_factory(
            state.session,
            policy,
            source_id=state.source.source_file_id,
            columns=self._columns(state),
            persona_count=generation.persona_count,
            sandbox=self._context.sandbox,
            **extra,
        )
        state.tools = tools
        self._listen_to_calls(state, tools)
        self._context.event(state, EventType.PROGRESS, message="分析を始めます")
        agent = self._context.agents.create_advanced_analysis_agent(
            prompts.analysis_instructions(detailed=detailed, tables=len(aliases))
            + prompts.READING_NOTE,
            tools.tools(),
        )
        output = self._context.agents.run_advanced_analysis(
            agent,
            prompts.analysis_prompt(
                state.plan,
                generation.persona_count,
                joins=[plan for plan, _ in state.joins] if detailed else (),
            ),
            {
                "plan": json.dumps(state.plan.to_dict(), ensure_ascii=False),
                "profile": self._profile_text(state),
                **self._table_data(state),
                **self._reading_data(state),
            },
            source_id=state.source.source_file_id,
        )
        self._context.checkpoint(state)  # a cancel during the call
        if tools.sandbox_error is not None:
            raise tools.sandbox_error
        if state.budget is not None:
            self._check_untouched(state)
        self._check_cluster_calls(state)
        state.segments, state.evidence = self._parse_analysis(output)
        runs: dict[str, Optional[EmittedResult]] = {}
        if state.budget is not None:
            # Only what a successful run recounted goes on
            runs = {call_id: None for call_id in tools.run_code_ids()}
            runs.update(tools.emitted_results())
            state.segments, state.evidence = filter_evidence(
                state.segments, state.evidence, runs
            )
        # A feature is a number that was counted, never free text
        state.segments = settle_features(
            state.segments,
            runs=runs,
            outputs={cid: tools.output_of(cid) for cid in tools.tool_call_ids()},
            skip=self._skipped(state),
        )
        if not state.segments:
            raise fail(ErrorCode.GENERATION_ANALYSIS_FAILED, "no segment")
        self._context.event(
            state,
            EventType.PROGRESS,
            message=f"分析が終わりました（{len(state.segments)} つの層）",
        )

    def _listen_to_calls(self, state: Run, tools: Any) -> None:
        """Tell the screen each tool call as it finishes (fixed words only)."""
        listen = getattr(tools, "set_call_listener", None)
        if listen is None:
            return
        count = {"n": 0}

        def told(call: Any) -> None:
            count["n"] += 1
            name = _TOOL_NAMES.get(call.tool, "分析")
            if call.playbook:
                # the fixed name of the playbook read, never the user's data
                name += f"（{_STEP_NAMES.get(call.playbook, call.playbook)}）"
            tail = (
                ""
                if call.ok
                else _CALL_TROUBLES.get(call.error_code or "", _CALL_TROUBLE)
            )
            self._context.event(
                state,
                EventType.PROGRESS,
                message=f"操作 {count['n']}: {name}{tail}",
            )

        listen(told)

    # --- AI detailed analysis -----------------

    def _new_budget(self, state: Run) -> RunCodeBudget:
        generation_id = state.generation.generation_id

        def disabled(reason: str, failures: int) -> None:
            # Once, then the fixed functions go on
            self._context.event(
                state, EventType.PROGRESS, message=_RUN_CODE_DISABLED_NOTICE
            )
            self._context.log(
                "advanced_generation.run_code_disabled",
                generation_id=generation_id,
                status="disabled",
                reason=reason,
                failures=failures,
            )

        def tampered(changed: tuple[str, ...]) -> None:
            self._log_tamper(generation_id, changed)

        def verify_failed() -> None:
            self._log_verify_failed(state)

        return RunCodeBudget(
            max_failures=int(config.ADVANCED_RUN_CODE_MAX_FAILURES),
            on_disabled=disabled,
            on_tampered=tampered,
            on_verify_failed=verify_failed,
        )

    def _log_verify_failed(self, state: Run) -> None:
        """The check failed; told apart from a tamper."""
        budget = state.budget
        self._context.log(
            "advanced_generation.run_code_disabled",
            generation_id=state.generation.generation_id,
            status="disabled",
            reason=REASON_VERIFY_FAILED,
            failures=budget.failures if budget is not None else 0,
        )

    def _log_tamper(self, generation_id: str, changed: Sequence[str]) -> None:
        self._context.log(
            "advanced_generation.tamper_detected",
            generation_id=generation_id,
            status="detected",
            changed=list(changed),
        )

    @staticmethod
    def _expected_parquet(state: Run) -> dict[str, str]:
        """The Parquet hashes the preparation counted (held by the run only)."""
        return {
            t.source.source_file_id: t.report.parquet_sha256
            for t in state.tables
            if t.report.parquet_sha256
        }

    def _detailed_in_use(self, state: Run) -> bool:
        """Whether the analysis will get ``run_code`` (as :meth:`analyze` decides).

        The planning is told the detailed-only steps only then.
        """
        if state.source is None:
            return False
        policy = AnalysisPolicy.tool_policy(
            detailed=state.generation.use_detailed_analysis,
            in_process=self._context.in_process(),
            sources=tuple(t.alias for t in state.tables),
        )
        return AnalysisTool.RUN_CODE in policy.tools and self._hashes_known(state)

    def _hashes_known(self, state: Run) -> bool:
        """Every table's Parquet hash and the library hash are there."""
        return (
            bool(self._context.sandbox.library_sha256)
            and bool(state.tables)
            and all(t.report.parquet_sha256 for t in state.tables)
        )

    def _check_untouched(self, state: Run) -> None:
        """Fail on a change: one found by a run, or one left after the last run.

        A halted budget fails the generation. When run_code ran at
        least once, the hashes are checked once more with the app's own
        library sent inside the command, before anything
        reaches the synthesis. Processes the code left are a change
        (``process``); a process scan that could not run fails closed but is
        no tamper.
        """
        assert state.budget is not None and state.session is not None
        if state.budget.halted():
            raise fail(ErrorCode.GENERATION_ANALYSIS_FAILED, "analysis changed")
        if not state.tools.run_code_ids():
            return
        result = self._context.sandbox.call_verified(
            state.session,
            "verify_hashes",
            {
                "pgid_files": state.tools.pgid_files(),
                "sources": {t.alias: t.source.source_file_id for t in state.tables},
            },
        )
        value = result.get("value") if result.get("ok") else None
        hashes = value.get("hashes") if isinstance(value, Mapping) else None
        # security review: processes the code left are a change; a scan that
        # could not run fails closed like a failed check
        left = processes_left(value) if isinstance(value, Mapping) else None
        changed = (
            self._changed_hashes(state, hashes) if isinstance(hashes, Mapping) else []
        )
        if left:
            changed.append(CHANGED_PROCESS)
        if changed:
            self._log_tamper(state.generation.generation_id, changed)
            raise fail(ErrorCode.GENERATION_ANALYSIS_FAILED, "analysis changed")
        if not isinstance(hashes, Mapping) or left is None:
            # The check itself failed -> fail closed, no tamper
            self._log_verify_failed(state)
            raise fail(ErrorCode.GENERATION_ANALYSIS_FAILED, "check failed")

    def _changed_hashes(self, state: Run, hashes: Mapping[str, Any]) -> list[str]:
        """A missing expected or returned hash is a change."""
        changed: list[str] = []
        library = hashes.get("library")
        expected_library = self._context.sandbox.library_sha256
        if not expected_library or not library or library != expected_library:
            changed.append("library")
        found = hashes.get("parquet")
        parquet: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
        if not state.tables or any(
            not t.report.parquet_sha256
            or parquet.get(t.source.source_file_id) != t.report.parquet_sha256
            for t in state.tables
        ):
            changed.append("parquet")
        return changed

    @staticmethod
    def _check_cluster_calls(state: Run) -> None:
        """A cluster plan whose clustering failed twice cannot go on."""
        if state.plan is None or state.plan.method is not AnalysisMethod.CLUSTER:
            return
        calls = [c for c in state.tools.calls if c.tool == "cluster_segments"]
        failed = [c for c in calls if not c.ok and c.error_code in _RESELECT_CODES]
        if len(failed) >= 2 and not any(c.ok for c in calls):
            raise fail(ErrorCode.GENERATION_ANALYSIS_FAILED, "clustering failed twice")

    @staticmethod
    def _parse_analysis(
        output: Mapping[str, Any],
    ) -> tuple[list[Segment], list[Evidence]]:
        try:
            segments = [Segment.from_dict(s) for s in output.get("segments") or []]
            evidence = [Evidence.from_dict(e) for e in output.get("evidence") or []]
        except (KeyError, TypeError, ValueError):
            raise fail(
                ErrorCode.GENERATION_ANALYSIS_FAILED, "bad analysis output"
            ) from None
        return segments, evidence

    def _assign_to_segments(self, state: Run) -> None:
        """Extraction with a table: each evidence gets 0 or 1 segment."""
        if not state.document_evidence or not state.segments:
            return
        self._context.before_model(state)
        agent = self._context.agents.create_advanced_synthesis_agent(
            prompts.ASSIGNMENT_INSTRUCTIONS
        )
        output = self._context.agents.run_advanced_synthesis(
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
        self._context.checkpoint(state)  # a cancel during the call
        state.document_evidence = documents.apply_assignments(
            state.document_evidence,
            list(output.assignments),
            segment_ids={s.id for s in state.segments},
        )

    def synthesize(self, state: Run) -> list[prompts.AdvancedPersonaOutput]:
        generation = state.generation
        has_documents = bool(state.documents)
        full_text = state.document_mode is DocumentMode.FULL_TEXT
        if state.document_mode is DocumentMode.EXTRACTION:
            self._assign_to_segments(state)
        self._context.before_model(state)
        self._context.event(
            state,
            EventType.PROGRESS,
            message=f"ペルソナ {generation.persona_count} 人を作っています",
        )
        agent = self._context.agents.create_advanced_synthesis_agent(
            prompts.SYNTHESIS_INSTRUCTIONS
            + (prompts.DOCUMENT_SYNTHESIS_NOTE if has_documents else "")
        )
        data = {
            "purpose": generation.purpose,
            "segments": json.dumps(
                [s.to_dict() for s in state.segments], ensure_ascii=False
            ),
            # Evidence without a segment reaches every persona
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
        output = self._context.agents.run_advanced_synthesis(
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
        self._context.checkpoint(state)  # a cancel during the call
        personas = list(output.personas)
        if len(personas) != generation.persona_count:
            raise fail(
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
            state.document_evidence = self._context.check_quotes(state, evidence)
        # Only the generation's own evidence IDs are kept
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
