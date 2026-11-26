"""SandboxQueryBackend: run ``analyze_dataset`` SQL in the analysis session.

The SQL is built in the app by ``sql_builder.build_analyze_query`` (column
allowlist, parameters) and sent to the session's ``run_query``, which
runs it on the view ``dataset`` over the prepared Parquet. The sandbox is
injected through a small Protocol, so this package does not import
``sandbox_service`` (no Service-to-Service imports).
"""

from __future__ import annotations

from typing import Any, List, Mapping, Protocol, Tuple

from .query_backend import DatasetQueryTimeout

#: Rows a query may return before the result is cut (``ToolLimits.max_limit``).
DEFAULT_MAX_ROWS = 200


class SandboxCaller(Protocol):
    """The part of ``SandboxService`` this backend uses."""

    def call(
        self, session: Any, function: str, args: Mapping[str, Any]
    ) -> dict[str, Any]: ...


class SandboxQueryError(Exception):
    """``run_query`` failed. The message is the library's error code only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SandboxQueryBackend:
    """:class:`DatasetQueryBackend` whose ``source`` is a prepared source ID."""

    def __init__(
        self, sandbox: SandboxCaller, session: Any, *, max_rows: int = DEFAULT_MAX_ROWS
    ) -> None:
        self._sandbox = sandbox
        self._session = session
        self._max_rows = max_rows

    def execute(
        self,
        source: str,
        sql: str,
        params: List[Any],
        *,
        timeout: float,
    ) -> Tuple[List[str], List[List[Any]]]:
        """Run ``sql`` on the prepared table ``source``; returns (columns, rows).

        Raises:
            DatasetQueryTimeout: The query ran past ``timeout`` in the session.
            SandboxQueryError: Any other failure, carrying the error code.
        """
        result = self._sandbox.call(
            self._session,
            "run_query",
            {
                "source_id": source,
                "sql": sql,
                "params": list(params),
                "max_rows": self._max_rows,
                "timeout_seconds": timeout,
            },
        )
        if not result.get("ok"):
            code = str((result.get("error") or {}).get("code") or "QUERY_FAILED")
            if code == "QUERY_TIMEOUT":
                raise DatasetQueryTimeout("query exceeded timeout in the sandbox")
            raise SandboxQueryError(code)
        value = result.get("value") or {}
        columns = [str(c) for c in value.get("columns", [])]
        rows = [list(r) for r in value.get("rows", [])]
        return columns, rows
