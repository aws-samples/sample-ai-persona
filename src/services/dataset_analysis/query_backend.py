"""DuckDB query backend for the analyze_dataset tool.

Responsibility: turn a dataset's backend path (S3 / local) into a VIEW, run parameterized SQL
and return ``(columns, rows)``. The access allowlist, httpfs LOAD (S3 only),
S3 credential setup and the soft timeout (interrupt and cleanup) all stay in this layer.

**The timeout is soft**: a Python thread cannot be killed, and DuckDB ``close()`` returns before the worker
ends. So "wait for the worker to end" and "never stall indefinitely" cannot both hold with threads.
This backend guarantees a bounded return time as follows:

1. The sync query runs on a dedicated worker, and a watchdog calls ``interrupt()`` after the timeout.
2. The request thread waits up to the timeout plus a short join grace, then returns (no indefinite block).
3. If the worker survives the interrupt, the connection is not force-closed; it is left to the worker's own exit,
   and the Semaphore slot is not released until the worker really ends
   (a lingering worker keeps holding its slot, so real concurrency never exceeds the limit).

A hard wall-clock timeout would need process isolation, which is out of scope.
"""

import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)


class DatasetAccessError(ValueError):
    """Raised when a backend path does not pass the access allowlist."""


class DatasetQueryTimeout(Exception):
    """Raised when a query did not finish within the soft timeout."""


@dataclass(frozen=True)
class _ViewSpec:
    """A validated read target. ``path`` is the normalized path used for the VIEW."""

    path: str
    is_s3: bool


class DatasetQueryBackend(Protocol):
    """Execution backend for analyze_dataset. A seam for swapping in Athena etc. later.

    Timeout, interruption and cleanup are the backend's job. Callers must not assume they
    can interrupt the connection directly.
    """

    def execute(
        self,
        source: str,
        sql: str,
        params: List[Any],
        *,
        timeout: float,
    ) -> Tuple[List[str], List[List[Any]]]:
        """Turn the source into a VIEW and run the SQL. Returns (columns, rows).

        Raises ``DatasetQueryTimeout`` when the timeout is exceeded.
        """
        raise NotImplementedError


