"""
SurveyBatchService
DuckDB/Parquetクエリ + Bedrock Batch Inference + HuggingFace DL に特化した
Service層クラス。他のServiceに依存しない。

S3への読み書きはManager層が担当し、本Serviceは純粋なSDK操作のみを行う。
"""

import io
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import polars as pl

logger = logging.getLogger(__name__)


class SurveyBatchServiceError(Exception):
    """SurveyBatchService層の基底例外"""

    pass


# ペルソナデータセットのフィルタ可能な属性カラムマッピング
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

# Hugging Face データセット名
DATASET_NAME = "nvidia/Nemotron-Personas-Japan"

# S3上のParquetファイルの固定キー
PARQUET_S3_KEY = "persona-dataset/nemotron-personas-japan.parquet"  # gitleaks:allow

CUSTOM_DATASET_PREFIX = "persona-dataset/custom/"

# DuckDBクエリで使用する有効なカラム名（SQLインジェクション防止用）
_VALID_COLUMNS = frozenset(
    list(FILTER_COLUMN_MAP.values())
    + PERSONA_ATTRIBUTE_COLUMNS
    + PERSONA_PROFILE_COLUMNS
    + ["uuid"]
)


class SurveyBatchService:
    """DuckDB/Parquetクエリ + Bedrock Batch Inference + HuggingFace DL

    他のServiceに依存しない。S3操作はManager層が担当する。
    DuckDB接続に必要な bucket_name / region_name はコンストラクタで受け取る。
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

    # =========================================================================
    # HuggingFace データセット
    # =========================================================================

    def download_nemotron_dataset(self) -> bytes:
        """HuggingFaceからNemotronデータセットをダウンロードしてParquetバイト列を返す。

        S3へのアップロードはManager層が担当する。
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
            logger.error(f"Failed to download Nemotron dataset: {e}")
            raise SurveyBatchServiceError(
                f"Nemotronデータセットのダウンロードに失敗しました: {e}"
            ) from e

    # =========================================================================
    # カスタムデータセット Parquet変換
    # =========================================================================

    def convert_csv_to_parquet(
        self,
        csv_bytes: bytes,
        column_mapping: Optional[Dict[str, str]] = None,
    ) -> Tuple[pl.DataFrame, bytes]:
        """CSVをバリデーション・マッピング・Parquet変換する。

        Returns:
            (DataFrame, parquet_bytes): 変換後のDataFrameとParquetバイト列。
            S3へのアップロードはManager層が担当する。
        """
        try:
            df = pl.read_csv(io.BytesIO(csv_bytes), infer_schema_length=1000)
        except Exception as e:
            raise SurveyBatchServiceError(f"CSVの読み込みに失敗しました: {e}") from e

        if len(df) == 0:
            raise SurveyBatchServiceError("CSVにデータ行がありません。")

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
                    "テキスト型のカラムが見つかりません。「ペルソナ概要」にマッピングするカラムを指定してください。"
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
    # DuckDB/Parquet クエリ
    # =========================================================================

    def set_parquet_s3_uri(self, s3_uri: str) -> None:
        """Manager層がS3上のParquet存在確認後に呼ぶ。DuckDB接続キャッシュをリセット。"""
        self._parquet_s3_uri = s3_uri

    def has_parquet_uri(self) -> bool:
        """Parquet S3 URIがセット済みかを返す。"""
        return self._parquet_s3_uri is not None

    def invalidate_datasource(self, datasource: str) -> None:
        """指定datasourceのDuckDB接続・フィルタキャッシュを破棄する。"""
        self._duckdb_conns.pop(datasource, None)
        self._filter_values_cache.pop(datasource, None)

    def _get_duckdb_conn(
        self, datasource: str = "nemotron"
    ) -> duckdb.DuckDBPyConnection:
        """DuckDB接続を取得する（datasourceごとにキャッシュ）。"""
        if datasource in self._duckdb_conns:
            return self._duckdb_conns[datasource]

        if datasource.startswith("custom:"):
            name = datasource.split(":", 1)[1]
            key = f"{CUSTOM_DATASET_PREFIX}{name}.parquet"
            s3_uri = f"s3://{self.bucket_name}/{key}"
        else:
            if self._parquet_s3_uri is None:
                raise SurveyBatchServiceError(
                    "Nemotronデータセットがまだダウンロードされていません。"
                    "アンケート調査 > ペルソナデータ設定からデータセットをダウンロードしてください。"
                )
            s3_uri = self._parquet_s3_uri

        if not re.fullmatch(r"s3://[a-zA-Z0-9.\-]+/[a-zA-Z0-9.\-_/　-鿿＀-￯]+", s3_uri):
            raise SurveyBatchServiceError(f"Invalid S3 URI format: {s3_uri}")

        conn = duckdb.connect(":memory:")
        conn.execute(
            "INSTALL httpfs; LOAD httpfs;"
        )  # nosemgrep: sqlalchemy-execute-raw-query

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
        conn.execute(
            f"CREATE VIEW personas AS SELECT * FROM read_parquet('{s3_uri}');"
        )  # nosemgrep: sqlalchemy-execute-raw-query

        self._duckdb_conns[datasource] = conn
        return conn

    def _query_duckdb(
        self, sql: str, params: Optional[List[Any]] = None, datasource: str = "nemotron"
    ) -> pl.DataFrame:
        """DuckDBでS3上のParquetに対してSQLクエリを実行し、Polars DataFrameで返す。"""
        conn = self._get_duckdb_conn(datasource)
        try:
            if params:
                result = conn.execute(sql, params)
            else:
                result = conn.execute(sql)
            return result.pl()
        except duckdb.IOException:
            self._duckdb_conns.pop(datasource, None)
            conn = self._get_duckdb_conn(datasource)
            if params:
                result = conn.execute(sql, params)
            else:
                result = conn.execute(sql)
            return result.pl()

    def _build_where_clause(
        self, filters: Dict[str, Any]
    ) -> Tuple[str, List[Any], int]:
        """フィルタ辞書からWHERE句・パラメータ・次のパラメータインデックスを返す。"""
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
        """データセットの総レコード数を取得する。"""
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
        """フィルタリングとサンプリングをDuckDB SQL内で1クエリで実行する。"""
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
        """フィルタ可能な属性値の一覧をフィルタタイプ情報付きで取得する（TTLキャッシュ）。"""
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
        """フィルタ条件に合致するペルソナ数を取得する。"""
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
        """プレビュー用の属性分布統計を取得する（DuckDB集計）。"""
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
                continue  # カラム不在・型不一致時はこの属性をスキップ

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
            pass  # 年齢統計取得失敗は無視して他の統計を返す

        return stats

    # =========================================================================
    # Batch Inference
    # =========================================================================

    def execute_batch_inference(
        self,
        prompts: List[Dict[str, Any]],
        model_id: str | None = None,
        s3_input_prefix: str = "batch-inference/input/",
        s3_output_prefix: str = "batch-inference/output/",
    ) -> List[Dict[str, Any]]:
        """Bedrock Batch Inference APIを使用して非同期推論を実行する。

        boto3クライアントを直接生成する（S3Service非依存）。
        """
        import boto3
        from ..config import Config

        try:
            cfg = Config()
            if model_id is None:
                model_id = cfg.BATCH_INFERENCE_MODEL_ID
            role_arn = cfg.BEDROCK_BATCH_ROLE_ARN
            if not role_arn:
                raise SurveyBatchServiceError(
                    "BEDROCK_BATCH_ROLE_ARN が設定されていません。"
                    "環境変数にBedrock Batch Inference用のIAMロールARNを設定してください。"
                )

            bedrock_client = boto3.client("bedrock", region_name=cfg.AWS_REGION)
            s3_client = boto3.client("s3", region_name=cfg.AWS_REGION)

            job_id = str(uuid.uuid4())
            input_key = f"{s3_input_prefix}{job_id}/input.jsonl"
            jsonl_content = "\n".join(
                json.dumps(p, ensure_ascii=False) for p in prompts
            )

            s3_client.put_object(
                Bucket=self.bucket_name,
                Key=input_key,
                Body=jsonl_content.encode("utf-8"),
            )
            input_s3_uri = f"s3://{self.bucket_name}/{input_key}"
            output_s3_uri = f"s3://{self.bucket_name}/{s3_output_prefix}{job_id}/"

            logger.info(f"Uploaded batch input to {input_s3_uri}")

            response = bedrock_client.create_model_invocation_job(
                jobName=f"survey-batch-{job_id[:8]}",
                modelId=model_id,
                roleArn=role_arn,
                inputDataConfig={"s3InputDataConfig": {"s3Uri": input_s3_uri}},
                outputDataConfig={"s3OutputDataConfig": {"s3Uri": output_s3_uri}},
            )
            job_arn = response["jobArn"]
            logger.info(f"Created batch inference job: {job_arn}")

            results = self._poll_batch_job(bedrock_client, job_arn)

            if results is None:
                output_results = self._fetch_batch_output(
                    s3_client, self.bucket_name, f"{s3_output_prefix}{job_id}/"
                )
                return output_results

            return results

        except SurveyBatchServiceError:
            raise
        except Exception as e:
            logger.error(f"Batch inference failed: {e}")
            raise SurveyBatchServiceError(f"バッチ推論の実行に失敗しました: {e}") from e

    def _poll_batch_job(
        self,
        bedrock_client: Any,
        job_arn: str,
        poll_interval: int = 60,
        max_wait: int = 18000,
    ) -> Optional[List[Dict[str, Any]]]:
        """バッチ推論ジョブの完了をポーリングする。"""
        elapsed = 0
        while elapsed < max_wait:
            response = bedrock_client.get_model_invocation_job(jobIdentifier=job_arn)
            status = response["status"]
            logger.info(f"Batch job status: {status} (elapsed: {elapsed}s)")

            if status == "Completed":
                return None
            elif status in ("Failed", "Stopped"):
                message = response.get("message", "Unknown error")
                raise SurveyBatchServiceError(
                    f"バッチ推論ジョブが失敗しました: {status} - {message}"
                )

            time.sleep(poll_interval)  # nosemgrep: arbitrary-sleep
            elapsed += poll_interval

        raise SurveyBatchServiceError(
            f"バッチ推論ジョブがタイムアウトしました（{max_wait}秒）"
        )

    def _fetch_batch_output(
        self, s3_client: Any, bucket_name: str, output_prefix: str
    ) -> List[Dict[str, Any]]:
        """S3からバッチ推論の出力結果を取得する。"""
        results: List[Dict[str, Any]] = []

        response = s3_client.list_objects_v2(Bucket=bucket_name, Prefix=output_prefix)
        for obj in response.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".jsonl") or key.endswith(".jsonl.out"):
                data = s3_client.get_object(Bucket=bucket_name, Key=key)
                content = data["Body"].read().decode("utf-8")
                for line in content.strip().split("\n"):
                    if line.strip():
                        results.append(json.loads(line))

        logger.info(f"Fetched {len(results)} results from batch output")
        return results

    # =========================================================================
    # Batch Inference入力構築（Bedrock API形式固有）
    # =========================================================================

    def build_batch_prompts(
        self,
        personas_df: pl.DataFrame,
        system_prompts: List[str],
        user_messages: List[List[Dict[str, Any]]],
        output_schema: dict,
    ) -> List[Dict[str, Any]]:
        """ペルソナごとのBedrock Batch Inference入力を構築する。

        Args:
            personas_df: ペルソナデータ（uuid列必須）
            system_prompts: 各ペルソナ用のシステムプロンプト文字列リスト
            user_messages: 各ペルソナ用のユーザーメッセージcontent配列リスト
            output_schema: Structured Output用JSONスキーマ

        Returns:
            Bedrock Batch Inference用のJSONL入力リスト
        """
        prompts: List[Dict[str, Any]] = []

        for i, row in enumerate(personas_df.iter_rows(named=True)):
            record_id = str(row.get("uuid", f"p{i}"))
            prompt = {
                "recordId": record_id,
                "modelInput": {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 2048,
                    "system": system_prompts[i],
                    "messages": [{"role": "user", "content": user_messages[i]}],
                    "output_config": {
                        "format": {"type": "json_schema", "schema": output_schema}
                    },
                },
            }
            prompts.append(prompt)

        logger.info(f"Built {len(prompts)} batch inference prompts")
        return prompts

    # =========================================================================
    # 統計要約生成（Polars/DataFrame操作）
    # =========================================================================

    def generate_statistical_summary(
        self,
        results_csv: str,
        questions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """CSV結果から統計要約を生成する。

        Args:
            results_csv: CSV形式の結果テキスト
            questions: 質問メタデータのリスト。各要素は
                {"id": str, "text": str, "question_type": str, "options": list, "allow_multiple": bool}

        Returns:
            統計要約辞書
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
        """人口統計情報を集計する。"""
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
    def _analyze_multiple_choice(
        df: pl.DataFrame,
        answer_col: str,
        options: List[str],
        allow_multiple: bool = False,
    ) -> Dict[str, Any]:
        """選択式質問の分析。"""
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
        """スケール評価質問の分析。"""
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
        """選択式質問の属性別クロス集計。"""
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
        """数値質問の属性別クロス集計。"""
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
        """自由記述のサンプル抽出。"""
        responses = df[answer_col].drop_nulls().to_list()
        responses.sort(key=len, reverse=True)
        return responses[:limit]
