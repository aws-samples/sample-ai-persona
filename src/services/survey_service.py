"""
SurveyService
アンケート実行に関する外部サービス連携を担当するサービスクラス。
Hugging Faceデータ取得、Bedrock Batch Inference実行、S3操作、インサイト生成を担当する。

メモリ効率改善: DuckDB + Polars を使用し、S3上のParquetファイルに直接クエリを実行。
100万行のデータセットをメモリに全展開せず、必要なカラム・行だけを読み込む。
"""

import csv
import io
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import polars as pl

from src.models.survey import InsightReport
from src.models.survey_template import Question, SurveyTemplate
from src.services.ai_service import AIService
from src.services.s3_service import S3Service

logger = logging.getLogger(__name__)


class SurveyServiceError(Exception):
    """SurveyService層の基底例外"""

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

# ペルソナ属性としてプロンプトに含めるカラム
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

# ペルソナのプロフィール情報カラム
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

# DuckDBクエリで使用する有効なカラム名（SQLインジェクション防止用）
_VALID_COLUMNS = frozenset(
    list(FILTER_COLUMN_MAP.values())
    + PERSONA_ATTRIBUTE_COLUMNS
    + PERSONA_PROFILE_COLUMNS
    + ["uuid"]
)


class SurveyService:
    """アンケート実行に関する外部サービス連携を担当するサービスクラス"""

    _CACHE_TTL_SECONDS: int = 300

    def __init__(self, ai_service: AIService, s3_service: S3Service) -> None:
        self.ai_service = ai_service
        self.s3_service = s3_service
        self._parquet_s3_uri: Optional[str] = None
        self._csv_cache: Dict[str, Tuple[float, bytes]] = {}
        self._duckdb_conns: Dict[str, duckdb.DuckDBPyConnection] = {}
        self._filter_values_cache: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}

    # =========================================================================
    # 画像圧縮（Batch Inference用）
    # =========================================================================

    @staticmethod
    def compress_image_for_batch(
        image_bytes: bytes, max_side: int = 768, quality: int = 50
    ) -> Tuple[bytes, str]:
        """
        画像をリサイズ・JPEG圧縮してbase64用のバイト列を返す。

        Args:
            image_bytes: 元画像のバイト列
            max_side: 長辺の最大ピクセル数（デフォルト: 768）
            quality: JPEG品質（1-100、デフォルト: 50）

        Returns:
            (compressed_bytes, media_type): 圧縮後バイト列とメディアタイプ
        """
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        # リサイズ（長辺がmax_sideを超える場合のみ）
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), Image.LANCZOS)  # type: ignore[attr-defined]
        # RGBA→RGB変換（JPEG保存用）
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")  # type: ignore[assignment]
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), "image/jpeg"

    # =========================================================================
    # データセットステータス確認
    # =========================================================================

    def check_nemotron_dataset_status(self) -> dict:
        """Nemotronデータセットの存在状況を返す。"""
        bucket = self.s3_service.bucket_name
        try:
            resp = self.s3_service.s3_client.head_object(
                Bucket=bucket, Key=PARQUET_S3_KEY  # gitleaks:allow
            )
            size_mb = round(resp["ContentLength"] / (1024 * 1024), 1)
            return {"exists": True, "size_mb": size_mb}
        except self.s3_service.s3_client.exceptions.ClientError:
            return {"exists": False, "size_mb": 0}

    def download_nemotron_dataset(self) -> dict:
        """Nemotronデータセットをダウンロードし、S3にParquetとして配置する。"""
        self._parquet_s3_uri = None  # キャッシュクリア
        self._duckdb_conns.pop("nemotron", None)
        self._filter_values_cache.pop("nemotron", None)

        bucket = self.s3_service.bucket_name
        s3_uri = f"s3://{bucket}/{PARQUET_S3_KEY}"

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

            self.s3_service.upload_file(parquet_bytes, PARQUET_S3_KEY)
            logger.info(f"Parquet uploaded to S3: {s3_uri}")
            self._parquet_s3_uri = s3_uri
        except Exception as e:
            logger.error(f"Failed to download Nemotron dataset: {e}")
            raise SurveyServiceError(
                f"Nemotronデータセットのダウンロードに失敗しました: {e}"
            ) from e

        return self.check_nemotron_dataset_status()

    # =========================================================================
    # カスタムデータセット管理
    # =========================================================================

    CUSTOM_DATASET_PREFIX = "persona-dataset/custom/"

    # マッピング可能な標準カラム定義
    STANDARD_COLUMNS = {
        "persona": {"label": "ペルソナ概要", "required": True, "group": "プロフィール"},
        "sex": {"label": "性別", "required": False, "group": "属性"},
        "age": {"label": "年齢", "required": False, "group": "属性"},
        "occupation": {"label": "職業", "required": False, "group": "属性"},
        "country": {"label": "出身国", "required": False, "group": "属性"},
        "region": {"label": "居住地域", "required": False, "group": "属性"},
        "prefecture": {"label": "都道府県", "required": False, "group": "属性"},
        "marital_status": {"label": "結婚・子供の有無", "required": False, "group": "属性"},
        "education_level": {"label": "学歴", "required": False, "group": "属性"},
        "cultural_background": {"label": "文化的背景", "required": False, "group": "プロフィール"},
        "skills_and_expertise": {"label": "スキル・専門知識", "required": False, "group": "プロフィール"},
        "hobbies_and_interests": {"label": "趣味・関心", "required": False, "group": "プロフィール"},
        "career_goals_and_ambitions": {"label": "キャリア目標", "required": False, "group": "プロフィール"},
    }

    def parse_csv_columns(self, csv_bytes: bytes) -> dict:
        """CSVを読み込み、カラム一覧とサンプルデータを返す。"""
        try:
            df = pl.read_csv(io.BytesIO(csv_bytes), infer_schema_length=1000, n_rows=5)
        except Exception as e:
            raise SurveyServiceError(f"CSVの読み込みに失敗しました: {e}") from e
        # 各カラムのサンプル値（最大3件）
        samples = {}
        for col in df.columns:
            vals = df[col].drop_nulls().head(3).to_list()
            samples[col] = [str(v) for v in vals]
        # 自動マッピング候補（カラム名が一致するもの）
        auto_mapping = {}
        for col in df.columns:
            col_lower = col.lower().strip()
            for std_col in self.STANDARD_COLUMNS:
                if col_lower == std_col or col_lower == self.STANDARD_COLUMNS[std_col]["label"]:
                    auto_mapping[std_col] = col
                    break
        return {
            "columns": df.columns,
            "samples": samples,
            "row_count_preview": len(df),
            "auto_mapping": auto_mapping,
        }

    def _save_dataset_metadata(self, name: str, metadata: Dict[str, Any]) -> None:
        """カスタムデータセットのメタデータをS3にJSON保存する。"""
        meta_key = f"{self.CUSTOM_DATASET_PREFIX}{name}.meta.json"
        self.s3_service.upload_file(
            json.dumps(metadata, ensure_ascii=False).encode("utf-8"), meta_key
        )
        logger.info(f"Dataset metadata saved: {meta_key}")

    def load_dataset_metadata(self, name: str) -> Optional[Dict[str, Any]]:
        """カスタムデータセットのメタデータをS3から読み込む。存在しなければNoneを返す。"""
        meta_key = f"{self.CUSTOM_DATASET_PREFIX}{name}.meta.json"
        try:
            bucket = self.s3_service.bucket_name
            raw = self.s3_service.download_file(f"s3://{bucket}/{meta_key}")
            return dict(json.loads(raw.decode("utf-8")))
        except Exception:
            return None

    def upload_custom_dataset(
        self, csv_bytes: bytes, filename: str,
        column_mapping: Optional[Dict[str, str]] = None,
        extra_columns: Optional[List[Dict[str, str]]] = None,
    ) -> dict:
        """
        CSVをバリデーション・マッピング・Parquet変換してS3に保存する。

        Args:
            csv_bytes: CSVファイルのバイト列
            filename: 元ファイル名
            column_mapping: {標準カラム名: CSVカラム名} のマッピング辞書
            extra_columns: その他カラム定義のリスト
                [{"csv_column": "会員ランク", "label": "会員ランク", "description": "補足情報"}]

        Returns:
            dict: name, row_count, columns, size_mb
        """
        try:
            df = pl.read_csv(io.BytesIO(csv_bytes), infer_schema_length=1000)
        except Exception as e:
            raise SurveyServiceError(f"CSVの読み込みに失敗しました: {e}") from e

        if len(df) == 0:
            raise SurveyServiceError("CSVにデータ行がありません。")

        # マッピングに基づきカラムをリネーム
        if column_mapping:
            rename_map = {}
            for std_col, csv_col in column_mapping.items():
                if csv_col and csv_col in df.columns and std_col != csv_col:
                    rename_map[csv_col] = std_col
            if rename_map:
                df = df.rename(rename_map)

        # persona列がなければテキストカラムを結合して生成
        if "persona" not in df.columns:
            text_cols = [c for c in df.columns if df[c].dtype == pl.Utf8]
            if not text_cols:
                raise SurveyServiceError(
                    "テキスト型のカラムが見つかりません。「ペルソナ概要」にマッピングするカラムを指定してください。"
                )
            df = df.with_columns(
                pl.concat_str(
                    [pl.col(c).fill_null("") for c in text_cols], separator=" / "
                ).alias("persona")
            )

        # uuid列がなければ追加
        if "uuid" not in df.columns:
            df = df.with_columns(
                pl.Series("uuid", [str(uuid.uuid4()) for _ in range(len(df))])
            )

        # Parquet変換してS3にアップロード
        import tempfile
        import os

        name = Path(filename).stem
        parquet_key = f"{self.CUSTOM_DATASET_PREFIX}{name}.parquet"

        with tempfile.TemporaryDirectory() as tmpdir:
            parquet_path = os.path.join(tmpdir, "data.parquet")
            df.write_parquet(parquet_path)
            with open(parquet_path, "rb") as f:
                parquet_bytes = f.read()

        self.s3_service.upload_file(parquet_bytes, parquet_key)
        logger.info(
            f"Custom dataset uploaded: {parquet_key} ({len(df)} rows, {len(df.columns)} cols)"
        )

        # メタデータを保存（標準マッピング + その他カラム定義）
        metadata: Dict[str, Any] = {
            "standard_mapping": column_mapping or {},
            "extra_columns": extra_columns or [],
        }
        self._save_dataset_metadata(name, metadata)

        return {
            "name": name,
            "row_count": len(df),
            "columns": df.columns,
            "size_mb": round(len(parquet_bytes) / (1024 * 1024), 1),
        }

    def list_custom_datasets(self) -> list:
        """S3上のカスタムデータセット一覧を返す。"""
        bucket = self.s3_service.bucket_name
        prefix = self.CUSTOM_DATASET_PREFIX
        datasets = []
        try:
            resp = self.s3_service.s3_client.list_objects_v2(
                Bucket=bucket, Prefix=prefix
            )
            for obj in resp.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".parquet"):
                    continue
                name = Path(key).stem
                size_mb = round(obj["Size"] / (1024 * 1024), 1)
                datasets.append({"name": name, "s3_key": key, "size_mb": size_mb})
        except Exception as e:
            logger.warning(f"Failed to list custom datasets: {e}")
        return datasets

    def delete_custom_dataset(self, name: str) -> None:
        """カスタムデータセットを削除する。"""
        bucket = self.s3_service.bucket_name
        key = f"{self.CUSTOM_DATASET_PREFIX}{name}.parquet"
        self.s3_service.s3_client.delete_object(Bucket=bucket, Key=key)
        # メタデータも削除
        meta_key = f"{self.CUSTOM_DATASET_PREFIX}{name}.meta.json"
        try:
            self.s3_service.s3_client.delete_object(Bucket=bucket, Key=meta_key)
        except Exception:
            pass
        # このデータソースのキャッシュ済み接続を破棄
        ds_key = f"custom:{name}"
        self._duckdb_conns.pop(ds_key, None)
        self._filter_values_cache.pop(ds_key, None)
        logger.info(f"Custom dataset deleted: {key}")

    # =========================================================================
    # Parquetデータ管理（S3配置・DuckDBクエリ）
    # =========================================================================

    def _ensure_parquet_on_s3(self) -> str:
        """
        S3上にParquetファイルが存在することを確認する。
        存在しない場合はエラーを投げる（ペルソナデータ設定から事前にダウンロードが必要）。

        Returns:
            str: S3上のParquetファイルのURI (s3://bucket/key)
        """
        if self._parquet_s3_uri is not None:
            return self._parquet_s3_uri

        bucket = self.s3_service.bucket_name
        s3_uri = f"s3://{bucket}/{PARQUET_S3_KEY}"

        # S3にParquetが存在するか確認
        try:
            self.s3_service.s3_client.head_object(Bucket=bucket, Key=PARQUET_S3_KEY)
            logger.info(f"Parquet already exists on S3: {s3_uri}")
            self._parquet_s3_uri = s3_uri
            return s3_uri
        except self.s3_service.s3_client.exceptions.ClientError:
            raise SurveyServiceError(
                "Nemotronデータセットがまだダウンロードされていません。"
                "アンケート調査 > ペルソナデータ設定からデータセットをダウンロードしてください。"
            )

    def _get_duckdb_conn(self, datasource: str = "nemotron") -> duckdb.DuckDBPyConnection:
        """
        DuckDB接続を取得する（datasourceごとにキャッシュ）。
        """
        if datasource in self._duckdb_conns:
            return self._duckdb_conns[datasource]

        # データソースに応じてS3 URIを決定
        if datasource.startswith("custom:"):
            name = datasource.split(":", 1)[1]
            key = f"{self.CUSTOM_DATASET_PREFIX}{name}.parquet"
            bucket = self.s3_service.bucket_name
            s3_uri = f"s3://{bucket}/{key}"
        else:
            s3_uri = self._ensure_parquet_on_s3()
        # Validate s3_uri format to prevent injection via DuckDB SQL
        import re

        if not re.fullmatch(r"s3://[a-zA-Z0-9.\-]+/[a-zA-Z0-9.\-_/]+", s3_uri):
            raise SurveyServiceError(f"Invalid S3 URI format: {s3_uri}")

        # DuckDB connection setup — all values are either static strings,
        # parameterized ($1), or validated above. Not SQLAlchemy.
        conn = duckdb.connect(":memory:")
        conn.execute("INSTALL httpfs; LOAD httpfs;")  # nosemgrep: sqlalchemy-execute-raw-query

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
        conn.execute("SET s3_region = $1;", [self.s3_service.region_name])  # nosemgrep: sqlalchemy-execute-raw-query
        # s3_uri is validated above; CREATE VIEW does not support parameter binding
        conn.execute(f"CREATE VIEW personas AS SELECT * FROM read_parquet('{s3_uri}');")  # nosemgrep: sqlalchemy-execute-raw-query

        self._duckdb_conns[datasource] = conn
        return conn

    def _query_duckdb(
        self, sql: str, params: Optional[List[Any]] = None, datasource: str = "nemotron"
    ) -> pl.DataFrame:
        """
        DuckDBでS3上のParquetに対してSQLクエリを実行し、Polars DataFrameで返す。
        接続はdatasourceごとにキャッシュ・再利用される。
        """
        conn = self._get_duckdb_conn(datasource)
        try:
            if params:
                result = conn.execute(sql, params)
            else:
                result = conn.execute(sql)
            return result.pl()
        except duckdb.IOException:
            # 認証トークン期限切れ等の場合、接続を再作成
            self._duckdb_conns.pop(datasource, None)
            conn = self._get_duckdb_conn(datasource)
            if params:
                result = conn.execute(sql, params)
            else:
                result = conn.execute(sql)
            return result.pl()

    def _get_total_count(self, datasource: str = "nemotron") -> int:
        """データセットの総レコード数を取得する。"""
        df = self._query_duckdb("SELECT count(*) AS cnt FROM personas", datasource=datasource)
        return int(df["cnt"][0])

    # =========================================================================
    # ペルソナデータ管理
    # =========================================================================

    def filter_personas(
        self, filters: Dict[str, Any], df: Optional[pl.DataFrame] = None,
        datasource: str = "nemotron",
    ) -> pl.DataFrame:
        """
        属性フィルタによるペルソナの絞り込み。
        DuckDB SQLでS3上のParquetに直接クエリを実行する。

        Args:
            filters: フィルタ条件の辞書
            df: フィルタ対象のPolars DataFrame。Noneの場合はS3 Parquetに対してクエリ。

        Returns:
            pl.DataFrame: フィルタ済みのペルソナデータ
        """
        # dfが渡された場合はPolarsのネイティブフィルタリングを使用
        if df is not None:
            return self._filter_polars(filters, df)

        # DuckDB SQLでフィルタリング
        where_clauses: List[str] = []
        params: List[Any] = []
        param_idx = 1

        for key, value in filters.items():
            column = FILTER_COLUMN_MAP.get(key, key)
            if column not in _VALID_COLUMNS:
                logger.warning(f"Unknown filter column: {key} (resolved: {column})")
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
        sql = f"SELECT * FROM personas WHERE {where_sql}"

        result = self._query_duckdb(sql, params if params else None, datasource=datasource)
        logger.info(f"Filtered personas: {len(result)} records (filters: {filters})")
        return result

    def _filter_polars(self, filters: Dict[str, Any], df: pl.DataFrame) -> pl.DataFrame:
        """Polars DataFrameに対するインメモリフィルタリング。"""
        for key, value in filters.items():
            column = FILTER_COLUMN_MAP.get(key, key)
            if column not in df.columns:
                continue
            if value is None or (isinstance(value, (list, str)) and len(value) == 0):
                continue

            filter_type = FILTER_TYPE_MAP.get(key, "select")

            if filter_type == "range" and isinstance(value, dict):
                col = df[column].cast(pl.Float64, strict=False)
                if value.get("min") is not None:
                    df = df.filter(col >= float(value["min"]))
                if value.get("max") is not None:
                    df = df.filter(col <= float(value["max"]))
            elif filter_type == "keyword" and isinstance(value, str):
                df = df.filter(
                    df[column]
                    .cast(pl.Utf8)
                    .str.to_lowercase()
                    .str.contains(value.lower())
                )
            elif isinstance(value, list):
                df = df.filter(df[column].cast(pl.Utf8).is_in([str(v) for v in value]))
            else:
                df = df.filter(df[column].cast(pl.Utf8) == str(value))

        return df

    def sample_personas(self, df: pl.DataFrame, count: int) -> pl.DataFrame:
        """
        指定数のペルソナをランダムサンプリングする。

        Args:
            df: サンプリング対象のPolars DataFrame
            count: サンプリング数

        Returns:
            pl.DataFrame: サンプリングされたペルソナデータ
        """
        actual_count = min(count, len(df))
        if actual_count <= 0:
            return df.head(0)

        sampled = df.sample(n=actual_count)
        logger.info(f"Sampled {actual_count} personas from {len(df)} available")
        return sampled

    def filter_and_sample_personas(
        self, filters: Dict[str, Any], count: int,
        datasource: str = "nemotron",
    ) -> pl.DataFrame:
        """
        フィルタリングとサンプリングをDuckDB SQL内で1クエリで実行する。
        フィルタ結果全体をメモリに展開せず、サンプリング後の行だけを返す。

        Args:
            filters: フィルタ条件の辞書
            count: サンプリング数

        Returns:
            pl.DataFrame: フィルタ＋サンプリング済みのペルソナデータ
        """
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

    def get_available_filter_values(self, datasource: str = "nemotron") -> Dict[str, Dict[str, Any]]:
        """
        フィルタ可能な属性値の一覧をフィルタタイプ情報付きで取得する。
        DuckDB SQLで集計クエリを実行し、メモリに全データを展開しない。
        結果はdatasourceごとにTTLキャッシュされる。
        """
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

    def get_filtered_count(self, filters: Optional[Dict[str, Any]] = None, datasource: str = "nemotron") -> int:
        """フィルタ条件に合致するペルソナ数を取得する（COUNT集計のみ）。"""
        if not filters:
            return self._get_total_count(datasource)

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
        df = self._query_duckdb(
            f"SELECT count(*) AS cnt FROM personas WHERE {where_sql}",
            params if params else None,
            datasource=datasource,
        )
        return int(df["cnt"][0])

    def get_preview_stats(
        self, filters: Optional[Dict[str, Any]] = None,
        datasource: str = "nemotron",
    ) -> Dict[str, Dict[str, Any]]:
        """プレビュー用の属性分布統計を取得する（DuckDB集計）。"""
        where_clauses: List[str] = []
        params: List[Any] = []
        param_idx = 1

        if filters:
            for key, value in filters.items():
                column = FILTER_COLUMN_MAP.get(key, key)
                if column not in _VALID_COLUMNS:
                    continue
                if value is None or (
                    isinstance(value, (list, str)) and len(value) == 0
                ):
                    continue
                filter_type = FILTER_TYPE_MAP.get(key, "select")
                if filter_type == "range" and isinstance(value, dict):
                    if value.get("min") is not None:
                        where_clauses.append(
                            f"CAST({column} AS DOUBLE) >= ${param_idx}"
                        )
                        params.append(float(value["min"]))
                        param_idx += 1
                    if value.get("max") is not None:
                        where_clauses.append(
                            f"CAST({column} AS DOUBLE) <= ${param_idx}"
                        )
                        params.append(float(value["max"]))
                        param_idx += 1
                elif filter_type == "keyword" and isinstance(value, str):
                    where_clauses.append(
                        f"LOWER(CAST({column} AS VARCHAR)) LIKE ${param_idx}"
                    )
                    params.append(f"%{value.lower()}%")
                    param_idx += 1
                elif isinstance(value, list):
                    placeholders = ", ".join(
                        f"${param_idx + i}" for i in range(len(value))
                    )
                    where_clauses.append(
                        f"CAST({column} AS VARCHAR) IN ({placeholders})"
                    )
                    params.extend([str(v) for v in value])
                    param_idx += len(value)
                else:
                    where_clauses.append(f"CAST({column} AS VARCHAR) = ${param_idx}")
                    params.append(str(value))
                    param_idx += 1

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        stats: Dict[str, Dict[str, Any]] = {}
        p = params if params else None

        # 属性分布（上位5件）
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
                pass

        # 年齢統計
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
            pass

        return stats

    # =========================================================================
    # アンケート実行（バッチ推論）
    # =========================================================================

    def build_persona_prompts(
        self, personas_df: pl.DataFrame, template: SurveyTemplate,
        datasource: str = "nemotron",
    ) -> List[Dict[str, Any]]:
        """
        ペルソナ属性からシステムプロンプトを構築し、アンケート質問を含むメッセージを生成する。

        Args:
            personas_df: ペルソナデータのPolars DataFrame
            template: アンケートテンプレート
            datasource: データソース識別子（"nemotron" or "custom:{name}"）

        Returns:
            list[dict]: Bedrock Batch Inference用のプロンプトリスト。
        """
        import base64

        # カスタムデータセットの場合、メタデータからextra_columnsを取得
        extra_columns: Optional[List[Dict[str, str]]] = None
        if datasource.startswith("custom:"):
            name = datasource.split(":", 1)[1]
            metadata = self.load_dataset_metadata(name)
            if metadata:
                extra_columns = metadata.get("extra_columns")

        prompts: List[Dict[str, Any]] = []
        questions_text = self._format_questions_for_prompt(
            template.questions, template.images
        )

        # Structured Outputのスキーマ定義（全ペルソナ共通）
        output_schema = {
            "type": "object",
            "properties": {
                "answers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question_id": {
                                "type": "string",
                                "description": "質問ID（プロンプトに記載されたIDと完全一致）",
                            },
                            "answer": {"type": "string", "description": "回答内容"},
                        },
                        "required": ["question_id", "answer"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["answers"],
            "additionalProperties": False,
        }

        # 画像がある場合、事前に圧縮・base64化（1回のみ、全ペルソナで共有）
        shared_image_contents: List[Dict[str, Any]] = []
        if template.images:
            for img in template.images:
                try:
                    if img.file_path.startswith("s3://"):
                        raw_bytes = self.s3_service.download_file(img.file_path)
                    else:
                        with open(img.file_path, "rb") as f:
                            raw_bytes = f.read()
                    compressed, media_type = self.compress_image_for_batch(raw_bytes)
                    b64 = base64.b64encode(compressed).decode("utf-8")
                    shared_image_contents.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        }
                    )
                except Exception as e:
                    logger.warning(f"Failed to load image {img.name}: {e}")

        for row in personas_df.iter_rows(named=True):
            record_id = str(row.get("uuid", f"p{len(prompts)}"))
            system_prompt = self._build_system_prompt(row, extra_columns=extra_columns)

            user_content: List[Dict[str, Any]] = []
            # 画像を先に追加（参照を共有）
            if shared_image_contents:
                image_desc_parts = [f"- {img.name}" for img in template.images]
                user_content.append(
                    {
                        "type": "text",
                        "text": "以下の画像が添付されています:\n"
                        + "\n".join(image_desc_parts)
                        + "\n",
                    }
                )
                user_content.extend(shared_image_contents)

            user_message = (
                "以下のアンケートに回答してください。\n\n"
                "【重要】回答は質問IDと回答内容のみを出力してください。思考プロセスや説明文は含めないでください。\n\n"
                "【回答方法】\n"
                "- あなたの属性（年齢、職業、学歴、居住地域）だけでなく、文化的背景、趣味、価値観、日常の経験も踏まえて回答してください\n"
                "- 選択式質問（単一回答）: 選択肢の文言をそのまま1つ選択\n"
                "- 選択式質問（複数回答）: 選択肢の文言をパイプ記号（|）で区切る（例: 旅行|外食|ギフト）\n"
                "- 自由記述質問: あなた自身の経験や具体的なエピソードを交えて回答（200文字以内推奨）\n"
                "- スケール評価質問: 指定範囲の整数で回答（例: 1〜5の場合、1・2・3・4・5のいずれか）\n\n"
                f"【アンケート質問】\n{questions_text}"
            )
            user_content.append({"type": "text", "text": user_message})

            prompt = {
                "recordId": record_id,
                "modelInput": {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 2048,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_content}],
                    "output_config": {
                        "format": {"type": "json_schema", "schema": output_schema}
                    },
                },
            }
            prompts.append(prompt)

        logger.info(
            f"Built {len(prompts)} persona prompts for template '{template.name}' (images: {len(template.images)})"
        )
        return prompts

    def _build_system_prompt(
        self, persona_row: Dict[str, Any],
        extra_columns: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """ペルソナ属性からシステムプロンプトを構築する。"""
        parts = [
            "あなたは以下の属性とプロフィールを持つ実在の人物です。この人物になりきってアンケートに回答してください。\n"
            "【重要な回答姿勢】\n"
            "- あなたの職業経験、文化的背景、価値観、日常の習慣に根ざした、あなたならではの視点で回答すること\n"
            "- 一般論や模範的な回答ではなく、あなた個人の本音・実感を反映すること\n"
            "- 自由記述では、あなたの具体的な経験・エピソード・こだわりを盛り込むこと\n",
        ]

        attr_labels = {
            "sex": "性別",
            "age": "年齢",
            "occupation": "職業",
            "country": "出身国",
            "region": "居住地域",
            "prefecture": "都道府県",
            "marital_status": "結婚・子供の有無",
            "education_level": "学歴",
        }
        for col in PERSONA_ATTRIBUTE_COLUMNS:
            value = persona_row.get(col)
            if value is not None:
                label = attr_labels.get(col, col)
                parts.append(f"- {label}: {value}")

        profile_labels = {
            "persona": "ペルソナ概要",
            "cultural_background": "文化的背景",
            "skills_and_expertise": "スキル・専門知識",
            "hobbies_and_interests": "趣味・関心",
            "career_goals_and_ambitions": "キャリア目標",
        }
        for col in PERSONA_PROFILE_COLUMNS:
            value = persona_row.get(col)
            if value is not None:
                label = profile_labels.get(col, col)
                parts.append(f"\n【{label}】\n{value}")

        # その他カラム（補足情報付き）
        if extra_columns:
            extra_parts = []
            for ec in extra_columns:
                csv_col = ec.get("csv_column", "")
                value = persona_row.get(csv_col)
                if value is None:
                    continue
                label = ec.get("label", csv_col)
                desc = ec.get("description", "")
                if desc:
                    extra_parts.append(f"- {label}（{desc}）: {value}")
                else:
                    extra_parts.append(f"- {label}: {value}")
            if extra_parts:
                parts.append("\n【その他情報】")
                parts.extend(extra_parts)

        return "\n".join(parts)

    def _format_questions_for_prompt(
        self, questions: List[Question], images: Optional[List] = None
    ) -> str:
        """質問リストをプロンプト用テキストに変換する。"""
        lines: List[str] = []
        for i, q in enumerate(questions, 1):
            lines.append(f"質問{i} (ID: {q.id}): {q.text}")
            if q.question_type == "multiple_choice":
                if q.allow_multiple:
                    max_note = (
                        f"（最大{q.max_selections}個まで）"
                        if q.max_selections > 0
                        else ""
                    )
                    lines.append(f"  タイプ: 選択式・複数回答{max_note}")
                    lines.append("  【必ず以下の選択肢から選んでください】")
                else:
                    lines.append("  タイプ: 選択式・単一回答")
                    lines.append("  【必ず以下の選択肢から1つ選んでください】")
                for j, opt in enumerate(q.options, 1):
                    lines.append(f"  {j}. {opt}")
                if q.allow_multiple:
                    lines.append(
                        f"  【回答例】{q.options[0]}|{q.options[1] if len(q.options) > 1 else q.options[0]}"
                    )
                    lines.append(
                        "  【注意】選択肢の文言をそのまま使用し、説明や理由は含めないでください"
                    )
                else:
                    lines.append(f"  【回答例】{q.options[0]}")
                    lines.append(
                        "  【注意】選択肢の文言をそのまま使用し、説明や理由は含めないでください"
                    )
            elif q.question_type == "free_text":
                lines.append(
                    "  タイプ: 自由記述（あなた自身の経験・価値観・こだわりを具体的に盛り込んで200文字以内で回答してください。一般的・抽象的な回答は避けてください）"
                )
            elif q.question_type == "scale_rating":
                lines.append(
                    f"  タイプ: スケール評価（{q.scale_min}〜{q.scale_max}の整数で回答してください）"
                )
            lines.append("")
        return "\n".join(lines)

    def execute_batch_inference(
        self,
        prompts: List[Dict[str, Any]],
        model_id: str | None = None,
        s3_input_prefix: str = "batch-inference/input/",
        s3_output_prefix: str = "batch-inference/output/",
    ) -> List[Dict[str, Any]]:
        """
        Bedrock Batch Inference APIを使用して非同期推論を実行する。

        Args:
            prompts: build_persona_promptsで生成されたプロンプトリスト
            model_id: 使用するモデルID（未指定時はConfigから取得）
            s3_input_prefix: S3入力プレフィックス
            s3_output_prefix: S3出力プレフィックス

        Returns:
            list[dict]: 各ペルソナの推論結果リスト
        """
        import boto3
        from src.config import Config

        try:
            cfg = Config()
            if model_id is None:
                model_id = cfg.BATCH_INFERENCE_MODEL_ID
            role_arn = cfg.BEDROCK_BATCH_ROLE_ARN
            if not role_arn:
                raise SurveyServiceError(
                    "BEDROCK_BATCH_ROLE_ARN が設定されていません。"
                    "環境変数にBedrock Batch Inference用のIAMロールARNを設定してください。"
                )

            bedrock_client = boto3.client("bedrock", region_name=cfg.AWS_REGION)
            s3_client = boto3.client("s3", region_name=cfg.AWS_REGION)
            bucket_name = self.s3_service.bucket_name

            # 1. 入力JSONLファイルを作成してS3にアップロード
            job_id = str(uuid.uuid4())
            input_key = f"{s3_input_prefix}{job_id}/input.jsonl"
            jsonl_content = "\n".join(
                json.dumps(p, ensure_ascii=False) for p in prompts
            )

            s3_client.put_object(
                Bucket=bucket_name,
                Key=input_key,
                Body=jsonl_content.encode("utf-8"),
            )
            input_s3_uri = f"s3://{bucket_name}/{input_key}"
            output_s3_uri = f"s3://{bucket_name}/{s3_output_prefix}{job_id}/"

            logger.info(f"Uploaded batch input to {input_s3_uri}")

            # 2. CreateModelInvocationJob呼び出し
            response = bedrock_client.create_model_invocation_job(
                jobName=f"survey-batch-{job_id[:8]}",
                modelId=model_id,
                roleArn=role_arn,
                inputDataConfig={"s3InputDataConfig": {"s3Uri": input_s3_uri}},
                outputDataConfig={"s3OutputDataConfig": {"s3Uri": output_s3_uri}},
            )
            job_arn = response["jobArn"]
            logger.info(f"Created batch inference job: {job_arn}")

            # 3. ジョブ完了をポーリング
            results = self._poll_batch_job(bedrock_client, job_arn)

            # 4. 出力結果をS3から取得
            if results is None:
                output_results = self._fetch_batch_output(
                    s3_client, bucket_name, f"{s3_output_prefix}{job_id}/"
                )
                return output_results

            return results

        except SurveyServiceError:
            raise
        except Exception as e:
            logger.error(f"Batch inference failed: {e}")
            raise SurveyServiceError(f"バッチ推論の実行に失敗しました: {e}") from e

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
                return None  # 呼び出し元でS3から結果を取得
            elif status in ("Failed", "Stopped"):
                message = response.get("message", "Unknown error")
                raise SurveyServiceError(
                    f"バッチ推論ジョブが失敗しました: {status} - {message}"
                )

            time.sleep(poll_interval)  # nosemgrep: arbitrary-sleep
            elapsed += poll_interval

        raise SurveyServiceError(
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
    # CSV結果ファイルの生成・読み込み
    # =========================================================================

    def save_results_to_s3(
        self,
        batch_results: List[Dict[str, Any]],
        personas_df: pl.DataFrame,
        template: SurveyTemplate,
        survey_id: str,
        s3_prefix: str = "survey-results/",
    ) -> str:
        """
        バッチ推論結果をパースし、ペルソナ属性と回答をCSV形式でS3に保存する。

        Args:
            batch_results: バッチ推論の結果リスト
            personas_df: ペルソナデータのPolars DataFrame
            template: アンケートテンプレート
            survey_id: アンケートID
            s3_prefix: S3プレフィックス

        Returns:
            str: S3パス
        """
        try:
            attribute_headers = [
                "persona_id",
                "sex",
                "age",
                "occupation",
                "country",
                "region",
                "prefecture",
                "marital_status",
            ]
            question_headers: List[str] = []
            for q in template.questions:
                question_headers.extend([f"{q.id}_text", f"{q.id}_answer"])

            all_headers = attribute_headers + question_headers

            rows: List[List[str]] = []
            # recordIdでペルソナを引けるようにインデックスを作成
            persona_index: Dict[str, Dict[str, Any]] = {}
            for row in personas_df.iter_rows(named=True):
                rid = str(row.get("uuid", ""))
                persona_index[rid] = row

            for result in batch_results:
                record_id = result.get("recordId", "")
                persona_row = persona_index.get(record_id)

                row_data: List[str] = []
                if persona_row is not None:
                    row_data.append(record_id)
                    for col in [
                        "sex",
                        "age",
                        "occupation",
                        "country",
                        "region",
                        "prefecture",
                        "marital_status",
                    ]:
                        val = persona_row.get(col, "")
                        row_data.append(str(val) if val is not None else "")
                else:
                    row_data = [record_id] + [""] * 7

                answers = self._parse_batch_result_answers(result, template.questions)
                answer_map = {a["question_id"]: a["answer"] for a in answers}

                for q in template.questions:
                    row_data.append(q.text)
                    answer = answer_map.get(q.id, "")
                    # バリデーション済みの回答を使用（無効な場合は空文字列）
                    row_data.append(str(answer))

                rows.append(row_data)

            csv_bytes = self._build_csv_bytes(all_headers, rows)

            s3_key = f"{s3_prefix}{survey_id}/results.csv"
            s3_path = self.s3_service.upload_file(csv_bytes, s3_key)

            logger.info(f"Survey results saved to {s3_path}")
            return s3_path

        except SurveyServiceError:
            raise
        except Exception as e:
            logger.error(f"Failed to save results to S3: {e}")
            raise SurveyServiceError(
                f"アンケート結果のS3保存に失敗しました: {e}"
            ) from e

    def _parse_batch_result_answers(
        self, result: Dict[str, Any], questions: List[Question]
    ) -> List[Dict[str, str]]:
        """バッチ推論結果から回答データをパースし、バリデーションを実行する。

        選択式・スケール評価で無効な回答は空文字列に置き換える。

        Args:
            result: バッチ推論結果
            questions: 質問リスト（バリデーション用）

        Returns:
            バリデーション済み回答リスト
        """
        try:
            model_output = result.get("modelOutput", {})
            # Bedrock Batch Inferenceの出力形式に対応
            if isinstance(model_output, dict):
                content = model_output.get("content", [])
                if isinstance(content, list) and len(content) > 0:
                    text = content[0].get("text", "")
                else:
                    text = str(model_output)
            else:
                text = str(model_output)

            # Structured Outputにより、textは常に有効なJSONオブジェクト
            data = json.loads(text)
            if "answers" not in data or not isinstance(data["answers"], list):
                logger.warning(f"Unexpected response format: {text}")
                return []

            # 質問IDでインデックス化
            question_map = {q.id: q for q in questions}
            validated_answers = []

            for answer_obj in data["answers"]:
                question_id = answer_obj.get("question_id", "")
                answer = answer_obj.get("answer", "")

                question = question_map.get(question_id)
                if not question:
                    validated_answers.append(
                        {"question_id": question_id, "answer": answer}
                    )
                    continue

                # バリデーション実行
                validated_answer = self._validate_answer(answer, question)
                validated_answers.append(
                    {"question_id": question_id, "answer": validated_answer}
                )

            return validated_answers

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse JSON from batch result: {e}")
            return []
        except Exception as e:
            logger.warning(f"Failed to parse batch result: {e}")
            return []

    def _validate_answer(self, answer: str, question: Question) -> str:
        """回答をバリデーションし、無効な場合は空文字列を返す。

        Args:
            answer: 回答文字列
            question: 質問オブジェクト

        Returns:
            有効な回答、または空文字列
        """
        if not answer or not answer.strip():
            return ""

        answer = answer.strip()

        # 選択式質問のバリデーション
        if question.question_type == "multiple_choice":
            if question.allow_multiple:
                # 複数回答: パイプ区切りで分割して各選択肢を検証
                selected = [s.strip() for s in answer.split("|") if s.strip()]
                valid_selected = [s for s in selected if s in question.options]

                if not valid_selected:
                    logger.warning(
                        f"Invalid multiple choice answer for {question.id}: {answer}"
                    )
                    return ""

                # 最大選択数チェック
                if (
                    question.max_selections > 0
                    and len(valid_selected) > question.max_selections
                ):
                    valid_selected = valid_selected[: question.max_selections]

                return "|".join(valid_selected)
            else:
                # 単一回答: 選択肢に含まれているか確認
                if answer not in question.options:
                    logger.warning(
                        f"Invalid single choice answer for {question.id}: {answer}"
                    )
                    return ""
                return answer

        # スケール評価のバリデーション
        elif question.question_type == "scale_rating":
            try:
                value = int(answer)
                if question.scale_min <= value <= question.scale_max:
                    return str(value)
                else:
                    logger.warning(
                        f"Scale rating out of range for {question.id}: {answer}"
                    )
                    return ""
            except ValueError:
                logger.warning(f"Invalid scale rating for {question.id}: {answer}")
                return ""

        # 自由記述はそのまま返す
        return answer

    @staticmethod
    def _build_csv_bytes(headers: List[str], rows: List[List[str]]) -> bytes:
        """CSVヘッダーと行データからCSVバイト列を生成する。"""
        output = io.StringIO()
        writer = csv.writer(output, quoting=csv.QUOTE_ALL)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)
        return output.getvalue().encode("utf-8-sig")

    def load_results_from_s3(self, s3_path: str) -> bytes:
        """
        S3からCSVデータをバイト列として取得する（TTL付きキャッシュ）。

        同一S3パスへのリクエストはキャッシュTTL内であればS3アクセスをスキップする。

        Args:
            s3_path: S3パス

        Returns:
            bytes: CSVデータのバイト列
        """
        try:
            now = time.monotonic()
            cached = self._csv_cache.get(s3_path)
            if cached is not None:
                cached_at, data = cached
                if now - cached_at < self._CACHE_TTL_SECONDS:
                    logger.debug(f"CSV cache hit for {s3_path}")
                    return data

            data = self.s3_service.download_file(s3_path)
            self._csv_cache[s3_path] = (now, data)
            return data
        except Exception as e:
            logger.error(f"Failed to load results from S3: {e}")
            raise SurveyServiceError(f"アンケート結果の取得に失敗しました: {e}") from e

    def invalidate_results_cache(self, s3_path: str) -> None:
        """指定S3パスのキャッシュを無効化する。"""
        self._csv_cache.pop(s3_path, None)

    @staticmethod
    def parse_results_csv(csv_bytes: bytes) -> List[Dict[str, Any]]:
        """
        CSVバイト列を回答データ構造にパースする。

        Args:
            csv_bytes: CSVデータのバイト列

        Returns:
            list[dict]: 各行を辞書として格納したリスト。
                        キーはCSVヘッダー、値は各セルの文字列。
        """
        # BOM付きUTF-8に対応
        text = csv_bytes.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        return [dict(row) for row in reader]

    # =========================================================================
    # インサイトレポート生成
    # =========================================================================

    def generate_insights(
        self, results_csv: str, template: SurveyTemplate
    ) -> InsightReport:
        """
        AIServiceを使用してアンケート結果からインサイトレポートを生成する。
        CSV全文ではなく統計要約データをLLMに渡すことでトークン効率化。

        Args:
            results_csv: CSV形式のアンケート結果テキスト
            template: アンケートテンプレート

        Returns:
            InsightReport: 生成されたインサイトレポート（survey_idは空文字列）
        """
        try:
            # CSV全文ではなく統計要約を生成
            summary = self._generate_statistical_summary(results_csv, template)
            prompt = self._build_insight_prompt(summary, template)

            # インサイトレポート生成には大きなmax_tokensを使用（8000トークン）
            content = self.ai_service._invoke_model(prompt, max_tokens=8000)

            report = InsightReport.create_new(survey_id="", content=content)
            logger.info("Insight report generated successfully")
            return report
        except Exception as e:
            logger.error(f"Failed to generate insights: {e}")
            raise SurveyServiceError(
                f"インサイトレポートの生成に失敗しました: {e}"
            ) from e

    def _generate_statistical_summary(
        self, results_csv: str, template: SurveyTemplate
    ) -> Dict[str, Any]:
        """
        CSV結果から統計要約を生成する。

        Returns:
            dict: {
                "total_responses": int,
                "demographics": {...},
                "question_analysis": [...]
            }
        """
        # 回答列を文字列として読み込むためのスキーマオーバーライド
        schema_overrides = {}
        for q in template.questions:
            answer_col = f"{q.id}_answer"
            schema_overrides[answer_col] = pl.Utf8  # 全ての回答列を文字列として読み込む

        # CSVをPolars DataFrameに変換
        df = pl.read_csv(
            io.StringIO(results_csv),
            schema_overrides=schema_overrides,
            ignore_errors=True,  # パースエラーを無視
        )
        total = len(df)

        # 人口統計情報の集計
        demographics = self._summarize_demographics(df)

        # 質問ごとの分析
        question_analysis = []
        for q in template.questions:
            answer_col = f"{q.id}_answer"
            if answer_col not in df.columns:
                continue

            analysis: Dict[str, Any] = {
                "question_id": q.id,
                "question_text": q.text,
                "question_type": q.question_type,
            }

            if q.question_type == "multiple_choice":
                analysis["distribution"] = self._analyze_multiple_choice(
                    df, answer_col, q.options, allow_multiple=q.allow_multiple
                )
                analysis["by_demographics"] = self._cross_tabulate(
                    df, answer_col, q.options, allow_multiple=q.allow_multiple
                )
            elif q.question_type == "scale_rating":
                analysis["statistics"] = self._analyze_scale_rating(df, answer_col)
                analysis["by_demographics"] = self._cross_tabulate_numeric(
                    df, answer_col
                )
            elif q.question_type == "free_text":
                analysis["sample_responses"] = self._sample_free_text(
                    df, answer_col, limit=10
                )

            question_analysis.append(analysis)

        return {
            "total_responses": total,
            "demographics": demographics,
            "question_analysis": question_analysis,
        }

    def _summarize_demographics(self, df: pl.DataFrame) -> Dict[str, Any]:
        """人口統計情報を集計"""
        demo: Dict[str, Any] = {}

        # 性別分布
        if "sex" in df.columns:
            demo["sex"] = df.group_by("sex").agg(pl.count()).to_dicts()

        # 年齢統計
        if "age" in df.columns:
            age_col = df["age"].cast(pl.Float64, strict=False)
            demo["age"] = {
                "mean": float(age_col.mean()) if age_col.mean() is not None else 0,  # type: ignore[arg-type]
                "min": int(age_col.min()) if age_col.min() is not None else 0,  # type: ignore[arg-type]
                "max": int(age_col.max()) if age_col.max() is not None else 0,  # type: ignore[arg-type]
            }

        # 地域分布（上位5件）
        if "region" in df.columns:
            demo["region"] = (
                df.group_by("region")
                .agg(pl.count())
                .sort("count", descending=True)
                .head(5)
                .to_dicts()
            )

        # 職業分布（上位5件）
        if "occupation" in df.columns:
            demo["occupation"] = (
                df.group_by("occupation")
                .agg(pl.count())
                .sort("count", descending=True)
                .head(5)
                .to_dicts()
            )

        return demo

    def _analyze_multiple_choice(
        self,
        df: pl.DataFrame,
        answer_col: str,
        options: List[str],
        allow_multiple: bool = False,
    ) -> Dict[str, Any]:
        """選択式質問の分析"""
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

    def _analyze_scale_rating(
        self, df: pl.DataFrame, answer_col: str
    ) -> Dict[str, Any]:
        """スケール評価質問の分析"""
        # 文字列を数値に変換（変換できない値はnullになる）
        numeric_col = df[answer_col].cast(pl.Float64, strict=False)

        # 分布計算用に元の列をフィルタリング（数値に変換可能な値のみ）
        valid_df = df.filter(numeric_col.is_not_null())

        return {
            "mean": round(float(numeric_col.mean()), 2) if numeric_col.mean() is not None else 0,  # type: ignore[arg-type]
            "median": float(numeric_col.median()) if numeric_col.median() is not None else 0,  # type: ignore[arg-type]
            "std": round(float(numeric_col.std()), 2) if numeric_col.std() is not None else 0,  # type: ignore[arg-type]
            "distribution": valid_df.group_by(answer_col)
            .agg(pl.count())
            .sort(answer_col)
            .to_dicts()
            if len(valid_df) > 0
            else [],
        }

    def _cross_tabulate(
        self,
        df: pl.DataFrame,
        answer_col: str,
        options: List[str],
        allow_multiple: bool = False,
    ) -> Dict[str, Any]:
        """選択式質問の属性別クロス集計（性別・年齢層のみ）"""
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

        cross_tab = {}

        # 性別別
        if "sex" in df.columns:
            cross_tab["by_sex"] = (
                df.group_by(["sex", answer_col])
                .agg(pl.count().alias("count"))
                .sort(["sex", "count"], descending=[False, True])
                .to_dicts()
            )

        # 年齢層別（10歳刻み）
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

    def _cross_tabulate_numeric(
        self, df: pl.DataFrame, answer_col: str
    ) -> Dict[str, Any]:
        """数値質問の属性別クロス集計"""
        cross_tab = {}

        # 性別別平均
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

        # 年齢層別平均
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

    def _sample_free_text(
        self, df: pl.DataFrame, answer_col: str, limit: int = 10
    ) -> List[str]:
        """自由記述のサンプル抽出"""
        responses = df[answer_col].drop_nulls().to_list()
        # 長さでソートして多様性を確保
        responses.sort(key=len, reverse=True)
        return responses[:limit]

    def _build_insight_prompt(
        self, summary: Dict[str, Any], template: SurveyTemplate
    ) -> str:
        """統計要約からインサイトレポート生成用のプロンプトを構築する。"""
        import json

        summary_json = json.dumps(summary, ensure_ascii=False, indent=2)

        return (
            "以下はアンケート調査の統計要約データです。\n"
            "このデータを分析し、マーケティング戦略に活用できるインサイトレポートを生成してください。\n\n"
            "【レポートに含めるべき内容】\n"
            "1. 全体的な傾向と主要な発見\n"
            "2. 属性別（性別、年齢、地域など）の回答傾向の違い\n"
            "3. 注目すべきパターンや相関関係\n"
            "4. マーケティング施策への具体的な提言\n"
            "5. 追加調査が必要な領域\n\n"
            "【統計要約データ】\n"
            f"{summary_json}\n\n"
            "注: 上記は統計処理済みのデータです。パーセンテージ、平均値、分布などの数値を活用して洞察を導いてください。"
        )
