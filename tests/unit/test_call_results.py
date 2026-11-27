"""What the analysis record shows of each step: counted aggregates, never rows."""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from src.managers.advanced_generation.call_results import (
    MAX_SHOWN_FEATURES,
    MIN_SHOWN_SIZE,
    call_result,
    table_summary,
)
from src.models.analysis import EmittedResult, PrepareReport, ReportColumn, Segment
from src.models.source_file import SourceEncoding
from src.services.dataset_analysis.analysis_tool_service import ToolCall

pytestmark = pytest.mark.unit


def _call(
    tool: str,
    *,
    ok: bool = True,
    error_code: Optional[str] = None,
    table: Optional[str] = "source_1",
    check_counts: Optional[dict[str, Any]] = None,
    call_id: str = "tc-1",
) -> ToolCall:
    return ToolCall(
        call_id,
        tool,
        ok,
        error_code,
        0.1,
        10,
        table=table,
        check_counts=check_counts,
    )


def _feature(
    name: str, value: float, baseline: float, spread: float = 1.0, kind: str = "mean"
) -> dict:
    return {
        "name": name,
        "kind": kind,
        "value": value,
        "baseline": baseline,
        "spread": spread,
    }


def _segment(label: str, **spec: Any) -> Segment:
    kind = "cluster" if "cluster_index" in spec else "derived"
    return Segment(
        id=f"seg-{label}",
        label=label,
        definition={"kind": kind, "spec": spec},
        size=100,
        share=0.5,
        features=(),
        evidence_ids=("ev-1",),
    )


_CLUSTERS = {
    "k": 3,
    "population": 1000,
    "clusters": [
        {
            "index": 0,
            "size": 600,
            "share": 0.6,
            "features": [
                _feature("age", 50.0, 40.0, spread=10.0),  # 1.0 spreads
                # 0.1 spreads: hardly different from the whole, no trait
                _feature("amount", 900.0, 1000.0, spread=1000.0),
                _feature("visits", 9.0, 5.0, spread=2.0),  # 2.0 spreads
                _feature("points", 2.0, 1.0, spread=2.0),  # 0.5 spreads
                _feature("rank", 3.0, 1.0, spread=5.0),  # 0.4 spreads
            ],
        },
        {"index": 1, "size": 395, "share": 0.395, "features": []},
        # a group too small to show: its numbers could point at a person
        {"index": 2, "size": MIN_SHOWN_SIZE - 1, "share": 0.005, "features": []},
    ],
}


class TestGroups:
    def test_a_cluster_shows_each_group_with_its_biggest_differences(self) -> None:
        segments = [_segment("よく来る層", tool_call_id="tc-1", cluster_index=0)]

        result = call_result(
            _call("cluster_segments"),
            output=_CLUSTERS,
            emitted=None,
            segments=segments,
            history_tables={},
        )

        assert result is not None
        assert result["population"] == 1000 and result["unit"] == "people"
        first, second, hidden = result["groups"]
        # the name the analysis gave the group, else its number
        assert first["label"] == "よく来る層" and second["label"] == "グループ 2"
        assert first["size"] == 600 and first["share"] == 0.6
        assert len(first["features"]) == MAX_SHOWN_FEATURES
        assert [f["name"] for f in first["features"]] == ["visits", "age", "points"]
        assert set(first["features"][0]) == {"name", "kind", "value", "baseline"}
        assert hidden == {"hidden": True}

    def test_the_rows_of_a_history_are_counted_as_rows(self) -> None:
        result = call_result(
            _call("cluster_segments"),
            output=_CLUSTERS,
            emitted=None,
            segments=[],
            history_tables={"source_1": "会員ID"},
        )

        assert result is not None and result["unit"] == "rows"

    def test_profile_segments_layers_are_groups(self) -> None:
        output = {
            "population": 50,
            "layers": [
                {"key": "gold", "size": 30, "share": 0.6, "features": []},
                {"key": "basic", "size": 20, "share": 0.4, "features": []},
            ],
        }

        result = call_result(
            _call("profile_segments"),
            output=output,
            emitted=None,
            segments=[],
            history_tables={},
        )

        assert result is not None
        assert [g["label"] for g in result["groups"]] == ["gold", "basic"]

    def test_a_small_group_hides_its_label_too(self) -> None:
        # a layer by a column of names would carry the person in its key
        output = {
            "population": 50,
            "layers": [{"key": "山田 太郎", "size": 1, "share": 0.02, "features": []}],
        }

        result = call_result(
            _call("profile_segments"),
            output=output,
            emitted=None,
            segments=[],
            history_tables={},
        )

        assert result is not None
        assert result["groups"] == [{"hidden": True}]
        assert "山田" not in json.dumps(result, ensure_ascii=False)


