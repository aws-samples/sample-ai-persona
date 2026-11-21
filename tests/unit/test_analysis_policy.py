"""AnalysisPolicy: the tools, limits and plan rules of one analysis (BR5.1, BR5.2).

The three fixed tools only (no ``run_code``), limits from config, and a plan
that departs from the guide must say why (asked once more, then the guide).
"""

from __future__ import annotations

import pytest

from src.config import config
from src.managers.advanced_generation.analysis_policy import AnalysisPolicy
from src.models.analysis import (
    LIMIT_MAX_CALLS,
    LIMIT_MAX_OUTPUT_CHARS,
    LIMIT_MAX_TOTAL_SECONDS,
    AnalysisMethod,
    AnalysisTool,
)

pytestmark = pytest.mark.unit


def _profile(*columns: dict) -> dict:
    return {"rows": 100, "columns": list(columns)}


ID_COLUMN = {"name": "顧客ID", "type": "BIGINT", "all_distinct": True}
AGE = {"name": "年齢", "type": "BIGINT", "all_distinct": False}
SPEND = {"name": "購入額", "type": "DOUBLE", "all_distinct": False}
AREA = {"name": "地域", "type": "VARCHAR", "all_distinct": False}


class TestTools:
    def test_only_the_three_fixed_tools(self) -> None:
        policy = AnalysisPolicy.tool_policy()

        assert policy.tools == (
            AnalysisTool.PROFILE_DATASET,
            AnalysisTool.ANALYZE_DATASET,
            AnalysisTool.CLUSTER_SEGMENTS,
        )

    def test_limits_come_from_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "ADVANCED_TOOL_MAX_CALLS", 7)
        monkeypatch.setattr(config, "ADVANCED_TOOL_MAX_TOTAL_SECONDS", 60)
        monkeypatch.setattr(config, "ADVANCED_TOOL_MAX_OUTPUT_CHARS", 500)

        limits = AnalysisPolicy.tool_policy().limits

        assert limits == {
            LIMIT_MAX_CALLS: 7,
            LIMIT_MAX_TOTAL_SECONDS: 60,
            LIMIT_MAX_OUTPUT_CHARS: 500,
        }


class TestGuide:
    def test_a_named_target_is_filter(self) -> None:
        assert (
            AnalysisPolicy.guide_method("30代の東京の顧客", _profile(AGE, AREA))
            is AnalysisMethod.FILTER
        )

    def test_a_comparison_is_compare(self) -> None:
        assert (
            AnalysisPolicy.guide_method("東京と大阪を比較したい", _profile(AGE, AREA))
            is AnalysisMethod.COMPARE
        )

    def test_empty_with_one_row_per_person_is_cluster(self) -> None:
        assert (
            AnalysisPolicy.guide_method("", _profile(ID_COLUMN, AREA))
            is AnalysisMethod.CLUSTER
        )

    def test_empty_with_two_numbers_is_cluster(self) -> None:
        assert (
            AnalysisPolicy.guide_method("  ", _profile(AGE, SPEND))
            is AnalysisMethod.CLUSTER
        )

    def test_empty_otherwise_is_filter(self) -> None:
        assert AnalysisPolicy.guide_method("", _profile(AREA)) is AnalysisMethod.FILTER


def _raw(method: str, **extra) -> dict:
    return {
        "interpreted_purpose": "全体の層を知る",
        "questions": ["どんな層があるか"],
        "method": method,
        "rationale": "指示が空欄のため",
        **extra,
    }


class TestSettlePlan:
    def test_plan_following_the_guide_gets_the_budget(self) -> None:
        plan = AnalysisPolicy.settle_plan(
            _raw("cluster"), guide=AnalysisMethod.CLUSTER, attempt=1, columns=["年齢"]
        )

        assert plan is not None
        assert plan.method is AnalysisMethod.CLUSTER
        assert plan.budget["max_tool_calls"] == config.ADVANCED_TOOL_MAX_CALLS

    def test_departure_without_reason_is_asked_again_once(self) -> None:
        assert (
            AnalysisPolicy.settle_plan(
                _raw("filter"), guide=AnalysisMethod.CLUSTER, attempt=1, columns=[]
            )
            is None
        )

    def test_second_departure_without_reason_falls_back_to_the_guide(self) -> None:
        plan = AnalysisPolicy.settle_plan(
            _raw("filter"), guide=AnalysisMethod.CLUSTER, attempt=2, columns=[]
        )

        assert plan is not None and plan.method is AnalysisMethod.CLUSTER

    def test_departure_with_reason_is_kept(self) -> None:
        plan = AnalysisPolicy.settle_plan(
            _raw("filter", deviation_reason="ID の列が実は重複している"),
            guide=AnalysisMethod.CLUSTER,
            attempt=1,
            columns=[],
        )

        assert plan is not None and plan.method is AnalysisMethod.FILTER
        assert plan.deviation_reason

    def test_split_axis_must_be_a_profiled_column(self) -> None:
        plan = AnalysisPolicy.settle_plan(
            _raw("filter", split_axis="DROP TABLE"),
            guide=AnalysisMethod.FILTER,
            attempt=1,
            columns=["地域"],
        )

        assert plan is not None and plan.split_axis is None

    def test_empty_questions_get_one(self) -> None:
        plan = AnalysisPolicy.settle_plan(
            _raw("filter", questions=[]),
            guide=AnalysisMethod.FILTER,
            attempt=1,
            columns=[],
        )

        assert plan is not None and len(plan.questions) == 1
