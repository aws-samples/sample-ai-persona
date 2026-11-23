"""PersonaAnalysisLibrary (``sandbox/persona_analysis.py``) run in process.

The library runs inside a Code Interpreter session in production, so the app
never imports it. These tests load it from its file path and call the same
functions in this process (NFR9.3). They cover the preparation (BR4.2〜BR4.4),
the profile, the clustering (BR5.3), the query used by ``analyze_dataset`` and
the download that must not print the URL (security-design.md §3).
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
        # The head is ASCII only (undetermined); a later line is CP932 (BR4.3).
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
    """BR16.1 and BR16.2: stop above 1% of the rows seen, with line numbers."""

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
    """BR17.1: the failure says how far it got and how many columns there are."""

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
        """Code Interpreter の stdout は長い出力の多バイト文字を壊すので、結果は ASCII だけで出す。"""
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
# U4: documents (BR24.1〜BR24.6, BR27.1, NFR1.20, NFR1.22, NFR5.39)
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
# U6: AI-written code, the check and the playbook functions
# (BR36.3, BR36.6, BR37.3, BR37.4, BR39.2, NFR3.35)
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
        assert written.column_names == ["person_id", "segment"]

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
        assert result["segments"] == [{"segment_key": "a", "size": 30, "features": []}]

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

    def test_joined_names_keep_japanese_and_never_collide(
        self, lib: ModuleType, tmp_path: Path
    ) -> None:
        """U6 addendum: only characters unusable in a name become ``_``."""
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
