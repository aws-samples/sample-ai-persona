"""Tools, limits and plan rules of one advanced generation analysis (BR5.1, BR5.2).

Only ``AdvancedPersonaGenerationManager`` uses this, so it is a module of that
Manager's package (components.md, "採らなかった分け方").

- Tools (BR5.2): the three fixed tools ``profile_dataset``,
  ``analyze_dataset`` and ``cluster_segments``. ``run_code`` is never given
  in U1.
- Limits (NFR5.8〜NFR5.10): call count, total time and characters of one
  output, from ``config``; the Service uses only what it is given.
- Guide (BR5.1): a named target is ``filter``, a comparison ``compare``, an
  empty instruction with one row per person ``cluster``; otherwise
  ``cluster`` with two numeric columns or more, else ``filter``.
- A plan whose method departs from the guide needs ``deviation_reason``;
  without one it is asked once more, then the guide's method is used.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence

from ...config import config
from ...models.analysis import (
    LIMIT_MAX_CALLS,
    LIMIT_MAX_OUTPUT_CHARS,
    LIMIT_MAX_TOTAL_SECONDS,
    U1_ANALYSIS_TOOLS,
    AnalysisMethod,
    AnalysisPlan,
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
#: The plan's question when the agent wrote none.
_DEFAULT_QUESTION = "データの中にどのような層があるか"
#: Attempts the planning agent gets before the guide's method is used.
PLAN_ATTEMPTS = 2


class AnalysisPolicy:
    """The decisions on tools, limits and the plan of one analysis."""

    @staticmethod
    def tool_policy() -> AnalysisToolPolicy:
        """The tools and limits handed to ``AnalysisToolService`` (ADR-012)."""
        return AnalysisToolPolicy(
            tools=U1_ANALYSIS_TOOLS,
            limits={
                LIMIT_MAX_CALLS: int(config.ADVANCED_TOOL_MAX_CALLS),
                LIMIT_MAX_TOTAL_SECONDS: int(config.ADVANCED_TOOL_MAX_TOTAL_SECONDS),
                LIMIT_MAX_OUTPUT_CHARS: int(config.ADVANCED_TOOL_MAX_OUTPUT_CHARS),
            },
        )

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
    def settle_plan(
        raw: Mapping[str, Any],
        *,
        guide: AnalysisMethod,
        attempt: int,
        columns: Sequence[str],
    ) -> Optional[AnalysisPlan]:
        """The plan to run, or ``None`` to ask the planning agent again (BR5.1).

        Args:
            raw: The planning agent's fields (no budget).
            guide: :meth:`guide_method`'s answer.
            attempt: 1 for the first answer, 2 for the second.
            columns: Profiled column names; ``split_axis`` must be one.
        """
        method = AnalysisMethod(str(raw.get("method")))
        reason = str(raw.get("deviation_reason") or "").strip() or None
        if method is not guide and reason is None:
            if attempt < PLAN_ATTEMPTS:
                return None
            method = guide
        axis = raw.get("split_axis")
        questions = [str(q) for q in raw.get("questions") or [] if str(q).strip()]
        return AnalysisPlan(
            interpreted_purpose=str(raw.get("interpreted_purpose", "")),
            questions=tuple(questions or [_DEFAULT_QUESTION]),
            method=method,
            rationale=str(raw.get("rationale", "")),
            budget=AnalysisPolicy.budget(),
            split_axis=str(axis) if axis in set(columns) else None,
            deviation_reason=reason if method is not guide else None,
        )


def _is_person_id(column: Mapping[str, Any]) -> bool:
    """A column whose values all differ and whose name or type says ID (BR5.1)."""
    if not column.get("all_distinct"):
        return False
    name = str(column.get("name", ""))
    is_text = not _NUMERIC_TYPE.match(str(column.get("type", "")))
    return bool(_ID_NAME.search(name)) or is_text
