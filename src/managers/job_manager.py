"""Asynchronous job management (persisted in DynamoDB).

Runs long jobs such as generate_personas / run_discussion on background
threads and tracks their status in DynamoDB. Items expire after 7 days (TTL).

The submitter (``owner_sub``) is recorded and the result is returned only to
that user. A job function receives the submitter's Principal as its first
argument and works only within that user's scope. Every time a result is
returned the Jobs table is read again and checked; the check is not cached.
The read uses a short-timeout client for decisions
(``AuthzLookupService``) and answers 503 when it cannot be reached.
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
    """DynamoDB-based asynchronous job management (via ServiceFactory)"""

    def __init__(
        self,
        job_store: Any = None,
        lookup_service: Any = None,
    ) -> None:
        """Create the manager.

        Args:
            job_store: Writes job records (``JobStore``).
            lookup_service: Reads job records for the owner check
                (``AuthzLookupService``).
        """
        if job_store is None:
            job_store = ServiceFactory().get_job_store()
        if lookup_service is None:
            lookup_service = ServiceFactory().get_authz_lookup_service()
        self._jobs = job_store
        self._lookup = lookup_service

    def submit(
        self,
        principal: Principal,
        job_type: str,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """Submit a job and return its job_id.

        Jobs run AI work on the submitter's behalf, so ``ai:execute`` is
        required. ``func`` is called as
        ``func(principal, *args, **kwargs)``.

        Raises:
            AccessDeniedError: The submitter lacks ``ai:execute``.
        """
        AccessPolicy.require(principal, Permission.AI_EXECUTE)
        job_id = str(uuid.uuid4())
        now = datetime.now()
        expires_at = int(now.timestamp()) + TTL_SECONDS

        self._jobs.create(
            job_id,
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
        """Return the job if ``principal`` submitted it.

        A missing job, a job without a submitter and someone else's job all
        raise the same ``JOB_NOT_FOUND`` and write the same one
        ``authz.denied`` line. An unreachable Jobs table is
        not a denial and writes none. Nothing is cached.

        Raises:
            JobNotFoundError: Not this user's job (404).
            LookupUnavailableError: The Jobs table could not be read (503).
        """
        try:
            job: Optional[Job] = self._lookup.get_job(job_id)
        except AuthzLookupError as e:
            raise LookupUnavailableError("jobs table unreachable") from e
        # A missing job is passed through the same check as one with no owner (indistinguishable from someone else's)
        try:
            AccessPolicy.ensure_owner(
                principal,
                job.owner_sub if job else None,
                ErrorCode.JOB_NOT_FOUND,
            )
        except CodedError as e:
            raise JobNotFoundError("job not found for this user") from e
        assert job is not None  # ensure_owner always rejects a job with no owner
        return job

    def _run(
        self,
        job_id: str,
        func: Callable[..., Any],
        args: tuple,
        kwargs: dict,
    ) -> None:
        self._jobs.update_status(job_id, JobStatus.RUNNING.value)

        try:
            result = func(*args, **kwargs)
            result_json = json.dumps(result, ensure_ascii=False, default=str)
            self._jobs.update_completed(job_id, result_json)
            logger.info(f"Job {job_id} completed")
        except Exception as e:
            # Store only the error code (exception text is never shown on screen or API)
            code = e.code if isinstance(e, CodedError) else ErrorCode.UNKNOWN
            self._jobs.update_failed(job_id, code.value)
            logger.error(f"Job {job_id} failed: {type(e).__name__}", exc_info=True)
