"""
非同期ジョブ管理（DynamoDB 永続化）

generate_personas / run_discussion など長時間処理を
バックグラウンドスレッドで実行し、ステータスを DynamoDB で管理する。
TTL 7日で自動削除。
"""

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

import boto3

from ..config import config

logger = logging.getLogger(__name__)

TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Job:
    id: str
    status: JobStatus
    created_at: datetime
    result: Optional[Any] = None
    error: Optional[str] = None
    updated_at: datetime = field(default_factory=datetime.now)


class JobManager:
    """DynamoDB ベースの非同期ジョブ管理"""

    def __init__(self) -> None:
        self._table_name = f"{config.DYNAMODB_TABLE_PREFIX}_Jobs"
        self._client = boto3.client("dynamodb", region_name=config.DYNAMODB_REGION)

    def submit(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        """ジョブを投入し job_id を返す"""
        job_id = str(uuid.uuid4())
        now = datetime.now()
        expires_at = int(now.timestamp()) + TTL_SECONDS

        self._client.put_item(
            TableName=self._table_name,
            Item={
                "id": {"S": job_id},
                "status": {"S": JobStatus.PENDING.value},
                "created_at": {"S": now.isoformat()},
                "updated_at": {"S": now.isoformat()},
                "expires_at": {"N": str(expires_at)},
            },
        )

        thread = threading.Thread(
            target=self._run, args=(job_id, func, args, kwargs), daemon=True
        )
        thread.start()
        return job_id

    def get(self, job_id: str) -> Optional[Job]:
        """ジョブを取得"""
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"id": {"S": job_id}},
        )
        item = response.get("Item")
        if not item:
            return None
        return self._item_to_job(item)

    def _run(
        self,
        job_id: str,
        func: Callable[..., Any],
        args: tuple,
        kwargs: dict,
    ) -> None:
        self._update_status(job_id, JobStatus.RUNNING)

        try:
            result = func(*args, **kwargs)
            self._update_completed(job_id, result)
            logger.info(f"Job {job_id} completed")
        except Exception as e:
            self._update_failed(job_id, str(e))
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)

    def _update_status(self, job_id: str, status: JobStatus) -> None:
        self._client.update_item(
            TableName=self._table_name,
            Key={"id": {"S": job_id}},
            UpdateExpression="SET #s = :s, updated_at = :u",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": {"S": status.value},
                ":u": {"S": datetime.now().isoformat()},
            },
        )

    def _update_completed(self, job_id: str, result: Any) -> None:
        self._client.update_item(
            TableName=self._table_name,
            Key={"id": {"S": job_id}},
            UpdateExpression="SET #s = :s, #r = :r, updated_at = :u",
            ExpressionAttributeNames={"#s": "status", "#r": "result"},
            ExpressionAttributeValues={
                ":s": {"S": JobStatus.COMPLETED.value},
                ":r": {"S": json.dumps(result, ensure_ascii=False, default=str)},
                ":u": {"S": datetime.now().isoformat()},
            },
        )

    def _update_failed(self, job_id: str, error: str) -> None:
        self._client.update_item(
            TableName=self._table_name,
            Key={"id": {"S": job_id}},
            UpdateExpression="SET #s = :s, #e = :e, updated_at = :u",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={
                ":s": {"S": JobStatus.FAILED.value},
                ":e": {"S": error},
                ":u": {"S": datetime.now().isoformat()},
            },
        )

    @staticmethod
    def _item_to_job(item: dict) -> Job:
        result_str = item.get("result", {}).get("S")
        return Job(
            id=item["id"]["S"],
            status=JobStatus(item["status"]["S"]),
            created_at=datetime.fromisoformat(item["created_at"]["S"]),
            updated_at=datetime.fromisoformat(item["updated_at"]["S"]),
            result=json.loads(result_str) if result_str else None,
            error=item.get("error", {}).get("S"),
        )
