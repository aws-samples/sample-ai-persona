"""
SurveyBatchService
A Service-layer class specialized in DuckDB/Parquet queries (persona extraction), statistical summaries and HuggingFace downloads.
It does not depend on other Services. Generating answers (inference) is done by Step Functions and
handled by SurveyRunnerService (the batch inference call was removed).

Reads and writes to S3 are done by the Manager layer; this Service only does pure SDK operations.
"""

import io
import logging
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import polars as pl

from ..models.custom_persona_data import (
    CUSTOM_FILE_PREFIX,
    PersonaDataRef,
    custom_file_key,
)
from ..models.errors import CodedError, ErrorCode

logger = logging.getLogger(__name__)


def _load_httpfs(conn: duckdb.DuckDBPyConnection) -> None:
    """LOAD httpfs (no download at run time).

    Disables autoinstall/autoload and only does LOAD. httpfs is assumed to be pre-INSTALLed
    in both container and local (container at build time, local via the setup steps in
    docs/local_development.md). In a closed network a missing one
    fails fast at LOAD instead of being hidden by a run-time download.
    """
    conn.execute("SET autoinstall_known_extensions=false;")
    conn.execute("SET autoload_known_extensions=false;")
    conn.execute("LOAD httpfs;")  # nosemgrep: sqlalchemy-execute-raw-query


class SurveyBatchServiceError(CodedError):
    """Base exception of the SurveyBatchService layer."""

    pass


# Filterable attribute column mapping of the persona dataset
FILTER_COLUMN_MAP: Dict[str, str] = {
    "性別": "sex",
    "年齢": "age",
    "職業": "occupation",
    "出身国": "country",
    "居住地域": "region",
    "都道府県": "prefecture",
    "結婚・子供の有無": "marital_status",
}

FILTER_TYPE_MAP: Dict[str, str] = {
    "性別": "select",
    "年齢": "range",
    "職業": "keyword",
    "出身国": "multi",
    "居住地域": "multi",
    "都道府県": "multi",
    "結婚・子供の有無": "multi",
}

PERSONA_ATTRIBUTE_COLUMNS: List[str] = [
    "sex",
    "age",
    "occupation",
    "country",
    "region",
    "prefecture",
    "marital_status",
    "education_level",
]

PERSONA_PROFILE_COLUMNS: List[str] = [
    "persona",
    "cultural_background",
    "skills_and_expertise",
    "hobbies_and_interests",
    "career_goals_and_ambitions",
]

# Hugging Face dataset name
DATASET_NAME = "nvidia/Nemotron-Personas-Japan"

# This Service does not hold the Nemotron Parquet key (closed-network support).
# The Manager builds the URI from Config.NEMOTRON_DATASET_KEY and passes it via set_parquet_s3_uri.

CUSTOM_DATASET_PREFIX = CUSTOM_FILE_PREFIX

# Valid column names for DuckDB queries (to prevent SQL injection)
_VALID_COLUMNS = frozenset(
    list(FILTER_COLUMN_MAP.values())
    + PERSONA_ATTRIBUTE_COLUMNS
    + PERSONA_PROFILE_COLUMNS
    + ["uuid"]
)


