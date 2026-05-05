"""
非同期ジョブ管理

generate_personas / run_discussion など長時間処理を
バックグラウンドスレッドで実行し、ステータスをポーリングで返す。
"""

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


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
    """インメモリ非同期ジョブ管理（ECS単一タスク前提）"""

    # 完了/失敗ジョブの保持時間（秒）
    DEFAULT_TTL_SECONDS = 3600  # 1時間

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._ttl_seconds = ttl_seconds

    def submit(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        """ジョブを投入しjob_idを返す"""
        job_id = str(uuid.uuid4())
        now = datetime.now()
        job = Job(id=job_id, status=JobStatus.PENDING, created_at=now, updated_at=now)

        with self._lock:
            self._purge_expired()
            self._jobs[job_id] = job

        thread = threading.Thread(
            target=self._run, args=(job_id, func, args, kwargs), daemon=True
        )
        thread.start()
        return job_id

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def _run(
        self,
        job_id: str,
        func: Callable[..., Any],
        args: tuple,
        kwargs: dict,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = JobStatus.RUNNING
            job.updated_at = datetime.now()

        try:
            result = func(*args, **kwargs)
            with self._lock:
                job = self._jobs[job_id]
                job.status = JobStatus.COMPLETED
                job.result = result
                job.updated_at = datetime.now()
            logger.info(f"Job {job_id} completed")
        except Exception as e:
            with self._lock:
                job = self._jobs[job_id]
                job.status = JobStatus.FAILED
                job.error = "処理中にエラーが発生しました"
                job.updated_at = datetime.now()
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)

    def _purge_expired(self) -> None:
        """TTLを超えた完了/失敗ジョブを削除する（ロック取得済み前提）"""
        now = datetime.now()
        expired = [
            jid
            for jid, job in self._jobs.items()
            if job.status in (JobStatus.COMPLETED, JobStatus.FAILED)
            and (now - job.updated_at).total_seconds() > self._ttl_seconds
        ]
        for jid in expired:
            del self._jobs[jid]