class DuckDBQueryBackend:
    """DuckDB implementation of :class:`DatasetQueryBackend`."""

    def __init__(
        self,
        bucket_name: str = "",
        region_name: str = "us-east-1",
        *,
        allowed_s3_prefixes: Optional[List[str]] = None,
        allowed_local_roots: Optional[List[str]] = None,
        max_concurrent_queries: int = 4,
        join_grace_seconds: float = 1.0,
    ) -> None:
        self.bucket_name = bucket_name
        self.region_name = region_name
        self.allowed_s3_prefixes = allowed_s3_prefixes or [
            "datasets/",
            "persona-dataset/",
            "persona_datasets/",  # Persona-only datasets (behavior data)
        ]
        roots = allowed_local_roots or [
            tempfile.gettempdir(),
            "/tmp",  # save_temp_csv writes here directly
            "datasets",  # Real location behind the local:// fallback (dataset_manager)
        ]
        # Normalize with realpath (absorbs differences such as /tmp -> /private/tmp).
        self._allowed_local_roots = sorted({os.path.realpath(r) for r in roots})
        self._sem = threading.BoundedSemaphore(max_concurrent_queries)
        self._join_grace = join_grace_seconds

    # ------------------------------------------------------------------
    # access allowlist
    # ------------------------------------------------------------------

    def _resolve(self, source: str) -> _ViewSpec:
        """Validate the backend path and return a normalized ``_ViewSpec``."""
        if source.startswith("s3://"):
            prefix = f"s3://{self.bucket_name}/"
            if not self.bucket_name or not source.startswith(prefix):
                raise DatasetAccessError("s3 path is not under the configured bucket")
            key = source[len(prefix) :]
            if ".." in key:
                raise DatasetAccessError("s3 key must not contain '..'")
            if not any(key.startswith(p) for p in self.allowed_s3_prefixes):
                raise DatasetAccessError("s3 key is not under an allowed prefix")
            return _ViewSpec(path=source, is_s3=True)

        local = source[len("local://") :] if source.startswith("local://") else source
        real = os.path.realpath(local)
        if not any(
            real == root or real.startswith(root + os.sep)
            for root in self._allowed_local_roots
        ):
            raise DatasetAccessError("local path is outside allowed roots")
        return _ViewSpec(path=real, is_s3=False)

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    def execute(
        self,
        source: str,
        sql: str,
        params: List[Any],
        *,
        timeout: float,
    ) -> Tuple[List[str], List[List[Any]]]:
        view_spec = self._resolve(source)

        import time

        start = time.monotonic()
        if not self._sem.acquire(timeout=timeout):
            raise DatasetQueryTimeout("timed out waiting for a query slot")
        # The worker's finally releases the Semaphore (a lingering worker keeps holding its slot).
        acquired_by_worker = False
        try:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise DatasetQueryTimeout("query budget exhausted before execution")

            holder: dict[str, Any] = {}
            conn_box: dict[str, Any] = {}
            done = threading.Event()

            def worker() -> None:
                conn = None
                try:
                    import duckdb

                    conn = duckdb.connect(":memory:")
                    conn_box["conn"] = conn
                    self._setup_view(conn, view_spec)
                    if params:
                        rel = conn.execute(
                            sql, params
                        )  # nosemgrep: sqlalchemy-execute-raw-query
                    else:
                        rel = conn.execute(
                            sql
                        )  # nosemgrep: sqlalchemy-execute-raw-query
                    columns = [d[0] for d in rel.description]
                    rows = [list(r) for r in rel.fetchall()]
                    holder["result"] = (columns, rows)
                except Exception as e:  # noqa: BLE001 - handed over to the request side
                    holder["error"] = e
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:  # noqa: BLE001 - best effort
                            pass
                    self._sem.release()
                    done.set()

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            # After start() succeeds, the worker's finally is the only releaser. If start()
            # fails with an exception the flag stays False, so the outer finally releases it
            # (setting True before start() would leak the slot permanently).
            acquired_by_worker = True

            if not done.wait(timeout=remaining):
                # watchdog: timeout. Interrupts the running query.
                conn = conn_box.get("conn")
                if conn is not None:
                    try:
                        conn.interrupt()
                    except Exception:  # noqa: BLE001 - best effort
                        pass
                # Even if the interrupt works and the worker ends within bounded time, the request
                # treats it as a timeout (a success at the boundary is discarded). The worker's finally
                # closes the connection and releases the Semaphore (while it lingers it keeps its slot).
                done.wait(timeout=self._join_grace)
                raise DatasetQueryTimeout("query exceeded timeout")

            if "error" in holder:
                raise holder["error"]
            columns, rows = holder["result"]
            return columns, rows
        finally:
            # On normal paths (early return / exception before the worker starts) the request releases it.
            # Once the worker has started, the worker's finally is the only releaser.
            if not acquired_by_worker:
                self._sem.release()

    def _setup_view(self, conn: Any, view_spec: _ViewSpec) -> None:
        conn.execute("SET autoinstall_known_extensions=false;")
        conn.execute("SET autoload_known_extensions=false;")
        if view_spec.is_s3:
            # httpfs is assumed to be pre-INSTALLed in both container and local (the runtime
            # does not download at run time; in a closed network a missing one fails fast at LOAD).
            # For the local pre-INSTALL steps see docs/local_development.md.
            conn.execute("LOAD httpfs;")  # nosemgrep: sqlalchemy-execute-raw-query
            self._set_s3_credentials(conn)
        escaped = view_spec.path.replace("'", "''")
        if view_spec.path.endswith(".parquet"):
            source = f"read_parquet('{escaped}')"
        else:
            # header=true always treats the first row as the header (prevents DuckDB from taking an all-numeric
            # first row as data and assigning column0.., which would diverge from the allowlist column names
            # derived from schema parsing (csv.reader)).
            source = f"read_csv_auto('{escaped}', header=true)"
        conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query
            f"CREATE VIEW dataset AS SELECT * FROM {source};"
        )

    def _set_s3_credentials(self, conn: Any) -> None:
        """Set S3 credentials on the DuckDB connection from the boto3 credential chain."""
        import boto3

        session = boto3.Session()
        credentials = session.get_credentials()
        if credentials:
            creds = credentials.get_frozen_credentials()
            conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query
                "SET s3_access_key_id = $1;", [creds.access_key]
            )  # gitleaks:allow
            conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query
                "SET s3_secret_access_key = $1;", [creds.secret_key]
            )  # gitleaks:allow
            if creds.token:
                conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query
                    "SET s3_session_token = $1;", [creds.token]
                )  # gitleaks:allow
        conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query
            "SET s3_region = $1;", [self.region_name]
        )
