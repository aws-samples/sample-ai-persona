"""sql_builder（純粋関数）単体テスト。"""

import pytest

from src.services.dataset_analysis.sql_builder import (
    SqlBuildError,
    build_analyze_query,
)

pytestmark = pytest.mark.unit

COLUMNS = [
    "user_id",
    "amount",
    "region",
    "product name",
    "order-type",
    "select",
    "metric_1",
]


def _build(**kwargs):
    params = {
        "view_name": "dataset",
        "allowed_columns": COLUMNS,
        "default_limit": 100,
        "max_limit": 200,
    }
    params.update(kwargs)
    return build_analyze_query(**params)


class TestAllowlist:
    def test_unknown_select_column_rejected(self):
        with pytest.raises(SqlBuildError):
            _build(select_columns=["ssn"])

    def test_unknown_filter_column_rejected(self):
        with pytest.raises(SqlBuildError):
            _build(filters=[{"column": "ssn", "operator": "eq", "value": 1}])

    def test_unknown_operator_rejected(self):
        with pytest.raises(SqlBuildError):
            _build(filters=[{"column": "amount", "operator": "regex", "value": 1}])

    def test_unknown_group_by_rejected(self):
        with pytest.raises(SqlBuildError):
            _build(metrics=[{"func": "count"}], group_by=["ssn"])


class TestQuoting:
    def test_whitespace_and_hyphen_columns_quoted(self):
        sql, _, _ = _build(select_columns=["product name", "order-type"])
        assert '"product name"' in sql
        assert '"order-type"' in sql

    def test_reserved_word_column_quoted(self):
        sql, _, _ = _build(select_columns=["select"])
        assert '"select"' in sql

    def test_double_quote_in_column_escaped(self):
        sql, _, _ = build_analyze_query(
            view_name="dataset",
            allowed_columns=['weird"col'],
            select_columns=['weird"col'],
        )
        assert '"weird""col"' in sql


class TestFilters:
    def test_eq_uses_positional_param(self):
        sql, params, _ = _build(
            filters=[{"column": "amount", "operator": "gte", "value": 10}]
        )
        assert '"amount" >= $1' in sql
        assert params == [10]

    def test_in_requires_list(self):
        with pytest.raises(SqlBuildError):
            _build(filters=[{"column": "region", "operator": "in", "value": "east"}])

    def test_in_generates_placeholders(self):
        sql, params, _ = _build(
            filters=[{"column": "region", "operator": "in", "value": ["e", "w"]}]
        )
        assert '"region" IN ($1, $2)' in sql
        assert params == ["e", "w"]

    def test_contains_wraps_value(self):
        sql, params, _ = _build(
            filters=[{"column": "region", "operator": "contains", "value": "as"}]
        )
        assert "LIKE $1" in sql
        assert params == ["%as%"]

    def test_injection_string_is_parameterized_not_inlined(self):
        malicious = "'; DROP TABLE dataset; --"
        sql, params, _ = _build(
            filters=[{"column": "region", "operator": "eq", "value": malicious}]
        )
        assert malicious not in sql  # 値は SQL 本文に現れない
        assert params == [malicious]


class TestForcedFilter:
    def test_forced_filter_always_appended(self):
        sql, params, _ = _build(forced_filter={"user_id": "U123"})
        assert '"user_id" = $1' in sql
        assert params == ["U123"]

    def test_forced_filter_cannot_be_removed_by_llm_filter(self):
        # LLM が同じ列に別条件を出しても forced filter は別枠で AND 付与される。
        sql, params, _ = _build(
            filters=[{"column": "user_id", "operator": "eq", "value": "OTHER"}],
            forced_filter={"user_id": "U123"},
        )
        assert sql.count('"user_id"') == 2
        assert "OTHER" not in sql and "U123" not in sql
        assert params == ["OTHER", "U123"]


class TestLimit:
    def test_limit_clamped_to_max(self):
        sql, _, _ = _build(limit=99999)
        assert "LIMIT 200" in sql

    def test_default_limit_applied(self):
        sql, _, _ = _build()
        assert "LIMIT 100" in sql

    def test_nonpositive_limit_uses_default(self):
        sql, _, _ = _build(limit=0)
        assert "LIMIT 100" in sql


class TestModes:
    def test_raw_rows_no_metrics_no_group_by(self):
        sql, _, label_map = _build()
        assert sql.startswith("SELECT * FROM dataset")
        assert label_map == {}

    def test_select_columns_subset(self):
        sql, _, _ = _build(select_columns=["amount", "region"])
        assert 'SELECT "amount", "region"' in sql

    def test_group_by_without_metrics_rejected(self):
        with pytest.raises(SqlBuildError):
            _build(group_by=["region"])

    def test_aggregate_with_select_columns_rejected(self):
        with pytest.raises(SqlBuildError):
            _build(metrics=[{"func": "count"}], select_columns=["amount"])

    def test_sum_requires_column(self):
        with pytest.raises(SqlBuildError):
            _build(metrics=[{"func": "sum"}])

    def test_count_star_vs_count_column(self):
        sql_star, _, _ = _build(metrics=[{"func": "count"}])
        assert "count(*)" in sql_star
        sql_col, _, labels = _build(metrics=[{"func": "count", "column": "amount"}])
        assert 'count("amount")' in sql_col
        assert labels["m0"] == "count(amount)"

    def test_global_aggregate_no_group_by(self):
        sql, _, _ = _build(metrics=[{"func": "sum", "column": "amount"}])
        assert "GROUP BY" not in sql
        assert 'sum("amount") AS m0' in sql

    def test_group_by_aggregate(self):
        sql, _, _ = _build(
            metrics=[{"func": "sum", "column": "amount"}], group_by=["region"]
        )
        assert 'GROUP BY "region"' in sql


class TestOrderBy:
    def test_column_and_metric_index_mutually_exclusive(self):
        with pytest.raises(SqlBuildError):
            _build(
                metrics=[{"func": "count"}],
                order_by=[{"column": "region", "metric_index": 0, "direction": "asc"}],
            )

    def test_order_by_requires_one_of_column_or_index(self):
        with pytest.raises(SqlBuildError):
            _build(order_by=[{"direction": "asc"}])

    def test_metric_index_orders_by_internal_alias(self):
        sql, _, _ = _build(
            metrics=[{"func": "sum", "column": "amount"}],
            group_by=["region"],
            order_by=[{"metric_index": 0, "direction": "desc"}],
        )
        assert "ORDER BY m0 DESC" in sql

    def test_metric_index_out_of_range_rejected(self):
        with pytest.raises(SqlBuildError):
            _build(
                metrics=[{"func": "count"}],
                order_by=[{"metric_index": 5, "direction": "asc"}],
            )

    def test_real_column_named_metric_1_does_not_collide(self):
        # 実カラム "metric_1" があっても内部別名は m0 なので衝突しない（回帰）。
        sql, _, labels = _build(
            select_columns=["metric_1"],
        )
        assert '"metric_1"' in sql
        # 集計モードで metric_1 という列名を group_by に使っても内部別名 m0 と別物。
        sql2, _, _ = _build(
            metrics=[{"func": "count", "column": "metric_1"}],
            group_by=["region"],
            order_by=[{"metric_index": 0, "direction": "asc"}],
        )
        assert "ORDER BY m0 ASC" in sql2
        assert 'count("metric_1")' in sql2

    def test_aggregate_order_by_column_must_be_in_group_by(self):
        with pytest.raises(SqlBuildError):
            _build(
                metrics=[{"func": "count"}],
                group_by=["region"],
                order_by=[{"column": "amount", "direction": "asc"}],
            )
