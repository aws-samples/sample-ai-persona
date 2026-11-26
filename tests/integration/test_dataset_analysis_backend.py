"""Tests for DuckDBQueryBackend access allowlist, local execution and soft timeout."""

import os
import threading
import time
from unittest.mock import patch

import pytest

from src.services.dataset_analysis.query_backend import (
    DatasetAccessError,
    DatasetQueryTimeout,
    DuckDBQueryBackend,
)

pytestmark = pytest.mark.integration


class TestAccessAllowlist:
    def _backend(self, tmp_path):
        return DuckDBQueryBackend(
            bucket_name="my-bucket",
            allowed_s3_prefixes=["datasets/"],
            allowed_local_roots=[str(tmp_path)],
        )

    def test_s3_wrong_bucket_rejected(self, tmp_path):
        b = self._backend(tmp_path)
        with pytest.raises(DatasetAccessError):
            b._resolve("s3://other-bucket/datasets/x.csv")

    def test_s3_wrong_prefix_rejected(self, tmp_path):
        b = self._backend(tmp_path)
        with pytest.raises(DatasetAccessError):
            b._resolve("s3://my-bucket/secret/x.csv")

    def test_s3_dotdot_rejected(self, tmp_path):
        b = self._backend(tmp_path)
        with pytest.raises(DatasetAccessError):
            b._resolve("s3://my-bucket/datasets/../secret.csv")

    def test_s3_allowed(self, tmp_path):
        b = self._backend(tmp_path)
        spec = b._resolve("s3://my-bucket/datasets/x.csv")
        assert spec.is_s3 is True

    def test_local_outside_root_rejected(self, tmp_path):
        b = self._backend(tmp_path)
        with pytest.raises(DatasetAccessError):
            b._resolve("local:///etc/passwd")

    def test_local_dotdot_escape_rejected(self, tmp_path):
        b = self._backend(tmp_path)
        with pytest.raises(DatasetAccessError):
            b._resolve(f"local://{tmp_path}/../escape.csv")

    def test_local_japanese_space_path_accepted(self, tmp_path):
        b = self._backend(tmp_path)
        p = tmp_path / "購買 データ.csv"
        p.write_text("a\n1\n")
        spec = b._resolve(f"local://{p}")
        assert spec.is_s3 is False

    def test_default_roots_include_tmp_and_datasets(self):
        b = DuckDBQueryBackend(bucket_name="b")
        # the realpaths of /tmp and datasets/ are allowed
        assert os.path.realpath("/tmp") in b._allowed_local_roots
        assert os.path.realpath("datasets") in b._allowed_local_roots


class TestLocalExecution:
    def test_local_csv_executes_without_httpfs(self, tmp_path):
        csv = tmp_path / "data.csv"
        csv.write_text("region,amount\neast,10\nwest,20\neast,5\n")
        backend = DuckDBQueryBackend(
            bucket_name="b", allowed_local_roots=[str(tmp_path)]
        )
        # LOAD httpfs is not called for local paths (so it works where it is not installed)
        columns, rows = backend.execute(
            f"local://{csv}",
            "SELECT region, sum(amount) AS m0 FROM dataset GROUP BY region ORDER BY region",
            [],
            timeout=10.0,
        )
        assert columns == ["region", "m0"]
        result = {r[0]: r[1] for r in rows}
        assert result["east"] == 15
        assert result["west"] == 20

    def test_parameterized_filter(self, tmp_path):
        csv = tmp_path / "data.csv"
        csv.write_text("region,amount\neast,10\nwest,20\n")
        backend = DuckDBQueryBackend(
            bucket_name="b", allowed_local_roots=[str(tmp_path)]
        )
        columns, rows = backend.execute(
            f"local://{csv}",
            "SELECT amount FROM dataset WHERE region = $1",
            ["east"],
            timeout=10.0,
        )
        assert rows == [[10]]

    def test_numeric_first_row_treated_as_header(self, tmp_path):
        """With header=true fixed, an all-numeric first row is still treated as the header.

        Without it, DuckDB wrongly decides there is no header and assigns column0.., which
        diverges from the allowlist column names taken from schema analysis (csv.reader).
        """
        csv = tmp_path / "data.csv"
        csv.write_text("1001,100,5\n1002,200,6\n")
        backend = DuckDBQueryBackend(
            bucket_name="b", allowed_local_roots=[str(tmp_path)]
        )
        # SELECT works with the column names csv.reader takes (the first row), so both sets of names match
        columns, rows = backend.execute(
            f"local://{csv}", 'SELECT "1001" FROM dataset', [], timeout=10.0
        )
        assert columns == ["1001"]
        assert rows == [[1002]]


