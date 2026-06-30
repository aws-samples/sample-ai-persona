"""
SurveyDatasetManager
データセット管理 + DWH連携 + フィルタ操作を担当するマネージャークラス。
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..prompts.survey_prompts import (
    DWH_SEGMENT_SYSTEM_PROMPT,
    STANDARD_COLUMNS,
    build_column_mapping_prompt,
    build_dataset_name_prompt,
    build_persona_system_prompt,
)
from ..services.ai_service import AIService
from ..services.agent_service import AgentService
from ..services.s3_service import S3Service
from ..services.service_factory import service_factory
from ..services.survey_batch_service import (
    CUSTOM_DATASET_PREFIX,
    PARQUET_S3_KEY,
    SurveyBatchService,
)
from .shared.file_utils import (
    parse_csv_columns_with_samples,
    parse_csv_first_row_with_mapping,
)

logger = logging.getLogger(__name__)


class SurveyDatasetManagerError(Exception):
    """SurveyDatasetManager層の基底例外"""

    pass


class SurveyDatasetValidationError(SurveyDatasetManagerError):
    """バリデーションエラー"""

    pass


class SurveyDatasetManager:
    """データセット管理 + DWH連携 + フィルタ操作"""

    MIN_SEGMENT_ROWS = 100
    MAX_SEGMENT_ROWS = 10000

    def __init__(
        self,
        survey_batch_service: Optional[SurveyBatchService] = None,
        agent_service: Optional[AgentService] = None,
        ai_service: Optional[AIService] = None,
        s3_service: Optional[S3Service] = None,
    ) -> None:
        self.batch_service = (
            survey_batch_service or service_factory.get_survey_batch_service()
        )
        self.agent_service = agent_service or service_factory.get_agent_service()
        self.ai_service = ai_service or service_factory.get_ai_service()
        self.s3_service: S3Service = s3_service or service_factory.get_s3_service()

    # =========================================================================
    # Nemotron データセット
    # =========================================================================

    def check_nemotron_status(self) -> Dict[str, Any]:
        """Nemotronデータセットの存在状況を返す。"""
        bucket = self.s3_service.bucket_name
        try:
            resp = self.s3_service.s3_client.head_object(
                Bucket=bucket, Key=PARQUET_S3_KEY
            )
            size_mb = round(resp["ContentLength"] / (1024 * 1024), 1)
            return {"exists": True, "size_mb": size_mb}
        except self.s3_service.s3_client.exceptions.ClientError:
            return {"exists": False, "size_mb": 0}

    def download_nemotron_dataset(self) -> Dict[str, Any]:
        """Nemotronデータセットをダウンロード・S3配置する。"""
        parquet_bytes = self.batch_service.download_nemotron_dataset()

        self.s3_service.upload_file(parquet_bytes, PARQUET_S3_KEY)
        s3_uri = f"s3://{self.s3_service.bucket_name}/{PARQUET_S3_KEY}"
        self.batch_service.set_parquet_s3_uri(s3_uri)
        logger.info(f"Parquet uploaded to S3: {s3_uri}")

        return self.check_nemotron_status()

    def _ensure_parquet_uri(self) -> None:
        """S3上にParquetが存在するか確認し、batch_serviceにURIをセットする。"""
        bucket = self.s3_service.bucket_name
        try:
            self.s3_service.s3_client.head_object(Bucket=bucket, Key=PARQUET_S3_KEY)
            s3_uri = f"s3://{bucket}/{PARQUET_S3_KEY}"
            self.batch_service.set_parquet_s3_uri(s3_uri)
        except self.s3_service.s3_client.exceptions.ClientError:
            raise SurveyDatasetManagerError(
                "Nemotronデータセットがまだダウンロードされていません。"
                "アンケート調査 > ペルソナデータ設定からデータセットをダウンロードしてください。"
            )

    # =========================================================================
    # カスタムデータセット
    # =========================================================================

    def list_custom_datasets(self) -> List[Dict[str, Any]]:
        """S3上のカスタムデータセット一覧を返す。"""
        bucket = self.s3_service.bucket_name
        datasets: List[Dict[str, Any]] = []
        try:
            resp = self.s3_service.s3_client.list_objects_v2(
                Bucket=bucket, Prefix=CUSTOM_DATASET_PREFIX
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

    def upload_custom_dataset(
        self,
        csv_bytes: bytes,
        filename: str,
        column_mapping: Optional[Dict[str, str]] = None,
        extra_columns: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """カスタムデータセットをアップロードする。"""
        df, parquet_bytes = self.batch_service.convert_csv_to_parquet(
            csv_bytes, column_mapping
        )

        name = Path(filename).stem
        parquet_key = f"{CUSTOM_DATASET_PREFIX}{name}.parquet"
        self.s3_service.upload_file(parquet_bytes, parquet_key)
        logger.info(
            f"Custom dataset uploaded: {parquet_key} ({len(df)} rows, {len(df.columns)} cols)"
        )

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

    def load_dataset_metadata(self, name: str) -> Optional[Dict[str, Any]]:
        """カスタムデータセットのメタデータをS3から読み込む。"""
        meta_key = f"{CUSTOM_DATASET_PREFIX}{name}.meta.json"
        try:
            bucket = self.s3_service.bucket_name
            raw = self.s3_service.download_file(f"s3://{bucket}/{meta_key}")
            return dict(json.loads(raw.decode("utf-8")))
        except Exception:
            return None

    def delete_custom_dataset(self, name: str) -> None:
        """カスタムデータセットを削除する。"""
        bucket = self.s3_service.bucket_name
        key = f"{CUSTOM_DATASET_PREFIX}{name}.parquet"
        self.s3_service.s3_client.delete_object(Bucket=bucket, Key=key)
        meta_key = f"{CUSTOM_DATASET_PREFIX}{name}.meta.json"
        try:
            self.s3_service.s3_client.delete_object(Bucket=bucket, Key=meta_key)
        except Exception:
            pass
        self.batch_service.invalidate_datasource(f"custom:{name}")
        logger.info(f"Custom dataset deleted: {key}")

    def _save_dataset_metadata(self, name: str, metadata: Dict[str, Any]) -> None:
        """カスタムデータセットのメタデータをS3にJSON保存する。"""
        meta_key = f"{CUSTOM_DATASET_PREFIX}{name}.meta.json"
        self.s3_service.upload_file(
            json.dumps(metadata, ensure_ascii=False).encode("utf-8"), meta_key
        )

    # =========================================================================
    # DWH連携セグメント抽出
    # =========================================================================

    def extract_segment_from_dwh(
        self, condition: str, event_queue: Any
    ) -> Dict[str, Any]:
        """データ分析エージェント連携でDWHから顧客セグメントを抽出する。"""
        from ..config import config
        from ..services.data_agent_service import (
            DataAgentService,
            create_data_agent_tool,
        )

        if not condition or not condition.strip():
            raise SurveyDatasetValidationError("抽出条件を入力してください")
        if not config.DATA_AGENT_RUNTIME_ARN:
            raise SurveyDatasetManagerError(
                "データ分析エージェントの接続設定がされていません。"
                "設定画面から Runtime ARN を設定してください"
            )

        logger.info(f"DWH セグメント抽出開始 (condition={condition!r})")
        event_queue.put(
            {"type": "status", "content": "データ分析エージェントに接続中..."}
        )

        csv_urls: List[str] = []

        class _CsvUrlCapture:
            def __init__(self, inner: Any, url_list: List[str]) -> None:
                self._inner = inner
                self._url_list = url_list

            def put(self, item: Any) -> None:
                if isinstance(item, dict) and item.get("type") == "csv_url":
                    url = item.get("url", "")
                    if url:
                        self._url_list.append(url)
                self._inner.put(item)

            def get(self, *args: Any, **kwargs: Any) -> Any:
                return self._inner.get(*args, **kwargs)

            def get_nowait(self) -> Any:
                return self._inner.get_nowait()

            def empty(self) -> bool:
                return bool(self._inner.empty())

        capture_queue = _CsvUrlCapture(event_queue, csv_urls)

        ask_data_agent = create_data_agent_tool(
            runtime_arn=config.DATA_AGENT_RUNTIME_ARN,
            region=config.DATA_AGENT_REGION,
            event_queue=capture_queue,  # type: ignore[arg-type]
        )

        user_prompt = (
            f"以下の条件でDWHから顧客セグメントデータを抽出してCSVエクスポートしてください:\n\n"
            f"条件: {condition}\n\n"
            f"最終出力: ask_data_agent に「CSVで出力してください」と依頼し、"
            f"ダウンロードURLを取得してください。"
        )

        event_queue.put({"type": "status", "content": "データ抽出中..."})
        self.agent_service.run_segment_extraction_agent(
            system_prompt=DWH_SEGMENT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tools=[ask_data_agent],
        )

        if not csv_urls:
            raise SurveyDatasetManagerError(
                "CSVエクスポートURLを取得できませんでした。"
            )

        csv_url = csv_urls[-1]
        event_queue.put({"type": "status", "content": "CSVデータをダウンロード中..."})
        data_agent_svc = DataAgentService(
            config.DATA_AGENT_RUNTIME_ARN, config.DATA_AGENT_REGION
        )
        csv_bytes = data_agent_svc.download_csv(csv_url)

        parse_result = parse_csv_columns_with_samples(csv_bytes, STANDARD_COLUMNS)
        row_count = parse_result["row_count"]
        self._validate_segment_row_count(row_count)

        suggested_name = self._generate_dataset_name(condition)

        logger.info(
            f"DWH セグメント抽出完了: {row_count}件, {len(parse_result['columns'])}カラム"
        )
        return {
            "csv_bytes": csv_bytes,
            "row_count": row_count,
            "columns": parse_result["columns"],
            "samples": parse_result["samples"],
            "auto_mapping": parse_result["auto_mapping"],
            "suggested_name": suggested_name,
        }

    def _generate_dataset_name(self, condition: str) -> str:
        """抽出条件からデータセット名を自動生成する。"""
        if self.ai_service:
            prompt = build_dataset_name_prompt(condition)
            try:
                name = self.ai_service.invoke_model(prompt).strip()
                name = name.strip("「」『』\"'")
                if 1 <= len(name) <= 30:
                    return name
            except Exception as e:
                logger.warning(f"データセット名自動生成失敗（フォールバック使用）: {e}")
        clean = condition.strip().replace("\n", " ")
        return clean[:20] if len(clean) > 20 else clean

    def _validate_segment_row_count(self, row_count: int) -> None:
        """抽出件数バリデーション。"""
        if row_count < self.MIN_SEGMENT_ROWS:
            raise SurveyDatasetValidationError(
                f"抽出件数が少なすぎます（{row_count}件）。"
                f"最低{self.MIN_SEGMENT_ROWS}件のデータが必要です。"
            )
        if row_count > self.MAX_SEGMENT_ROWS:
            raise SurveyDatasetValidationError(
                f"抽出件数が多すぎます（{row_count}件）。"
                f"最大{self.MAX_SEGMENT_ROWS}件までです。"
            )

    # =========================================================================
    # カラムマッピング提案
    # =========================================================================

    def suggest_column_mapping(
        self, columns: List[str], samples: Dict[str, List[str]]
    ) -> Dict[str, Any]:
        """LLMを使ったカラムマッピング提案。"""
        if not self.agent_service:
            return {"mapping": {}, "extra_columns": []}

        prompt = build_column_mapping_prompt(columns, samples, STANDARD_COLUMNS)

        try:
            result = self.agent_service.suggest_column_mapping_with_llm(prompt)

            valid_mapping = {
                k: v
                for k, v in result.get("mapping", {}).items()
                if k in STANDARD_COLUMNS and v in columns
            }
            valid_extra = [
                e
                for e in result.get("extra_columns", [])
                if e.get("csv_column") in columns
                and e.get("csv_column") not in valid_mapping.values()
            ]
            logger.info(
                f"LLMマッピング提案: mapping={len(valid_mapping)}件, "
                f"extra={len(valid_extra)}件"
            )
            return {
                "mapping": valid_mapping,
                "extra_columns": valid_extra,
            }
        except Exception as e:
            logger.warning(f"LLMマッピング提案失敗: {e}")

        return {"mapping": {}, "extra_columns": []}

    # =========================================================================
    # 一時ファイル操作（S3Service直接利用）
    # =========================================================================

    def upload_temp_file(self, content: bytes, key: str) -> None:
        """一時ファイルをS3にアップロードする。"""
        self.s3_service.upload_file(content, key)

    def download_temp_file(self, key: str) -> bytes:
        """一時ファイルをS3からダウンロードする。"""
        bucket = self.s3_service.bucket_name
        return self.s3_service.download_file(f"s3://{bucket}/{key}")

    def delete_temp_file(self, key: str) -> None:
        """一時ファイルをS3から削除する。"""
        self.s3_service.s3_client.delete_object(
            Bucket=self.s3_service.bucket_name, Key=key
        )

    # =========================================================================
    # CSV解析・プレビュー
    # =========================================================================

    def parse_csv_columns(self, content: bytes) -> Dict[str, Any]:
        """CSVファイルのカラムを解析する。"""
        return parse_csv_columns_with_samples(content, STANDARD_COLUMNS)

    def get_standard_columns(self) -> Dict[str, Dict[str, Any]]:
        """標準カラム定義を取得する。"""
        return STANDARD_COLUMNS

    def preview_system_prompt(
        self,
        csv_bytes: bytes,
        column_mapping: Dict[str, str],
        extra_columns: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """マッピング設定に基づくシステムプロンプトのプレビュー。"""
        row = parse_csv_first_row_with_mapping(csv_bytes, column_mapping)
        return build_persona_system_prompt(row, extra_columns)

    # =========================================================================
    # フィルタ・プレビュー統計
    # =========================================================================

    def get_available_filter_values(self, datasource: str) -> Dict[str, Dict[str, Any]]:
        """利用可能なフィルター値を取得する。"""
        self._ensure_parquet_uri_if_nemotron(datasource)
        return self.batch_service.get_available_filter_values(datasource)

    def get_filtered_count(
        self,
        filters: Optional[Dict[str, Any]] = None,
        datasource: str = "nemotron",
    ) -> int:
        """フィルター条件に合致するレコード数を取得する。"""
        self._ensure_parquet_uri_if_nemotron(datasource)
        return self.batch_service.get_filtered_count(filters, datasource)

    def get_preview_stats(
        self,
        filters: Optional[Dict[str, Any]] = None,
        datasource: str = "nemotron",
    ) -> Dict[str, Any]:
        """プレビュー用の統計情報を取得する。"""
        self._ensure_parquet_uri_if_nemotron(datasource)
        return self.batch_service.get_preview_stats(filters, datasource)

    def get_datasource_count(self, datasource: str) -> int:
        """データソースの総レコード数を取得する。"""
        self._ensure_parquet_uri_if_nemotron(datasource)
        return self.batch_service.get_total_count(datasource)

    # =========================================================================
    # 画像
    # =========================================================================

    def get_image_presigned_url(self, file_path: str) -> Optional[str]:
        """画像のPresigned URLを取得する。"""
        return self.s3_service.generate_presigned_url(file_path)

    # =========================================================================
    # 内部ヘルパー
    # =========================================================================

    def _ensure_parquet_uri_if_nemotron(self, datasource: str) -> None:
        """nemotronデータソース使用時、batch_serviceにS3 URIがセットされていなければ確認・セットする。"""
        if not datasource.startswith("custom:"):
            if not self.batch_service.has_parquet_uri():
                self._ensure_parquet_uri()
