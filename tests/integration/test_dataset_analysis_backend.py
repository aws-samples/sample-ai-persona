"""DuckDBQueryBackend の access allowlist・ローカル実行・soft timeout テスト。"""

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
        # /tmp と datasets/ の realpath が許可されていること
        assert os.path.realpath("/tmp") in b._allowed_local_roots
        assert os.path.realpath("datasets") in b._allowed_local_roots


class TestLocalExecution:
    def test_local_csv_executes_without_httpfs(self, tmp_path):
        csv = tmp_path / "data.csv"
        csv.write_text("region,amount\neast,10\nwest,20\neast,5\n")
        backend = DuckDBQueryBackend(
            bucket_name="b", allowed_local_roots=[str(tmp_path)]
        )
        # ローカルパスでは LOAD httpfs を呼ばない（未導入環境でも成功する）。
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


class _FakeConn:
    """execute が block し、interrupt で解放される擬似 DuckDB 接続。"""

    def __init__(self, release_event, started_event):
        self._release = release_event
        self._started = started_event
        self.closed = False

    def execute(self, sql, params=None):
        if sql.strip().upper().startswith("SELECT"):
            self._started.set()
            # interrupt（release_event）まで block
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

        # request は有界時間で戻る（timeout + join grace 程度）。
        assert elapsed < 3.0
        # watchdog が interrupt を呼び、worker が終了して connection を close する。
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
            # worker が終了して Semaphore を解放するまで待つ。
            deadline = time.monotonic() + 3.0
            acquired = False
            while time.monotonic() < deadline:
                if backend._sem.acquire(timeout=0.1):
                    acquired = True
                    backend._sem.release()
                    break
            assert acquired is True
