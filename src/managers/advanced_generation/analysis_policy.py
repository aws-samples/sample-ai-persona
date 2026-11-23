"""Tools, limits and plan rules of one advanced generation analysis (BR5.1, BR5.2).

Only ``AdvancedPersonaGenerationManager`` uses this, so it is a module of that
Manager's package (components.md, "採らなかった分け方").

- Tools (BR5.2): the three fixed tools ``profile_dataset``,
  ``analyze_dataset`` and ``cluster_segments``. U6 (BR35.3, replacing
  BR5.2's "never ``run_code``"): ``run_code`` and ``load_playbook`` with the
  detailed analysis outside this process.
- Limits (NFR5.8〜NFR5.10): call count, total time and characters of one
  output, from ``config``; the Service uses only what it is given.
- Guide (BR5.1): a named target is ``filter``, a comparison ``compare``, an
  empty instruction with one row per person ``cluster``; otherwise
  ``cluster`` with two numeric columns or more, else ``filter``.
- A plan whose method departs from the guide needs ``deviation_reason``;
  without one it is asked once more, then the guide's method is used.
- U6 addendum: the plan is a sequence of steps. ``guide_candidates`` suggests
  them from the words and the column shapes (the guide's method stays the
  last), and ``settle_plan`` drops the steps that cannot run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from ...config import config
from ...models.analysis import (
    DETAILED_STEP_KINDS,
    MAX_PLAN_STEPS,
    AnalysisStep,
    AnalysisStepKind,
    JoinPlan,
    step_method,
    LIMIT_MAX_CALLS,
    LIMIT_MAX_OUTPUT_CHARS,
    LIMIT_MAX_TOTAL_SECONDS,
    LIMIT_RUN_CODE_MAX_FAILURES,
    LIMIT_RUN_CODE_MAX_CHARS,
    LIMIT_RUN_CODE_TIMEOUT_SECONDS,
    U1_ANALYSIS_TOOLS,
    AnalysisMethod,
    AnalysisPlan,
    AnalysisTool,
    AnalysisToolPolicy,
)

#: Words that ask for a comparison (BR5.1).
_COMPARE_WORDS = ("比較", "比べ", "違い", "差", "対比", "vs", "VS", "compare")
#: Column names that look like a person's ID (BR5.1).
_ID_NAME = re.compile(r"(id|ＩＤ|番号)", re.IGNORECASE)
_NUMERIC_TYPE = re.compile(
    r"^(TINYINT|SMALLINT|INTEGER|INT|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|"
    r"UBIGINT|FLOAT|REAL|DOUBLE|DECIMAL.*|NUMERIC.*)$",
    re.IGNORECASE,
)
#: Words that point at a purchase history scored by RFM (U6 addendum).
_RFM_WORDS = ("購買", "頻度", "最近", "新しさ", "RFM", "rfm", "優良", "離反", "離れ")
_DATE_TYPE = re.compile(r"^(DATE|TIMESTAMP.*|TIME.*)$", re.IGNORECASE)
#: Distinct values of a column that splits the rows into a few groups.
_FEW_VALUES = range(2, 6)
#: The fixed-tool step a detailed-only kind falls back to (U6 addendum).
_FIXED_STEP = {
    AnalysisStepKind.RFM: AnalysisStepKind.FILTER,
    AnalysisStepKind.COMPARE_GROUPS: AnalysisStepKind.COMPARE,
}
_REASON = {
    AnalysisStepKind.JOIN: "結合できる表の組がある",
    AnalysisStepKind.RFM: "1 人に複数行の履歴があり、日付と数値の列がある",
    AnalysisStepKind.COMPARE: "数種類の値の列と数値の列があり、群どうしを比べられる",
    AnalysisStepKind.COMPARE_GROUPS: "群の差が偶然かどうかを確かめられる",
    AnalysisStepKind.CLUSTER: "指示が空欄で、人の ID の列か数値の列が 2 つ以上ある",
    AnalysisStepKind.FILTER: "指示が対象を指定している",
}
_REASON_RFM_WORDS = "指示に購買の新しさ・頻度・優良などの語がある"
_REASON_COMPARE_WORDS = "指示が比較を求めている"
#: The plan's question when the agent wrote none.
_DEFAULT_QUESTION = "データの中にどのような層があるか"
#: Attempts the planning agent gets before the guide's method is used.
PLAN_ATTEMPTS = 2


@dataclass(frozen=True)
class StepCandidate:
    """A step the guide suggests to the planning agent, with its reason."""

    kind: AnalysisStepKind
    reason: str


class AnalysisPolicy:
    """The decisions on tools, limits and the plan of one analysis."""

    @staticmethod
    def tool_policy(
        *,
        detailed: bool = False,
        in_process: bool = False,
        sources: Sequence[str] = (),
    ) -> AnalysisToolPolicy:
        """The tools and limits handed to ``AnalysisToolService`` (ADR-012).

        U6 BR35.3 (replaces BR5.2): ``run_code`` and ``load_playbook`` are
        added only when the generation uses the detailed analysis, has a
        table and does not run in this process (NFR3.33), with the failures
        and the time of one run (BR36.2).

        Args:
            detailed: ``AdvancedGeneration.use_detailed_analysis``.
            in_process: The sandbox runs in this process.
            sources: The table aliases the tools may choose (BR37.2).
        """
        limits = {
            LIMIT_MAX_CALLS: int(config.ADVANCED_TOOL_MAX_CALLS),
            LIMIT_MAX_TOTAL_SECONDS: int(config.ADVANCED_TOOL_MAX_TOTAL_SECONDS),
            LIMIT_MAX_OUTPUT_CHARS: int(config.ADVANCED_TOOL_MAX_OUTPUT_CHARS),
        }
        tools: tuple[AnalysisTool, ...] = U1_ANALYSIS_TOOLS
        if detailed and not in_process and sources:
            tools = (
                *U1_ANALYSIS_TOOLS,
                AnalysisTool.RUN_CODE,
                AnalysisTool.LOAD_PLAYBOOK,
            )
            limits[LIMIT_RUN_CODE_MAX_FAILURES] = int(
                config.ADVANCED_RUN_CODE_MAX_FAILURES
            )
            limits[LIMIT_RUN_CODE_TIMEOUT_SECONDS] = int(
                config.ADVANCED_RUN_CODE_TIMEOUT_SECONDS
            )
            limits[LIMIT_RUN_CODE_MAX_CHARS] = int(config.ADVANCED_RUN_CODE_MAX_CHARS)
        return AnalysisToolPolicy(tools=tools, limits=limits, sources=tuple(sources))

    @staticmethod
    def guide_table(tables: Sequence[tuple[str, Mapping[str, Any]]]) -> str:
        """The table the guide reads: one row per person, else the first (BR37.2)."""
        for alias, profile in tables:
            if any(_is_person_id(c) for c in profile.get("columns") or []):
                return alias
        return tables[0][0]

    @staticmethod
    def budget() -> dict[str, int]:
        """The bounds written into the plan (shown in the record)."""
        return {
            "max_tool_calls": int(config.ADVANCED_TOOL_MAX_CALLS),
            "max_tool_seconds": int(config.ADVANCED_TOOL_MAX_TOTAL_SECONDS),
        }

    @staticmethod
    def guide_method(purpose: str, profile: Mapping[str, Any]) -> AnalysisMethod:
        """The method the guide expects for this instruction and table (BR5.1)."""
        text = purpose.strip()
        if text:
            if any(word in text for word in _COMPARE_WORDS):
                return AnalysisMethod.COMPARE
            return AnalysisMethod.FILTER
        columns = list(profile.get("columns") or [])
        if any(_is_person_id(c) for c in columns):
            return AnalysisMethod.CLUSTER
        numeric = [c for c in columns if _NUMERIC_TYPE.match(str(c.get("type", "")))]
        if len(numeric) >= 2:
            return AnalysisMethod.CLUSTER
        return AnalysisMethod.FILTER

    @staticmethod
    def guide_candidates(
        purpose: str,
        tables: Sequence[tuple[str, Mapping[str, Any]]],
        joins: Sequence[JoinPlan],
        detailed: bool,
    ) -> list[StepCandidate]:
        """The steps the guide suggests, from the words and the column shapes.

        U6 addendum: a join plan puts ``join`` first; a history with a date
        and a number (or purchase words) suggests ``rfm``; comparison words,
        or a column of two to five values beside a number, suggest
        ``compare`` and then ``compare_groups``. The last candidate always
        stands for :meth:`guide_method` (matching and BR5.1 use it). The
        detailed-only kinds come only when the detailed analysis is used.

        Args:
            purpose: The user's instruction.
            tables: ``(alias, profile)`` of every table, in order.
            joins: The join plans found (BR37.3).
            detailed: The generation uses the AI detailed analysis.
        """
        if not tables:
            return []
        text = purpose.strip()
        out: list[StepCandidate] = []
        if detailed and joins:
            out.append(
                StepCandidate(AnalysisStepKind.JOIN, _REASON[AnalysisStepKind.JOIN])
            )
        if detailed:
            joined = {j.table for j in joins}
            if any(word in text for word in _RFM_WORDS):
                out.append(StepCandidate(AnalysisStepKind.RFM, _REASON_RFM_WORDS))
            elif any(
                _is_history(profile, alias in joined) for alias, profile in tables
            ):
                out.append(
                    StepCandidate(AnalysisStepKind.RFM, _REASON[AnalysisStepKind.RFM])
                )
        compare_words = bool(text) and any(word in text for word in _COMPARE_WORDS)
        if compare_words or any(_has_groups(profile) for _, profile in tables):
            reason = (
                _REASON_COMPARE_WORDS
                if compare_words
                else _REASON[AnalysisStepKind.COMPARE]
            )
            out.append(StepCandidate(AnalysisStepKind.COMPARE, reason))
            if detailed:
                out.append(
                    StepCandidate(
                        AnalysisStepKind.COMPARE_GROUPS,
                        _REASON[AnalysisStepKind.COMPARE_GROUPS],
                    )
                )
        guide_alias = AnalysisPolicy.guide_table(tables)
        profile = next(p for alias, p in tables if alias == guide_alias)
        guide = AnalysisPolicy.guide_method(purpose, profile)
        if not out or step_method(out[-1].kind) is not guide:
            kind = AnalysisStepKind(guide.value)
            out = [c for c in out if c.kind is not kind]
            out.append(StepCandidate(kind, _REASON[kind]))
        return out

    @staticmethod
    def settle_plan(
        raw: Mapping[str, Any],
        *,
        guide: AnalysisMethod,
        attempt: int,
        columns: Sequence[str],
        detailed: bool = False,
        tables: Optional[Mapping[str, Sequence[str]]] = None,
    ) -> Optional[AnalysisPlan]:
        """The plan to run, or ``None`` to ask the planning agent again (BR5.1).

        Args:
            raw: The planning agent's fields (no budget).
            guide: :meth:`guide_method`'s answer.
            attempt: 1 for the first answer, 2 for the second.
            columns: Profiled column names; ``split_axis`` must be one.
            detailed: U6 addendum: the generation uses the detailed analysis;
                without it the detailed-only steps are replaced or dropped.
            tables: U6 addendum: profiled column names per table alias; a
                step naming another table or column is dropped. Defaults to
                ``columns`` as ``source_1``.
        """
        method = AnalysisMethod(str(raw.get("method")))
        reason = str(raw.get("deviation_reason") or "").strip() or None
        if method is not guide and reason is None:
            if attempt < PLAN_ATTEMPTS:
                return None
            method = guide
        axis = raw.get("split_axis")
        questions = [str(q) for q in raw.get("questions") or [] if str(q).strip()]
        known = dict(tables) if tables is not None else {"source_1": list(columns)}
        steps: Optional[tuple[AnalysisStep, ...]] = None
        if raw.get("steps"):
            steps = _settle_steps(raw["steps"], detailed=detailed, tables=known) or (
                AnalysisStep(
                    AnalysisStepKind(method.value), str(raw.get("rationale", ""))
                ),
            )
        return AnalysisPlan(
            interpreted_purpose=str(raw.get("interpreted_purpose", "")),
            questions=tuple(questions or [_DEFAULT_QUESTION]),
            method=method,
            rationale=str(raw.get("rationale", "")),
            budget=AnalysisPolicy.budget(),
            split_axis=str(axis) if axis in set(columns) else None,
            deviation_reason=reason if method is not guide else None,
            steps=steps,
        )


def _settle_steps(
    raw_steps: Any, *, detailed: bool, tables: Mapping[str, Sequence[str]]
) -> tuple[AnalysisStep, ...]:
    """The steps that can run, in order, at most four (U6 addendum)."""
    every_column = {str(c) for names in tables.values() for c in names}
    out: list[AnalysisStep] = []
    for item in raw_steps if isinstance(raw_steps, list) else []:
        if not isinstance(item, Mapping):
            continue
        try:
            step = AnalysisStep.from_dict(item)
        except (KeyError, ValueError):
            continue
        if step.kind in DETAILED_STEP_KINDS and not detailed:
            fixed = _FIXED_STEP.get(step.kind)
            if fixed is None:
                continue
            step = AnalysisStep(fixed, step.reason, step.table, step.column)
        if step.table is not None and step.table not in tables:
            continue
        names = (
            {str(c) for c in tables[step.table]}
            if step.table is not None
            else every_column
        )
        if step.column is not None and step.column not in names:
            continue
        if out and out[-1].kind is step.kind:
            continue
        out.append(step)
    return tuple(out[:MAX_PLAN_STEPS])


def _is_history(profile: Mapping[str, Any], joined: bool) -> bool:
    """A table with several rows per person, a date and a number (U6 addendum)."""
    columns = list(profile.get("columns") or [])
    several = joined or any(
        _ID_NAME.search(str(c.get("name", ""))) and not c.get("all_distinct")
        for c in columns
    )
    has_date = any(_DATE_TYPE.match(str(c.get("type", ""))) for c in columns)
    has_number = any(
        _NUMERIC_TYPE.match(str(c.get("type", "")))
        and not _ID_NAME.search(str(c.get("name", "")))
        for c in columns
    )
    return bool(several and has_date and has_number)


def _has_groups(profile: Mapping[str, Any]) -> bool:
    """A column of two to five values and another numeric column (U6 addendum)."""
    columns = list(profile.get("columns") or [])
    groups = [c for c in columns if c.get("distinct") in _FEW_VALUES]
    return any(
        _NUMERIC_TYPE.match(str(n.get("type", "")))
        and not _ID_NAME.search(str(n.get("name", "")))
        and n is not g
        for g in groups
        for n in columns
    )


def is_numeric_type(type_name: str) -> bool:
    """Whether a profiled DuckDB type is a number."""
    return bool(_NUMERIC_TYPE.match(str(type_name)))


def is_person_id(column: Mapping[str, Any]) -> bool:
    """A column that looks like a person's ID (BR5.1, U6 BR37.6)."""
    return _is_person_id(column)


def _is_person_id(column: Mapping[str, Any]) -> bool:
    """A column whose values all differ and whose name or type says ID (BR5.1)."""
    if not column.get("all_distinct"):
        return False
    name = str(column.get("name", ""))
    is_text = not _NUMERIC_TYPE.match(str(column.get("type", "")))
    return bool(_ID_NAME.search(name)) or is_text
