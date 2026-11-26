"""Concurrency safety tests for SurveyBatchService's DuckDB access (Issue #118).

The service is a singleton, and the Router queries it concurrently from a
ThreadPoolExecutor. Running execute on one cached DuckDBPyConnection from several
threads at once mixes up results, gives `IndexError` (empty result) or crashes the
process. Verifies that `_query_duckdb` issues a `cursor()` per thread to isolate execution.
"""

import threading

import polars as pl
import pytest

from src.services.survey_batch_service import SurveyBatchService


@pytest.fixture
def service_with_local_parquet(tmp_path):
    """Return a SurveyBatchService with a local Parquet registered as a VIEW (no S3 needed)."""
    parquet = tmp_path / "personas.parquet"
    pl.DataFrame({"age": range(200_000), "persona": ["p"] * 200_000}).write_parquet(
        parquet
    )

    svc = SurveyBatchService(bucket_name="b", region_name="ap-northeast-1")

    # _get_duckdb_conn sets up S3 auth, so the tests put a connection with a local Parquet
    # registered as a VIEW straight into the cache (the goal is to check concurrent execution).
    import duckdb

    conn = duckdb.connect(":memory:")
    conn.execute(
        f"CREATE VIEW personas AS SELECT * FROM read_parquet('{parquet}')"
    )  # nosemgrep: sqlalchemy-execute-raw-query
    svc._duckdb_conns["nemotron"] = conn
    return svc


@pytest.mark.unit
def test_concurrent_queries_do_not_corrupt_results(service_with_local_parquet):
    """Eight threads querying one connection at once see no mixing, empty results or exceptions.

    Before the fix (executing on the connection directly) this setup reproduced `IndexError`
    and swapped results. With cursor() isolation each thread gets the correct count.
    """
    svc = service_with_local_parquet
    errors: list[str] = []
    results: list[tuple[int, int]] = []

    def worker(threshold: int) -> None:
        try:
            for _ in range(30):
                df = svc._query_duckdb(
                    "SELECT count(*) AS cnt FROM personas WHERE age >= ?",
                    [threshold],
                    datasource="nemotron",
                )
                results.append((threshold, int(df["cnt"][0])))
        except Exception as e:  # noqa: BLE001 - collect failures so they surface
            errors.append(f"{type(e).__name__}: {e}")

    thresholds = [i * 1000 for i in range(8)]
    threads = [threading.Thread(target=worker, args=(t,)) for t in thresholds]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"並行実行で例外が発生した: {errors[:3]}"
    # each thread expects 200000 - threshold; a mismatch means results got mixed up
    mismatched = [(t, c) for t, c in results if c != 200_000 - t]
    assert mismatched == [], f"結果が混線した: {mismatched[:5]}"
    assert len(results) == 8 * 30


@pytest.mark.unit
def test_cursor_inherits_view_and_is_independent(service_with_local_parquet):
    """The issued cursor inherits the VIEW and is independent of the connection itself."""
    svc = service_with_local_parquet
    cursor = svc._acquire_cursor("nemotron")
    # the VIEW is visible (the cursor inherits the connection state)
    total = cursor.execute("SELECT count(*) FROM personas").fetchone()[0]
    assert total == 200_000
    # a separate cursor is independent
    other = svc._acquire_cursor("nemotron")
    assert other is not cursor


# --- Custom persona data is read by ID key ---


def _connect_spy(monkeypatch):
    from unittest.mock import MagicMock

    import src.services.survey_batch_service as module

    conn = MagicMock()
    monkeypatch.setattr(module.duckdb, "connect", lambda *_: conn)
    monkeypatch.setattr(module, "_load_httpfs", lambda _conn: None)
    monkeypatch.setattr(SurveyBatchService, "_set_s3_credentials", lambda *_: None)
    return conn


@pytest.mark.unit
def test_custom_data_is_read_from_its_id_key(monkeypatch):
    conn = _connect_spy(monkeypatch)
    svc = SurveyBatchService(bucket_name="bucket", region_name="ap-northeast-1")
    data_id = "2b0f8d2e-1111-4000-8000-000000000000"

    svc._get_duckdb_conn(f"custom:{data_id}")

    sql = conn.execute.call_args_list[-1].args[0]
    assert f"s3://bucket/persona-dataset/custom/{data_id}.parquet" in sql


@pytest.mark.unit
@pytest.mark.parametrize("value", ["custom:顧客データ", "custom:../x", "custom:"])
def test_custom_data_by_name_or_path_is_refused(monkeypatch, value):
    from src.services.survey_batch_service import SurveyBatchServiceError

    conn = _connect_spy(monkeypatch)
    svc = SurveyBatchService(bucket_name="bucket", region_name="ap-northeast-1")

    with pytest.raises(SurveyBatchServiceError):
        svc._get_duckdb_conn(value)
    conn.execute.assert_not_called()
