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
from ...models.advanced_generation import TableReading
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
#: Column names that look like a person's ID (BR5.1): "id" as a word of its own
#: (``id``, ``customer_id``, ``customerId``, ``顧客ID``), ``ＩＤ`` and ``番号``.
#: A name that merely holds the letters (``residence``, ``paid``, ``video``) is not.
_ID_NAME = re.compile(
    r"(?<![A-Za-z])[Ii][Dd](?![a-z])"
    r"|(?<=[a-z])I[Dd](?![a-z])"
    r"|(?<=[a-z])ID(?![A-Za-z])"
    r"|ＩＤ|番号"
)

#: Words of an ID column that points at a person (not at a store or a product).
_PERSON_WORD = re.compile(
    r"customer|user|member|person|client|employee|staff|respondent|account"
    r"|顧客|会員|ユーザー|社員|従業員|回答者|利用者|アカウント",
    re.IGNORECASE,
)
#: Columns that are codes (no trait of a person): ``zip``, ``postal``, ``code``,
#: ``no``, ``num`` as a word of their own, ``コード`` and ``郵便``.
_CODE_NAME = re.compile(
    r"(?<![A-Za-z])(?:no|num|code|zip|postal)(?![a-z])|コード|郵便", re.IGNORECASE
)


def is_id_name(name: str) -> bool:
    """Whether a column name says it is an ID (see ``_ID_NAME``)."""
    return bool(_ID_NAME.search(str(name)))


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
#: Reasons of the steps the policy adds (shown on the plan card).
_LAST_STEP_REASON = "最後に層を作る"
_JOIN_STEP_REASON = "複数の表を、同じ人でつなげる"
#: The fixed-tool step a detailed-only kind falls back to (U6 addendum).
_FIXED_STEP = {
    AnalysisStepKind.RFM: AnalysisStepKind.FILTER,
    AnalysisStepKind.COMPARE_GROUPS: AnalysisStepKind.COMPARE,
}
_REASON = {
    AnalysisStepKind.JOIN: "結合できる表の組がある",
    AnalysisStepKind.PERSON_PROFILE: "1 人に複数行の履歴があり、人ごとの特徴を作れる",
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
    def read_tables(
        tables: Sequence[tuple[str, str, Mapping[str, Any]]],
    ) -> tuple[TableReading, ...]:
        """How each table reads, proposed from its column profile (U6 addendum).

        ``tables`` are ``(alias, file name, profile)``. The user may correct
        the proposal on the plan card; what they confirmed is used as it is.
        """
        return tuple(
            _read_table(alias, name, profile) for alias, name, profile in tables
        )

    @staticmethod
    def guide_alias(readings: Sequence[TableReading]) -> str:
        """The table the guide reads: one row per person, else the first (BR37.2)."""
        for reading in readings:
            if reading.kind == "people" and reading.person_id is not None:
                return reading.alias
        return readings[0].alias

    @staticmethod
    def readings_count_rows(readings: Sequence[TableReading]) -> bool:
        """Whether every table is a history, so what is counted is rows (U6)."""
        return bool(readings) and all(r.kind == "history" for r in readings)

    @staticmethod
    def guide_table(tables: Sequence[tuple[str, Mapping[str, Any]]]) -> str:
        """The table the guide reads, from the profiles alone (BR37.2)."""
        return AnalysisPolicy.guide_alias(
            AnalysisPolicy.read_tables([(a, a, p) for a, p in tables])
        )

    @staticmethod
    def rows_are_not_people(
        tables: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> bool:
        """Whether every table is a history, from the profiles alone (U6).

        The counts and shares of such a result are of rows (orders, events),
        not of people.
        """
        return AnalysisPolicy.readings_count_rows(
            AnalysisPolicy.read_tables([(a, a, p) for a, p in tables])
        )

    @staticmethod
    def budget() -> dict[str, int]:
        """The bounds written into the plan (shown in the record)."""
        return {
            "max_tool_calls": int(config.ADVANCED_TOOL_MAX_CALLS),
            "max_tool_seconds": int(config.ADVANCED_TOOL_MAX_TOTAL_SECONDS),
        }

    @staticmethod
    def guide_method(
        purpose: str,
        profile: Mapping[str, Any],
        reading: Optional[TableReading] = None,
    ) -> AnalysisMethod:
        """The method the guide expects for this instruction and table (BR5.1).

        ``reading`` is how the table reads (what the user confirmed on the plan
        card, else the proposal); without it the profile alone is read.
        """
        text = purpose.strip()
        if text:
            if any(word in text for word in _COMPARE_WORDS):
                return AnalysisMethod.COMPARE
            return AnalysisMethod.FILTER
        columns = list(profile.get("columns") or [])
        if reading is not None:
            kind, person = reading.kind, reading.person_id
        else:
            kind, person = _kind_and_person(profile)
        if kind == "people" and person is not None:
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
        readings: Sequence[TableReading] = (),
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
            readings: How each table reads (what the user confirmed, else the
                proposal). A table that has one decides by it, not by its
                profile.
        """
        if not tables:
            return []
        kinds = {r.alias: r.kind for r in readings}

        def history(alias: str, profile: Mapping[str, Any]) -> bool:
            if alias in kinds:
                return kinds[alias] == "history"
            return _kind_and_person(profile)[0] == "history"

        text = purpose.strip()
        out: list[StepCandidate] = []
        if detailed and joins:
            out.append(
                StepCandidate(AnalysisStepKind.JOIN, _REASON[AnalysisStepKind.JOIN])
            )
        if detailed and any(history(alias, profile) for alias, profile in tables):
            out.append(
                StepCandidate(
                    AnalysisStepKind.PERSON_PROFILE,
                    _REASON[AnalysisStepKind.PERSON_PROFILE],
                )
            )
        if detailed:
            joined = {j.table for j in joins}
            if any(word in text for word in _RFM_WORDS):
                out.append(StepCandidate(AnalysisStepKind.RFM, _REASON_RFM_WORDS))
            elif any(
                _is_history(profile, alias in joined, history(alias, profile))
                for alias, profile in tables
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
        if readings:
            guide_alias = AnalysisPolicy.guide_alias(readings)
        else:
            guide_alias = AnalysisPolicy.guide_table(tables)
        profile = next(p for alias, p in tables if alias == guide_alias)
        reading = next((r for r in readings if r.alias == guide_alias), None)
        guide = AnalysisPolicy.guide_method(purpose, profile, reading)
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
        joined: bool = False,
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
            joined: U6 addendum: the tables have a join plan; a plan without a
                join step gets one first (it is the only way they meet).
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
            steps = _end_with_method(
                _settle_steps(raw["steps"], detailed=detailed, tables=known), method
            )
            if joined and detailed:
                steps = _with_join(steps)
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
        if any(done.kind is step.kind for done in out):
            continue  # one step of a kind: the first says why
        out.append(step)
    return tuple(out)


def _fit(steps: tuple[AnalysisStep, ...]) -> tuple[AnalysisStep, ...]:
    """At most four steps: the earliest go first, but a join stays at the head."""
    out = list(steps)
    while len(out) > MAX_PLAN_STEPS:
        at = 1 if out[0].kind is AnalysisStepKind.JOIN and len(out) > 2 else 0
        del out[at]
    return tuple(out)


def _end_with_method(
    steps: tuple[AnalysisStep, ...], method: AnalysisMethod
) -> tuple[AnalysisStep, ...]:
    """Steps whose last one makes the segments of ``method``, at most four.

    The plan's ``method`` is the method of the step that makes the segments
    (U6 addendum). When the agent's steps end elsewhere (a join, or another
    method because ``method`` was forced to the guide), the step of ``method``
    the agent already wrote moves to the end, or one is added.
    """
    kind = AnalysisStepKind(method.value)
    if not steps:
        return (AnalysisStep(kind, _LAST_STEP_REASON),)
    if step_method(steps[-1].kind) is not method:
        same = next((s for s in steps if s.kind is kind), None)
        if same is not None:
            steps = (*[s for s in steps if s is not same], same)
        else:
            steps = (*steps, AnalysisStep(kind, _LAST_STEP_REASON))
    return _fit(steps)


def _with_join(steps: tuple[AnalysisStep, ...]) -> tuple[AnalysisStep, ...]:
    """A plan over joined tables starts with the join."""
    if any(s.kind is AnalysisStepKind.JOIN for s in steps):
        return steps
    return _fit((AnalysisStep(AnalysisStepKind.JOIN, _JOIN_STEP_REASON), *steps))


def _id_columns(profile: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        c
        for c in profile.get("columns") or []
        if _ID_NAME.search(str(c.get("name", "")))
    ]


def _kind_and_person(profile: Mapping[str, Any]) -> tuple[str, Optional[str]]:
    """``(kind, person ID column)`` read from the names and the distinct counts.

    An ID that points at a person (``customer_id``) tells the table's kind. An
    ID that repeats but points at a store or a product does not: the table of
    customers that also has ``store_id`` is still one row per person.
    """
    ids = _id_columns(profile)
    persons = [c for c in ids if _PERSON_WORD.search(str(c.get("name", "")))]
    if persons:
        chosen = next((c for c in persons if c.get("all_distinct")), persons[0])
        return (
            "people" if chosen.get("all_distinct") else "history",
            str(chosen.get("name")),
        )
    unique = next((c for c in ids if c.get("all_distinct")), None)
    if unique is not None:
        return "people", str(unique.get("name"))
    if ids:
        return "history", str(ids[0].get("name"))
    text = next(
        (c for c in profile.get("columns") or [] if _is_person_id(c)),
        None,
    )
    return "people", (str(text.get("name")) if text is not None else None)


def _read_table(alias: str, name: str, profile: Mapping[str, Any]) -> TableReading:
    # Each table is read on its own: a table beside a history is not one itself.
    kind, person = _kind_and_person(profile)
    columns = [str(c.get("name")) for c in profile.get("columns") or []]
    keys = [
        str(c.get("name"))
        for c in profile.get("columns") or []
        if _ID_NAME.search(str(c.get("name", "")))
        or _CODE_NAME.search(str(c.get("name", "")))
        or _is_person_id(c)
    ]
    skip = list(dict.fromkeys([*([person] if person else []), *keys]))
    named = person is not None and any(
        str(c.get("name")) == person
        and (_ID_NAME.search(person) or _PERSON_WORD.search(person))
        for c in profile.get("columns") or []
    )
    return TableReading(
        alias=alias,
        name=name,
        kind=kind,
        person_id=person,
        skip=tuple(skip),
        columns=tuple(columns),
        uncertain=not named,
    )


def _is_history(
    profile: Mapping[str, Any], joined: bool, history: Optional[bool] = None
) -> bool:
    """A table with several rows per person, a date and a number (U6 addendum).

    ``history`` is what the table's reading says; without it the profile's
    columns decide.
    """
    columns = list(profile.get("columns") or [])
    several = joined or (
        history if history is not None else _kind_and_person(profile)[0] == "history"
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
