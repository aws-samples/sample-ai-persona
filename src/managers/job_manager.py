"""
非同期ジョブ管理（DynamoDB 永続化、約束 C14）

generate_personas / run_discussion など長時間処理を
バックグラウンドスレッドで実行し、ステータスを DynamoDB で管理する。
TTL 7日で自動削除。

v1.0.0 では投入した人（``owner_sub``）を記録し、結果はその人にだけ返す
（BR2.12）。処理の関数は投入した人の Principal を最初の引数で受け取り、その人の
範囲だけで処理する。結果を返すたびに Jobs テーブルを読み直して照合し、照合の
結果はキャッシュしない。読み取りは判定用の短い待ち時間のクライアント
（``AuthzLookupService``）で行い、届かなければ 503 にする（NFR9.22）。
"""

import json
import logging
import threading
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

from ..models.authz import Permission, Principal
from ..models.errors import CodedError, ErrorCode, LookupUnavailableError
from ..models.job import Job, JobStatus
from ..services.authz_lookup_service import AuthzLookupError
from ..services.service_factory import ServiceFactory
from .components.access_policy import AccessPolicy

logger = logging.getLogger(__name__)

TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

# Re-export for backward compatibility
__all__ = ["Job", "JobStatus", "JobManager", "JobNotFoundError"]


class JobNotFoundError(CodedError):
    """The job does not exist or was not submitted by the caller (404)."""

    code = ErrorCode.JOB_NOT_FOUND


class JobManager:
    """DynamoDB ベースの非同期ジョブ管理（ServiceFactory 経由）"""

    def __init__(
        self,
        database_service: Any = None,
        lookup_service: Any = None,
    ) -> None:
        """Create the manager.

        Args:
            database_service: Writes job records (``DatabaseService``).
            lookup_service: Reads job records for the owner check
                (``AuthzLookupService``).
        """
        if database_service is None:
            database_service = ServiceFactory().get_database_service()
        if lookup_service is None:
            lookup_service = ServiceFactory().get_authz_lookup_service()
        self._db = database_service
        self._lookup = lookup_service

    def submit(
        self,
        principal: Principal,
        job_type: str,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """ジョブを投入し job_id を返す。

        Jobs run AI work on the submitter's behalf, so ``ai:execute`` is
        required (contract C12). ``func`` is called as
        ``func(principal, *args, **kwargs)``.

        Raises:
            AccessDeniedError: The submitter lacks ``ai:execute``.
        """
        AccessPolicy.require(principal, Permission.AI_EXECUTE)
        job_id = str(uuid.uuid4())
        now = datetime.now()
        expires_at = int(now.timestamp()) + TTL_SECONDS

        self._db.save_job(
            job_id=job_id,
            status=JobStatus.PENDING.value,
            expires_at=expires_at,
            owner_sub=principal.sub,
            job_type=job_type,
        )

        thread = threading.Thread(
            target=self._run,
            args=(job_id, func, (principal, *args), kwargs),
            daemon=True,
        )
        thread.start()
        return job_id

    def get(self, principal: Principal, job_id: str) -> Job:
        """Return the job if ``principal`` submitted it (BR2.12).

        A missing job, a job without a submitter and someone else's job all
        raise the same ``JOB_NOT_FOUND``. Nothing is cached.

        Raises:
            JobNotFoundError: Not this user's job (404).
            LookupUnavailableError: The Jobs table could not be read (503).
        """
        try:
            job: Optional[Job] = self._lookup.get_job(job_id)
        except AuthzLookupError as e:
            raise LookupUnavailableError("jobs table unreachable") from e
        if job is None or not AccessPolicy.is_owner(principal, job.owner_sub):
            raise JobNotFoundError("job not found for this user")
        return job

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
