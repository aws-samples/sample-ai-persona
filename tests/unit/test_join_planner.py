"""join_planner: the axis table and the join key of several tables.

A pure decision of the Manager's package: the overlap counts come from the
fixed ``key_overlap`` through a callable (``None`` when the check ran out of
time).
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from src.managers.advanced_generation.join_planner import (
    PlannedTable,
    axis_of,
    candidates,
    plan_joins,
)
from src.models.analysis import JoinPlan

pytestmark = pytest.mark.unit


def _column(name: str, type_: str, *, distinct: bool = False) -> dict[str, Any]:
    return {"name": name, "type": type_, "all_distinct": distinct}


CUSTOMERS = PlannedTable(
    alias="source_1",
    source_id="f1",
    profile={
        "columns": [
            _column("customer_id", "VARCHAR", distinct=True),
            _column("age", "BIGINT"),
            _column("joined_on", "DATE"),
        ]
    },
)
PURCHASES = PlannedTable(
    alias="source_2",
    source_id="f2",
    profile={
        "columns": [
            _column("customer_id", "VARCHAR"),
            _column("age", "VARCHAR"),
            _column("joined_on", "TIMESTAMP"),
            _column("amount", "DOUBLE"),
        ]
    },
)
VISITS = PlannedTable(
    alias="source_3",
    source_id="f3",
    profile={
        "columns": [_column("customer_id", "VARCHAR"), _column("page", "VARCHAR")]
    },
)
LOG = PlannedTable(
    alias="source_1",
    source_id="f0",
    profile={"columns": [_column("page", "VARCHAR"), _column("views", "BIGINT")]},
)


class _Overlap:
    def __init__(self, rates: dict[tuple[str, str], list[dict[str, Any]] | None]):
        self.rates = rates
        self.asked: list[tuple[str, str, list[str]]] = []

    def __call__(
        self, axis: PlannedTable, other: PlannedTable, columns: list[str]
    ) -> Optional[list[dict[str, Any]]]:
        self.asked.append((axis.alias, other.alias, list(columns)))
        return self.rates.get((axis.alias, other.alias))


def _rate(column: str, rate: float, matched: int = 10) -> dict[str, Any]:
    return {"column": column, "match_rate": rate, "axis_matched": matched}


class TestAxis:
    def test_the_first_table_with_a_person_id_is_the_axis(self) -> None:
        assert axis_of([PURCHASES, CUSTOMERS]) is CUSTOMERS

    def test_no_person_id_means_no_axis(self) -> None:
        assert axis_of([LOG, PURCHASES]) is None


class TestCandidates:
    def test_same_name_same_type_class_and_distinct_on_the_axis(self) -> None:
        # age: number vs text; joined_on: both dates but not distinct on the axis
        assert candidates(CUSTOMERS.profile, PURCHASES.profile) == ["customer_id"]

    def test_nothing_in_common(self) -> None:
        assert candidates(CUSTOMERS.profile, LOG.profile) == []


class TestPlanJoins:
    def test_the_best_column_at_or_over_the_rate_is_the_plan(self) -> None:
        overlap = _Overlap({("source_1", "source_2"): [_rate("customer_id", 0.5, 24)]})

        joins = plan_joins([CUSTOMERS, PURCHASES], overlap=overlap, min_match_rate=0.5)

        assert joins == [
            (
                JoinPlan(
                    axis="source_1",
                    table="source_2",
                    key_column="customer_id",
                    match_rate=0.5,
                ),
                24,
            )
        ]

    def test_under_the_rate_is_no_plan(self) -> None:
        overlap = _Overlap({("source_1", "source_2"): [_rate("customer_id", 0.49)]})

        assert (
            plan_joins([CUSTOMERS, PURCHASES], overlap=overlap, min_match_rate=0.5)
            == []
        )

    def test_no_candidate_asks_nothing(self) -> None:
        overlap = _Overlap({})

        assert plan_joins([CUSTOMERS, LOG], overlap=overlap, min_match_rate=0.5) == []
        assert overlap.asked == []

    def test_a_check_out_of_time_is_no_plan(self) -> None:
        overlap = _Overlap({("source_1", "source_2"): None})

        assert (
            plan_joins([CUSTOMERS, PURCHASES], overlap=overlap, min_match_rate=0.5)
            == []
        )

    def test_only_axis_pairs_are_judged_in_the_order_chosen(self) -> None:
        overlap = _Overlap(
            {
                ("source_1", "source_2"): [_rate("customer_id", 0.9)],
                ("source_1", "source_3"): [_rate("customer_id", 0.7)],
            }
        )

        joins = plan_joins(
            [CUSTOMERS, PURCHASES, VISITS], overlap=overlap, min_match_rate=0.5
        )

        assert [(a, b) for a, b, _ in overlap.asked] == [
            ("source_1", "source_2"),
            ("source_1", "source_3"),
        ]
        assert [plan.table for plan, _ in joins] == ["source_2", "source_3"]

    def test_no_axis_no_join(self) -> None:
        overlap = _Overlap({})

        assert plan_joins([LOG, PURCHASES], overlap=overlap, min_match_rate=0.5) == []

    def test_one_table_has_nothing_to_join(self) -> None:
        assert plan_joins([CUSTOMERS], overlap=_Overlap({}), min_match_rate=0.5) == []
