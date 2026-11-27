"""Service-level tests for SurveyBatchService against real DuckDB on local files.

`_get_duckdb_conn` makes a VIEW over Parquet on S3, so query tests put a connection
with a local Parquet VIEW into the cache (no S3 or network needed). Only the boundary
that goes to the network (Hugging Face load_dataset) is replaced.
"""

import io

import duckdb
import polars as pl
import pytest

from src.models.errors import ErrorCode
from src.services.survey_batch_service import (
    SurveyBatchService,
    SurveyBatchServiceError,
)
from tests.error_helpers import raises_code

_PERSONAS = pl.DataFrame(
    {
        "uuid": [f"u{i}" for i in range(6)],
        "sex": ["男", "女", "女", "男", "女", None],
        "age": [25, 34, 47, 52, 61, 30],
        "occupation": [
            "Software Engineer",
            "看護師",
            "software tester",
            "農家",
            "教師",
            "a_b",
        ],
        "region": ["関東", "関東", "近畿", "九州", "関東", "近畿"],
        "prefecture": ["東京都", "神奈川県", "大阪府", "福岡県", "東京都", "京都府"],
        "persona": [f"persona {i}" for i in range(6)],
    }
)


@pytest.fixture(autouse=True)
def _fake_aws_credentials(monkeypatch):
    """Keep boto3's credential chain on fake values (no real profile is read)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def _service_over(df: pl.DataFrame, tmp_path, datasource: str = "nemotron"):
    """Return a service whose cached connection views a local Parquet of ``df``."""
    path = tmp_path / f"{datasource.replace(':', '_')}.parquet"
    df.write_parquet(path)
    svc = SurveyBatchService(bucket_name="bucket", region_name="ap-northeast-1")
    conn = duckdb.connect(":memory:")
    conn.execute(f"CREATE VIEW personas AS SELECT * FROM read_parquet('{path}')")
    svc._duckdb_conns[datasource] = conn
    return svc


@pytest.fixture
def svc(tmp_path):
    return _service_over(_PERSONAS, tmp_path)


# =============================================================================
# filter_and_sample_personas / get_filtered_count
# =============================================================================


class TestFilterAndSample:
    def test_combined_filters_return_only_matching_rows(self, svc):
        """select + range + multi filters are ANDed together."""
        df = svc.filter_and_sample_personas(
            {
                "性別": "女",
                "年齢": {"min": 30, "max": 50},
                "居住地域": ["関東", "近畿"],
            },
            count=10,
        )
        assert sorted(df["uuid"].to_list()) == ["u1", "u2"]

    def test_count_limits_the_sample_and_rows_are_distinct(self, svc):
        df = svc.filter_and_sample_personas({}, count=3)
        assert len(df) == 3
        assert df["uuid"].n_unique() == 3

    def test_count_above_available_returns_all_matches(self, svc):
        df = svc.filter_and_sample_personas({"居住地域": ["関東"]}, count=100)
        assert sorted(df["uuid"].to_list()) == ["u0", "u1", "u4"]

    def test_filter_matching_nothing_returns_empty_frame(self, svc):
        df = svc.filter_and_sample_personas({"都道府県": ["北海道"]}, count=10)
        assert df.is_empty()

    def test_keyword_filter_is_case_insensitive_substring(self, svc):
        df = svc.filter_and_sample_personas({"職業": "SOFTWARE"}, count=10)
        assert sorted(df["uuid"].to_list()) == ["u0", "u2"]

    def test_unknown_and_empty_filters_are_ignored(self, svc):
        """Columns outside the allowlist and empty values do not narrow the result."""
        df = svc.filter_and_sample_personas(
            {
                "no_such_col; DROP TABLE x": "1",
                "性別": "",
                "居住地域": [],
                "年齢": None,
            },
            count=100,
        )
        assert len(df) == len(_PERSONAS)

    def test_quote_in_filter_value_is_bound_not_interpolated(self, svc):
        """An injection-shaped value is a literal parameter, so it matches nothing."""
        df = svc.filter_and_sample_personas(
            {"性別": "男' OR '1'='1", "居住地域": ["関東') OR ('1'='1"]}, count=100
        )
        assert df.is_empty()

    def test_range_with_only_min_bound(self, svc):
        df = svc.filter_and_sample_personas({"年齢": {"min": 50}}, count=100)
        assert sorted(df["uuid"].to_list()) == ["u3", "u4"]

    @pytest.mark.xfail(
        strict=True,
        reason="keyword LIKE does not escape % / _ (survey_batch_service.py:384)",
    )
    def test_keyword_underscore_is_literal(self, svc):
        """'_' in a keyword should match a literal underscore, not any character."""
        df = svc.filter_and_sample_personas({"職業": "e_g"}, count=100)
        # "e_g" would match the "eng" in "software engineer"
        assert df.is_empty()


class TestFilteredCount:
    def test_no_filters_returns_total(self, svc):
        assert svc.get_filtered_count(None) == len(_PERSONAS)
        assert svc.get_filtered_count({}) == len(_PERSONAS)

    def test_counts_matching_rows(self, svc):
        assert svc.get_filtered_count({"性別": "女", "居住地域": ["関東"]}) == 2

    def test_filters_that_are_all_ignored_count_everything(self, svc):
        """A non-empty dict whose entries are all skipped yields WHERE 1=1 (no params)."""
        assert svc.get_filtered_count({"年齢": {}}) == len(_PERSONAS)

    def test_no_match_counts_zero(self, svc):
        assert svc.get_filtered_count({"年齢": {"min": 200}}) == 0


# =============================================================================
# get_available_filter_values
# =============================================================================


class TestAvailableFilterValues:
    def test_lists_options_ranges_and_keyword_types(self, svc):
        values = svc.get_available_filter_values()
        assert values["性別"] == {"type": "select", "options": ["女", "男"]}
        assert values["年齢"] == {"type": "range", "min": 25, "max": 61}
        assert values["職業"] == {"type": "keyword"}
        assert values["居住地域"]["options"] == ["九州", "近畿", "関東"]

    def test_missing_columns_are_omitted(self, svc):
        """Columns absent from the dataset (country, marital_status) are skipped."""
        values = svc.get_available_filter_values()
        assert "出身国" not in values
        assert "結婚・子供の有無" not in values

    def test_result_is_cached_until_invalidated(self, svc):
        first = svc.get_available_filter_values()
        # Even if the data is replaced, the cache is returned within the TTL
        svc._duckdb_conns["nemotron"].execute(
            "CREATE OR REPLACE VIEW personas AS SELECT 'X' AS sex, 1 AS age"
        )
        assert svc.get_available_filter_values() == first

        svc._filter_values_cache.pop("nemotron")
        assert svc.get_available_filter_values()["性別"]["options"] == ["X"]

    def test_cache_expires_after_ttl(self, svc, monkeypatch):
        svc.get_available_filter_values()
        cached_at, _ = svc._filter_values_cache["nemotron"]
        import src.services.survey_batch_service as mod

        monkeypatch.setattr(
            mod.time, "monotonic", lambda: cached_at + svc._CACHE_TTL_SECONDS + 1
        )
        svc._duckdb_conns["nemotron"].execute(
            "CREATE OR REPLACE VIEW personas AS SELECT 'X' AS sex, 1 AS age"
        )
        assert svc.get_available_filter_values()["性別"]["options"] == ["X"]

    def test_empty_dataset_drops_range_but_keeps_others(self, tmp_path):
        svc = _service_over(_PERSONAS.clear(), tmp_path)
        values = svc.get_available_filter_values()
        assert "年齢" not in values  # MIN/MAX are NULL
        assert values["性別"] == {"type": "select", "options": []}


# =============================================================================
# get_preview_stats
# =============================================================================


class TestPreviewStats:
    def test_distributions_and_age_summary(self, svc):
        stats = svc.get_preview_stats()
        assert stats["性別"] == {"女": 3, "男": 2}  # NULL is not counted
        assert stats["居住地域"]["関東"] == 3
        assert stats["年齢"] == {"平均": 41.5, "最小": 25, "最大": 61}

    def test_filters_narrow_the_stats(self, svc):
        stats = svc.get_preview_stats({"居住地域": ["近畿"]})
        assert stats["性別"] == {"女": 1}
        assert stats["年齢"] == {"平均": 38.5, "最小": 30, "最大": 47}

    def test_top_five_only(self, tmp_path):
        df = pl.DataFrame(
            {"occupation": [f"job{i}" for i in range(8) for _ in range(i + 1)]}
        )
        stats = _service_over(df, tmp_path).get_preview_stats()
        assert list(stats["職業"]) == ["job7", "job6", "job5", "job4", "job3"]

    def test_no_match_yields_empty_distributions_and_no_age(self, svc):
        stats = svc.get_preview_stats({"性別": "その他"})
        assert stats["性別"] == {}
        assert "年齢" not in stats

    def test_missing_columns_are_skipped(self, tmp_path):
        stats = _service_over(
            pl.DataFrame({"sex": ["男"]}), tmp_path
        ).get_preview_stats()
        assert stats == {"性別": {"男": 1}}


# =============================================================================
# _query_duckdb / _get_duckdb_conn
# =============================================================================


class TestQueryDuckdb:
    def test_io_error_drops_connection_and_retries_once(self, tmp_path, monkeypatch):
        """A broken cached connection is discarded and the query is re-run."""
        missing = tmp_path / "gone.parquet"
        _PERSONAS.write_parquet(missing)
        svc = SurveyBatchService(bucket_name="bucket", region_name="ap-northeast-1")
        broken = duckdb.connect(":memory:")
        broken.execute(
            f"CREATE VIEW personas AS SELECT * FROM read_parquet('{missing}')"
        )
        missing.unlink()  # later reads raise IOException
        svc._duckdb_conns["nemotron"] = broken

        good = tmp_path / "good.parquet"
        _PERSONAS.head(2).write_parquet(good)
        rebuilt = []

        def fresh_conn(datasource="nemotron"):
            if datasource not in svc._duckdb_conns:
                conn = duckdb.connect(":memory:")
                conn.execute(
                    f"CREATE VIEW personas AS SELECT * FROM read_parquet('{good}')"
                )
                svc._duckdb_conns[datasource] = conn
                rebuilt.append(datasource)
            return svc._duckdb_conns[datasource]

        monkeypatch.setattr(svc, "_get_duckdb_conn", fresh_conn)
        assert svc.get_total_count() == 2
        assert rebuilt == ["nemotron"]

    def test_nemotron_without_uri_is_not_downloaded(self):
        svc = SurveyBatchService(bucket_name="bucket", region_name="ap-northeast-1")
        with raises_code(
            SurveyBatchServiceError, ErrorCode.SURVEY_DATASET_NOT_DOWNLOADED
        ):
            svc.get_total_count()

    @pytest.mark.parametrize(
        "datasource", ["custom:", "custom:../../etc", "custom:a/b"]
    )
    def test_malformed_custom_datasource_is_not_found(self, datasource):
        svc = SurveyBatchService(bucket_name="bucket", region_name="ap-northeast-1")
        with raises_code(
            SurveyBatchServiceError, ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND
        ):
            svc.get_total_count(datasource=datasource)

    def test_uri_with_quote_is_rejected_before_any_sql(self):
        """A URI that could break out of read_parquet('...') never reaches DuckDB."""
        svc = SurveyBatchService(bucket_name="bucket", region_name="ap-northeast-1")
        svc.set_parquet_s3_uri("s3://bucket/x.parquet'); DROP VIEW personas; --")
        with raises_code(SurveyBatchServiceError, ErrorCode.SURVEY_OPERATION_FAILED):
            svc.get_total_count()
        assert "nemotron" not in svc._duckdb_conns

    def test_invalidate_datasource_drops_conn_and_cache(self, svc):
        svc.get_available_filter_values()
        svc.invalidate_datasource("nemotron")
        assert "nemotron" not in svc._filter_values_cache
        # The cached connection is gone and the URI is unset, so it cannot reconnect
        with raises_code(
            SurveyBatchServiceError, ErrorCode.SURVEY_DATASET_NOT_DOWNLOADED
        ):
            svc.get_total_count()


# =============================================================================
# execute_dataset_preview
# =============================================================================


class TestDatasetPreview:
    @pytest.fixture
    def service(self):
        return SurveyBatchService(bucket_name="bucket", region_name="ap-northeast-1")

    def test_local_csv_count_and_rows_with_params(self, service, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "datasets").mkdir()
        (tmp_path / "datasets" / "d.csv").write_text(
            "id,city\n1,Tokyo\n2,Osaka\n3,Tokyo\n"
        )
        total, columns, rows = service.execute_dataset_preview(
            "local://datasets/d.csv",
            count_sql='SELECT COUNT(*) FROM dataset WHERE "city" = $1',
            data_sql='SELECT * FROM dataset WHERE "city" = $1 ORDER BY id LIMIT 1',
            params=["Tokyo"],
        )
        assert total == 2
        assert columns == ["id", "city"]
        assert rows == [[1, "Tokyo"]]

    def test_local_parquet_without_params(self, service, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _PERSONAS.write_parquet(tmp_path / "p.parquet")
        total, columns, rows = service.execute_dataset_preview(
            "local://p.parquet",
            count_sql="SELECT COUNT(*) FROM dataset",
            data_sql="SELECT uuid FROM dataset ORDER BY uuid LIMIT 2",
        )
        assert (total, columns, rows) == (6, ["uuid"], [["u0"], ["u1"]])

    @pytest.mark.parametrize(
        "path",
        [
            "local://../secret.csv",
            "local:///etc/passwd",
            "local://a.csv'); SELECT 1; --",
            "s3://bucket/a b.csv",
            "s3://bucket/x.parquet'); --",
        ],
    )
    def test_unsafe_paths_are_rejected(self, service, path):
        with raises_code(SurveyBatchServiceError, ErrorCode.SURVEY_OPERATION_FAILED):
            service.execute_dataset_preview(
                path, "SELECT COUNT(*) FROM dataset", "SELECT * FROM dataset"
            )

    def test_missing_local_file_raises_duckdb_error(
        self, service, tmp_path, monkeypatch
    ):
        """Not wrapped here: the Manager converts it to DATASET_OPERATION_FAILED."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(duckdb.IOException):
            service.execute_dataset_preview(
                "local://nope.csv", "SELECT COUNT(*) FROM dataset", "SELECT 1"
            )


