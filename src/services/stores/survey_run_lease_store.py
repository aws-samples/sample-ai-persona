"""DynamoDB reads and writes of the survey runner's lease (BR2.3).

``SurveyRunLease``: PK ``id``. The deployment has one row, ``id=survey-run``,
holding the task that may run the survey runner (``holder``) and until when
(``expires_at``, epoch seconds). Every write is conditional:

- taken when the row is missing, expired, or already ours;
- extended only while it is ours;
- removed only while it is ours (a clean stop, NFR4.10).

Which task works, and when it stops, is decided by ``SurveyRunManager``.
"""

import logging
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode

logger = logging.getLogger(__name__)

#: The single lease row of a deployment.
LEASE_ID = "survey-run"


class SurveyRunLeaseStoreError(CodedError):
    """A lease read or write failed."""

    code = ErrorCode.DATABASE_OPERATION_FAILED


class SurveyRunLeaseStore:
    """Conditional writes of ``SurveyRunLease``."""

    def __init__(self, *, region: str, table_name: str, client: Any = None) -> None:
        """Create the store.

        Args:
            region: AWS region of the table.
            table_name: Physical name of ``SurveyRunLease``.
            client: Optional pre-built DynamoDB client (tests).
        """
        self.table_name = table_name
        self.client = client or boto3.client("dynamodb", region_name=region)

    def acquire(self, holder: str, *, now: int, ttl_seconds: int) -> bool:
        """Take the lease if it is free, expired, or ours; extend it then.

        Returns:
            ``False`` when another holder's lease is still valid.
        """
        return self._conditional(
            "put_item",
            Item={
                "id": {"S": LEASE_ID},
                "holder": {"S": holder},
                "expires_at": {"N": str(now + ttl_seconds)},
            },
            ConditionExpression=(
                "attribute_not_exists(id) OR expires_at < :now OR holder = :me"
            ),
            ExpressionAttributeValues={
                ":now": {"N": str(now)},
                ":me": {"S": holder},
            },
        )

    def renew(self, holder: str, *, now: int, ttl_seconds: int) -> bool:
        """Extend our lease (it is never created here).

        Returns:
            ``False`` when the lease is gone or another task holds it.
        """
        return self._conditional(
            "update_item",
            Key={"id": {"S": LEASE_ID}},
            UpdateExpression="SET expires_at = :until",
            ConditionExpression="holder = :me",
            ExpressionAttributeValues={
                ":until": {"N": str(now + ttl_seconds)},
                ":me": {"S": holder},
            },
        )

    def release(self, holder: str) -> bool:
        """Remove our lease so another task can take it at once.

        Returns:
            ``False`` when the lease is not ours (kept as it is).
        """
        return self._conditional(
            "delete_item",
            Key={"id": {"S": LEASE_ID}},
            ConditionExpression="holder = :me",
            ExpressionAttributeValues={":me": {"S": holder}},
        )

    def _conditional(self, operation: str, **kwargs: Any) -> bool:
        try:
            getattr(self.client, operation)(TableName=self.table_name, **kwargs)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return False
            logger.warning("%s on the survey run lease failed: %s", operation, code)
            raise SurveyRunLeaseStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            logger.warning(
                "%s on the survey run lease unreachable: %s",
                operation,
                type(e).__name__,
            )
            raise SurveyRunLeaseStoreError(
                f"{operation} unreachable: {type(e).__name__}"
            ) from e
