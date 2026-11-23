"""The axis table and the join keys of several tables (U6 BR37.3, BR37.6).

Only ``AdvancedPersonaGenerationManager`` uses this, so it is a module of that
Manager's package. It decides from the profiles alone; the overlap of the
values is counted by the fixed ``key_overlap`` in the sandbox, which the
Manager runs through ``overlap`` (it returns ``None`` when the check ran out
of time, NFR1.29). No value of a column ever comes here.

- The axis is the first table, in the order chosen, with a column that looks
  like a person's ID (BR5.1). Without one nothing is joined.
- Candidates are columns of the same name in both tables whose type class
  (number, text, date) matches and whose values all differ in the axis.
- The candidate with the highest match rate at or over ``min_match_rate`` is
  the table's join key. Only axis pairs are judged (two plans at most).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from ...models.analysis import JoinPlan
from .analysis_policy import is_numeric_type, is_person_id

_DATE_TYPE = re.compile(r"^(DATE|TIME.*|TIMESTAMP.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class PlannedTable:
    """A prepared and profiled table of the generation (entities.md TableSource)."""

    alias: str
    source_id: str
    profile: Mapping[str, Any]


#: ``(axis, other, columns) -> [{column, match_rate, axis_matched}]`` or ``None``.
OverlapFn = Callable[
    [PlannedTable, PlannedTable, list[str]], Optional[list[Mapping[str, Any]]]
]


def _columns(profile: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [c for c in profile.get("columns") or [] if isinstance(c, Mapping)]


def _type_class(type_name: Any) -> str:
    text = str(type_name or "")
    if is_numeric_type(text):
        return "number"
    if _DATE_TYPE.match(text):
        return "date"
    return "text"


def axis_of(tables: Sequence[PlannedTable]) -> Optional[PlannedTable]:
    """The first table with a person's ID column, or ``None`` (BR37.6)."""
    for table in tables:
        if any(is_person_id(c) for c in _columns(table.profile)):
            return table
    return None


def candidates(
    axis_profile: Mapping[str, Any], other_profile: Mapping[str, Any]
) -> list[str]:
    """Same-named columns of one type class, all distinct on the axis (BR37.3)."""
    others = {str(c.get("name")): c for c in _columns(other_profile)}
    found: list[str] = []
    for column in _columns(axis_profile):
        name = str(column.get("name"))
        other = others.get(name)
        if other is None or not column.get("all_distinct"):
            continue
        if _type_class(column.get("type")) == _type_class(other.get("type")):
            found.append(name)
    return found


def _best(
    axis: PlannedTable,
    other: PlannedTable,
    overlaps: Sequence[Mapping[str, Any]],
    min_match_rate: float,
    allowed: Sequence[str],
) -> Optional[tuple[JoinPlan, int]]:
    best: Optional[tuple[float, str, int]] = None
    for row in overlaps:
        column = str(row.get("column"))
        try:
            rate = float(row.get("match_rate") or 0)
            matched = int(row.get("axis_matched") or 0)
        except (TypeError, ValueError):
            continue
        if column not in allowed or rate < min_match_rate or rate > 1:
            continue
        if best is None or rate > best[0]:
            best = (rate, column, matched)
    if best is None:
        return None
    rate, column, matched = best
    plan = JoinPlan(
        axis=axis.alias, table=other.alias, key_column=column, match_rate=rate
    )
    return plan, matched


def plan_joins(
    tables: Sequence[PlannedTable],
    *,
    overlap: OverlapFn,
    min_match_rate: float,
) -> list[tuple[JoinPlan, int]]:
    """The join plans and the axis people each one matched (BR37.3, BR37.6).

    Args:
        tables: The generation's tables in the order chosen.
        overlap: Counts the overlap of the candidate columns of a pair.
        min_match_rate: The lowest match rate that makes a key (0.5).

    Returns:
        ``(JoinPlan, matched_people)`` per joined table, in the order chosen.
    """
    axis = axis_of(tables)
    if axis is None or len(tables) < 2:
        return []
    joins: list[tuple[JoinPlan, int]] = []
    for other in tables:
        if other.alias == axis.alias:
            continue
        columns = candidates(axis.profile, other.profile)
        if not columns:
            continue
        overlaps = overlap(axis, other, columns)
        if overlaps is None:
            continue
        chosen = _best(axis, other, overlaps, min_match_rate, columns)
        if chosen is not None:
            joins.append(chosen)
    return joins
