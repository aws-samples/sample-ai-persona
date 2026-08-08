"""SurveyBatchService の DuckDB 並行アクセス安全性テスト（Issue #118）。

このサービスはシングルトンで、Router が ThreadPoolExecutor 上から並行に
クエリを投げる。キャッシュした単一の DuckDBPyConnection を複数スレッドで同時に
execute すると、結果の混線や `IndexError`（空結果）、プロセスクラッシュが起きる。
`_query_duckdb` がスレッドごとに `cursor()` を発行して実行を分離することを検証する。
"""

import threading

import polars as pl
import pytest

from src.services.survey_batch_service import SurveyBatchService


@pytest.fixture
def service_with_local_parquet(tmp_path):
    """ローカルParquetをVIEWに登録済みの SurveyBatchService を返す（S3不要）。"""
    parquet = tmp_path / "personas.parquet"
    pl.DataFrame({"age": range(200_000), "persona": ["p"] * 200_000}).write_parquet(
        parquet
    )

    svc = SurveyBatchService(bucket_name="b", region_name="ap-northeast-1")

    # _get_duckdb_conn は S3 認証を張るため、テストではローカルParquetを
    # VIEW 登録した接続を直接キャッシュに差し込む（並行実行の検証が目的）。
    import duckdb

    conn = duckdb.connect(":memory:")
    conn.execute(
        f"CREATE VIEW personas AS SELECT * FROM read_parquet('{parquet}')"
    )  # nosemgrep: sqlalchemy-execute-raw-query
    svc._duckdb_conns["nemotron"] = conn
    return svc


@pytest.mark.unit
def test_concurrent_queries_do_not_corrupt_results(service_with_local_parquet):
    """8スレッドが同一接続へ同時にクエリしても、混線・空結果・例外が起きない。

    修正前（接続を直接 execute）はこの構成で `IndexError` や結果の取り違えが
    再現する。cursor() 分離により各スレッドが独立して正しい件数を得る。
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
        except Exception as e:  # noqa: BLE001 - 失敗を集約して可視化する
            errors.append(f"{type(e).__name__}: {e}")

    thresholds = [i * 1000 for i in range(8)]
    threads = [threading.Thread(target=worker, args=(t,)) for t in thresholds]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"並行実行で例外が発生した: {errors[:3]}"
    # 各スレッドの期待値は 200000 - threshold。混線していれば一致しない。
    mismatched = [(t, c) for t, c in results if c != 200_000 - t]
    assert mismatched == [], f"結果が混線した: {mismatched[:5]}"
    assert len(results) == 8 * 30


@pytest.mark.unit
def test_cursor_inherits_view_and_is_independent(service_with_local_parquet):
    """発行したカーソルが VIEW を継承し、接続本体と独立していること。"""
    svc = service_with_local_parquet
    cursor = svc._acquire_cursor("nemotron")
    # VIEW が見える（接続の状態を継承している）
    total = cursor.execute("SELECT count(*) FROM personas").fetchone()[0]
    assert total == 200_000
    # 別カーソルは独立
    other = svc._acquire_cursor("nemotron")
    assert other is not cursor