class TestDetailedAnalysis:
    def test_an_accepted_result_shows_the_recounted_people(self) -> None:
        emitted = EmittedResult(
            population=7933,
            population_source="people:source_1",
            segments=(
                {
                    "segment_key": "high",
                    "size": 2221,
                    "features": [_feature("最後からの日数", 100.6, 44.3)],
                },
                {"segment_key": "low", "size": 5712, "features": []},
            ),
            source_rows=1_471_388,
            rows_without_id=87_568,
            largest_id_rows=900,
        )
        segments = [_segment("離反リスク高", tool_call_id="tc-1", segment_key="high")]

        result = call_result(
            _call("run_code"),
            output=emitted.recounted(),
            emitted=emitted,
            segments=segments,
            history_tables={"source_1": "会員ID"},
        )

        assert result is not None
        assert result["population"] == 7933 and result["unit"] == "people"
        assert result["history"] == {
            "id_column": "会員ID",
            "source_rows": 1_471_388,
            "rows_without_id": 87_568,
        }
        high, low = result["groups"]
        assert high["label"] == "離反リスク高" and high["size"] == 2221
        assert high["share"] == round(2221 / 7933, 4)
        assert high["features"][0]["name"] == "最後からの日数"
        assert low["label"] == "low"

    def test_a_refused_result_shows_its_reason_and_counts(self) -> None:
        call = _call(
            "run_code",
            ok=False,
            error_code="DUPLICATE_IDS",
            check_counts={"side": "result", "rows": 13402, "distinct_ids": 7933},
        )

        result = call_result(
            call, output=None, emitted=None, segments=[], history_tables={}
        )

        assert result == {
            "failure": "DUPLICATE_IDS",
            "counts": {"side": "result", "rows": 13402, "distinct_ids": 7933},
        }

    def test_a_failure_without_counts_shows_its_reason_only(self) -> None:
        call = _call("run_code", ok=False, error_code="RUN_CODE_FAILED")

        result = call_result(
            call, output=None, emitted=None, segments=[], history_tables={}
        )

        assert result == {"failure": "RUN_CODE_FAILED"}


class TestOtherTools:
    def test_a_query_keeps_how_many_rows_it_returned_not_the_rows(self) -> None:
        output = {"columns": ["name", "n"], "rows": [["山田 太郎", 3], ["鈴木", 2]]}

        result = call_result(
            _call("analyze_dataset"),
            output=output,
            emitted=None,
            segments=[],
            history_tables={},
        )

        assert result == {"rows_returned": 2}

    def test_a_profile_keeps_counts_and_types_not_values(self) -> None:
        output = {
            "rows": 100,
            "columns": [
                {
                    "name": "会員ID",
                    "type": "VARCHAR",
                    "non_null": 90,
                    "distinct": 80,
                    "top_values": [{"value": "M0007857", "count": 5}],
                },
                {
                    "name": "金額",
                    "type": "BIGINT",
                    "non_null": 100,
                    "distinct": 50,
                    "min": 1,
                    "max": 98000,
                    "mean": 300.0,
                },
            ],
        }

        result = call_result(
            _call("profile_dataset"),
            output=output,
            emitted=None,
            segments=[],
            history_tables={},
        )

        assert result == {
            "rows": 100,
            "columns": 2,
            "types": {"text": 1, "number": 1},
            "missing": [{"name": "会員ID", "count": 10}],
        }
        text = json.dumps(result, ensure_ascii=False)
        assert "M0007857" not in text and "98000" not in text

    @pytest.mark.parametrize("tool", ["load_playbook", "unknown"])
    def test_a_step_with_nothing_counted_has_no_result(self, tool: str) -> None:
        result = call_result(
            _call(tool),
            output={"text": "..."},
            emitted=None,
            segments=[],
            history_tables={},
        )

        assert result is None

    def test_an_output_of_an_unexpected_shape_has_no_result(self) -> None:
        result = call_result(
            _call("cluster_segments"),
            output={"clusters": "x"},
            emitted=None,
            segments=[],
            history_tables={},
        )

        assert result is None


class TestTableSummary:
    def test_the_rows_columns_and_missing_values_of_a_table(self) -> None:
        report = PrepareReport(
            source_id="f1",
            rows_read=1000,
            rows_excluded=3,
            columns=(ReportColumn("会員ID", "VARCHAR"), ReportColumn("金額", "BIGINT")),
            encoding=SourceEncoding.UTF8,
            prepare_version="v1",
        )
        profile = {
            "rows": 1000,
            "columns": [
                {
                    "name": "会員ID",
                    "type": "VARCHAR",
                    "non_null": 940,
                    "top_values": [],
                },
                {"name": "金額", "type": "BIGINT", "non_null": 1000},
                {"name": "購入日時", "type": "TIMESTAMP", "non_null": 1000},
            ],
        }

        summary = table_summary("pos.csv", report, profile)

        assert summary == {
            "name": "pos.csv",
            "rows_read": 1000,
            "rows_excluded": 3,
            "columns": 3,
            "types": {"text": 1, "number": 1, "date": 1},
            "missing": [{"name": "会員ID", "count": 60}],
        }
