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


# --- U7: カスタムペルソナデータは ID のキーで読む（BR4.3、NFR1.82） ---


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
