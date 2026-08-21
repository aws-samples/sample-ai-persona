"""analyze_dataset tool closure + 入力契約(schema) 単体テスト。"""

import inspect

import pytest

from src.services.dataset_analysis.dataset_tools import (
    ResolvedDataset,
    ToolLimits,
    create_analyze_dataset_tool,
)
from src.services.dataset_analysis.query_backend import DatasetQueryTimeout

pytestmark = pytest.mark.unit


class FakeBackend:
    """SQL/params を記録し、固定結果を返す backend。"""

    def __init__(self, columns=None, rows=None, raise_exc=None):
        self.columns = columns or ["region", "m0"]
        self.rows = rows if rows is not None else [["east", 10]]
        self.calls = []
        self.raise_exc = raise_exc

    def execute(self, source, sql, params, *, timeout):
        self.calls.append({"source": source, "sql": sql, "params": params})
        if self.raise_exc:
            raise self.raise_exc
        return self.columns, self.rows


def _fn(tool):
    """@tool でラップされた元関数を取り出す。"""
    return tool.__wrapped__ if hasattr(tool, "__wrapped__") else tool


def _make(resolved, backend, limits=None):
    return create_analyze_dataset_tool(resolved, backend, limits)


def _rd(alias="dataset_1", path="local://datasets/x.csv", columns=None, forced=None):
    return ResolvedDataset(
        alias=alias,
        backend_path=path,
        columns=columns or ["region", "amount", "user_id"],
        forced_filter=forced,
    )


class TestToolContract:
    def test_signature_has_no_sql_path_or_creds_params(self):
        tool = _make([_rd()], FakeBackend())
        params = set(inspect.signature(_fn(tool)).parameters)
        assert params == {
            "dataset_id",
            "select_columns",
            "filters",
            "group_by",
            "metrics",
            "order_by",
            "limit",
        }
        # SQL・パス・認証情報を受け取る引数が無いこと
        for forbidden in ("sql", "query", "s3_path", "path", "credentials", "secret"):
            assert forbidden not in params

    def test_schema_has_literals_and_optional_metric_column(self):
        tool = _make([_rd()], FakeBackend())
        schema = tool.tool_spec["inputSchema"]["json"]
        defs = schema["$defs"]
        assert defs["Filter"]["properties"]["operator"]["enum"] == [
            "eq",
            "ne",
            "gt",
            "gte",
            "lt",
            "lte",
            "in",
            "contains",
        ]
        assert defs["Metric"]["properties"]["func"]["enum"] == [
            "count",
            "sum",
            "avg",
            "min",
            "max",
        ]
        assert defs["OrderBy"]["properties"]["direction"]["enum"] == ["asc", "desc"]
        # Metric.column は optional（required に含まれない）
        assert "column" not in defs["Metric"].get("required", [])
        assert defs["Metric"]["required"] == ["func"]


class TestToolBehavior:
    def test_unknown_dataset_id_returns_safe_string(self):
        tool = _make([_rd(alias="dataset_1")], FakeBackend())
        out = _fn(tool)(dataset_id="nope")
        assert "利用できません" in out
        assert "dataset_1" in out

    def test_forced_filter_is_applied_and_not_removable(self):
        backend = FakeBackend()
        tool = _make([_rd(forced={"user_id": "U123"})], backend)
        _fn(tool)(
            dataset_id="dataset_1",
            filters=[{"column": "user_id", "operator": "eq", "value": "OTHER"}],
        )
        sql = backend.calls[0]["sql"]
        params = backend.calls[0]["params"]
        assert sql.count('"user_id"') == 2  # LLM filter + forced filter
        assert "U123" in params and "OTHER" in params

    def test_invalid_query_returns_safe_string(self):
        tool = _make([_rd()], FakeBackend())
        out = _fn(tool)(dataset_id="dataset_1", select_columns=["ssn"])
        assert "不正" in out

    def test_timeout_returns_safe_string(self):
        backend = FakeBackend(raise_exc=DatasetQueryTimeout("boom"))
        tool = _make([_rd()], backend)
        out = _fn(tool)(dataset_id="dataset_1")
        assert "タイムアウト" in out

    def test_backend_exception_returns_safe_string(self):
        backend = FakeBackend(raise_exc=RuntimeError("kaboom"))
        tool = _make([_rd()], backend)
        out = _fn(tool)(dataset_id="dataset_1")
        assert "エラー" in out
        assert "kaboom" not in out  # 内部例外文は露出しない

    def test_empty_result_message(self):
        backend = FakeBackend(rows=[])
        tool = _make([_rd()], backend)
        out = _fn(tool)(dataset_id="dataset_1")
        assert "一致するデータはありません" in out

    def test_result_char_cap(self):
        rows = [[f"region{i}", i] for i in range(1000)]
        backend = FakeBackend(rows=rows)
        tool = _make([_rd()], backend, ToolLimits(max_result_chars=200))
        out = _fn(tool)(dataset_id="dataset_1")
        assert len(out) <= 200 + len("\n...(結果を省略しました)")
        assert "省略" in out

    def test_metric_label_rendered_in_output(self):
        backend = FakeBackend(columns=["region", "m0"], rows=[["east", 42]])
        tool = _make([_rd()], backend)
        out = _fn(tool)(
            dataset_id="dataset_1",
            metrics=[{"func": "sum", "column": "amount"}],
            group_by=["region"],
        )
        assert "sum(amount)" in out
        assert "m0" not in out  # 内部別名は露出しない


class TestMultiDatasetSingleTool:
    def test_two_datasets_one_tool_selectable_by_alias(self):
        backend = FakeBackend()
        r1 = _rd(alias="dataset_1", path="local://datasets/a.csv")
        r2 = _rd(alias="dataset_2", path="local://datasets/b.csv")
        tool = _make([r1, r2], backend)
        # ツールは 1 つ（名前 analyze_dataset）
        assert tool.tool_spec["name"] == "analyze_dataset"
        _fn(tool)(dataset_id="dataset_1")
        _fn(tool)(dataset_id="dataset_2")
        assert backend.calls[0]["source"] == "local://datasets/a.csv"
        assert backend.calls[1]["source"] == "local://datasets/b.csv"

    def test_two_closures_same_alias_do_not_mix(self):
        # 並行する 2 生成が同名 alias を別 path に割り当てても混線しない。
        b1, b2 = FakeBackend(), FakeBackend()
        t1 = _make([_rd(alias="source_1", path="local://datasets/gen1.csv")], b1)
        t2 = _make([_rd(alias="source_1", path="local://datasets/gen2.csv")], b2)
        _fn(t1)(dataset_id="source_1")
        _fn(t2)(dataset_id="source_1")
        assert b1.calls[0]["source"] == "local://datasets/gen1.csv"
        assert b2.calls[0]["source"] == "local://datasets/gen2.csv"
