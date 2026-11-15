"""DatasetAnalysisService（build_binding_tools / build_source_tools）単体テスト。"""

from datetime import datetime

import pytest

from src.models.dataset import Dataset, DatasetColumn
from src.services.dataset_analysis.query_backend import DuckDBQueryBackend
from src.services.dataset_analysis.service import DatasetAnalysisService

pytestmark = pytest.mark.unit


class RecordingBackend:
    def __init__(self):
        self.calls = []

    def execute(self, source, sql, params, *, timeout):
        self.calls.append({"source": source, "sql": sql, "params": params})
        return ["region"], [["east"]]


def _dataset(dataset_id, name="購買データ"):
    now = datetime.now()
    return Dataset(
        id=dataset_id,
        name=name,
        description="購買履歴",
        s3_path=f"s3://bucket/{dataset_id}.csv",
        columns=[
            DatasetColumn(name="user_id", data_type="string"),
            DatasetColumn(name="amount", data_type="integer"),
        ],
        row_count=500,
        created_at=now,
        updated_at=now,
    )


def _service():
    return DatasetAnalysisService(backend=RecordingBackend())


def _fn(tool):
    return tool.__wrapped__ if hasattr(tool, "__wrapped__") else tool


class TestBuildBindingTools:
    def test_single_binding_builds_tool_and_descriptor(self):
        svc = _service()
        tools, descriptors = svc.build_binding_tools(
            [{"dataset_id": "ds-1", "binding_keys": {"user_id": "U1"}}],
            [_dataset("ds-1")],
        )
        assert len(tools) == 1
        assert len(descriptors) == 1
        d = descriptors[0]
        assert d["alias"] == "dataset_1"
        assert d["name"] == "購買データ"
        assert d["row_count"] == 500
        # columns は {name, data_type, description} の dict リスト
        col_names = [c["name"] for c in d["columns"]]
        assert "amount" in col_names
        assert any(c["data_type"] == "integer" for c in d["columns"])
        # descriptor は backend_path / forced_filter 値を含まない
        assert "s3_path" not in d and "backend_path" not in d
        assert "binding_keys" not in d and "U1" not in str(d)

    def test_two_bindings_single_tool_both_selectable(self):
        svc = _service()
        tools, descriptors = svc.build_binding_tools(
            [
                {"dataset_id": "ds-1", "binding_keys": {"user_id": "U1"}},
                {"dataset_id": "ds-2", "binding_keys": {}},
            ],
            [_dataset("ds-1"), _dataset("ds-2", name="行動ログ")],
        )
        # 単一ツール（analyze_dataset 名は 1 個）
        assert len(tools) == 1
        assert tools[0].tool_spec["name"] == "analyze_dataset"
        aliases = {d["alias"] for d in descriptors}
        assert aliases == {"dataset_1", "dataset_2"}
        # 両方 alias で参照可能
        _fn(tools[0])(dataset_id="dataset_1")
        _fn(tools[0])(dataset_id="dataset_2")

    def test_empty_binding_keys_allowed(self):
        svc = _service()
        tools, descriptors = svc.build_binding_tools(
            [{"dataset_id": "ds-1", "binding_keys": {}}],
            [_dataset("ds-1")],
        )
        assert len(tools) == 1

    def test_no_matching_dataset_returns_empty(self):
        svc = _service()
        tools, descriptors = svc.build_binding_tools(
            [{"dataset_id": "missing", "binding_keys": {}}],
            [_dataset("ds-1")],
        )
        assert tools == [] and descriptors == []

    def test_forced_filter_applied_in_query(self):
        backend = RecordingBackend()
        svc = DatasetAnalysisService(backend=backend)
        tools, _ = svc.build_binding_tools(
            [{"dataset_id": "ds-1", "binding_keys": {"user_id": "U1"}}],
            [_dataset("ds-1")],
        )
        _fn(tools[0])(dataset_id="dataset_1")
        assert '"user_id" = $1' in backend.calls[0]["sql"]
        assert backend.calls[0]["params"] == ["U1"]


class TestBuildSourceTools:
    def test_multiple_aliases_single_tool(self):
        backend = RecordingBackend()
        svc = DatasetAnalysisService(backend=backend)
        tools = svc.build_source_tools(
            [
                {"alias": "source_1", "path": "/tmp/a.csv", "columns": ["c"]},
                {"alias": "source_2", "path": "/tmp/b.csv", "columns": ["c"]},
            ]
        )
        assert len(tools) == 1
        _fn(tools[0])(dataset_id="source_1", select_columns=["c"])
        _fn(tools[0])(dataset_id="source_2", select_columns=["c"])
        assert backend.calls[0]["source"] == "/tmp/a.csv"
        assert backend.calls[1]["source"] == "/tmp/b.csv"

    def test_alias_not_reassigned_by_service(self):
        # Service は Manager が採番した別名を保持するだけ（再採番しない）。
        svc = _service()
        tools = svc.build_source_tools(
            [{"alias": "source_7", "path": "/tmp/x.csv", "columns": ["c"]}]
        )
        out = _fn(tools[0])(dataset_id="source_7", select_columns=["c"])
        assert "利用できません" not in out

    def test_duplicate_alias_rejected(self):
        svc = _service()
        with pytest.raises(ValueError):
            svc.build_source_tools(
                [
                    {"alias": "source_1", "path": "/tmp/a.csv", "columns": ["c"]},
                    {"alias": "source_1", "path": "/tmp/b.csv", "columns": ["c"]},
                ]
            )

    def test_empty_returns_no_tools(self):
        assert _service().build_source_tools([]) == []


class TestConcurrencyWiring:
    def test_max_concurrent_queries_reaches_default_backend(self):
        # 明示 backend を渡さない経路で、設定値が DuckDB backend の semaphore
        # 上限（_value）へ伝播することを確認する。
        svc = DatasetAnalysisService(max_concurrent_queries=7)
        assert isinstance(svc.backend, DuckDBQueryBackend)
        assert svc.backend._sem._value == 7

    def test_default_is_four(self):
        svc = DatasetAnalysisService()
        assert isinstance(svc.backend, DuckDBQueryBackend)
        assert svc.backend._sem._value == 4

    def test_injected_backend_ignores_max_concurrent(self):
        # backend 注入時は注入側の設定を尊重し、引数で上書きしない。
        backend = RecordingBackend()
        svc = DatasetAnalysisService(backend=backend, max_concurrent_queries=7)
        assert svc.backend is backend