# =============================================================================
# convert_csv_to_parquet
# =============================================================================


class TestConvertCsvToParquet:
    @pytest.fixture
    def service(self):
        return SurveyBatchService(bucket_name="bucket", region_name="ap-northeast-1")

    def test_mapping_renames_and_parquet_round_trips(self, service):
        csv = "名前,性別,年齢,紹介\nA,男,30,こんにちは\nB,女,40,よろしく\n".encode()
        df, data = service.convert_csv_to_parquet(
            csv, {"sex": "性別", "age": "年齢", "persona": "紹介", "region": ""}
        )
        assert {"sex", "age", "persona", "uuid", "名前"} == set(df.columns)
        back = pl.read_parquet(io.BytesIO(data))
        assert back["persona"].to_list() == ["こんにちは", "よろしく"]
        assert back["uuid"].n_unique() == 2

    def test_without_persona_column_text_columns_are_joined(self, service):
        csv = b"name,city,score\nA,Tokyo,1\nB,,2\n"
        df, _ = service.convert_csv_to_parquet(csv)
        assert df["persona"].to_list() == ["A / Tokyo", "B / "]

    def test_existing_uuid_is_kept(self, service):
        df, _ = service.convert_csv_to_parquet(b"uuid,persona\nid-1,x\n")
        assert df["uuid"].to_list() == ["id-1"]

    def test_header_only_csv_is_empty(self, service):
        with raises_code(SurveyBatchServiceError, ErrorCode.SURVEY_DATASET_CSV_EMPTY):
            service.convert_csv_to_parquet(b"persona,age\n")

    @pytest.mark.parametrize("csv", [b"", b"a,b\n1,2,3\n"])
    def test_unreadable_csv(self, service, csv):
        with raises_code(
            SurveyBatchServiceError, ErrorCode.SURVEY_DATASET_CSV_UNREADABLE
        ):
            service.convert_csv_to_parquet(csv)

    def test_numeric_only_csv_has_no_text_column(self, service):
        with raises_code(
            SurveyBatchServiceError, ErrorCode.SURVEY_DATASET_NO_TEXT_COLUMN
        ):
            service.convert_csv_to_parquet(b"age,score\n30,1\n")

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "mapping onto an already-present column raises polars DuplicateError "
            "unwrapped (survey_batch_service.py:200-201)"
        ),
    )
    def test_mapping_onto_existing_column_is_a_coded_error(self, service):
        with pytest.raises(SurveyBatchServiceError):
            service.convert_csv_to_parquet(
                b"persona,summary\nold,new\n", {"persona": "summary"}
            )


