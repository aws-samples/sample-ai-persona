"""
DynamoDB reads for authorization decisions (contracts C3, C8, C14).

Reads only the columns an authorization decision needs (``id``, ``owner_sub``,
``shared_group_ids`` and one display-name column) with a projection, and the
Jobs table for ``JobManager.get``. Nothing here writes.

Limits (``reliability-design.md``, NFR9.18, NFR9.22):

- One call: connect 0.5 s, read 0.5 s, two attempts in botocore's ``standard``
  retry mode (:data:`CLIENT_CONFIG`). This client is separate from the
  ``DatabaseService`` client used for the resource bodies.
- Deadline: ``read_targets`` receives the caller's deadline (a
  ``time.monotonic`` value). Before every call -- including the single re-read
  of ``UnprocessedKeys`` -- the remaining time is checked, and with less than
  :data:`MIN_CALL_BUDGET_SECONDS` left nothing is read (NFR Design R-01).
- Throttling: botocore's ``standard`` mode backs off between its two attempts
  inside one call. That back-off and the second attempt happen inside the
  call, so under throttling one call can run past the 1-second budget; the
  deadline check before the next call still stops further reads.

Every failure raises :class:`AuthzLookupError` (``LOOKUP_UNAVAILABLE``) with a
``reason`` of ``deadline``, ``unprocessed`` or ``unreachable``. Messages never
carry resource IDs.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from src.models.authz import AccessTarget, LookupReader, ResourceType
from src.models.errors import CodedError, ErrorCode
from src.models.job import Job

logger = logging.getLogger(__name__)

#: botocore settings for authorization reads (``reliability-design.md``).
#: ``total_max_attempts`` counts the first call (2 = two attempts in total).
CLIENT_CONFIG = Config(
    connect_timeout=0.5,
    read_timeout=0.5,
    retries={"total_max_attempts": 2, "mode": "standard"},
)

#: Time one attempt may take (connect + read). With less than this left before
#: the deadline, a read is not started (NFR Design R-01).
MIN_CALL_BUDGET_SECONDS = 1.0

#: ``BatchGetItem`` accepts at most 100 keys per call.
BATCH_LIMIT = 100

_ID = "id"
_OWNER = "owner_sub"
_SHARED = "shared_group_ids"


class AuthzLookupError(CodedError):
    """An authorization read could not be completed (fail closed).

    Attributes:
        reason: ``deadline``, ``unprocessed`` or ``unreachable`` (for logs).
    """

    code = ErrorCode.LOOKUP_UNAVAILABLE

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class LookupTable:
    """Where one resource type's decision columns live (contract C8).

    Attributes:
        resource_type: The resource type read from this table.
        table_name: Physical DynamoDB table name.
        display_name_attribute: Column used as the display name (``name`` for
            personas, ``topic`` for discussions), or ``None``.
    """

    resource_type: ResourceType
    table_name: str
    display_name_attribute: str | None = None


class _TableReader:
    """:class:`~src.models.authz.LookupReader` for one table."""

    def __init__(self, service: "AuthzLookupService", table: LookupTable) -> None:
        self._service = service
        self._table = table

    def read(self, ids: Sequence[str], deadline: float) -> dict[str, AccessTarget]:
        return self._service.read_targets(self._table, ids, deadline)


class AuthzLookupService:
    """Projection reads of decision columns and of the Jobs table."""

    def __init__(
        self,
        *,
        region: str,
        jobs_table_name: str,
        client: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the service.

        Args:
            region: AWS region of the tables.
            jobs_table_name: Physical name of the Jobs table.
            client: Optional pre-built DynamoDB client (tests).
            clock: Monotonic clock used for the deadline checks.
        """
        self.jobs_table_name = jobs_table_name
        self.client = client or boto3.client(
            "dynamodb", region_name=region, config=CLIENT_CONFIG
        )
        self._clock = clock
        self._deserializer = TypeDeserializer()

    def reader(self, table: LookupTable) -> LookupReader:
        """Return the reader to register with ``ResourceLookup`` for ``table``."""
        return _TableReader(self, table)

    # --- decision columns -------------------------------------------------------

    def read_targets(
        self, table: LookupTable, ids: Sequence[str], deadline: float
    ) -> dict[str, AccessTarget]:
        """Read the decision columns of at most 100 IDs before ``deadline``.

        One ID uses ``GetItem``; more use one ``BatchGetItem`` and at most one
        re-read of ``UnprocessedKeys``. Both reads are strongly consistent so
        that a just-removed share is never seen as still present.

        Returns:
            AccessTarget per ID that exists. IDs that do not exist are left out.

        Raises:
            AuthzLookupError: Out of time, keys left unprocessed, or DynamoDB
                unreachable.
        """
        if not ids:
            return {}
        if len(ids) == 1:
            item = self._get_item(table, ids[0], deadline)
            return {} if item is None else {ids[0]: self._to_target(table, item)}
        return self._batch_get(table, ids, deadline)

    def _projection(self, table: LookupTable) -> dict[str, Any]:
        columns = [_ID, _OWNER, _SHARED]
        if table.display_name_attribute:
            columns.append(table.display_name_attribute)
        names = {f"#c{i}": column for i, column in enumerate(columns)}
        return {
            "ProjectionExpression": ", ".join(names),
            "ExpressionAttributeNames": names,
        }

    def _get_item(
        self, table: LookupTable, resource_id: str, deadline: float
    ) -> dict[str, Any] | None:
        self._ensure_time_left(deadline)
        response = self._call(
            "get_item",
            TableName=table.table_name,
            Key={_ID: {"S": resource_id}},
            ConsistentRead=True,
            **self._projection(table),
        )
        item: dict[str, Any] | None = response.get("Item")
        return item

    def _batch_get(
        self, table: LookupTable, ids: Sequence[str], deadline: float
    ) -> dict[str, AccessTarget]:
        keys = [{_ID: {"S": resource_id}} for resource_id in ids]
        result: dict[str, AccessTarget] = {}
        for attempt in range(2):  # the first read and one re-read
            self._ensure_time_left(deadline)
            response = self._call(
                "batch_get_item",
                RequestItems={
                    table.table_name: {
                        "Keys": keys,
                        "ConsistentRead": True,
                        **self._projection(table),
                    }
                },
            )
            for item in response.get("Responses", {}).get(table.table_name, []):
                target = self._to_target(table, item)
                result[target.resource_id] = target
            keys = (
                response.get("UnprocessedKeys", {})
                .get(table.table_name, {})
                .get("Keys", [])
            )
            if not keys:
                return result
        logger.warning(
            "authz lookup left %d keys unprocessed in %s",
            len(keys),
            table.resource_type.value,
        )
        raise AuthzLookupError(
            "BatchGetItem left keys unprocessed after one re-read",
            reason="unprocessed",
        )

    def _to_target(self, table: LookupTable, item: dict[str, Any]) -> AccessTarget:
        data = {k: self._deserializer.deserialize(v) for k, v in item.items()}
        display = (
            data.get(table.display_name_attribute)
            if table.display_name_attribute
            else None
        )
        return AccessTarget(
            resource_type=table.resource_type,
            resource_id=str(data[_ID]),
            owner_sub=data.get(_OWNER) or None,
            shared_group_ids=frozenset(data.get(_SHARED) or ()),
            display_name=str(display) if display else None,
        )

    # --- jobs -------------------------------------------------------------------

    def get_job(self, job_id: str) -> Job | None:
        """Read one job with a strongly consistent ``GetItem`` (NFR9.22).

        Raises:
            AuthzLookupError: The Jobs table is unreachable.
        """
        response = self._call(
            "get_item",
            TableName=self.jobs_table_name,
            Key={_ID: {"S": job_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        data = {k: self._deserializer.deserialize(v) for k, v in item.items()}
        raw_result = data.get("result")
        data["result"] = json.loads(raw_result) if raw_result else None
        return Job.from_dict(data)

    # --- shared -----------------------------------------------------------------

    def _ensure_time_left(self, deadline: float) -> None:
        if deadline - self._clock() < MIN_CALL_BUDGET_SECONDS:
            raise AuthzLookupError(
                "authorization read deadline reached", reason="deadline"
            )

    def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response: dict[str, Any] = getattr(self.client, operation)(**kwargs)
            return response
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            logger.warning("authz lookup %s failed: %s", operation, error_code)
            raise AuthzLookupError(
                f"{operation} failed: {error_code}", reason="unreachable"
            ) from e
        except BotoCoreError as e:
            logger.warning(
                "authz lookup %s unreachable: %s", operation, type(e).__name__
            )
            raise AuthzLookupError(
                f"{operation} unreachable: {type(e).__name__}", reason="unreachable"
            ) from e
