"""AnalysisToolService: the fixed tools of one advanced generation (C9, BR5.2).

Made per generation by ``ServiceFactory.create_analysis_tools`` (never a
singleton; ADR-012). Only the tools named in ``AnalysisToolPolicy`` are made:
``profile_dataset``, ``analyze_dataset`` and ``cluster_segments`` in U1.

- The agent never writes SQL. ``analyze_dataset`` takes the structured
  arguments of this package and ``sql_builder`` builds the SQL here, checking
  every column against the profiled ones (security-design.md §7).
- ``cluster_segments`` accepts profiled columns only; the group count is the
  persona count and the seed is fixed (BR5.3).
- Every call gets a ``tool_call_id``. Its structured output is kept for the
  Manager (Evidence.location, AnalysisVerifier), and the text the agent sees
  is wrapped as untrusted data (BR5.6).
- Call count, total time and output characters are limited by the policy
  (NFR5.8〜NFR5.10). Past a limit the tool answers ``LIMIT_REACHED`` so the
  agent sums up with what it has.

This package must not import ``sandbox_service``; the sandbox is injected.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

try:
    from strands import tool
except ImportError:  # pragma: no cover - SDK not installed

    def tool(func: Callable) -> Callable:  # type: ignore[no-redef]
        return func


from ...models.analysis import (
    DEFAULT_SEED,
    LIMIT_MAX_CALLS,
    LIMIT_MAX_OUTPUT_CHARS,
    LIMIT_MAX_TOTAL_SECONDS,
    AnalysisTool,
    AnalysisToolPolicy,
    wrap_untrusted_data,
)
from ...models.errors import CodedError
from .dataset_tools import Filter, GroupBy, Metric, OrderBy, ToolLimits
from .query_backend import DatasetQueryTimeout
from .sandbox_backend import SandboxCaller, SandboxQueryBackend, SandboxQueryError
from .sql_builder import SqlBuildError, build_analyze_query

logger = logging.getLogger(__name__)

_VIEW_NAME = "dataset"
#: Rows the clustering learns from at most (BR5.3).
DEFAULT_SAMPLE_MAX_ROWS = 1_000_000
_DEFAULT_LIMITS = {
    LIMIT_MAX_CALLS: 30,
    LIMIT_MAX_TOTAL_SECONDS: 20 * 60,
    LIMIT_MAX_OUTPUT_CHARS: 20_000,
}
_TRUNCATED_NOTE = "...(truncated)"
_RESELECT_CODES = frozenset({"TOO_FEW_COLUMNS", "EMPTY_CLUSTER"})


@dataclass(frozen=True)
class ToolCall:
    """One tool call, kept for the Manager (IDs, counts and codes only)."""

    tool_call_id: str
    tool: str
    ok: bool
    error_code: Optional[str]
    elapsed_seconds: float
    output_chars: int


class AnalysisToolService:
    """The tools and their record for one generation's analysis agent."""

    def __init__(
        self,
        sandbox: SandboxCaller,
        session: Any,
        policy: AnalysisToolPolicy,
        *,
        source_id: str,
        columns: Sequence[str],
        persona_count: int,
        seed: int = DEFAULT_SEED,
        sample_max_rows: int = DEFAULT_SAMPLE_MAX_ROWS,
        query_limits: Optional[ToolLimits] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sandbox = sandbox
        self._session = session
        self._policy = policy
        self._source_id = source_id
        self._columns = list(columns)
        self._persona_count = persona_count
        self._seed = seed
        self._sample_max_rows = sample_max_rows
        self._query_limits = query_limits or ToolLimits()
        self._backend = SandboxQueryBackend(
            sandbox, session, max_rows=self._query_limits.max_limit
        )
        self._clock = clock
        limits = {**_DEFAULT_LIMITS, **dict(policy.limits)}
        self._max_calls = int(limits[LIMIT_MAX_CALLS])
        self._max_seconds = float(limits[LIMIT_MAX_TOTAL_SECONDS])
        self._max_chars = int(limits[LIMIT_MAX_OUTPUT_CHARS])
        self.calls: List[ToolCall] = []
        self._outputs: Dict[str, Any] = {}
        self._executed = 0
        self._elapsed = 0.0
        #: A sandbox failure seen during a tool call; the Manager fails on it.
        self.sandbox_error: Optional[CodedError] = None

    # --- what the Manager reads ------------------------------------------------

    def tools(self) -> List[Callable]:
        """The allowed tools, in the policy's order (BR5.2)."""
        builders: Dict[AnalysisTool, Callable[[], Callable]] = {
            AnalysisTool.PROFILE_DATASET: self._profile_tool,
            AnalysisTool.ANALYZE_DATASET: self._analyze_tool,
            AnalysisTool.CLUSTER_SEGMENTS: self._cluster_tool,
        }
        return [builders[t]() for t in self._policy.tools if t in builders]

    def tool_call_ids(self) -> List[str]:
        """IDs of the calls that returned a result, in order."""
        return [c.tool_call_id for c in self.calls if c.ok]

    def output_of(self, tool_call_id: str) -> Optional[Any]:
        """Structured output of a call (what the verifier checks against)."""
        return self._outputs.get(tool_call_id)

    # --- the common path of every call ----------------------------------------

    def _limit_reached(self) -> bool:
        return self._executed >= self._max_calls or self._elapsed >= self._max_seconds

    def _next_call_id(self) -> str:
        return f"tc-{len(self.calls) + 1}"

    def _fail(
        self,
        tool_name: str,
        code: str,
        elapsed: float,
        context: Optional[Mapping[str, Any]] = None,
    ) -> str:
        call_id = self._next_call_id()
        self.calls.append(ToolCall(call_id, tool_name, False, code, elapsed, 0))
        logger.info(
            "analysis tool failed: tool=%s tool_call_id=%s error_code=%s",
            tool_name,
            call_id,
            code,
        )
        return self._error_text(call_id, code, context)

    def _run(self, tool_name: str, fn: Callable[[], Dict[str, Any]]) -> str:
        if self._limit_reached():
            return self._fail(tool_name, "LIMIT_REACHED", 0.0)
        started = self._clock()
        try:
            result = fn()
        except CodedError as e:
            # Code Interpreter failed after its retries: the generation fails.
            self.sandbox_error = e
            result = {"ok": False, "error": {"code": "SANDBOX_UNAVAILABLE"}}
        elapsed = max(self._clock() - started, 0.0)
        self._executed += 1
        self._elapsed += elapsed
        return self._finish(tool_name, result, elapsed)

    def _finish(self, tool_name: str, result: Mapping[str, Any], elapsed: float) -> str:
        if not result.get("ok"):
            error = dict(result.get("error") or {})
            code = str(error.get("code") or "TOOL_FAILED")
            return self._fail(tool_name, code, elapsed, error.get("context"))
        call_id = self._next_call_id()
        text = self._result_text(tool_name, call_id, result.get("value"))
        self.calls.append(ToolCall(call_id, tool_name, True, None, elapsed, len(text)))
        self._outputs[call_id] = result.get("value")
        logger.info(
            "analysis tool ok: tool=%s tool_call_id=%s chars=%d elapsed=%.2fs",
            tool_name,
            call_id,
            len(text),
            elapsed,
        )
        return text

    def _result_text(self, tool_name: str, call_id: str, value: Any) -> str:
        header = f"tool_call_id: {call_id}\n"
        payload = json.dumps(value, ensure_ascii=False, default=str)
        text = header + wrap_untrusted_data(payload, tool_name, tool_call_id=call_id)
        if len(text) <= self._max_chars:
            return text
        overhead = len(text) - len(payload) + len(_TRUNCATED_NOTE)
        cut = payload[: max(self._max_chars - overhead, 0)] + _TRUNCATED_NOTE
        return header + wrap_untrusted_data(cut, tool_name, tool_call_id=call_id)

    @staticmethod
    def _error_text(
        call_id: str, code: str, context: Optional[Mapping[str, Any]] = None
    ) -> str:
        lines = [f"tool_call_id: {call_id}", f"error: {code}"]
        if context:
            lines.append("context: " + json.dumps(dict(context), ensure_ascii=False))
        if code in _RESELECT_CODES:
            lines.append("Choose other profiled columns and try once more.")
        elif code == "LIMIT_REACHED":
            lines.append("No more tool calls. Sum up with the results you have.")
        return "\n".join(lines)

    # --- the three tools -------------------------------------------------------

    def _profile_tool(self) -> Callable:
        run = self._run
        call = self._sandbox_call

        @tool
        def profile_dataset() -> str:
            """Summarise every column of the table (rows, types, values).

            Returns:
                The summary as JSON inside a data block, with its tool_call_id.
            """
            return run(
                "profile_dataset",
                lambda: call("profile_dataset", {"source_id": self._source_id}),
            )

        return profile_dataset

    def _sandbox_call(self, function: str, args: Mapping[str, Any]) -> Dict[str, Any]:
        return dict(self._sandbox.call(self._session, function, args))

    def _analyze_tool(self) -> Callable:
        run = self._run
        query = self._query

        @tool
        def analyze_dataset(
            select_columns: Optional[List[str]] = None,
            filters: Optional[List[Filter]] = None,
            group_by: Optional[List[Union[str, GroupBy]]] = None,
            metrics: Optional[List[Metric]] = None,
            order_by: Optional[List[OrderBy]] = None,
            limit: Optional[int] = None,
        ) -> str:
            """Filter, group and aggregate the table (you cannot write SQL).

            Use profiled column names only. Without metrics it returns rows;
            with metrics it aggregates (group_by is optional).

            Args:
                select_columns: Columns to return (rows mode only).
                filters: Conditions of column / operator / value. Operators are
                    eq, ne, gt, gte, lt, lte, in (list value), contains (text).
                group_by: Column names, or {"column", "date_trunc"} with day,
                    week, month, quarter or year.
                metrics: count, count_distinct, sum, avg, min, max, median.
                    count needs no column; the others need one.
                order_by: column or metric_index (0-based), and asc / desc.
                limit: Maximum rows.

            Returns:
                The result as JSON inside a data block, with its tool_call_id.
            """
            arguments = {
                "select_columns": select_columns,
                "filters": [dict(f) for f in filters] if filters else None,
                "group_by": (
                    [g if isinstance(g, str) else dict(g) for g in group_by]
                    if group_by
                    else None
                ),
                "metrics": [dict(m) for m in metrics] if metrics else None,
                "order_by": [dict(o) for o in order_by] if order_by else None,
                "limit": limit,
            }
            return run("analyze_dataset", lambda: query(arguments))

        return analyze_dataset

    def _query(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            sql, params, label_map = build_analyze_query(
                view_name=_VIEW_NAME,
                allowed_columns=self._columns,
                default_limit=self._query_limits.default_limit,
                max_limit=self._query_limits.max_limit,
                **arguments,
            )
        except SqlBuildError:
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT"}}
        try:
            columns, rows = self._backend.execute(
                self._source_id,
                sql,
                params,
                timeout=self._query_limits.timeout_seconds,
            )
        except DatasetQueryTimeout:
            return {"ok": False, "error": {"code": "QUERY_TIMEOUT"}}
        except SandboxQueryError as e:
            return {"ok": False, "error": {"code": e.code}}
        labels = [label_map.get(c, c) for c in columns]
        return {"ok": True, "value": {"columns": labels, "rows": rows}}

    def _cluster_tool(self) -> Callable:
        run = self._run
        cluster = self._cluster

        @tool
        def cluster_segments(columns: List[str]) -> str:
            """Split everyone into as many groups as personas, by these columns.

            Learns on a sample and counts every row, so the group sizes add up
            to the whole table. Use profiled column names only.

            Args:
                columns: Numeric or category columns to group by.

            Returns:
                Groups with size, share and features as JSON inside a data
                block, with its tool_call_id.
            """
            return run("cluster_segments", lambda: cluster(list(columns)))

        return cluster_segments

    def _cluster(self, columns: List[str]) -> Dict[str, Any]:
        known = set(self._columns)
        if not columns or any(c not in known for c in columns):
            return {"ok": False, "error": {"code": "TOO_FEW_COLUMNS"}}
        return self._sandbox_call(
            "cluster_segments",
            {
                "source_id": self._source_id,
                "columns": columns,
                "k": self._persona_count,
                "seed": self._seed,
                "sample_max_rows": self._sample_max_rows,
            },
        )