class SurveyBatchService:
    """DuckDB/Parquet queries + statistical summaries + HuggingFace download.

    Does not depend on other Services. S3 operations are the Manager layer's job.
    bucket_name / region_name needed for the DuckDB connection are taken in the constructor.
    """

    _CACHE_TTL_SECONDS: int = 300

    def __init__(self, bucket_name: str, region_name: str) -> None:
        self.bucket_name = bucket_name
        self.region_name = region_name
        self._parquet_s3_uri: Optional[str] = None
        self._duckdb_conns: Dict[str, duckdb.DuckDBPyConnection] = {}
        self._filter_values_cache: Dict[
            str, Tuple[float, Dict[str, Dict[str, Any]]]
        ] = {}
        # This service is a singleton, and the Router issues queries concurrently from a ThreadPoolExecutor.
        # Executing on one cached DuckDBPyConnection from several threads at once
        # mixes up results or crashes, so getting/creating the connection and issuing cursors are
        # serialized with this lock (Issue #118).
        self._conn_lock = threading.Lock()

    # =========================================================================
    # HuggingFace dataset
    # =========================================================================

    def download_nemotron_dataset(self) -> bytes:
        """Download the Nemotron dataset from HuggingFace and return Parquet bytes.

        Uploading to S3 is the Manager layer's job.
        """
        self._parquet_s3_uri = None
        self._duckdb_conns.pop("nemotron", None)
        self._filter_values_cache.pop("nemotron", None)

        try:
            from datasets import load_dataset
            import tempfile
            import os

            logger.info(f"Downloading dataset from Hugging Face: {DATASET_NAME}")
            dataset = load_dataset(DATASET_NAME, split="train")

            with tempfile.TemporaryDirectory() as tmpdir:
                parquet_path = os.path.join(tmpdir, "data.parquet")
                dataset.to_parquet(parquet_path)
                with open(parquet_path, "rb") as f:
                    parquet_bytes = f.read()

            return parquet_bytes
        except Exception as e:
            logger.error("Failed to download Nemotron dataset", exc_info=True)
            raise SurveyBatchServiceError(
                f"nemotron dataset download failed ({type(e).__name__})",
                code=ErrorCode.SURVEY_OPERATION_FAILED,
            ) from e

    # =========================================================================
    # Custom dataset Parquet conversion
    # =========================================================================

    def convert_csv_to_parquet(
        self,
        csv_bytes: bytes,
        column_mapping: Optional[Dict[str, str]] = None,
    ) -> Tuple[pl.DataFrame, bytes]:
        """Validate, map and convert a CSV to Parquet.

        Returns:
            (DataFrame, parquet_bytes): The converted DataFrame and Parquet bytes.
            Uploading to S3 is the Manager layer's job.
        """
        try:
            df = pl.read_csv(io.BytesIO(csv_bytes), infer_schema_length=1000)
        except Exception as e:
            raise SurveyBatchServiceError(
                f"csv read failed ({type(e).__name__})",
                code=ErrorCode.SURVEY_DATASET_CSV_UNREADABLE,
            ) from e

        if len(df) == 0:
            raise SurveyBatchServiceError(
                "csv has no data rows",
                code=ErrorCode.SURVEY_DATASET_CSV_EMPTY,
            )

        if column_mapping:
            rename_map = {}
            for std_col, csv_col in column_mapping.items():
                if csv_col and csv_col in df.columns and std_col != csv_col:
                    rename_map[csv_col] = std_col
            if rename_map:
                df = df.rename(rename_map)

        if "persona" not in df.columns:
            text_cols = [c for c in df.columns if df[c].dtype == pl.Utf8]
            if not text_cols:
                raise SurveyBatchServiceError(
                    "csv has no text column to use as persona summary",
                    code=ErrorCode.SURVEY_DATASET_NO_TEXT_COLUMN,
                )
            df = df.with_columns(
                pl.concat_str(
                    [pl.col(c).fill_null("") for c in text_cols], separator=" / "
                ).alias("persona")
            )

        if "uuid" not in df.columns:
            df = df.with_columns(
                pl.Series("uuid", [str(uuid.uuid4()) for _ in range(len(df))])
            )

        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            parquet_path = os.path.join(tmpdir, "data.parquet")
            df.write_parquet(parquet_path)
            with open(parquet_path, "rb") as f:
                parquet_bytes = f.read()

        return df, parquet_bytes

    # =========================================================================
    # DuckDB/Parquet queries
    # =========================================================================

    def set_parquet_s3_uri(self, s3_uri: str) -> None:
        """Called by the Manager layer after checking the Parquet exists on S3. Resets the DuckDB connection cache."""
        self._parquet_s3_uri = s3_uri

    def has_parquet_uri(self) -> bool:
        """Return whether the Parquet S3 URI has been set."""
        return self._parquet_s3_uri is not None

    def invalidate_datasource(self, datasource: str) -> None:
        """Discard the DuckDB connection and filter cache for the given datasource."""
        self._duckdb_conns.pop(datasource, None)
        self._filter_values_cache.pop(datasource, None)

    def _get_duckdb_conn(
        self, datasource: str = "nemotron"
    ) -> duckdb.DuckDBPyConnection:
        """Get the DuckDB connection (cached per datasource)."""
        if datasource in self._duckdb_conns:
            return self._duckdb_conns[datasource]

        if datasource.startswith("custom:"):
            # Only custom:{id} is accepted and the key is built from the ID (names and paths are not used)
            try:
                ref = PersonaDataRef.parse(datasource)
            except ValueError as e:
                raise SurveyBatchServiceError(
                    "custom datasource must be custom:{id}",
                    code=ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND,
                ) from e
            assert ref.custom_id is not None
            s3_uri = f"s3://{self.bucket_name}/{custom_file_key(ref.custom_id)}"
        else:
            if self._parquet_s3_uri is None:
                raise SurveyBatchServiceError(
                    "nemotron parquet uri is not configured",
                    code=ErrorCode.SURVEY_DATASET_NOT_DOWNLOADED,
                )
            s3_uri = self._parquet_s3_uri

        if not re.fullmatch(r"s3://[a-zA-Z0-9.\-]+/[a-zA-Z0-9.\-_/　-鿿＀-￯]+", s3_uri):
            # The URI is diagnostic information. For users it is reduced to generic wording (Issue #118).
            logger.error("Invalid S3 URI format: %s", s3_uri)
            raise SurveyBatchServiceError(
                "invalid s3 uri format",
                code=ErrorCode.SURVEY_OPERATION_FAILED,
            )

        conn = duckdb.connect(":memory:")
        # httpfs is built in at container build time. To avoid run-time downloads,
        # autoinstall/autoload are disabled and only LOAD is done (no INSTALL;
        # in environments where it is not built in, fail fast at LOAD).
        _load_httpfs(conn)
        self._set_s3_credentials(conn)
        conn.execute(
            f"CREATE VIEW personas AS SELECT * FROM read_parquet('{s3_uri}');"
        )  # nosemgrep: sqlalchemy-execute-raw-query

        self._duckdb_conns[datasource] = conn
        return conn

    def _query_duckdb(
        self, sql: str, params: Optional[List[Any]] = None, datasource: str = "nemotron"
    ) -> pl.DataFrame:
        """Run a SQL query on Parquet on S3 with DuckDB and return a Polars DataFrame.

        The connection is a single :class:`DuckDBPyConnection` cached per datasource and
        shared, but it cannot run concurrently. Issuing a ``cursor()`` per thread
        inherits the VIEW and connection settings (S3 credentials etc.) while isolating only execution
        (DuckDB's official approach to concurrent access). Getting/creating the connection and issuing ``cursor()`` are
        serialized with a lock, and the query itself runs outside the lock (Issue #118).
        """
        cursor = self._acquire_cursor(datasource)
        try:
            return self._run(cursor, sql, params)
        except duckdb.IOException:
            # The connection may have dropped. Discard it, reconnect and retry with a new cursor.
            with self._conn_lock:
                self._duckdb_conns.pop(datasource, None)
            cursor = self._acquire_cursor(datasource)
            return self._run(cursor, sql, params)

    def _acquire_cursor(self, datasource: str) -> duckdb.DuckDBPyConnection:
        """Issue a thread-dedicated cursor (locks around connection creation/retrieval).

        ``cursor()`` shares the main connection's catalog (VIEW definitions etc.)
        but does not inherit session-scoped settings such as ``SET s3_access_key_id``.
        Assuming they are inherited makes a cursor from a cached connection
        have no credentials, so S3 access always fails with 403. Set them
        explicitly on every cursor.
        """
        with self._conn_lock:
            conn = self._get_duckdb_conn(datasource)
            cursor = conn.cursor()
            self._set_s3_credentials(cursor)
            return cursor

    def _set_s3_credentials(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Set S3 credentials on the DuckDB connection from the boto3 credential chain.

        ``cursor()`` does not inherit them, so this must be called both when initializing
        the main connection and when issuing a cursor.
        """
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
        conn.execute(
            "SET s3_region = $1;", [self.region_name]
        )  # nosemgrep: sqlalchemy-execute-raw-query

    @staticmethod
    def _run(
        cursor: duckdb.DuckDBPyConnection,
        sql: str,
        params: Optional[List[Any]],
    ) -> pl.DataFrame:
        if params:
            return cursor.execute(sql, params).pl()
        return cursor.execute(sql).pl()

    def _build_where_clause(
        self, filters: Dict[str, Any]
    ) -> Tuple[str, List[Any], int]:
        """Return the WHERE clause, parameters and next parameter index from a filter dict."""
        where_clauses: List[str] = []
        params: List[Any] = []
        param_idx = 1

        for key, value in filters.items():
            column = FILTER_COLUMN_MAP.get(key, key)
            if column not in _VALID_COLUMNS:
                continue
            if value is None or (isinstance(value, (list, str)) and len(value) == 0):
                continue

            filter_type = FILTER_TYPE_MAP.get(key, "select")

            if filter_type == "range" and isinstance(value, dict):
                if value.get("min") is not None:
                    where_clauses.append(f"CAST({column} AS DOUBLE) >= ${param_idx}")
                    params.append(float(value["min"]))
                    param_idx += 1
                if value.get("max") is not None:
                    where_clauses.append(f"CAST({column} AS DOUBLE) <= ${param_idx}")
                    params.append(float(value["max"]))
                    param_idx += 1
            elif filter_type == "keyword" and isinstance(value, str):
                where_clauses.append(
                    f"LOWER(CAST({column} AS VARCHAR)) LIKE ${param_idx}"
                )
                params.append(f"%{value.lower()}%")
                param_idx += 1
            elif isinstance(value, list):
                placeholders = ", ".join(f"${param_idx + i}" for i in range(len(value)))
                where_clauses.append(f"CAST({column} AS VARCHAR) IN ({placeholders})")
                params.extend([str(v) for v in value])
                param_idx += len(value)
            else:
                where_clauses.append(f"CAST({column} AS VARCHAR) = ${param_idx}")
                params.append(str(value))
                param_idx += 1

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        return where_sql, params, param_idx

    def get_total_count(self, datasource: str = "nemotron") -> int:
        """Get the total number of records in the dataset."""
        df = self._query_duckdb(
            "SELECT count(*) AS cnt FROM personas", datasource=datasource
        )
        return int(df["cnt"][0])

    def filter_and_sample_personas(
        self,
        filters: Dict[str, Any],
        count: int,
        datasource: str = "nemotron",
    ) -> pl.DataFrame:
        """Run filtering and sampling in a single query inside DuckDB SQL."""
        where_sql, params, param_idx = self._build_where_clause(filters)
        sql = (
            f"SELECT * FROM personas WHERE {where_sql} "
            f"ORDER BY random() LIMIT ${param_idx}"
        )
        params.append(count)

        result = self._query_duckdb(sql, params, datasource=datasource)
        logger.info(
            f"Filter+sample in single query: {len(result)} personas "
            f"(requested: {count}, filters: {filters})"
        )
        return result

    def get_available_filter_values(
        self, datasource: str = "nemotron"
    ) -> Dict[str, Dict[str, Any]]:
        """Get the filterable attribute values with filter-type info (TTL cache)."""
        now = time.monotonic()
        if datasource in self._filter_values_cache:
            cached_at, cached_result = self._filter_values_cache[datasource]
            if now - cached_at < self._CACHE_TTL_SECONDS:
                return cached_result

        result: Dict[str, Dict[str, Any]] = {}

        for filter_name, column in FILTER_COLUMN_MAP.items():
            if column not in _VALID_COLUMNS:
                continue

            filter_type = FILTER_TYPE_MAP.get(filter_name, "select")

            try:
                if filter_type == "range":
                    df = self._query_duckdb(
                        f"SELECT MIN(CAST({column} AS DOUBLE)) AS min_val, "
                        f"MAX(CAST({column} AS DOUBLE)) AS max_val FROM personas "
                        f"WHERE {column} IS NOT NULL",
                        datasource=datasource,
                    )
                    result[filter_name] = {
                        "type": "range",
                        "min": int(df["min_val"][0]),
                        "max": int(df["max_val"][0]),
                    }
                elif filter_type == "keyword":
                    result[filter_name] = {"type": "keyword"}
                else:
                    df = self._query_duckdb(
                        f"SELECT DISTINCT CAST({column} AS VARCHAR) AS val "
                        f"FROM personas WHERE {column} IS NOT NULL ORDER BY val",
                        datasource=datasource,
                    )
                    result[filter_name] = {
                        "type": filter_type,
                        "options": df["val"].to_list(),
                    }
            except Exception as e:
                logger.warning(f"Failed to get filter values for {filter_name}: {e}")

        self._filter_values_cache[datasource] = (now, result)
        return result

    def get_filtered_count(
        self, filters: Optional[Dict[str, Any]] = None, datasource: str = "nemotron"
    ) -> int:
        """Get the number of personas that match the filter."""
        if not filters:
            return self.get_total_count(datasource)

        where_sql, params, _ = self._build_where_clause(filters)
        df = self._query_duckdb(
            f"SELECT count(*) AS cnt FROM personas WHERE {where_sql}",
            params if params else None,
            datasource=datasource,
        )
        return int(df["cnt"][0])

    def get_preview_stats(
        self,
        filters: Optional[Dict[str, Any]] = None,
        datasource: str = "nemotron",
    ) -> Dict[str, Dict[str, Any]]:
        """Get attribute distribution stats for the preview (DuckDB aggregation)."""
        if filters:
            where_sql, params, _ = self._build_where_clause(filters)
        else:
            where_sql = "1=1"
            params = []

        stats: Dict[str, Dict[str, Any]] = {}
        p = params if params else None

        for label, col in {
            "性別": "sex",
            "居住地域": "region",
            "職業": "occupation",
        }.items():
            try:
                df = self._query_duckdb(
                    f"SELECT CAST({col} AS VARCHAR) AS val, count(*) AS cnt "
                    f"FROM personas WHERE {where_sql} AND {col} IS NOT NULL "
                    f"GROUP BY val ORDER BY cnt DESC LIMIT 5",
                    p,
                    datasource=datasource,
                )
                stats[label] = dict(zip(df["val"].to_list(), df["cnt"].to_list()))
            except Exception:
                continue  # If the column is missing or the type mismatches, skip this attribute

        try:
            df = self._query_duckdb(
                f"SELECT AVG(CAST(age AS DOUBLE)) AS avg_age, "
                f"MIN(CAST(age AS DOUBLE)) AS min_age, "
                f"MAX(CAST(age AS DOUBLE)) AS max_age "
                f"FROM personas WHERE {where_sql} AND age IS NOT NULL",
                p,
                datasource=datasource,
            )
            if len(df) > 0 and df["avg_age"][0] is not None:
                stats["年齢"] = {
                    "平均": round(float(df["avg_age"][0]), 1),
                    "最小": int(df["min_age"][0]),
                    "最大": int(df["max_age"][0]),
                }
        except Exception:
            pass  # Ignore a failure to get age stats and return the other stats

        return stats

    # =========================================================================
    # Statistical summary generation (Polars/DataFrame operations)
    # =========================================================================

    def generate_statistical_summary(
        self,
        results_csv: str,
        questions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Generate a statistical summary from CSV results.

        Args:
            results_csv: Result text in CSV format
            questions: List of question metadata. Each item is
                {"id": str, "text": str, "question_type": str, "options": list, "allow_multiple": bool}

        Returns:
            Statistical summary dict
        """
        import io

        schema_overrides = {}
        for q in questions:
            answer_col = f"{q['id']}_answer"
            schema_overrides[answer_col] = pl.Utf8

        df = pl.read_csv(
            io.StringIO(results_csv),
            schema_overrides=schema_overrides,
            ignore_errors=True,
        )
        total = len(df)

        demographics = self._summarize_demographics(df)

        question_analysis = []
        for q in questions:
            answer_col = f"{q['id']}_answer"
            if answer_col not in df.columns:
                continue

            analysis: Dict[str, Any] = {
                "question_id": q["id"],
                "question_text": q["text"],
                "question_type": q["question_type"],
            }

            if q["question_type"] == "multiple_choice":
                analysis["distribution"] = self._analyze_multiple_choice(
                    df,
                    answer_col,
                    q.get("options", []),
                    allow_multiple=q.get("allow_multiple", False),
                )
                analysis["by_demographics"] = self._cross_tabulate_for_report(
                    df,
                    answer_col,
                    allow_multiple=q.get("allow_multiple", False),
                )
            elif q["question_type"] == "scale_rating":
                analysis["statistics"] = self._analyze_scale_rating(df, answer_col)
                analysis["by_demographics"] = self._cross_tabulate_numeric_for_report(
                    df, answer_col
                )
            elif q["question_type"] == "free_text":
                analysis["sample_responses"] = self._sample_free_text(
                    df, answer_col, limit=10
                )

            question_analysis.append(analysis)

        return {
            "total_responses": total,
            "demographics": demographics,
            "question_analysis": question_analysis,
        }

    @staticmethod
    def _summarize_demographics(df: pl.DataFrame) -> Dict[str, Any]:
        """Aggregate demographic information."""
        demo: Dict[str, Any] = {}

        if "sex" in df.columns:
            demo["sex"] = df.group_by("sex").agg(pl.count()).to_dicts()

        if "age" in df.columns:
            age_col = df["age"].cast(pl.Float64, strict=False)
            age_mean = age_col.mean()
            age_min = age_col.min()
            age_max = age_col.max()
            demo["age"] = {
                "mean": float(age_mean) if isinstance(age_mean, (int, float)) else 0,
                "min": int(age_min) if isinstance(age_min, (int, float)) else 0,
                "max": int(age_max) if isinstance(age_max, (int, float)) else 0,
            }

        if "region" in df.columns:
            demo["region"] = (
                df.group_by("region")
                .agg(pl.count())
                .sort("count", descending=True)
                .head(5)
                .to_dicts()
            )

        if "occupation" in df.columns:
            demo["occupation"] = (
                df.group_by("occupation")
                .agg(pl.count())
                .sort("count", descending=True)
                .head(5)
                .to_dicts()
            )

        return demo

    @staticmethod
    def _answered(df: pl.DataFrame, answer_col: str) -> pl.DataFrame:
        """Rows that answered the question (a blank answer is no answer)."""
        column = pl.col(answer_col)
        return df.filter(
            column.is_not_null() & (column.cast(pl.Utf8).str.strip_chars() != "")
        )

    @staticmethod
    def _analyze_multiple_choice(
        df: pl.DataFrame,
        answer_col: str,
        options: List[str],
        allow_multiple: bool = False,
    ) -> Dict[str, Any]:
        """Analysis of a choice question. The denominator is the number of respondents."""
        df = SurveyBatchService._answered(df, answer_col)
        total = len(df)
        if allow_multiple:
            counter: Dict[str, int] = {}
            for ans in df[answer_col].drop_nulls().to_list():
                for part in str(ans).split("|"):
                    part = part.strip()
                    if part:
                        counter[part] = counter.get(part, 0) + 1
            distribution = {}
            for key, count in counter.items():
                distribution[key] = {
                    "count": count,
                    "percentage": round(count / total * 100, 1) if total > 0 else 0,
                }
        else:
            counts = df.group_by(answer_col).agg(pl.count().alias("count")).to_dicts()
            distribution = {}
            for item in counts:
                answer = item[answer_col]
                count = item["count"]
                distribution[answer] = {
                    "count": count,
                    "percentage": round(count / total * 100, 1) if total > 0 else 0,
                }

        return distribution

    @staticmethod
    def _analyze_scale_rating(df: pl.DataFrame, answer_col: str) -> Dict[str, Any]:
        """Analysis of a scale-rating question."""
        numeric_col = df[answer_col].cast(pl.Float64, strict=False)
        valid_df = df.filter(numeric_col.is_not_null())

        col_mean = numeric_col.mean()
        col_median = numeric_col.median()
        col_std = numeric_col.std()
        return {
            "mean": round(float(col_mean), 2)
            if isinstance(col_mean, (int, float))
            else 0,
            "median": float(col_median) if isinstance(col_median, (int, float)) else 0,
            "std": round(float(col_std), 2) if isinstance(col_std, (int, float)) else 0,
            "distribution": valid_df.group_by(answer_col)
            .agg(pl.count())
            .sort(answer_col)
            .to_dicts()
            if len(valid_df) > 0
            else [],
        }

    @staticmethod
    def _cross_tabulate_for_report(
        df: pl.DataFrame,
        answer_col: str,
        allow_multiple: bool = False,
    ) -> Dict[str, Any]:
        """Cross-tabulation of a choice question by attribute (blank answers are excluded)."""
        df = SurveyBatchService._answered(df, answer_col)
        if df.is_empty():
            return {}
        if allow_multiple:
            rows = []
            for row in df.iter_rows(named=True):
                ans = row.get(answer_col, "")
                for part in str(ans).split("|"):
                    part = part.strip()
                    if part:
                        new_row = dict(row)
                        new_row[answer_col] = part
                        rows.append(new_row)
            if rows:
                df = pl.DataFrame(rows)
            else:
                return {}

        cross_tab: Dict[str, Any] = {}

        if "sex" in df.columns:
            cross_tab["by_sex"] = (
                df.group_by(["sex", answer_col])
                .agg(pl.count().alias("count"))
                .sort(["sex", "count"], descending=[False, True])
                .to_dicts()
            )

        if "age" in df.columns:
            df_with_bracket = df.with_columns(
                (pl.col("age").cast(pl.Int64, strict=False) // 10 * 10).alias(
                    "age_bracket"
                )
            )
            cross_tab["by_age"] = (
                df_with_bracket.group_by(["age_bracket", answer_col])
                .agg(pl.count().alias("count"))
                .sort(["age_bracket", "count"], descending=[False, True])
                .to_dicts()
            )

        return cross_tab

    @staticmethod
    def _cross_tabulate_numeric_for_report(
        df: pl.DataFrame, answer_col: str
    ) -> Dict[str, Any]:
        """Cross-tabulation of a numeric question by attribute."""
        cross_tab: Dict[str, Any] = {}

        if "sex" in df.columns:
            cross_tab["by_sex"] = (
                df.group_by("sex")
                .agg(
                    pl.col(answer_col)
                    .cast(pl.Float64, strict=False)
                    .mean()
                    .alias("mean")
                )
                .to_dicts()
            )

        if "age" in df.columns:
            df_with_bracket = df.with_columns(
                (pl.col("age").cast(pl.Int64, strict=False) // 10 * 10).alias(
                    "age_bracket"
                )
            )
            cross_tab["by_age"] = (
                df_with_bracket.group_by("age_bracket")
                .agg(
                    pl.col(answer_col)
                    .cast(pl.Float64, strict=False)
                    .mean()
                    .alias("mean")
                )
                .sort("age_bracket")
                .to_dicts()
            )

        return cross_tab

    @staticmethod
    def _sample_free_text(
        df: pl.DataFrame, answer_col: str, limit: int = 10
    ) -> List[str]:
        """Sample extraction of free-text answers (blank ones excluded)."""
        responses = SurveyBatchService._answered(df, answer_col)[answer_col].to_list()
        responses.sort(key=len, reverse=True)
        return responses[:limit]

    # =========================================================================
    # Generic dataset preview
    # =========================================================================

    def execute_dataset_preview(
        self,
        s3_path: str,
        count_sql: str,
        data_sql: str,
        params: Optional[List[Any]] = None,
    ) -> Tuple[int, List[str], List[List[Any]]]:
        """Run a COUNT + SELECT preview on any dataset file.

        Creates a DuckDB connection, registers an S3/local file as the VIEW "dataset",
        then runs the given SQL. The connection is closed each time, not cached.

        Returns:
            (total_count, column_names, rows)
        """
        conn = duckdb.connect(":memory:")
        try:
            if s3_path.startswith("s3://"):
                # httpfs is built in at build time. To avoid run-time downloads only LOAD is done
                # (no INSTALL; in environments where it is not built in, fail fast at LOAD).
                _load_httpfs(conn)

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
                conn.execute(
                    "SET s3_region = $1;", [self.region_name]
                )  # nosemgrep: sqlalchemy-execute-raw-query

                if not re.fullmatch(r"s3://[a-zA-Z0-9.\-]+/[a-zA-Z0-9.\-_/]+", s3_path):
                    logger.error("Invalid S3 URI format: %s", s3_path)
                    raise SurveyBatchServiceError(
                        "invalid s3 uri format",
                        code=ErrorCode.SURVEY_OPERATION_FAILED,
                    )

                read_fn = (
                    "read_parquet" if s3_path.endswith(".parquet") else "read_csv_auto"
                )
                conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query
                    f"CREATE VIEW dataset AS SELECT * FROM {read_fn}('{s3_path}');"
                )
            else:
                local_path = s3_path.replace("local://", "")
                if ".." in local_path or not re.fullmatch(
                    r"[a-zA-Z0-9_][a-zA-Z0-9.\-_/]*", local_path
                ):
                    logger.error("Invalid local path format: %s", local_path)
                    raise SurveyBatchServiceError(
                        "invalid local path format",
                        code=ErrorCode.SURVEY_OPERATION_FAILED,
                    )
                read_fn = (
                    "read_parquet"
                    if local_path.endswith(".parquet")
                    else "read_csv_auto"
                )
                conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query
                    f"CREATE VIEW dataset AS SELECT * FROM {read_fn}('{local_path}');"
                )

            count_result = conn.execute(
                count_sql, params or []
            )  # nosemgrep: sqlalchemy-execute-raw-query
            count_row = count_result.fetchone()
            total_count = count_row[0] if count_row else 0

            data_result = conn.execute(
                data_sql, params or []
            )  # nosemgrep: sqlalchemy-execute-raw-query
            columns = [desc[0] for desc in data_result.description]
            rows = [list(row) for row in data_result.fetchall()]

            return total_count, columns, rows
        finally:
            conn.close()
