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
    LIMIT_RUN_CODE_MAX_CHARS,
    LIMIT_RUN_CODE_MAX_FAILURES,
    LIMIT_RUN_CODE_TIMEOUT_SECONDS,
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
    def test_only_the_four_fixed_tools(self) -> None:
        policy = AnalysisPolicy.tool_policy()

        assert policy.tools == (
            AnalysisTool.PROFILE_DATASET,
            AnalysisTool.ANALYZE_DATASET,
            AnalysisTool.CLUSTER_SEGMENTS,
            AnalysisTool.PROFILE_SEGMENTS,
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


# --- U6: run_code and several tables (BR35.3, BR36.2, BR37.2, NFR3.33) ------

SOURCES = ("source_1", "source_2")


class TestToolsU6:
    def test_detailed_outside_the_process_adds_run_code_and_playbooks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_RUN_CODE_MAX_FAILURES", 4)
        monkeypatch.setattr(config, "ADVANCED_RUN_CODE_TIMEOUT_SECONDS", 90)
        monkeypatch.setattr(config, "ADVANCED_RUN_CODE_MAX_CHARS", 12_345)

        policy = AnalysisPolicy.tool_policy(
            detailed=True, in_process=False, sources=SOURCES
        )

        assert policy.tools[-2:] == (AnalysisTool.RUN_CODE, AnalysisTool.LOAD_PLAYBOOK)
        assert policy.limits[LIMIT_RUN_CODE_MAX_FAILURES] == 4
        assert policy.limits[LIMIT_RUN_CODE_TIMEOUT_SECONDS] == 90
        assert policy.limits[LIMIT_RUN_CODE_MAX_CHARS] == 12_345
        assert policy.sources == SOURCES

    @pytest.mark.parametrize(
        ("detailed", "in_process"), [(False, False), (True, True), (False, True)]
    )
    def test_otherwise_the_fixed_tools_only(
        self, detailed: bool, in_process: bool
    ) -> None:
        policy = AnalysisPolicy.tool_policy(
            detailed=detailed, in_process=in_process, sources=SOURCES
        )

        assert AnalysisTool.RUN_CODE not in policy.tools
        assert AnalysisTool.LOAD_PLAYBOOK not in policy.tools
        assert LIMIT_RUN_CODE_MAX_FAILURES not in policy.limits

    def test_no_table_no_run_code(self) -> None:
        policy = AnalysisPolicy.tool_policy(detailed=True, in_process=False, sources=())

        assert AnalysisTool.RUN_CODE not in policy.tools


class TestGuideTable:
    def test_the_first_one_row_per_person_table_decides(self) -> None:
        tables = [
            ("source_1", _profile(AGE, SPEND)),
            ("source_2", _profile(ID_COLUMN, AREA)),
            ("source_3", _profile(ID_COLUMN)),
        ]

        assert AnalysisPolicy.guide_table(tables) == "source_2"

    def test_without_one_source_1_decides(self) -> None:
        tables = [("source_1", _profile(AREA)), ("source_2", _profile(AGE, SPEND))]

        assert AnalysisPolicy.guide_table(tables) == "source_1"


ORDER_ID = {"name": "注文ID", "type": "VARCHAR", "all_distinct": True}
BUYER_ID = {"name": "顧客ID", "type": "VARCHAR", "all_distinct": False}
BOUGHT_ON = {"name": "注文日", "type": "DATE", "all_distinct": False}
AMOUNT = {"name": "金額", "type": "BIGINT", "all_distinct": False}


class TestRowsAreNotPeople:
    def test_a_table_where_the_person_id_repeats_counts_rows(self) -> None:
        orders = _profile(ORDER_ID, BUYER_ID, BOUGHT_ON, AMOUNT)

        assert AnalysisPolicy.rows_are_not_people([("source_1", orders)]) is True

    def test_a_table_with_one_row_per_person_counts_people(self) -> None:
        customers = _profile(ID_COLUMN, AGE)

        assert AnalysisPolicy.rows_are_not_people([("source_1", customers)]) is False

    def test_a_people_table_next_to_a_history_counts_people(self) -> None:
        tables = [
            ("source_1", _profile(BUYER_ID, BOUGHT_ON, AMOUNT)),
            ("source_2", _profile({**ID_COLUMN, "name": "顧客ID"}, AGE)),
        ]

        assert AnalysisPolicy.rows_are_not_people(tables) is False

    def test_a_table_without_an_id_column_is_not_told_as_rows(self) -> None:
        assert (
            AnalysisPolicy.rows_are_not_people([("source_1", _profile(AGE))]) is False
        )

    def test_the_order_id_is_not_taken_for_the_person_in_the_guide(self) -> None:
        orders = _profile(ORDER_ID, BUYER_ID, BOUGHT_ON, AMOUNT)

        # no purpose: it would cluster on "people" when the order ID passed as one
        method = AnalysisPolicy.guide_method("", orders)

        assert method is AnalysisMethod.FILTER

    def test_the_guide_table_skips_a_history(self) -> None:
        tables = [
            ("source_1", _profile(ORDER_ID, BUYER_ID, BOUGHT_ON, AMOUNT)),
            ("source_2", _profile(ID_COLUMN, AGE)),
        ]

        assert AnalysisPolicy.guide_table(tables) == "source_2"


class TestIdNames:
    @pytest.mark.parametrize(
        "name",
        [
            "id",
            "ID",
            "customer_id",
            "customerId",
            "customerID",
            "顧客ID",
            "user id",
            "注文番号",
            "ＩＤ",
            "ID番号",
            "id_customer",
        ],
    )
    def test_names_that_say_id_are_ids(self, name: str) -> None:
        from src.managers.advanced_generation.analysis_policy import is_id_name

        assert is_id_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "residence",
            "paid",
            "video",
            "width",
            "side",
            "kids",
            "provider",
            "median",
            "identity_theft_flag_x",
            "Idea",
            "年齢",
            "地域",
            "",
        ],
    )
    def test_names_that_merely_contain_id_are_not(self, name: str) -> None:
        from src.managers.advanced_generation.analysis_policy import is_id_name

        assert is_id_name(name) is False

    def test_a_table_of_people_with_a_residence_column_counts_people(self) -> None:
        people = _profile(
            {"name": "社員ID", "type": "VARCHAR", "all_distinct": True},
            {"name": "residence", "type": "VARCHAR", "all_distinct": False},
            AGE,
        )

        assert AnalysisPolicy.rows_are_not_people([("source_1", people)]) is False
        assert AnalysisPolicy.guide_method("", people) is AnalysisMethod.CLUSTER


