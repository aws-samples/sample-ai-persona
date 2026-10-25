"""DynamoDB writes of job records (contract C14).

``Jobs``: PK ``id``, TTL on ``expires_at``.

- A job records its submitter (``owner_sub``) and kind (``job_type``).
- A failure is stored as an ``ErrorCode`` value in ``error_code``; exception
  text is never written (U6 BR8.3).

Reading a job for the owner check is ``AuthzLookupService.get_job``; who may
submit or read a job is decided by ``JobManager``.
"""

import logging
from datetime import datetime
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode

logger = logging.getLogger(__name__)


class JobStoreError(CodedError):
    """A job write failed."""

    code = ErrorCode.DATABASE_OPERATION_FAILED


class JobStore:
    """Keyed writes of ``Jobs``."""

    def __init__(self, *, region: str, table_name: str, client: Any = None) -> None:
        """Create the store.

        Args:
            region: AWS region of the table.
            table_name: Physical name of ``Jobs``.
            client: Optional pre-built DynamoDB client (tests).
        """
        self.table_name = table_name
        self.client = client or boto3.client("dynamodb", region_name=region)

    def create(
        self,
        job_id: str,
        *,
        status: str,
        expires_at: int,
        owner_sub: Optional[str] = None,
        job_type: Optional[str] = None,
    ) -> None:
        """Write a new job record.

        Args:
            job_id: The job.
            status: The first status (``pending``).
            expires_at: Epoch seconds after which DynamoDB TTL removes it.
            owner_sub: ``sub`` of the submitter. Results go only to this user.
            job_type: What the job does (for logs).
        """
        now = datetime.now().isoformat()
        item: dict[str, Any] = {
            "id": {"S": job_id},
            "status": {"S": status},
            "created_at": {"S": now},
            "updated_at": {"S": now},
            "expires_at": {"N": str(expires_at)},
        }
        if owner_sub:
            item["owner_sub"] = {"S": owner_sub}
        if job_type:
            item["job_type"] = {"S": job_type}
        self._call("put_item", TableName=self.table_name, Item=item)

    def update_status(self, job_id: str, status: str) -> None:
        """Set the status of a job."""
        self._set(job_id, status=status)

    def update_completed(self, job_id: str, result_json: str) -> None:
        """Mark a job as completed with its JSON result."""
        self._set(job_id, status="completed", result=result_json)

    def update_failed(self, job_id: str, error_code: str) -> None:
        """Mark a job as failed with an ``ErrorCode`` value (never exception text)."""
        self._set(job_id, status="failed", error_code=error_code)

    def _set(self, job_id: str, **columns: str) -> None:
        columns["updated_at"] = datetime.now().isoformat()
        names = {f"#c{i}": name for i, name in enumerate(columns)}
        values = {f":c{i}": {"S": value} for i, value in enumerate(columns.values())}
        self._call(
            "update_item",
            TableName=self.table_name,
            Key={"id": {"S": job_id}},
            UpdateExpression="SET "
            + ", ".join(f"{n} = {v}" for n, v in zip(names, values)),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response: dict[str, Any] = getattr(self.client, operation)(**kwargs)
            return response
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            logger.warning("%s on jobs failed: %s", operation, code)
            raise JobStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            logger.warning("%s on jobs unreachable: %s", operation, type(e).__name__)
            raise JobStoreError(f"{operation} unreachable: {type(e).__name__}") from e