class _FakeConn:
    """A fake DuckDB connection whose execute blocks and is released by interrupt."""

    def __init__(self, release_event, started_event):
        self._release = release_event
        self._started = started_event
        self.closed = False

    def execute(self, sql, params=None):
        if sql.strip().upper().startswith("SELECT"):
            self._started.set()
            # block until interrupt (release_event)
            self._release.wait(timeout=5.0)
            raise RuntimeError("interrupted")
        return self

    @property
    def description(self):
        return [("c",)]

    def fetchall(self):
        return []

    def interrupt(self):
        self._release.set()

    def close(self):
        self.closed = True


class TestSoftTimeout:
    def test_timeout_interrupts_and_returns_bounded(self, tmp_path):
        csv = tmp_path / "d.csv"
        csv.write_text("c\n1\n")
        backend = DuckDBQueryBackend(
            bucket_name="b",
            allowed_local_roots=[str(tmp_path)],
            max_concurrent_queries=1,
            join_grace_seconds=1.0,
        )
        release = threading.Event()
        started = threading.Event()
        fake = _FakeConn(release, started)

        with patch("duckdb.connect", return_value=fake):
            start = time.monotonic()
            with pytest.raises(DatasetQueryTimeout):
                backend.execute(
                    f"local://{csv}", "SELECT * FROM dataset", [], timeout=0.3
                )
            elapsed = time.monotonic() - start

        # the request returns in bounded time (about timeout + join grace)
        assert elapsed < 3.0
        # the watchdog calls interrupt, the worker ends and the connection is closed
        release.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while not fake.closed and time.monotonic() < deadline:
            time.sleep(0.02)
        assert fake.closed is True

    def test_semaphore_released_by_worker_after_timeout(self, tmp_path):
        csv = tmp_path / "d.csv"
        csv.write_text("c\n1\n")
        backend = DuckDBQueryBackend(
            bucket_name="b",
            allowed_local_roots=[str(tmp_path)],
            max_concurrent_queries=1,
            join_grace_seconds=1.0,
        )
        release = threading.Event()
        started = threading.Event()
        fake = _FakeConn(release, started)

        with patch("duckdb.connect", return_value=fake):
            with pytest.raises(DatasetQueryTimeout):
                backend.execute(
                    f"local://{csv}", "SELECT * FROM dataset", [], timeout=0.3
                )
            # wait until the worker ends and releases the Semaphore
            deadline = time.monotonic() + 3.0
            acquired = False
            while time.monotonic() < deadline:
                if backend._sem.acquire(timeout=0.1):
                    acquired = True
                    backend._sem.release()
                    break
            assert acquired is True

    def test_semaphore_released_when_thread_start_fails(self, tmp_path):
        """A failure of thread.start() must not leak the Semaphore slot.

        If acquired_by_worker is set before start(), the worker's finally never runs and the
        outer finally skips the release, so the slot is lost for good (regression guard).
        """
        csv = tmp_path / "d.csv"
        csv.write_text("c\n1\n")
        backend = DuckDBQueryBackend(
            bucket_name="b",
            allowed_local_roots=[str(tmp_path)],
            max_concurrent_queries=1,
        )
        with patch.object(
            threading.Thread,
            "start",
            side_effect=RuntimeError("can't start new thread"),
        ):
            with pytest.raises(RuntimeError):
                backend.execute(
                    f"local://{csv}", "SELECT * FROM dataset", [], timeout=1.0
                )
        # the slot is released, so a later acquire succeeds immediately
        assert backend._sem.acquire(timeout=0.1) is True
        backend._sem.release()


class TestDefaultS3Prefixes:
    """The default allowlist covers every place a registered dataset lives."""

    def test_persona_dedicated_dataset_accepted(self):
        # PersonaManager maps the dedicated dataset of behavior data onto this prefix
        from src.managers.persona_manager import DEDICATED_DATASET_PREFIX

        b = DuckDBQueryBackend(bucket_name="b")
        spec = b._resolve(f"s3://b/{DEDICATED_DATASET_PREFIX}/p-1/d-1.csv")
        assert spec.is_s3 is True

    @pytest.mark.parametrize("key", ["datasets/x.csv", "persona-dataset/x.parquet"])
    def test_existing_prefixes_still_accepted(self, key):
        b = DuckDBQueryBackend(bucket_name="b")
        assert b._resolve(f"s3://b/{key}").is_s3 is True

    @pytest.mark.parametrize(
        "key",
        [
            "uploads/u/document/x.csv",
            "candidates/u/c/x.csv",
            "persona_datasets_x/a.csv",
        ],
    )
    def test_other_prefixes_rejected(self, key):
        b = DuckDBQueryBackend(bucket_name="b")
        with pytest.raises(DatasetAccessError):
            b._resolve(f"s3://b/{key}")
