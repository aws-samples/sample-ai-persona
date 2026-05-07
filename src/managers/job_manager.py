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

from ..services.service_factory import ServiceFactory

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
    """DynamoDB ベースの非同期ジョブ管理（ServiceFactory 経由）"""

    def __init__(self, database_service: Any = None) -> None:
        if database_service is None:
            database_service = ServiceFactory().get_database_service()
        self._db = database_service

    def submit(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        """ジョブを投入し job_id を返す"""
        job_id = str(uuid.uuid4())
        now = datetime.now()
        expires_at = int(now.timestamp()) + TTL_SECONDS

        self._db.save_job(
            job_id=job_id,
            status=JobStatus.PENDING.value,
            expires_at=expires_at,
        )

        thread = threading.Thread(
            target=self._run, args=(job_id, func, args, kwargs), daemon=True
        )
        thread.start()
        return job_id

    def get(self, job_id: str) -> Optional[Job]:
        """ジョブを取得"""
        item = self._db.get_job(job_id)
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
        self._db.update_job_status(job_id, JobStatus.RUNNING.value)

        try:
            result = func(*args, **kwargs)
            result_json = json.dumps(result, ensure_ascii=False, default=str)
            self._db.update_job_completed(job_id, result_json)
            logger.info(f"Job {job_id} completed")
        except Exception as e:
            self._db.update_job_failed(job_id, str(e))
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)

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
