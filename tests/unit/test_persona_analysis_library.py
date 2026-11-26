"""PersonaAnalysisLibrary (``sandbox/persona_analysis.py``) run in process.

The library runs inside a Code Interpreter session in production, so the app
never imports it. These tests load it from its file path and call the same
functions in this process. They cover the preparation, the profile, the clustering,
the query used by ``analyze_dataset`` and the download that must not print the URL.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import random
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

import pytest

pytestmark = pytest.mark.unit

_LIBRARY_PATH = (
    Path(__file__).resolve().parent.parent.parent / "sandbox" / "persona_analysis.py"
)


def _load_library() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "persona_analysis_under_test", _LIBRARY_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lib() -> ModuleType:
    return _load_library()


def _put_source(workdir: Path, source_id: str, data: bytes) -> None:
    sources = workdir / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    (sources / source_id).write_bytes(data)


def _metadata(encoding: str = "utf-8", delimiter: str = ",") -> dict[str, Any]:
    fmt = "tsv" if delimiter == "\t" else "csv"
    return {"format": fmt, "encoding": encoding, "delimiter": delimiter}


def _call(lib: ModuleType, workdir: Path, function: str, **args: Any) -> Any:
    return lib.dispatch(function, args, str(workdir))


def _prepare(
    lib: ModuleType,
    workdir: Path,
    data: bytes,
    *,
    source_id: str = "src1",
    encoding: str = "utf-8",
    delimiter: str = ",",
    **extra: Any,
) -> dict[str, Any]:
    _put_source(workdir, source_id, data)
    return _call(
        lib,
        workdir,
        "prepare",
        source_id=source_id,
        metadata=_metadata(encoding, delimiter),
        progress_id="p1",
        **extra,
    )


_SMALL_CSV = "id,age,gender\n1,30,男性\n2,41,女性\n3,25,女性\n".encode("utf-8")


class TestPrepare:
    def test_writes_parquet_and_reports_rows_and_columns(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        result = _prepare(lib, tmp_path, _SMALL_CSV)

        assert result["ok"] is True
        report = result["value"]
        assert report["source_id"] == "src1"
        assert report["rows_read"] == 3
        assert report["rows_excluded"] == 0
        assert [c["name"] for c in report["columns"]] == ["id", "age", "gender"]
        assert report["encoding"] == "utf-8"
        assert report["encoding_retried"] is False
        assert report["prepare_version"] == lib.PREPARE_VERSION
        assert (tmp_path / "prepared" / "src1.parquet").is_file()

    def test_writes_the_progress_file(self, lib: ModuleType, tmp_path: Path) -> None:
        _prepare(lib, tmp_path, _SMALL_CSV)

        progress = json.loads(
            (tmp_path / "progress" / "p1.json").read_text(encoding="utf-8")
        )
        assert progress["phase"] == "finished"
        assert progress["rows"] == 3

    def test_reads_tsv(self, lib: ModuleType, tmp_path: Path) -> None:
        data = "id\tcity\n1\t東京\n2\t大阪\n".encode("utf-8")

        result = _prepare(lib, tmp_path, data, delimiter="\t")

        assert result["value"]["rows_read"] == 2
        assert [c["name"] for c in result["value"]["columns"]] == ["id", "city"]

    def test_undetermined_falls_back_to_cp932(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        # The head is ASCII only (undetermined); a later line is CP932.
        lines = ["id,name"] + [f"{i},abc" for i in range(50)] + ["51,髙橋①"]
        data = ("\n".join(lines) + "\n").encode("cp932")

        result = _prepare(lib, tmp_path, data, encoding="undetermined")

        report = result["value"]
        assert report["encoding"] == "cp932"
        assert report["encoding_retried"] is True
        assert report["rows_read"] == 51

    def test_lines_that_do_not_decode_are_excluded(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        good = "".join(f"{i},abc\n" for i in range(3, 203)).encode("ascii")
        data = "id,name\n1,佐藤\n".encode("cp932") + b"2,\x81\n" + good

        result = _prepare(lib, tmp_path, data, encoding="cp932")

        report = result["value"]
        assert report["rows_read"] == 201
        assert report["rows_excluded"] == 1
        assert report["excluded_reasons"]["counts"] == {"decode_error": 1}

    def test_rows_with_a_wrong_column_count_are_excluded(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        good = "".join(f"{i},30\n" for i in range(3, 203)).encode("ascii")
        data = b"id,age\n1,30\n2,40,extra\n" + good

        result = _prepare(lib, tmp_path, data)

        report = result["value"]
        assert report["rows_read"] == 201
        assert report["rows_excluded"] == 1
        assert sum(report["excluded_reasons"]["counts"].values()) == 1

    def test_same_file_gives_the_same_report(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        first = _prepare(lib, tmp_path / "a", _SMALL_CSV)
        second = _prepare(lib, tmp_path / "b", _SMALL_CSV)

        assert first == second

    def test_header_only_is_unreadable(self, lib: ModuleType, tmp_path: Path) -> None:
        result = _prepare(lib, tmp_path, b"id,age\n")

        assert result["ok"] is False
        assert result["error"]["code"] == "SOURCE_UNREADABLE"

    def test_a_source_id_with_a_path_is_rejected(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        result = _call(
            lib,
            tmp_path,
            "prepare",
            source_id="../x",
            metadata=_metadata(),
            progress_id="p1",
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_ARGUMENT"


def _csv_with_bad_lines(total: int, bad_lines: set[int], bad: bytes) -> bytes:
    """A file of ``total`` data rows; the given 1-based file lines are ``bad``."""
    out = [b"id,age"]
    for line in range(2, total + 2):
        out.append(bad if line in bad_lines else f"{line},30".encode("ascii"))
    return b"\n".join(out) + b"\n"


class TestPrepareBadRowBoundary:
    """Stop above 1% of the rows seen, with line numbers."""

    def test_three_bad_rows_in_a_hundred_thousand_go_on(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        data = _csv_with_bad_lines(100_000, {10, 5000, 90000}, b"1,2,3")

        result = _prepare(lib, tmp_path, data)

        report = result["value"]
        assert report["rows_read"] == 99_997
        assert report["rows_excluded"] == 3
        assert report["excluded_reasons"]["examples"]

    def test_exactly_one_percent_goes_on(self, lib: ModuleType, tmp_path: Path) -> None:
        bad = set(range(2, 202, 2))  # 100 lines
        data = _csv_with_bad_lines(10_000, bad, b"9,\x81")

        result = _prepare(lib, tmp_path, data, encoding="cp932")

        assert result["ok"] is True
        assert result["value"]["rows_read"] == 9_900
        assert result["value"]["rows_excluded"] == 100

    def test_one_more_than_one_percent_stops(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        bad = set(range(2, 204, 2))  # 101 lines
        data = _csv_with_bad_lines(10_000, bad, b"9,\x81")

        result = _prepare(lib, tmp_path, data, encoding="cp932")

        assert result["ok"] is False
        assert result["error"]["code"] == "TOO_MANY_BAD_ROWS"
        context = result["error"]["context"]
        assert context["rows_read"] == 9_899
        assert context["rows_excluded"] == 101

    def test_the_limit_is_an_argument(self, lib: ModuleType, tmp_path: Path) -> None:
        bad = set(range(2, 1002, 2))  # 500 of 1000 rows
        data = _csv_with_bad_lines(1000, bad, b"1,2,3")

        stopped = _prepare(lib, tmp_path / "a", data, max_excluded_percent=49)
        passed = _prepare(lib, tmp_path / "b", data, max_excluded_percent=50)

        assert stopped["error"]["code"] == "TOO_MANY_BAD_ROWS"
        assert passed["ok"] is True

    def test_a_bad_limit_argument_is_refused(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        result = _prepare(lib, tmp_path, _SMALL_CSV, max_excluded_percent="1")

        assert result["error"]["code"] == "INVALID_ARGUMENT"

    def test_line_numbers_are_the_original_file_lines(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        # Undecodable lines are blanked, not dropped: numbers after them hold.
        data = _csv_with_bad_lines(40, {3, 7}, b"9,\x81")
        lines = data.split(b"\n")
        lines[20] = b"1,2,3"  # file line 21: wrong column count
        lines[30] = b"1,2,3"  # file line 31

        result = _prepare(
            lib, tmp_path, b"\n".join(lines), encoding="cp932", max_excluded_percent=50
        )

        examples = result["value"]["excluded_reasons"]["examples"]
        assert examples["decode_error"] == [3, 7]
        other = [v for k, v in examples.items() if k != "decode_error"]
        assert other == [[21, 31]]

    def test_examples_are_capped_by_reason_and_in_total(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        data = _csv_with_bad_lines(200, {2, 4, 6, 8, 10, 12}, b"9,\x81")
        lines = data.split(b"\n")
        for n in (21, 23, 25, 27):
            lines[n - 1] = b"1,2,3"

        result = _prepare(
            lib, tmp_path, b"\n".join(lines), encoding="cp932", max_excluded_percent=50
        )

        examples = result["value"]["excluded_reasons"]["examples"]
        assert all(len(v) <= 3 for v in examples.values())
        flat = [n for v in examples.values() for n in v]
        assert len(flat) == 5
        assert all(isinstance(n, int) and not isinstance(n, bool) for n in flat)

    def test_a_stop_carries_row_examples_as_integers_only(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        data = _csv_with_bad_lines(100, set(range(2, 22)), b"1,2,3")

        result = _prepare(lib, tmp_path, data)

        context = result["error"]["context"]
        assert context["row_examples"] == [2, 3, 4]
        assert set(context) <= {"rows_read", "rows_excluded", "row_examples"}


class TestPrepareOutOfMemoryContext:
    """The failure says how far it got and how many columns there are."""

    def test_out_of_memory_in_the_parquet_step_has_rows_and_columns(
        self, lib: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_: Any, **__: Any) -> Any:
            raise lib.AnalysisError("OUT_OF_MEMORY")

        monkeypatch.setattr(lib, "_to_parquet", boom)
        data = b"id,age,city\n" + b"1,30,x\n" * 50

        result = _prepare(lib, tmp_path, data)

        assert result["error"]["code"] == "OUT_OF_MEMORY"
        assert result["error"]["context"] == {"rows_read": 50, "columns_count": 3}

    def test_a_memory_error_while_converting_has_the_rows_so_far(
        self, lib: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = lib._decode_block
        calls = {"n": 0}

        def flaky(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] > 1:
                raise MemoryError
            return original(*args, **kwargs)

        monkeypatch.setattr(lib, "_CHUNK_BYTES", 64)
        monkeypatch.setattr(lib, "_decode_block", flaky)
        data = b"id,age\n" + b"".join(f"{i},30\n".encode() for i in range(100))

        result = _prepare(lib, tmp_path, data)

        assert result["error"]["code"] == "OUT_OF_MEMORY"
        context = result["error"]["context"]
        assert context["columns_count"] == 2
        assert 0 < context["rows_read"] < 100


def _people_csv(rows: int = 300) -> bytes:
    """Three clear groups of people, made with seed 42."""
    rnd = random.Random(42)
    lines = ["id,age,income,job"]
    centres = [(25, 300, "学生"), (45, 900, "会社員"), (70, 400, "無職")]
    for i in range(rows):
        age, income, job = centres[i % 3]
        lines.append(
            f"{i},{age + rnd.randint(-3, 3)},{income + rnd.randint(-30, 30)},{job}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


class TestProfileAndQuery:
    def test_profile_has_rows_and_column_summaries(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        _prepare(lib, tmp_path, _people_csv(30))

        result = _call(lib, tmp_path, "profile_dataset", source_id="src1")

        value = result["value"]
        assert value["rows"] == 30
        columns = {c["name"]: c for c in value["columns"]}
        assert columns["id"]["all_distinct"] is True
        assert columns["job"]["all_distinct"] is False
        assert columns["age"]["min"] <= columns["age"]["max"]
        assert {t["value"] for t in columns["job"]["top_values"]} == {
            "学生",
            "会社員",
            "無職",
        }

    def test_run_query_uses_parameters(self, lib: ModuleType, tmp_path: Path) -> None:
        _prepare(lib, tmp_path, _people_csv(30))

        result = _call(
            lib,
            tmp_path,
            "run_query",
            source_id="src1",
            sql='SELECT "job", count(*) AS m0 FROM dataset WHERE "age" > $1 '
            'GROUP BY "job" ORDER BY "job"',
            params=[60],
            max_rows=100,
        )

        assert result["value"]["columns"] == ["job", "m0"]
        assert result["value"]["rows"] == [["無職", 10]]

    def test_run_query_refuses_anything_but_select(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        _prepare(lib, tmp_path, _people_csv(30))

        result = _call(
            lib,
            tmp_path,
            "run_query",
            source_id="src1",
            sql="COPY dataset TO 'x.csv'",
            params=[],
            max_rows=10,
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_ARGUMENT"


class TestClusterSegments:
    def test_counts_every_row_and_finds_the_groups(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        _prepare(lib, tmp_path, _people_csv())

        result = _call(
            lib,
            tmp_path,
            "cluster_segments",
            source_id="src1",
            columns=["age", "income", "job"],
            k=3,
            seed=42,
            sample_max_rows=200,
        )

        value = result["value"]
        assert value["population"] == 300
        assert value["seed"] == 42
        sizes = [c["size"] for c in value["clusters"]]
        assert sum(sizes) == 300
        assert sorted(sizes) == [100, 100, 100]
        assert sum(c["share"] for c in value["clusters"]) == pytest.approx(1.0)
        names = {f["name"] for f in value["clusters"][0]["features"]}
        assert {"age", "income"} <= names

    def test_is_reproducible_with_the_same_seed(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        _prepare(lib, tmp_path, _people_csv())
        args = {
            "source_id": "src1",
            "columns": ["age", "income"],
            "k": 3,
            "seed": 42,
            "sample_max_rows": 150,
        }

        first = lib.dispatch("cluster_segments", args, str(tmp_path))
        second = lib.dispatch("cluster_segments", args, str(tmp_path))

        assert first == second

    def test_one_group_needs_no_clustering(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        _prepare(lib, tmp_path, _people_csv(30))

        result = _call(
            lib, tmp_path, "cluster_segments", source_id="src1", columns=["age"], k=1
        )

        assert [c["size"] for c in result["value"]["clusters"]] == [30]

    @pytest.mark.parametrize("columns", [[], ["no_such_column"]])
    def test_too_few_columns(
        self, lib: ModuleType, tmp_path: Path, columns: list[str]
    ) -> None:
        _prepare(lib, tmp_path, _people_csv(30))

        result = _call(
            lib, tmp_path, "cluster_segments", source_id="src1", columns=columns, k=2
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "TOO_FEW_COLUMNS"

    def test_empty_cluster(self, lib: ModuleType, tmp_path: Path) -> None:
        # Every row is the same, so three groups cannot all have rows.
        data = ("id,age\n" + "".join(f"{i},30\n" for i in range(10))).encode()
        _prepare(lib, tmp_path, data)

        result = _call(
            lib, tmp_path, "cluster_segments", source_id="src1", columns=["age"], k=3
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "EMPTY_CLUSTER"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.startswith("/ok"):
            body = _SMALL_CSV
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(403)
            self.end_headers()

    def log_message(self, *args: Any) -> None:
        return


class TestTallyIgnoresMissingValues:
    """The shown mean counts only the cells that have a value."""

    def _tally(self, lib: ModuleType, labels: list[int], values: list[Any]):
        batch = {"x": values}
        features = lib._Features([("x", True)], {"x": [10.0, 20.0, 30.0]})
        tally = lib._Tally(2, features)
        tally.add(labels, batch)
        return tally

    def test_a_blank_does_not_pull_the_mean_to_the_overall_mean(self, lib) -> None:
        # The overall mean of the sample is 20; the blank must not count as 20.
        tally = self._tally(lib, [0, 0, 1], [100.0, None, 40.0])

        (first,) = tally.features_of(0, 3)
        (second,) = tally.features_of(1, 3)

        assert first["value"] == 100.0
        assert second["value"] == 40.0
        assert first["baseline"] == round((100.0 + 40.0) / 2, 4)

    def test_a_cluster_of_only_blanks_shows_no_mean(self, lib) -> None:
        # "mean 0" would read as "lower than everyone"; there is nothing to say
        tally = self._tally(lib, [0, 1], [None, 5.0])

        assert tally.features_of(0, 2) == []
        assert [f["value"] for f in tally.features_of(1, 2)] == [5.0]

    def test_a_column_blank_for_everyone_shows_no_mean(self, lib) -> None:
        tally = self._tally(lib, [0, 1], [None, None])

        assert tally.features_of(0, 2) == [] and tally.features_of(1, 2) == []


@pytest.fixture()
def http_base() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def https_base(
    lib: ModuleType, http_base: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """An ``https://`` base the library accepts, served by the local server.

    The library takes ``https://`` only. The test side, not the library, turns
    the scheme back to ``http://`` when the request is opened, so production
    keeps refusing plain http.
    """
    real_urlopen = lib.urllib.request.urlopen
    https = "https://" + http_base[len("http://") :]

    def urlopen(url: str, *args: Any, **kwargs: Any) -> Any:
        assert url.startswith(https)
        return real_urlopen(http_base + url[len(https) :], *args, **kwargs)

    monkeypatch.setattr(lib.urllib.request, "urlopen", urlopen)
    yield https


_SECRET_QUERY = "?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIA"


def _members_csv() -> bytes:
    lines = ["id,age,plan,area"]
    for i in range(12):
        lines.append(
            f"M{i:02d},{20 + i * 2},{'gold' if i < 4 else 'basic'},"
            f"{'east' if i % 2 == 0 else 'west'}"
        )
    return ("\n".join(lines) + "\n").encode()


class TestProfileSegments:
    """Features of the layers a fixed tool made, counted by the library."""

    def _profile(self, lib: ModuleType, tmp_path: Path, **args: Any) -> Any:
        _prepare(lib, tmp_path, _members_csv(), source_id="src1")
        return _call(lib, tmp_path, "profile_segments", source_id="src1", **args)

    def test_each_value_of_a_column_is_a_layer_with_its_features(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        result = self._profile(lib, tmp_path, by="plan", filters=[])

        value = result["value"]
        assert value["population"] == 12
        layers = {layer["key"]: layer for layer in value["layers"]}
        assert {k: layer["size"] for k, layer in layers.items()} == {
            "basic": 8,
            "gold": 4,
        }
        age = next(f for f in layers["gold"]["features"] if f["name"] == "age")
        # the whole is every row, not the rows of the layers
        assert age["value"] == 23.0 and age["baseline"] == 31.0
        assert age["kind"] == "mean" and age["spread"] > 0
        assert [layer["key"] for layer in value["layers"]] == ["basic", "gold"]

    def test_the_grouping_column_is_not_a_feature_of_its_own_layer(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        result = self._profile(lib, tmp_path, by="plan", filters=[])

        names = {
            f["name"] for layer in result["value"]["layers"] for f in layer["features"]
        }
        assert "age" in names
        assert not any(n.startswith("plan") for n in names)

    def test_filters_make_one_layer_and_an_equal_column_is_left_out(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        filters = [
            {"column": "plan", "operator": "eq", "value": "basic"},
            {"column": "age", "operator": "gte", "value": 30},
        ]

        result = self._profile(lib, tmp_path, filters=filters)

        [layer] = result["value"]["layers"]
        assert layer["size"] == 7 and layer["share"] == round(7 / 12, 6)
        names = {f["name"] for f in layer["features"]}
        assert "age" in names  # a range is no trait given away by the filter
        assert not any(n.startswith("plan") for n in names)
        area = next(f for f in layer["features"] if f["name"].startswith("area="))
        assert area["kind"] == "share" and 0.0 <= area["baseline"] <= 1.0

    def test_the_share_baseline_counts_every_row_of_the_table(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        filters = [{"column": "plan", "operator": "eq", "value": "gold"}]

        result = self._profile(lib, tmp_path, filters=filters)

        [layer] = result["value"]["layers"]
        area = next(f for f in layer["features"] if f["name"].startswith("area="))
        # gold = M00..M03: east twice of four; the whole: east 6 of 12
        assert area["value"] == 0.5 and area["baseline"] == 0.5

    def test_no_row_matching_gives_no_layer(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        filters = [{"column": "age", "operator": "gt", "value": 999}]

        result = self._profile(lib, tmp_path, filters=filters)

        assert result["value"]["layers"] == [] and result["value"]["population"] == 12

    @pytest.mark.parametrize(
        "args",
        [
            {"by": "nope"},
            {"filters": [{"column": "nope", "operator": "eq", "value": 1}]},
            {"filters": [{"column": "age", "operator": "like", "value": 1}]},
            {"filters": [{"column": "age", "operator": "in", "value": []}]},
            {"filters": [{"column": "age", "operator": "eq", "value": {"a": 1}}]},
            {"filters": "age>3"},
        ],
    )
    def test_a_bad_argument_is_refused(
        self, lib: ModuleType, tmp_path: Path, args: dict[str, Any]
    ) -> None:
        result = self._profile(lib, tmp_path, **args)

        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_ARGUMENT"

    def test_a_value_that_looks_like_sql_is_only_a_value(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        filters = [
            {"column": "plan", "operator": "eq", "value": "x'; DROP VIEW dataset; --"}
        ]

        result = self._profile(lib, tmp_path, filters=filters)

        assert result["value"]["layers"] == [] and result["value"]["population"] == 12

    def test_a_key_like_column_is_not_a_feature(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        data = "id,age,zip_code\n" + "\n".join(
            f"M{i},{20 + i},{1000000 + i}" for i in range(10)
        )
        _prepare(lib, tmp_path, (data + "\n").encode(), source_id="src1")

        result = _call(
            lib,
            tmp_path,
            "profile_segments",
            source_id="src1",
            filters=[{"column": "age", "operator": "gte", "value": 25}],
        )

        names = {f["name"] for f in result["value"]["layers"][0]["features"]}
        assert "zip_code" not in names

    def test_at_most_twenty_layers_come_back_the_biggest_first(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        rows = ["id,group,v"]
        for g in range(30):
            for n in range(g + 1):
                rows.append(f"R{g}_{n},g{g:02d},{n}")
        _prepare(lib, tmp_path, ("\n".join(rows) + "\n").encode(), source_id="src1")

        result = _call(
            lib, tmp_path, "profile_segments", source_id="src1", by="group", filters=[]
        )

        layers = result["value"]["layers"]
        assert len(layers) == 20 and layers[0]["key"] == "g29"
        assert [x["size"] for x in layers] == sorted(
            (x["size"] for x in layers), reverse=True
        )


class TestDownload:
    def test_downloads_to_the_sources_folder(
        self, lib: ModuleType, tmp_path: Path, https_base: str
    ) -> None:
        result = _call(
            lib,
            tmp_path,
            "download",
            source_id="src1",
            url=f"{https_base}/ok{_SECRET_QUERY}",
        )

        assert result["value"] == {
            "ok": True,
            "bytes": len(_SMALL_CSV),
            "http_status": 200,
        }
        assert (tmp_path / "sources" / "src1").read_bytes() == _SMALL_CSV

    def test_a_refused_url_reports_only_the_status(
        self, lib: ModuleType, tmp_path: Path, https_base: str
    ) -> None:
        result = _call(
            lib,
            tmp_path,
            "download",
            source_id="src1",
            url=f"{https_base}/expired{_SECRET_QUERY}",
        )

        assert result["value"] == {"ok": False, "bytes": 0, "http_status": 403}
        assert "X-Amz" not in json.dumps(result)

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:9/ok",
            "HTTP://127.0.0.1:9/ok",
            "file:///etc/passwd",
            "ftp://example.com/data.csv",
        ],
    )
    def test_anything_but_https_is_refused_before_opening(
        self,
        lib: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        url: str,
    ) -> None:
        opened: list[str] = []
        monkeypatch.setattr(lib.urllib.request, "urlopen", opened.append)

        result = _call(
            lib, tmp_path, "download", source_id="src1", url=f"{url}{_SECRET_QUERY}"
        )

        assert result == {
            "ok": False,
            "error": {"code": "INVALID_ARGUMENT", "context": {"field": "url"}},
        }
        assert opened == []
        assert not (tmp_path / "sources" / "src1").exists()

    def test_the_command_line_prints_no_url(
        self, tmp_path: Path, http_base: str
    ) -> None:
        # https:// to a plain-http server: the TLS handshake fails in the library
        url = "https://" + http_base[len("http://") :] + f"/expired{_SECRET_QUERY}"
        args = base64.b64encode(
            json.dumps({"source_id": "src1", "url": url}).encode()
        ).decode()

        done = subprocess.run(
            [
                sys.executable,
                "-I",
                str(_LIBRARY_PATH),
                "--workdir",
                str(tmp_path),
                "download",
                args,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert "X-Amz" not in done.stdout + done.stderr
        assert "127.0.0.1" not in done.stdout + done.stderr
        result = json.loads(done.stdout.strip().splitlines()[-1])
        assert result["value"] == {"ok": False, "bytes": 0, "http_status": None}


class TestCommandLineOutput:
    def test_the_result_line_is_ascii_only(self, tmp_path: Path) -> None:
        """Code Interpreter stdout breaks multibyte characters in long output, so the result is ASCII only."""
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "src1").write_text(
            "地域,メモ\n" + "".join(f"東京都,{'…' * 30}{i}\n" for i in range(40)),
            encoding="utf-8",
        )
        args = base64.b64encode(
            json.dumps(
                {"source_id": "src1", "metadata": {"encoding": "utf-8"}}
            ).encode()
        ).decode()

        done = subprocess.run(
            [sys.executable, "-I", str(_LIBRARY_PATH), "--workdir", str(tmp_path)]
            + ["prepare", args],
            capture_output=True,
            timeout=60,
            check=False,
        )

        assert done.stdout.isascii()
        result = json.loads(done.stdout.decode("ascii").strip())
        assert result["ok"] is True
        assert result["value"]["columns"][0]["name"] == "地域"


class TestDispatch:
    def test_unknown_function(self, lib: ModuleType, tmp_path: Path) -> None:
        result = _call(lib, tmp_path, "run_code", code="print(1)")

        assert result == {"ok": False, "error": {"code": "UNKNOWN_FUNCTION"}}

    def test_profile_before_prepare_is_unreadable(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        result = _call(lib, tmp_path, "profile_dataset", source_id="src1")

        assert result["ok"] is False
        assert result["error"]["code"] == "SOURCE_UNREADABLE"


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def _prepare_document(
    lib: ModuleType,
    workdir: Path,
    data: bytes,
    fmt: str,
    *,
    source_id: str = "doc1",
    source_index: int = 0,
    encoding: str | None = None,
    chunk_chars: int = 15_000,
    overlap_chars: int = 500,
) -> dict[str, Any]:
    _put_source(workdir, source_id, data)
    args: dict[str, Any] = {
        "source_id": source_id,
        "source_index": source_index,
        "format": fmt,
        "chunk_chars": chunk_chars,
        "overlap_chars": overlap_chars,
        "progress_id": "p1",
    }
    if encoding:
        args["encoding"] = encoding
    return lib.dispatch("prepare_document", args, str(workdir))


def _chunk(
    lib: ModuleType,
    workdir: Path,
    index: int,
    *,
    source_id: str = "doc1",
    offset: int = 0,
    max_chars: int = 100_000,
) -> dict[str, Any]:
    return lib.dispatch(
        "document_chunk",
        {
            "source_id": source_id,
            "index": index,
            "offset": offset,
            "max_chars": max_chars,
        },
        str(workdir),
    )


def _all_chunks(lib: ModuleType, workdir: Path, count: int, **kw: Any) -> list[str]:
    return [_chunk(lib, workdir, i, **kw)["value"]["text"] for i in range(count)]


class TestPrepareDocument:
    def test_a_blank_page_is_left_out_and_the_rest_goes_on(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        from tests.document_fixtures import pdf_bytes

        result = _prepare_document(
            lib, tmp_path, pdf_bytes(["First page text", "", "Third page"]), "pdf"
        )

        value = result["value"]
        assert value["pages"] == 3
        assert value["no_text_pages"] == [2]
        assert value["chunks"] == 1
        assert value["chars"] > 0
        text = _chunk(lib, tmp_path, 0)["value"]
        assert "First page text" in text["text"] and "Third page" in text["text"]
        assert (text["page_from"], text["page_to"]) == (1, 3)
        progress = json.loads((tmp_path / "progress" / "p1.json").read_text())
        assert progress["pages"] == 3

    def test_every_page_blank_is_no_text(self, lib: ModuleType, tmp_path: Path) -> None:
        from tests.document_fixtures import pdf_bytes

        result = _prepare_document(
            lib, tmp_path, pdf_bytes(["", ""]), "pdf", source_index=3
        )

        assert result["ok"] is False
        assert result["error"] == {
            "code": "DOCUMENT_NO_TEXT",
            "context": {"source_index": 3},
        }

    def test_an_encrypted_pdf_is_refused(self, lib: ModuleType, tmp_path: Path) -> None:
        from tests.document_fixtures import pdf_bytes

        result = _prepare_document(
            lib, tmp_path, pdf_bytes(["secret"], encrypted=True), "pdf", source_index=1
        )

        assert result["error"] == {
            "code": "DOCUMENT_ENCRYPTED",
            "context": {"source_index": 1},
        }

    def test_docx_chunks_are_cut_at_headings(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        from tests.document_fixtures import docx_bytes

        body_a = "Alpha body sentence. " * 400  # about 8,400 characters
        body_b = "Beta body sentence. " * 400
        data = docx_bytes([(1, "Part A"), body_a, (2, "Part B"), body_b])

        result = _prepare_document(lib, tmp_path, data, "docx")

        value = result["value"]
        assert value["headings"] == 2
        assert value["chunks"] == 2
        assert "pages" not in value and value["no_text_pages"] == []
        first, second = _all_chunks(lib, tmp_path, 2)
        assert first.startswith("# Part A")
        assert "Part B" not in first
        assert second[500:].startswith("## Part B")
        assert second[:500] == first[-500:]
        assert _chunk(lib, tmp_path, 1)["value"]["heading_path"] == ["Part A", "Part B"]

    def test_headings_of_a_japanese_word_are_read(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        from tests.document_fixtures import docx_bytes

        data = docx_bytes([(1, "第1章"), "本文です。"], japanese_style_ids=True)

        result = _prepare_document(lib, tmp_path, data, "docx")

        assert result["value"]["headings"] == 1
        assert _chunk(lib, tmp_path, 0)["value"]["text"].startswith("# 第1章")

    def test_a_long_body_without_breaks_is_cut_by_characters_with_overlap(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        text = "あ" * 20_000 + "い" * 20_000

        result = _prepare_document(lib, tmp_path, text.encode("utf-8"), "txt")

        chunks = _all_chunks(lib, tmp_path, result["value"]["chunks"])
        assert result["value"]["chunks"] == 3
        assert [len(c) for c in chunks] == [15_000, 15_000, 11_000]
        assert chunks[1][:500] == chunks[0][-500:]
        assert result["value"]["chars"] == 40_000

    def test_paragraphs_are_preferred_to_a_character_cut(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        paragraphs = ["段落" + str(i) + "。" + "う" * 3_990 for i in range(6)]
        text = "\n\n".join(paragraphs)

        result = _prepare_document(lib, tmp_path, text.encode("utf-8"), "txt")

        first = _all_chunks(lib, tmp_path, result["value"]["chunks"])[0]
        assert first.rstrip().endswith("う")
        assert first.count("段落") == 3

    def test_pages_are_preferred_to_paragraphs(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        from tests.document_fixtures import pdf_bytes

        page = "\n".join(["Line of the page number one."] * 120)  # about 3,500
        pages = [page.replace("one", str(i)) for i in range(6)]

        _prepare_document(lib, tmp_path, pdf_bytes(pages), "pdf", chunk_chars=8_000)

        first = _chunk(lib, tmp_path, 0)["value"]
        assert first["page_to"] == 2
        assert first["text"].rstrip().endswith("number 1.")

    @pytest.mark.parametrize(
        ("encoding", "payload"),
        [
            ("utf-8", "面談の記録"),
            ("cp932", "面談の記録"),
            ("undetermined", "面談の記録"),
        ],
    )
    def test_text_is_read_in_its_encoding(
        self, lib: ModuleType, tmp_path: Path, encoding: str, payload: str
    ) -> None:
        raw_encoding = "cp932" if encoding in ("cp932", "undetermined") else "utf-8"
        data = (payload + "\n本文です。").encode(raw_encoding)

        result = _prepare_document(lib, tmp_path, data, "txt", encoding=encoding)

        assert result["ok"] is True
        assert payload in _chunk(lib, tmp_path, 0)["value"]["text"]

    def test_markdown_headings_are_counted(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        data = "# 概要\n本文\n\n## 不満\n待ち時間が長い\n".encode("utf-8")

        result = _prepare_document(lib, tmp_path, data, "md", encoding="utf-8")

        assert result["value"]["headings"] == 2

    def test_empty_text_is_no_text(self, lib: ModuleType, tmp_path: Path) -> None:
        result = _prepare_document(lib, tmp_path, b"  \n\n \n", "txt", source_index=2)

        assert result["error"]["code"] == "DOCUMENT_NO_TEXT"
        assert result["error"]["context"] == {"source_index": 2}

    def test_a_docx_that_is_not_a_zip_is_unreadable(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        result = _prepare_document(lib, tmp_path, b"not a zip", "docx", source_index=4)

        assert result["error"]["code"] == "SOURCE_UNREADABLE"
        assert result["error"]["context"] == {"source_index": 4}

    def test_running_out_of_memory_is_reported(
        self, lib: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise MemoryError()

        monkeypatch.setattr(lib, "_document_blocks", boom)

        result = _prepare_document(lib, tmp_path, b"text", "txt")

        assert result["error"]["code"] == "OUT_OF_MEMORY"

    def test_a_reader_failure_keeps_only_the_type_name(
        self, lib: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("secret body text")

        monkeypatch.setattr(lib, "_document_blocks", broken)

        result = _prepare_document(lib, tmp_path, b"text", "txt")

        assert "secret" not in json.dumps(result)
        assert result["ok"] is False

    @pytest.mark.parametrize(
        "bad",
        [
            {"chunk_chars": 0},
            {"overlap_chars": 15_000},
            {"format": "csv"},
            {"source_index": -1},
        ],
    )
    def test_bad_arguments_are_refused(
        self, lib: ModuleType, tmp_path: Path, bad: dict[str, Any]
    ) -> None:
        _put_source(tmp_path, "doc1", b"text")
        args: dict[str, Any] = {
            "source_id": "doc1",
            "source_index": 0,
            "format": "txt",
            "chunk_chars": 15_000,
            "overlap_chars": 500,
            **bad,
        }

        result = lib.dispatch("prepare_document", args, str(tmp_path))

        assert result["error"]["code"] == "INVALID_ARGUMENT"


class TestDocumentChunk:
    def test_a_chunk_is_handed_over_in_pieces(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        _prepare_document(lib, tmp_path, ("え" * 7_000).encode("utf-8"), "txt")

        first = _chunk(lib, tmp_path, 0, max_chars=3_000)["value"]
        second = _chunk(lib, tmp_path, 0, offset=first["next_offset"], max_chars=3_000)[
            "value"
        ]
        last = _chunk(lib, tmp_path, 0, offset=second["next_offset"], max_chars=3_000)[
            "value"
        ]

        assert len(first["text"]) == 3_000 and first["next_offset"] == 3_000
        assert second["next_offset"] == 6_000
        assert len(last["text"]) == 1_000 and last["next_offset"] is None
        assert first["chunks"] == 1

    def test_an_index_out_of_range_is_refused(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        _prepare_document(lib, tmp_path, b"short text", "txt")

        assert _chunk(lib, tmp_path, 5)["error"]["code"] == "INVALID_ARGUMENT"

    def test_a_document_not_prepared_is_unreadable(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        assert _chunk(lib, tmp_path, 0)["error"]["code"] == "SOURCE_UNREADABLE"


class TestVerifyQuotes:
    def _verify(
        self, lib: ModuleType, workdir: Path, quotes: list[str], **kw: Any
    ) -> Any:
        return lib.dispatch(
            "verify_quotes",
            {
                "source_id": kw.get("source_id", "doc1"),
                "quotes": quotes,
                "min_chars": 10,
            },
            str(workdir),
        )

    def test_quotes_match_after_normalising(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        text = "受付で３０分も\n待たされたのが不満です。ソフト­ハイフン入りの文章です。"
        _prepare_document(lib, tmp_path, text.encode("utf-8"), "txt")

        result = self._verify(
            lib,
            tmp_path,
            [
                "受付で30分も待たされた",  # NFKC and the line break
                "受付で 30 分も 待たされたのが",  # spaces
                "ソフトハイフン入りの文章です",  # U+00AD removed
                "<​data の中にない引用文です",  # not in the text
                "待たされた",  # under 10 characters
            ],
        )

        assert result["value"]["found"] == [True, True, True, False, False]

    def test_a_quote_across_the_overlap_is_found(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        text = "お" * 14_990 + "境目をまたぐ引用の文章" + "か" * 10_000
        _prepare_document(lib, tmp_path, text.encode("utf-8"), "txt")

        assert self._verify(lib, tmp_path, ["お境目をまたぐ引用の文章か"])["value"][
            "found"
        ] == [True]

    def test_a_quote_of_another_document_is_not_found(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        _prepare_document(
            lib, tmp_path, "一つ目の文書にだけある文章です".encode(), "txt"
        )
        _prepare_document(
            lib,
            tmp_path,
            "二つ目の文書にだけある文章です".encode(),
            "txt",
            source_id="doc2",
        )

        assert self._verify(
            lib, tmp_path, ["一つ目の文書にだけある文章"], source_id="doc2"
        )["value"]["found"] == [False]

    def test_more_than_fifty_quotes_are_refused(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        _prepare_document(lib, tmp_path, b"text text text text", "txt")

        result = self._verify(lib, tmp_path, ["x" * 10] * 51)

        assert result["error"]["code"] == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# AI-written code, the check and the playbook functions
# ---------------------------------------------------------------------------

_SOURCES = {"source_1": "cust", "source_2": "buys"}


def _customers(rows: int = 30) -> bytes:
    lines = ["customer_id,age,plan"]
    for i in range(rows):
        lines.append(f"C{i:03d},{20 + i},{'gold' if i % 3 == 0 else 'basic'}")
    return ("\n".join(lines) + "\n").encode()


def _purchases(people: int = 24) -> bytes:
    lines = ["customer_id,amount,bought_on"]
    for i in range(people):
        for n in range(1 + i % 3):
            lines.append(f"C{i:03d},{100 * (n + 1) + i},2026-0{1 + n}-1{i % 9}")
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture()
def tables(lib: ModuleType, tmp_path: Path) -> dict[str, Any]:
    customers = _prepare(lib, tmp_path, _customers(), source_id="cust")
    purchases = _prepare(lib, tmp_path, _purchases(), source_id="buys")
    assert customers["ok"] and purchases["ok"]
    (tmp_path / "persona_analysis.py").write_bytes(_LIBRARY_PATH.read_bytes())
    return {"cust": customers["value"], "buys": purchases["value"]}


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_ai(
    lib: ModuleType, workdir: Path, code: str, token: str = "t1", **extra: Any
) -> dict[str, Any]:
    args = {
        "code": code,
        "work_dir": f"ai_work/{token}",
        "pgid_file": f"ai_work/{token}.pgid",
        "sources": _SOURCES,
        "timeout_seconds": 60,
        **extra,
    }
    return lib.dispatch("run_ai_code", args, str(workdir))


def _finalize(lib: ModuleType, workdir: Path, token: str = "t1") -> dict[str, Any]:
    result = lib.dispatch(
        "finalize_ai_result",
        {
            "work_dir": f"ai_work/{token}",
            "pgid_file": f"ai_work/{token}.pgid",
            "sources": _SOURCES,
            "session_library": str(workdir / "persona_analysis.py"),
        },
        str(workdir),
    )
    assert result["ok"] is True
    return result["value"]


_EMIT_BY_AGE = """
df = read_table("source_1")
df["seg"] = ["young" if a < 35 else "old" for a in df["age"]]
emit_result(df, "seg", "source_1", id_column="customer_id")
"""


class TestPrepareHash:
    def test_prepare_reports_the_parquet_hash(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        parquet = tmp_path / "prepared" / "cust.parquet"

        assert tables["cust"]["parquet_sha256"] == _sha(parquet)


class TestRunAiCode:
    def test_runs_in_its_own_process_group_and_records_it(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        result = _run_ai(
            lib, tmp_path, "import os\nprint('pgid', os.getpgrp(), os.getpid())"
        )

        value = result["value"]
        assert value["status"] == "ok" and value["exit_code"] == 0
        _, pgid, pid = value["stdout"].split()
        assert pgid == pid  # a new session: the code leads its own group
        recorded = (tmp_path / "ai_work" / "t1.pgid").read_text().strip()
        assert recorded == pgid

    def test_a_folder_used_before_is_refused(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        assert _run_ai(lib, tmp_path, "x = 1")["ok"]

        again = _run_ai(lib, tmp_path, "x = 1")

        assert again["ok"] is False and again["error"]["code"] == "INVALID_ARGUMENT"

    def test_an_exception_is_a_failure_with_its_text(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        value = _run_ai(lib, tmp_path, "raise KeyError('income')")["value"]

        assert value["status"] == "failed"
        assert "KeyError" in value["error_text"]

    def test_an_exception_after_emit_result_is_still_a_failure(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        value = _run_ai(lib, tmp_path, _EMIT_BY_AGE + "\nraise ValueError('late')")[
            "value"
        ]

        assert value["status"] == "failed"

    def test_a_run_past_its_time_is_stopped(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        value = _run_ai(lib, tmp_path, "while True:\n    pass", timeout_seconds=1)[
            "value"
        ]

        assert value["status"] == "failed" and value["timed_out"] is True


class TestEmitAndFinalize:
    def test_the_check_counts_sizes_and_population_from_the_data(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        assert _run_ai(lib, tmp_path, _EMIT_BY_AGE)["value"]["status"] == "ok"

        value = _finalize(lib, tmp_path)

        result = value["result"]
        assert value["error"] is None
        assert result["population"] == 30
        assert result["population_source"] == "source_1"
        sizes = {s["segment_key"]: s["size"] for s in result["segments"]}
        assert sizes == {"young": 15, "old": 15}
        assert value["hashes"]["parquet"] == {
            "cust": tables["cust"]["parquet_sha256"],
            "buys": tables["buys"]["parquet_sha256"],
        }
        assert value["hashes"]["library"] == _sha(_LIBRARY_PATH)

    def test_emit_result_writes_only_ids_and_segments(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        _run_ai(lib, tmp_path, _EMIT_BY_AGE)

        import pyarrow.parquet as pq

        written = pq.read_table(tmp_path / "ai_work" / "t1" / "assignment.parquet")
        assert written.column_names[:2] == ["person_id", "segment"]
        # nothing else of the table: only numbers the code computed (metrics)
        assert all(c.startswith("metric_") for c in written.column_names[2:])

    def test_a_second_emit_result_raises_and_fails_the_check(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        code = (
            _EMIT_BY_AGE
            + "\ntry:\n"
            + "".join(
                "    " + line + "\n" for line in _EMIT_BY_AGE.strip().splitlines()
            )
            + "except Exception:\n    pass\n"
        )
        _run_ai(lib, tmp_path, code)

        value = _finalize(lib, tmp_path)

        assert value["result"] is None and value["error"] == "EMIT_TWICE"

    def test_no_emit_result_is_no_result(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        _run_ai(lib, tmp_path, "x = 1")

        assert _finalize(lib, tmp_path)["error"] == "NO_RESULT"

    @pytest.mark.parametrize(
        ("code", "error"),
        [
            # a person left out
            (
                'df = read_table("source_1").iloc[1:]\ndf["seg"] = "a"\n'
                'emit_result(df, "seg", "source_1", id_column="customer_id")',
                "ID_MISMATCH",
            ),
            # a person who is not in the table
            (
                'import pandas as pd\ndf = read_table("source_1")\n'
                'df = pd.concat([df, df.iloc[:1].assign(customer_id="X999")])\n'
                'df["seg"] = "a"\n'
                'emit_result(df, "seg", "source_1", id_column="customer_id")',
                "ID_MISMATCH",
            ),
            # the same person twice
            (
                'import pandas as pd\ndf = read_table("source_1")\n'
                "df = pd.concat([df.iloc[1:], df.iloc[:1], df.iloc[:1]])\n"
                'df["seg"] = "a"\n'
                'emit_result(df, "seg", "source_1", id_column="customer_id")',
                "DUPLICATE_IDS",
            ),
            # the purchases table has several rows per person
            (
                'df = read_table("source_2")\ndf["seg"] = "a"\n'
                'emit_result(df, "seg", "source_2", id_column="customer_id")',
                "DUPLICATE_IDS",
            ),
        ],
    )
    def test_people_must_match_the_source_exactly(
        self,
        lib: ModuleType,
        tmp_path: Path,
        tables: dict[str, Any],
        code: str,
        error: str,
    ) -> None:
        _run_ai(lib, tmp_path, code)

        value = _finalize(lib, tmp_path)

        assert value["result"] is None and value["error"] == error

    @pytest.mark.parametrize(
        ("code", "error", "context"),
        [
            # the purchases table as the population: several rows per person
            (
                'df = read_table("source_2")\ndf["seg"] = "a"\n'
                'emit_result(df, "seg", "source_2", id_column="customer_id")',
                "DUPLICATE_IDS",
                {"side": "population", "rows": 48, "distinct_ids": 24},
            ),
            # the frame repeats a person
            (
                'import pandas as pd\ndf = read_table("source_1")\n'
                "df = pd.concat([df.iloc[1:], df.iloc[:1], df.iloc[:1]])\n"
                'df["seg"] = "a"\n'
                'emit_result(df, "seg", "source_1", id_column="customer_id")',
                "DUPLICATE_IDS",
                {"side": "result", "rows": 31, "distinct_ids": 30},
            ),
            (
                'df = read_table("source_1").iloc[1:]\ndf["seg"] = "a"\n'
                'emit_result(df, "seg", "source_1", id_column="customer_id")',
                "ID_MISMATCH",
                {
                    "population_rows": 30,
                    "result_rows": 29,
                    "not_in_result": 1,
                    "not_in_population": 0,
                },
            ),
            (
                'import pandas as pd\ndf = read_table("source_1")\n'
                'df = pd.concat([df, df.iloc[:1].assign(customer_id="X999")])\n'
                'df["seg"] = "a"\n'
                'emit_result(df, "seg", "source_1", id_column="customer_id")',
                "ID_MISMATCH",
                {
                    "population_rows": 30,
                    "result_rows": 31,
                    "not_in_result": 0,
                    "not_in_population": 1,
                },
            ),
            (
                'df = read_table("source_1")\ndf["seg"] = ["a"] * 28 + [None, None]\n'
                'emit_result(df, "seg", "source_1", id_column="customer_id")',
                "UNASSIGNED",
                {"rows_unassigned": 2},
            ),
            # pandas' missing values (a left merge) are no segment called "nan"
            (
                'import pandas as pd\ndf = read_table("source_1")\n'
                'seg = pd.DataFrame({"customer_id": ["C000"], "seg": ["a"]})\n'
                'df = df.merge(seg, on="customer_id", how="left")\n'
                'emit_result(df, "seg", "source_1", id_column="customer_id")',
                "UNASSIGNED",
                {"rows_unassigned": 29},
            ),
        ],
    )
    def test_a_refused_result_tells_the_counts_behind_it(
        self,
        lib: ModuleType,
        tmp_path: Path,
        tables: dict[str, Any],
        code: str,
        error: str,
        context: dict[str, Any],
    ) -> None:
        _run_ai(lib, tmp_path, code)

        value = _finalize(lib, tmp_path)

        assert value["error"] == error
        assert value["error_context"] == context

    def test_population_rows_without_an_id_are_not_called_duplicates(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        data = _customers().decode().rstrip("\n") + "\n,50,basic\n"
        assert _prepare(lib, tmp_path, data.encode(), source_id="cust")["ok"]
        assert _prepare(lib, tmp_path, _purchases(), source_id="buys")["ok"]
        (tmp_path / "persona_analysis.py").write_bytes(_LIBRARY_PATH.read_bytes())
        _run_ai(
            lib,
            tmp_path,
            'df = read_table("source_1")\ndf["seg"] = "a"\n'
            'emit_result(df, "seg", "source_1", id_column="customer_id")',
        )

        value = _finalize(lib, tmp_path)

        assert value["error"] == "POPULATION_ID_MISSING"
        assert value["error_context"] == {"rows_without_id": 1}

    @pytest.mark.parametrize(
        ("code", "read"),
        [
            # a merge of its own instead of join_tables
            (
                'c = read_table("source_1")\nb = read_table("source_2")\n'
                'n = b.groupby("customer_id").size().rename("n").reset_index()\n'
                'df = c.merge(n, on="customer_id", how="left")\n'
                'df["seg"] = ["buyer" if v == v else "none" for v in df["n"]]\n'
                'emit_result(df, "seg", "source_1", id_column="customer_id")',
                ["source_1", "source_2"],
            ),
            (
                'df = join_tables("source_1", "customer_id", [{"source": "source_2",'
                ' "aggregations": [{"column": "amount", "func": "sum"}]}])\n'
                'df["seg"] = "a"\n'
                'emit_result(df, "seg", "join:source_1+source_2",'
                ' id_column="customer_id")',
                ["source_1", "source_2"],
            ),
            (
                'df = read_table("source_1")\ndf["seg"] = "a"\n'
                'emit_result(df, "seg", "source_1", id_column="customer_id")',
                ["source_1"],
            ),
        ],
    )
    def test_the_tables_the_code_read_are_recorded(
        self,
        lib: ModuleType,
        tmp_path: Path,
        tables: dict[str, Any],
        code: str,
        read: list[str],
    ) -> None:
        _run_ai(lib, tmp_path, code)

        result = _finalize(lib, tmp_path)["result"]

        assert result["tables_read"] == read

    def test_only_known_tables_are_recorded_as_read(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        # the code passes its own list: the wrapper's list wins, and the check
        # keeps only the tables of this run
        _run_ai(
            lib,
            tmp_path,
            'df = read_table("source_1")\ndf["seg"] = "a"\n'
            'emit_result(df, "seg", "source_1", id_column="customer_id",'
            ' tables_read=["source_9", "</data>"])',
        )

        assert _finalize(lib, tmp_path)["result"]["tables_read"] == ["source_1"]

    def test_an_accepted_result_has_no_error_context(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        _run_ai(lib, tmp_path, _EMIT_BY_AGE)

        value = _finalize(lib, tmp_path)

        assert value["error"] is None and value.get("error_context") is None

    def test_a_result_file_written_by_hand_is_recounted(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        code = (
            "import json, pyarrow as pa, pyarrow.parquet as pq\n"
            'ids = list(read_table("source_1")["customer_id"])\n'
            "pq.write_table(pa.table({'person_id': ids, 'segment': ['a'] * len(ids)}),"
            " 'assignment.parquet')\n"
            "open('emit_count', 'w').write('1')\n"
            "json.dump({'population_source': 'source_1', 'id_column': 'customer_id',"
            " 'population': 5000, 'sizes': {'a': 4999}}, open('meta.json', 'w'))\n"
            'print(json.dumps({"ok": True, "value": {"population": 5000}}))\n'
        )
        _run_ai(lib, tmp_path, code)

        result = _finalize(lib, tmp_path)["result"]

        assert result["population"] == 30
        assert [(s["segment_key"], s["size"]) for s in result["segments"]] == [
            ("a", 30)
        ]

    def test_features_are_counted_by_the_library_for_each_segment(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        _run_ai(lib, tmp_path, _EMIT_BY_AGE)

        result = _finalize(lib, tmp_path)["result"]

        by_key = {s["segment_key"]: s["features"] for s in result["segments"]}
        age = {f["kind"]: f for f in by_key["young"] if f["name"] == "age"}["mean"]
        assert age["value"] == 27.0 and age["baseline"] == 34.5
        old_age = next(f for f in by_key["old"] if f["name"] == "age")
        assert old_age["value"] == 42.0 and old_age["baseline"] == 34.5
        plan = [f for f in by_key["old"] if f["name"].startswith("plan=")]
        assert plan and plan[0]["kind"] == "share"
        assert 0.0 <= plan[0]["value"] <= 1.0 and 0.0 <= plan[0]["baseline"] <= 1.0
        # the person's ID is not a feature
        assert not any(f["name"].startswith("customer_id") for f in by_key["old"])

    def test_features_the_code_passed_are_not_used(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        code = (
            'df = read_table("source_1")\n'
            'df["seg"] = ["young" if a < 35 else "old" for a in df["age"]]\n'
            'emit_result(df, "seg", "source_1", id_column="customer_id",'
            ' features={"young": [{"name": "made_up", "value": 1, "baseline": 2}]})\n'
        )
        _run_ai(lib, tmp_path, code)

        result = _finalize(lib, tmp_path)["result"]

        names = {f["name"] for s in result["segments"] for f in s["features"]}
        assert "made_up" not in names and "age" in names

    def test_a_metric_the_code_computed_is_averaged_by_the_library(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        code = (
            'df = read_table("source_1")\n'
            'df["seg"] = ["young" if a < 35 else "old" for a in df["age"]]\n'
            'df["score"] = df["age"] * 2\n'
            'df["note"] = "text"\n'
            'emit_result(df, "seg", "source_1", id_column="customer_id",'
            ' metrics=["score", "note"])\n'
        )
        _run_ai(lib, tmp_path, code)

        result = _finalize(lib, tmp_path)["result"]

        by_key = {s["segment_key"]: s["features"] for s in result["segments"]}
        score = next(f for f in by_key["young"] if f["name"] == "score")
        assert score == {
            "name": "score",
            "kind": "derived",
            "value": 54.0,
            "baseline": 69.0,
            "spread": pytest.approx(17.3109, abs=1e-3),
        }
        # text is no metric
        assert not any(f["name"] == "note" for f in by_key["young"])

    def test_numeric_columns_the_code_added_are_metrics_without_being_named(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        code = (
            'df = read_table("source_1")\n'
            'df["seg"] = ["young" if a < 35 else "old" for a in df["age"]]\n'
            'df["score"] = df["age"] * 2\n'
            'emit_result(df, "seg", "source_1", id_column="customer_id")\n'
        )
        _run_ai(lib, tmp_path, code)

        result = _finalize(lib, tmp_path)["result"]

        features = {
            (f["name"], f["kind"])
            for s in result["segments"]
            if s["segment_key"] == "young"
            for f in s["features"]
        }
        assert ("score", "derived") in features
        # a column of the table is counted by the library, not as a metric
        assert ("age", "mean") in features and ("age", "derived") not in features

    def test_a_metric_that_is_not_a_column_is_an_error_of_the_code(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        code = (
            'df = read_table("source_1")\ndf["seg"] = "a"\n'
            'emit_result(df, "seg", "source_1", id_column="customer_id",'
            ' metrics=["nope"])\n'
        )
        _run_ai(lib, tmp_path, code)

        assert _finalize(lib, tmp_path)["error"] == "NO_RESULT"

    def test_every_column_is_looked_at_and_ids_are_not_features(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        cols = ["customer_id", "age", "zip_code", "member_no", "residence"]
        cols += [f"q{i}" for i in range(30)] + ["key_metric"]
        lines = [",".join(cols)]
        for i in range(30):
            row = [f"C{i:03d}", str(20 + i), str(1500000 + i * 37), str(i + 1)]
            row.append("east" if i % 2 else "west")
            row += [str((i * 7 + j) % 5) for j in range(30)]
            row.append(str(1000 if i < 15 else 9000))
            lines.append(",".join(row))
        assert _prepare(
            lib, tmp_path, ("\n".join(lines) + "\n").encode(), source_id="cust"
        )["ok"]
        (tmp_path / "persona_analysis.py").write_bytes(_LIBRARY_PATH.read_bytes())
        _run_ai(lib, tmp_path, _EMIT_BY_AGE)

        result = _finalize(lib, tmp_path)["result"]

        names = {f["name"] for s in result["segments"] for f in s["features"]}
        # the last of 35 columns is the one that differs: it is not skipped
        assert "key_metric" in names
        # a numeric ID or a number is no trait of a person
        assert "zip_code" not in names and "member_no" not in names
        # a word that merely holds "id" is an ordinary column
        assert any(n.startswith("residence=") for n in names)

    def test_a_mean_carries_the_spread_of_the_whole(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        _run_ai(lib, tmp_path, _EMIT_BY_AGE)

        result = _finalize(lib, tmp_path)["result"]

        age = next(f for f in result["segments"][0]["features"] if f["name"] == "age")
        # ages 20..49: the population standard deviation is sqrt((30**2 - 1) / 12)
        assert age["spread"] == pytest.approx(8.6554, abs=1e-3)
        share = next(
            f for f in result["segments"][0]["features"] if f["kind"] == "share"
        )
        assert "spread" not in share

    def test_a_replaced_emit_result_does_not_change_the_numbers(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        code = (
            "import persona_analysis\n"
            "real = emit_result\n"
            "def emit_result(frame, *a, **k):\n"
            "    persona_analysis._EMIT_POPULATION = 9999\n"
            "    real(frame, *a, **k)\n" + _EMIT_BY_AGE
        )
        _run_ai(lib, tmp_path, code)

        result = _finalize(lib, tmp_path)["result"]

        assert result["population"] == 30

    def test_a_changed_parquet_shows_in_the_hash(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        _run_ai(
            lib,
            tmp_path,
            "open('../../prepared/cust.parquet', 'ab').write(b'x')",
        )

        hashes = _finalize(lib, tmp_path)["hashes"]

        assert hashes["parquet"]["cust"] != tables["cust"]["parquet_sha256"]
        assert hashes["parquet"]["buys"] == tables["buys"]["parquet_sha256"]

    def test_a_changed_library_shows_in_the_hash(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        _run_ai(lib, tmp_path, "open('../../persona_analysis.py', 'a').write('#')")

        assert _finalize(lib, tmp_path)["hashes"]["library"] != _sha(_LIBRARY_PATH)

    def test_the_check_stops_what_the_code_left_running(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        import os

        code = (
            "import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "open('child.pid', 'w').write(str(p.pid))\n"
        )
        _run_ai(lib, tmp_path, code)
        child = int((tmp_path / "ai_work" / "t1" / "child.pid").read_text())

        _finalize(lib, tmp_path)

        import time

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            try:
                os.waitpid(child, os.WNOHANG)
            except ChildProcessError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("the left process still runs")

    def test_verify_hashes_returns_the_hashes_only(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        result = lib.dispatch(
            "verify_hashes",
            {
                "pgid_files": [],
                "sources": _SOURCES,
                "session_library": str(tmp_path / "persona_analysis.py"),
            },
            str(tmp_path),
        )

        assert set(result["value"]) == {"hashes", "process_scan", "leftover_processes"}
        assert result["value"]["process_scan"] == "ok"
        assert result["value"]["leftover_processes"] == 0
        assert (
            result["value"]["hashes"]["parquet"]["cust"]
            == (tables["cust"]["parquet_sha256"])
        )


# --- security review: a process that left the group (fork + setsid) --------

_ESCAPE = (
    "import os, time\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    os.setsid()\n"
    "    time.sleep(30)\n"
    "    os._exit(0)\n"
    "open('child.pid', 'w').write(str(pid))\n"
)
_needs_fork = pytest.mark.skipif(
    not hasattr(__import__("os"), "fork"), reason="needs os.fork (POSIX)"
)


def _alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except OSError:
        pass
    state = subprocess.run(
        ["/bin/ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _wait_dead(pid: int, seconds: float = 3.0) -> bool:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


@pytest.fixture()
def escaped_pids() -> Iterator[list[int]]:
    """Pids the tests started; killed at the end whatever happened."""
    import os
    import signal

    pids: list[int] = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class TestLeftoverProcesses:
    """A process the code left outside its group is found, stopped and told."""

    def _escape(
        self, lib: ModuleType, tmp_path: Path, pids: list[int], token: str = "t1"
    ) -> int:
        value = _run_ai(lib, tmp_path, _ESCAPE, token=token)["value"]
        assert value["status"] == "ok", value["error_text"]
        child = int((tmp_path / "ai_work" / token / "child.pid").read_text())
        pids.append(child)
        assert _alive(child)
        return child

    @_needs_fork
    def test_finalize_stops_an_escaped_process_and_counts_it(
        self,
        lib: ModuleType,
        tmp_path: Path,
        tables: dict[str, Any],
        escaped_pids: list[int],
    ) -> None:
        child = self._escape(lib, tmp_path, escaped_pids)

        value = _finalize(lib, tmp_path)

        assert value["process_scan"] == "ok"
        assert value["leftover_processes"] >= 1
        assert _wait_dead(child)

    @_needs_fork
    def test_verify_hashes_stops_an_escaped_process_and_counts_it(
        self,
        lib: ModuleType,
        tmp_path: Path,
        tables: dict[str, Any],
        escaped_pids: list[int],
    ) -> None:
        child = self._escape(lib, tmp_path, escaped_pids)

        result = lib.dispatch(
            "verify_hashes",
            {
                "pgid_files": ["ai_work/t1.pgid"],
                "sources": _SOURCES,
                "session_library": str(tmp_path / "persona_analysis.py"),
            },
            str(tmp_path),
        )

        assert result["value"]["leftover_processes"] >= 1
        assert _wait_dead(child)

    def test_nothing_left_is_zero(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        _run_ai(lib, tmp_path, _EMIT_BY_AGE)

        value = _finalize(lib, tmp_path)

        assert value["process_scan"] == "ok" and value["leftover_processes"] == 0

    def test_a_child_inside_the_group_is_stopped_but_not_a_leftover(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        code = (
            "import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; "
            "time.sleep(30)'])\n"
            "open('child.pid', 'w').write(str(p.pid))\n"
        )
        _run_ai(lib, tmp_path, code)
        child = int((tmp_path / "ai_work" / "t1" / "child.pid").read_text())

        value = _finalize(lib, tmp_path)

        # the group stop reaches it: nothing escaped
        assert value["leftover_processes"] == 0
        assert _wait_dead(child)

    def test_without_a_process_list_the_scan_is_unavailable(
        self,
        lib: ModuleType,
        tmp_path: Path,
        tables: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(lib, "_PROC_ROOT", str(tmp_path / "no-proc"))
        monkeypatch.setattr(lib, "_PS", str(tmp_path / "no-ps"))
        _run_ai(lib, tmp_path, _EMIT_BY_AGE)

        value = _finalize(lib, tmp_path)

        assert value["process_scan"] == "unavailable"
        assert "leftover_processes" not in value

    def test_the_proc_table_is_read(self, lib: ModuleType, tmp_path: Path) -> None:
        import os

        proc = tmp_path / "proc"
        for pid, stat, cmd, cwd in [
            (os.getpid(), f"{os.getpid()} (py) R 1 7 7 0", b"python\0", "/"),
            (40, "40 (python3 (x)) S 1 40 40 0 1 2", b"python\0-c\0x\0", "/w/a"),
            (41, "41 (sh) Z 40 40 40 0", b"", "/"),
        ]:
            d = proc / str(pid)
            d.mkdir(parents=True)
            (d / "stat").write_text(stat)
            (d / "cmdline").write_bytes(cmd)
            os.symlink(cwd, d / "cwd")
        (proc / "self").mkdir()
        (proc / "41" / "cwd").unlink()

        table = lib._proc_table(str(proc))

        assert table is not None and set(table) == {os.getpid(), 40, 41}
        assert table[40] == {
            "ppid": 1,
            "pgid": 40,
            "sid": 40,
            "cwd": "/w/a",
            "cmdline": "python -c x",
            "zombie": False,
        }
        assert table[41]["zombie"] is True and table[41]["cwd"] == ""

    def test_a_proc_table_without_this_process_is_unavailable(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        (tmp_path / "proc").mkdir()

        assert lib._proc_table(str(tmp_path / "proc")) is None

    def test_what_counts_as_left_by_the_code(self, lib: ModuleType) -> None:
        root = "/s/work/ai_work"

        def proc(ppid: int, pgid: int, sid: int, cwd: str = "/", cmd: str = "x"):
            return {
                "ppid": ppid,
                "pgid": pgid,
                "sid": sid,
                "cwd": cwd,
                "cmdline": cmd,
                "zombie": False,
            }

        table = {
            1: proc(0, 1, 1),
            10: proc(1, 10, 10),  # the command server
            11: proc(10, 11, 11),  # the check itself
            20: proc(1, 21, 20),  # same session as a recorded run
            30: proc(1, 30, 31),  # same group as a recorded run
            40: proc(1, 40, 40, cwd=root + "/t9"),  # works inside ai_work
            50: proc(1, 50, 50, cmd=f"python -c b {root}/t9 x"),  # its command
            60: proc(55, 60, 60),  # a descendant of a recorded run
            55: proc(1, 56, 56, cmd="sleep"),
            70: proc(1, 70, 70, cwd="/s/work/ai_workx"),  # a look-alike folder
            80: {**proc(1, 20, 20), "zombie": True},  # already dead
            90: proc(1, 90, 90),  # something else
        }

        found = lib._find_leftovers(table, {20, 30, 55, 11, 1}, root, self_pid=11)

        assert sorted(found) == [20, 30, 40, 50, 55, 60]


class TestPlaybookFunctions:
    def test_key_overlap_counts_without_values(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        result = lib.dispatch(
            "key_overlap",
            {"axis": "cust", "other": "buys", "columns": ["customer_id", "missing"]},
            str(tmp_path),
        )

        rows = {r["column"]: r for r in result["value"]["columns"]}
        # the smaller table (30 customers): 24 of their IDs are among the buyers
        assert rows["customer_id"]["match_rate"] == 0.8
        assert rows["customer_id"]["axis_matched"] == 24
        assert rows["missing"]["match_rate"] == 0.0
        assert "C001" not in json.dumps(result)

    def test_join_tables_keeps_everyone_and_marks_no_match(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        code = (
            'df = join_tables("source_1", "customer_id", [{"source": "source_2",'
            ' "aggregations": [{"column": "amount", "func": "sum"},'
            ' {"column": "bought_on", "func": "last"}]}])\n'
            "import json\n"
            "print(json.dumps({'rows': len(df), 'cols': list(df.columns),"
            " 'unmatched': int((~df['source_2_matched']).sum()),"
            " 'zero': int((df.loc[~df['source_2_matched'], 'source_2_rows'] == 0).all()),"
            " 'joined': df.attrs['joined_columns']}))\n"
            'df["seg"] = ["buyer" if m else "none" for m in df["source_2_matched"]]\n'
            'emit_result(df, "seg", "join:source_1+source_2", id_column="customer_id")\n'
        )
        value = _run_ai(lib, tmp_path, code)["value"]

        printed = json.loads(value["stdout"].strip().splitlines()[-1])
        assert printed["rows"] == 30 and printed["unmatched"] == 6
        assert printed["zero"] == 1
        assert "source_2_amount_sum" in printed["cols"]
        assert printed["joined"][0] == "customer_id"
        result = _finalize(lib, tmp_path)["result"]
        assert result["population"] == 30
        assert result["population_source"] == "join:source_1+source_2"
        assert "source_2_amount_sum" in result["joined_columns"]

    def test_the_join_is_recorded_even_when_the_frame_lost_its_attrs(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        code = (
            'df = join_tables("source_1", "customer_id", [{"source": "source_2",'
            ' "aggregations": [{"column": "amount", "func": "sum"}]}])\n'
            "import pandas as pd\n"
            "# a frame built anew has no attrs\n"
            "fresh = pd.DataFrame({c: df[c].tolist() for c in df.columns})\n"
            'fresh["seg"] = ["a" if i % 2 else "b" for i in range(len(fresh))]\n'
            'emit_result(fresh, "seg", "join:source_1+source_2", id_column="customer_id")\n'
        )
        _run_ai(lib, tmp_path, code)

        result = _finalize(lib, tmp_path)["result"]

        assert result["joined_columns"][0] == "customer_id"
        assert "source_2_amount_sum" in result["joined_columns"]

    def test_joined_names_keep_japanese_and_never_collide(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        """Only characters unusable in a name become ``_``."""
        import duckdb

        axis, other = str(tmp_path / "axis.parquet"), str(tmp_path / "other.parquet")
        con = duckdb.connect()
        con.execute(
            f"COPY (SELECT * FROM (VALUES (1, 'a'), (2, 'b')) t(顧客ID, 名前)) "
            f"TO '{axis}' (FORMAT parquet)"
        )
        con.execute(
            "COPY (SELECT * FROM (VALUES (1, 10, 20, 30)) "
            f't(顧客ID, "購入 金額", "購入-金額", 単価)) TO \'{other}\' (FORMAT parquet)'
        )
        con.close()

        frame = lib.join_tables(
            "source_1",
            "顧客ID",
            [
                {
                    "source": "source_2",
                    "aggregations": [
                        {"column": "単価", "func": "sum"},
                        {"column": "購入 金額", "func": "sum"},
                        {"column": "購入-金額", "func": "sum"},
                    ],
                }
            ],
            paths={"source_1": axis, "source_2": other},
        )

        joined = frame.attrs["joined_columns"]
        assert joined[1:4] == [
            "source_2_単価_sum",
            "source_2_購入_金額_sum",
            "source_2_購入_金額_sum_2",
        ]
        assert len(set(frame.columns)) == len(frame.columns)
        assert frame["source_2_購入_金額_sum_2"].tolist()[0] == 20

    def test_names_differing_only_in_case_never_collide(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        """Code review: DuckDB names are case-insensitive."""
        import duckdb

        axis, other = str(tmp_path / "axis.parquet"), str(tmp_path / "other.parquet")
        con = duckdb.connect()
        con.execute(
            "COPY (SELECT * FROM (VALUES (1, 5)) t(id, SOURCE_2_AMOUNT_SUM)) "
            f"TO '{axis}' (FORMAT parquet)"
        )
        con.execute(
            "COPY (SELECT * FROM (VALUES (1, 10)) t(id, amount)) "
            f"TO '{other}' (FORMAT parquet)"
        )
        con.close()

        frame = lib.join_tables(
            "source_1",
            "id",
            [
                {
                    "source": "source_2",
                    "aggregations": [{"column": "amount", "func": "sum"}],
                }
            ],
            paths={"source_1": axis, "source_2": other},
        )

        lowered = [c.lower() for c in frame.columns]
        assert len(set(lowered)) == len(lowered)
        assert frame.attrs["joined_columns"][1] == "source_2_amount_sum_2"

    def test_compare_groups_tests_numbers_and_categories(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        code = (
            "import json\n"
            'df = read_table("source_1")\n'
            'df["grp"] = ["a" if a < 35 else "b" for a in df["age"]]\n'
            'stats = compare_groups(df, "grp", ["age", "plan"])\n'
            "print(json.dumps(stats))\n"
        )
        value = _run_ai(lib, tmp_path, code)["value"]

        stats = {s["column"]: s for s in json.loads(value["stdout"].splitlines()[-1])}
        assert stats["age"]["name"] == "welch_t"
        assert stats["age"]["p_value"] < 0.001 and abs(stats["age"]["effect_size"]) > 1
        assert stats["plan"]["name"] == "chi_square"
        assert 0 <= stats["plan"]["p_value"] <= 1

    def test_rfm_gives_people_with_the_same_behaviour_the_same_scores(
        self, lib: ModuleType
    ) -> None:
        import pandas as pd

        rows = [{"id": f"C{i:02d}", "d": "2026-03-01", "a": 1000} for i in range(20)]

        out = lib.rfm(pd.DataFrame(rows), "id", "d", "a")

        for column in ("r_score", "f_score", "m_score"):
            assert out[column].nunique() == 1

    def test_rfm_scores_follow_the_values_and_ties_share_a_score(
        self, lib: ModuleType
    ) -> None:
        import pandas as pd

        rows = []
        for i in range(10):
            # persons 0-4 buy once for 100, persons 5-9 buy 3 times for 900
            times = 1 if i < 5 else 3
            for n in range(times):
                rows.append(
                    {
                        "id": f"C{i}",
                        "d": f"2026-0{1 + n}-15",
                        "a": 100 if i < 5 else 300,
                    }
                )

        out = lib.rfm(pd.DataFrame(rows), "id", "d", "a", as_of="2026-12-31")
        by_id = out.set_index("id")

        low = by_id.loc[[f"C{i}" for i in range(5)]]
        high = by_id.loc[[f"C{i}" for i in range(5, 10)]]
        for column in ("f_score", "m_score"):
            assert low[column].nunique() == 1 and high[column].nunique() == 1
            assert high[column].iloc[0] > low[column].iloc[0]

    def test_rfm_spreads_distinct_values_over_the_scores(self, lib: ModuleType) -> None:
        import pandas as pd

        rows = [
            {"id": f"C{i:02d}", "d": f"2026-01-{1 + i:02d}", "a": 100 * (i + 1)}
            for i in range(10)
        ]

        out = lib.rfm(pd.DataFrame(rows), "id", "d", "a", as_of="2026-12-31")

        assert sorted(set(out["m_score"])) == [1, 2, 3, 4, 5]
        assert sorted(set(out["r_score"])) == [1, 2, 3, 4, 5]

    def test_rfm_scores_each_person(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        code = (
            "import json\n"
            'df = rfm(read_table("source_2"), "customer_id", "bought_on", "amount",'
            ' as_of="2026-12-31")\n'
            "print(json.dumps({'rows': len(df), 'cols': sorted(df.columns),"
            " 'scores': sorted(set(df['r_score']))}))\n"
        )
        value = _run_ai(lib, tmp_path, code)["value"]

        printed = json.loads(value["stdout"].splitlines()[-1])
        assert printed["rows"] == 24
        assert {"recency", "frequency", "monetary", "r_score"} <= set(printed["cols"])
        assert set(printed["scores"]) <= {1, 2, 3, 4, 5}


class TestKeyNames:
    """A column named like a key is no trait; one that merely holds the letters is."""

    @pytest.mark.parametrize(
        "name",
        [
            "id",
            "customer_id",
            "customerId",
            "顧客ID",
            "zip",
            "ZIP",
            "postal_code",
            "price_no",
            "郵便番号",
            "コード",
        ],
    )
    def test_keys_are_keys(self, lib: ModuleType, name: str) -> None:
        assert lib._KEY_NAME.search(name)

    @pytest.mark.parametrize(
        "name",
        [
            "paid",
            "is_valid",
            "android",
            "valid",
            "residence",
            "video",
            "NOTE",
            "number_of_visits",
        ],
    )
    def test_names_that_only_hold_the_letters_are_not_keys(
        self, lib: ModuleType, name: str
    ) -> None:
        assert not lib._KEY_NAME.search(name)


class TestDefaultMetrics:
    def _emit(self, lib: ModuleType, tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
        import json

        import pandas as pd

        frame = pd.DataFrame(
            {"pid": [f"p{i}" for i in range(4)], "seg": ["a", "a", "b", "b"]}
            | {f"c{i}": [float(i + n) for n in range(4)] for i in range(15)}
        )
        lib.emit_result(
            frame, "seg", "source_1", id_column="pid", out_dir=str(tmp_path), **kwargs
        )
        return json.loads((tmp_path / lib._META_FILE).read_text(encoding="utf-8"))

    def test_left_out_every_numeric_column_is_kept_for_the_check_to_sort(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        meta = self._emit(lib, tmp_path)

        assert len(meta["metrics"]) == 15

    def test_named_metrics_stay_within_the_cap(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        meta = self._emit(lib, tmp_path, metrics=[f"c{i}" for i in range(15)])

        assert len(meta["metrics"]) == 15


class TestPersonProfile:
    """One row per person from any history table."""

    @staticmethod
    def _history() -> Any:
        import pandas as pd

        return pd.DataFrame(
            {
                "row_no": [f"r{i}" for i in range(7)],  # a key: all different
                "who": ["a", "a", "a", "b", "b", "c", None],
                "at": pd.to_datetime(
                    [
                        "2025-01-04 09:00",  # Saturday morning
                        "2025-01-06 19:30",
                        "2025-01-06 20:10",
                        "2025-01-02 12:00",
                        "2025-01-03 12:00",
                        "2024-12-01 08:00",
                        "2025-01-05 10:00",
                    ]
                ),
                "amount": [100, 200, 300, 50, 150, 400, 999],
                "channel": ["web", "web", "shop", "shop", "shop", "web", "web"],
                "memo": [f"m{i}" for i in range(7)],
            }
        )

    def test_a_row_per_person_with_counts_dates_and_numbers(
        self, lib: ModuleType
    ) -> None:
        got = lib.person_profile(self._history(), "who").set_index("who")

        assert list(got.index) == ["a", "b", "c"]  # a row without a person is left out
        assert got.loc["a", "回数（行）"] == 3
        assert (
            got.loc["a", "amountの合計"] == 600 and got.loc["a", "amountの平均"] == 200
        )
        # as of the table's last date (2025-01-06 20:10)
        assert (
            got.loc["a", "最後からの日数"] == 0 and got.loc["c", "最後からの日数"] == 36
        )
        assert got.loc["a", "活動日数"] == 2  # two different days
        assert got.loc["a", "土日の割合"] == round(1 / 3, 4)

    def test_time_of_day_and_category_shares_come_from_the_data(
        self, lib: ModuleType
    ) -> None:
        got = lib.person_profile(self._history(), "who").set_index("who")

        assert got.loc["a", "6〜11時の割合"] == round(1 / 3, 4)
        assert got.loc["a", "18〜23時の割合"] == round(2 / 3, 4)
        assert got.loc["a", "channel=webの割合"] == round(2 / 3, 4)
        assert got.loc["b", "channel=shopの割合"] == 1.0

    def test_keys_and_free_text_are_not_features(self, lib: ModuleType) -> None:
        columns = " ".join(lib.person_profile(self._history(), "who").columns)

        assert "row_no" not in columns and "memo" not in columns

    def test_no_column_name_is_assumed(self, lib: ModuleType) -> None:
        frame = self._history().rename(
            columns={
                "who": "ユーザー",
                "at": "日時",
                "amount": "利用分",
                "channel": "機能",
            }
        )

        got = lib.person_profile(frame, "ユーザー")

        assert "利用分の合計" in got.columns and "機能=webの割合" in got.columns
        assert "最後からの日数" in got.columns

    def test_a_table_without_dates_still_gives_counts_and_shares(
        self, lib: ModuleType
    ) -> None:
        frame = self._history().drop(columns=["at"])

        got = lib.person_profile(frame, "who")

        assert "最後からの日数" not in got.columns and "回数（行）" in got.columns

    def test_the_columns_the_user_left_out_are_not_features(
        self, lib: ModuleType
    ) -> None:
        got = lib.person_profile(self._history(), "who", skip=["amount"])

        assert not any(c.startswith("amount") for c in got.columns)

    def test_an_unknown_id_column_is_an_error_of_the_code(
        self, lib: ModuleType
    ) -> None:
        with pytest.raises(KeyError):
            lib.person_profile(self._history(), "nobody")

    def test_the_profile_reaches_the_features_through_metrics(
        self, lib: ModuleType, tmp_path: Path, tables: dict[str, Any]
    ) -> None:
        code = (
            'buys = read_table("source_2")\n'
            'prof = person_profile(buys, "customer_id")\n'
            'df = read_table("source_1").merge(prof, on="customer_id", how="left")\n'
            'df["seg"] = ["many" if (n or 0) >= 3 else "few" for n in df["回数（行）"]]\n'
            'emit_result(df, "seg", "source_1", id_column="customer_id",'
            ' metrics=[c for c in prof.columns if c != "customer_id"])\n'
        )
        _run_ai(lib, tmp_path, code)

        result = _finalize(lib, tmp_path)["result"]

        names = {
            f["name"]
            for s in result["segments"]
            for f in s["features"]
            if f["kind"] == "derived"
        }
        assert "回数（行）" in names and any("合計" in n for n in names)
