"""SandboxQueryBackend: the ``DatasetQueryBackend`` seam run in the session (C9).

The SQL is built here by ``sql_builder.build_analyze_query`` (ADR-005) and run
by the session's duckdb through ``run_query``. The in-process sandbox stands in
for Code Interpreter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from src.services.dataset_analysis.dataset_tools import (
    ResolvedDataset,
    create_analyze_dataset_tool,
)
from src.services.dataset_analysis.query_backend import DatasetQueryTimeout
from src.services.dataset_analysis.sandbox_backend import (
    SandboxQueryBackend,
    SandboxQueryError,
)
from src.services.dataset_analysis.sql_builder import build_analyze_query
from src.services.sandbox_service import InProcessSandbox, SandboxSession

_LIBRARY = (
    Path(__file__).resolve().parent.parent.parent / "sandbox" / "persona_analysis.py"
)
_CSV = (
    "id,age,job\n1,30,学生\n2,45,会社員\n3,70,無職\n4,33,学生\n5,52,会社員\n"
).encode("utf-8")
_COLUMNS = ["id", "age", "job"]


@pytest.fixture()
def prepared(tmp_path: Path) -> tuple[InProcessSandbox, SandboxSession]:
    sandbox = InProcessSandbox(
        library_path=str(_LIBRARY),
        root_dir=str(tmp_path),
        read_object={"k": _CSV}.__getitem__,
    )
    session = sandbox.start_session("gen1", lifetime_seconds=60)
    sandbox.put_files(session, [{"source_file_id": "f1", "s3_key": "k"}])
    assert sandbox.call(session, "prepare", {"source_id": "f1", "metadata": {}})["ok"]
    return sandbox, session


class _FakeSandbox:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(
        self, session: Any, function: str, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((function, dict(args)))
        return self.result


def test_runs_the_built_sql_in_the_session(
    prepared: tuple[InProcessSandbox, SandboxSession],
) -> None:
    sandbox, session = prepared
    backend = SandboxQueryBackend(sandbox, session, max_rows=50)
    sql, params, _ = build_analyze_query(
        view_name="dataset",
        allowed_columns=_COLUMNS,
        filters=[{"column": "age", "operator": "gte", "value": 40}],
        group_by=["job"],
        metrics=[{"func": "count"}],
        order_by=[{"column": "job", "direction": "asc"}],
    )

    columns, rows = backend.execute("f1", sql, params, timeout=10)

    assert columns == ["job", "m0"]
    assert rows == [["会社員", 2], ["無職", 1]]


def test_fits_the_analyze_dataset_tool(
    prepared: tuple[InProcessSandbox, SandboxSession],
) -> None:
    sandbox, session = prepared
    tool = create_analyze_dataset_tool(
        [ResolvedDataset("dataset_1", "f1", _COLUMNS, None)],
        SandboxQueryBackend(sandbox, session),
    )

    text = tool(dataset_id="dataset_1", metrics=[{"func": "count"}])

    assert "5" in text


def test_sends_the_limits_with_the_query() -> None:
    fake = _FakeSandbox({"ok": True, "value": {"columns": ["n"], "rows": [[1]]}})
    backend = SandboxQueryBackend(fake, object(), max_rows=7)

    backend.execute("f1", "SELECT 1 AS n FROM dataset", [3], timeout=12.0)

    assert fake.calls == [
        (
            "run_query",
            {
                "source_id": "f1",
                "sql": "SELECT 1 AS n FROM dataset",
                "params": [3],
                "max_rows": 7,
                "timeout_seconds": 12.0,
            },
        )
    ]


def test_a_timeout_is_a_dataset_timeout() -> None:
    fake = _FakeSandbox({"ok": False, "error": {"code": "QUERY_TIMEOUT"}})

    with pytest.raises(DatasetQueryTimeout):
        SandboxQueryBackend(fake, object()).execute("f1", "SELECT 1", [], timeout=1)


def test_other_failures_carry_only_the_code() -> None:
    fake = _FakeSandbox({"ok": False, "error": {"code": "QUERY_FAILED"}})

    with pytest.raises(SandboxQueryError) as raised:
        SandboxQueryBackend(fake, object()).execute("f1", "SELECT 1", [], timeout=1)

    assert raised.value.code == "QUERY_FAILED"
    assert str(raised.value) == "QUERY_FAILED"


def test_a_source_that_was_not_prepared(
    prepared: tuple[InProcessSandbox, SandboxSession],
) -> None:
    sandbox, session = prepared

    with pytest.raises(SandboxQueryError) as raised:
        SandboxQueryBackend(sandbox, session).execute(
            "f9", "SELECT * FROM dataset", [], timeout=5
        )

    assert raised.value.code == "SOURCE_UNREADABLE"