# =============================================================================
# download_nemotron_dataset (only the Hugging Face boundary is replaced)
# =============================================================================


class TestDownloadNemotron:
    def test_returns_parquet_bytes_and_resets_state(self, svc, monkeypatch):
        import datasets

        captured = {}

        def fake_load(name, split):
            captured["args"] = (name, split)
            return datasets.Dataset.from_dict({"uuid": ["a", "b"], "age": [1, 2]})

        monkeypatch.setattr(datasets, "load_dataset", fake_load)
        svc.set_parquet_s3_uri("s3://bucket/nemotron.parquet")
        svc.get_available_filter_values()

        data = svc.download_nemotron_dataset()

        assert pl.read_parquet(io.BytesIO(data))["uuid"].to_list() == ["a", "b"]
        assert captured["args"] == ("nvidia/Nemotron-Personas-Japan", "train")
        assert not svc.has_parquet_uri()
        assert "nemotron" not in svc._duckdb_conns
        assert "nemotron" not in svc._filter_values_cache

    def test_download_failure_is_coded(self, monkeypatch):
        import datasets

        def offline(*_a, **_k):
            raise ConnectionError("no network")

        monkeypatch.setattr(datasets, "load_dataset", offline)
        svc = SurveyBatchService(bucket_name="bucket", region_name="ap-northeast-1")
        with raises_code(SurveyBatchServiceError, ErrorCode.SURVEY_OPERATION_FAILED):
            svc.download_nemotron_dataset()