def _col(name: str, *, distinct: bool, type_: str = "VARCHAR") -> dict:
    return {"name": name, "type": type_, "all_distinct": distinct}


class TestReadTables:
    """The proposal of how each table reads (U6 addendum)."""

    def test_a_customer_table_with_a_repeating_store_id_is_people(self) -> None:
        profile = {
            "columns": [
                _col("customer_id", distinct=True),
                _col("store_id", distinct=False),
                _col("age", distinct=False, type_="BIGINT"),
            ]
        }
        (reading,) = AnalysisPolicy.read_tables([("source_1", "c.csv", profile)])

        assert reading.kind == "people"
        assert reading.person_id == "customer_id"
        assert "store_id" in reading.skip and "customer_id" in reading.skip
        assert not AnalysisPolicy.readings_count_rows([reading])

    def test_a_repeating_person_id_is_a_history(self) -> None:
        profile = {
            "columns": [
                _col("user_id", distinct=False),
                _col("利用日", distinct=False, type_="DATE"),
                _col("利用分", distinct=False, type_="BIGINT"),
            ]
        }
        (reading,) = AnalysisPolicy.read_tables([("source_1", "e.csv", profile)])

        assert reading.kind == "history" and reading.person_id == "user_id"
        assert AnalysisPolicy.readings_count_rows([reading])

    def test_an_order_table_points_at_the_customer_not_the_order(self) -> None:
        profile = {
            "columns": [
                _col("order_id", distinct=True),
                _col("customer_id", distinct=False),
                _col("金額", distinct=False, type_="BIGINT"),
            ]
        }
        (reading,) = AnalysisPolicy.read_tables([("source_1", "o.csv", profile)])

        assert reading.kind == "history" and reading.person_id == "customer_id"

    def test_codes_and_ids_are_not_traits(self) -> None:
        profile = {
            "columns": [
                _col("id", distinct=True),
                _col("郵便番号", distinct=False),
                _col("zip", distinct=False),
                _col("paid", distinct=False, type_="BOOLEAN"),
            ]
        }
        (reading,) = AnalysisPolicy.read_tables([("source_1", "x.csv", profile)])

        assert set(reading.skip) == {"id", "郵便番号", "zip"}

    def test_the_guide_reads_the_people_table(self) -> None:
        history = {"columns": [_col("user_id", distinct=False)]}
        people = {"columns": [_col("user_id", distinct=True)]}
        readings = AnalysisPolicy.read_tables(
            [("source_1", "e.csv", history), ("source_2", "u.csv", people)]
        )

        assert AnalysisPolicy.guide_alias(readings) == "source_2"
