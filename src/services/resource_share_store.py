"""
DynamoDB reads and writes for department sharing (ADR-003, contracts C7, C8).

``ResourceShares`` (PK ``group_id``, SK ``{resource_type}#{created_at}#{id}``)
is an index of ``shared_group_ids`` on the resource bodies. The body column is
the source of truth; the two are always written in one ``TransactWriteItems``
conditioned on the value read before (BR1.6, BR4.1). The body tables are
passed in by the caller from ``ResourceSharing``'s registration, so this
service never imports another service.

Failure classification (``reliability-design.md`` §1). ``TransactWriteItems``
reports both a failed condition and a concurrent transaction as
``TransactionCanceledException``; ``CancellationReasons`` tells them apart:

- ``TransactionConflict`` anywhere -> :class:`ShareTransactionConflict`
- throttling (any item, or the call itself) -> retried twice here, 100 and
  200 ms apart, then :class:`ShareThrottled`
- ``ConditionalCheckFailed`` on the body (the first item) ->
  :class:`ShareConditionFailed`
- anything else -> :class:`ResourceShareStoreError` (``reason="failed"``)

The caller re-reads and retries on a condition failure or a conflict.
"""

import base64
import binascii
import json
import logging
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from src.models.authz import ResourceType
from src.models.errors import CodedError, ErrorCode
from src.models.sharing import ResourceShareEntry, share_key

logger = logging.getLogger(__name__)

#: One attempt per call; this service retries throttling itself.
CLIENT_CONFIG = Config(
    connect_timeout=1.0,
    read_timeout=3.0,
    retries={"total_max_attempts": 1},
)

#: Waits before the two throttling retries.
THROTTLE_BACKOFF_SECONDS: tuple[float, ...] = (0.1, 0.2)

_THROTTLE_CODES = frozenset(
    {
        "ThrottlingException",
        "ThrottlingError",
        "ProvisionedThroughputExceeded",
        "ProvisionedThroughputExceededException",
        "RequestLimitExceeded",
    }
)

_ID = "id"
_SHARED = "shared_group_ids"
_GROUP = "group_id"
_KEY = "share_key"


class ResourceShareStoreError(CodedError):
    """A sharing read or write failed.

    Attributes:
        reason: ``condition``, ``conflict``, ``throttled``, ``start_key``,
            ``unprocessed`` or ``failed`` (for logs and retries).
    """

    code = ErrorCode.DATABASE_OPERATION_FAILED

    def __init__(self, message: str, *, reason: str = "failed") -> None:
        super().__init__(message)
        self.reason = reason


class ShareConditionFailed(ResourceShareStoreError):
    """The body changed (or disappeared) after it was read."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="condition")


class ShareTransactionConflict(ResourceShareStoreError):
    """Another transaction on the same item was in progress."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="conflict")


class ShareThrottled(ResourceShareStoreError):
    """DynamoDB kept throttling after the retries."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="throttled")


class ShareStartKeyInvalid(ResourceShareStoreError):
    """DynamoDB refused the ``ExclusiveStartKey`` taken from a cursor."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="start_key")


def encode_position(key: Mapping[str, Any]) -> str:
    """Encode a DynamoDB key (typed attribute values) as URL-safe text."""
    raw = json.dumps(key, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_position(value: str) -> dict[str, Any] | None:
    """Decode :func:`encode_position`; ``None`` for anything unreadable."""
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


class ResourceShareStore:
    """Conditional transactions, index queries and consistent body reads."""

    def __init__(
        self,
        *,
        region: str,
        shares_table: str,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the store.

        Args:
            region: AWS region of the tables.
            shares_table: Physical name of the ``ResourceShares`` table.
            client: Optional pre-built DynamoDB client (tests).
            sleep: Sleep function for the back-offs (tests).
        """
        self.shares_table = shares_table
        self.client = client or boto3.client(
            "dynamodb", region_name=region, config=CLIENT_CONFIG
        )
        self._sleep = sleep
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    # --- bodies -------------------------------------------------------------------

    def get_item(
        self, table: str, resource_id: str, attributes: Sequence[str]
    ) -> dict[str, Any] | None:
        """Read one body with a strongly consistent ``GetItem``.

        Returns:
            The projected columns, or ``None`` when the item does not exist.
        """
        response = self._call(
            "get_item",
            TableName=table,
            Key={_ID: {"S": resource_id}},
            ConsistentRead=True,
            **_projection(attributes),
        )
        item = response.get("Item")
        return self._plain(item) if item else None

    # --- writes -------------------------------------------------------------------

    def write_shares(
        self,
        table: str,
        resource_type: ResourceType,
        resource_id: str,
        *,
        expected: frozenset[str],
        new: frozenset[str],
        created_at: str,
        owner_sub: str,
    ) -> None:
        """Set ``shared_group_ids`` and the index items in one transaction.

        Args:
            table: Body table.
            resource_type: Type of the body.
            resource_id: ID of the body.
            expected: ``shared_group_ids`` as read (empty = column absent).
            new: The new set (empty removes the column, BR1.7).
            created_at: Body ``created_at`` copied into the index (BR1.10).
            owner_sub: Body ``owner_sub`` copied into the index (BR1.10).

        Raises:
            ShareConditionFailed: The body is gone or its shares changed.
            ShareTransactionConflict: A concurrent transaction.
            ShareThrottled: Throttled after the retries.
            ResourceShareStoreError: Any other failure.
        """
        key = share_key(resource_type, created_at, resource_id)
        values: dict[str, Any] = {}
        condition = f"attribute_exists({_ID}) AND " + self._shared_condition(
            expected, values
        )
        if new:
            update = f"SET {_SHARED} = :new"
            values[":new"] = {"SS": sorted(new)}
        else:
            update = f"REMOVE {_SHARED}"
        body: dict[str, Any] = {
            "TableName": table,
            "Key": {_ID: {"S": resource_id}},
            "UpdateExpression": update,
            "ConditionExpression": condition,
        }
        if values:
            body["ExpressionAttributeValues"] = values
        items: list[dict[str, Any]] = [{"Update": body}]
        for group_id in sorted(new - expected):
            entry = ResourceShareEntry(
                group_id, resource_type, resource_id, created_at, owner_sub
            )
            items.append(
                {
                    "Put": {
                        "TableName": self.shares_table,
                        "Item": self._entry_item(entry),
                    }
                }
            )
        for group_id in sorted(expected - new):
            items.append({"Delete": self._share_delete(group_id, key)})
        self._transact(items)

    def delete_with_shares(
        self,
        table: str,
        resource_type: ResourceType,
        resource_id: str,
        *,
        expected: frozenset[str],
        created_at: str,
        cleanup: Mapping[str, Any],
    ) -> None:
        """Delete the body and its index items in one transaction (BR4.1).

        Args:
            expected: ``shared_group_ids`` as read.
            created_at: Body ``created_at`` (part of the index keys).
            cleanup: Cleanup columns as read (``None`` = absent); the delete
                happens only while they are unchanged.

        Raises:
            ShareConditionFailed: The body changed or is gone.
            ShareTransactionConflict: A concurrent transaction.
            ShareThrottled: Throttled after the retries.
            ResourceShareStoreError: Any other failure.
        """
        values: dict[str, Any] = {}
        names: dict[str, str] = {}
        parts = [f"attribute_exists({_ID})", self._shared_condition(expected, values)]
        for index, (column, value) in enumerate(sorted(cleanup.items())):
            name = f"#c{index}"
            names[name] = column
            if value is None:
                parts.append(f"attribute_not_exists({name})")
            else:
                placeholder = f":c{index}"
                values[placeholder] = self._serializer.serialize(value)
                parts.append(f"{name} = {placeholder}")
        body: dict[str, Any] = {
            "TableName": table,
            "Key": {_ID: {"S": resource_id}},
            "ConditionExpression": " AND ".join(parts),
        }
        if values:
            body["ExpressionAttributeValues"] = values
        if names:
            body["ExpressionAttributeNames"] = names
        key = share_key(resource_type, created_at, resource_id)
        items: list[dict[str, Any]] = [{"Delete": body}]
        items += [{"Delete": self._share_delete(g, key)} for g in sorted(expected)]
        self._transact(items)

    @staticmethod
    def _shared_condition(expected: frozenset[str], values: dict[str, Any]) -> str:
        if not expected:
            return f"attribute_not_exists({_SHARED})"
        values[":old"] = {"SS": sorted(expected)}
        return f"{_SHARED} = :old"

    def _share_delete(self, group_id: str, key: str) -> dict[str, Any]:
        return {
            "TableName": self.shares_table,
            "Key": {_GROUP: {"S": group_id}, _KEY: {"S": key}},
        }

    def _entry_item(self, entry: ResourceShareEntry) -> dict[str, Any]:
        return {
            _GROUP: {"S": entry.group_id},
            _KEY: {"S": entry.share_key},
            "resource_type": {"S": entry.resource_type.value},
            "resource_id": {"S": entry.resource_id},
            "created_at": {"S": entry.created_at},
            "owner_sub": {"S": entry.owner_sub},
        }

    def _transact(self, items: list[dict[str, Any]]) -> None:
        for attempt in range(len(THROTTLE_BACKOFF_SECONDS) + 1):
            try:
                self.client.transact_write_items(TransactItems=items)
                return
            except ClientError as e:
                failure = self._classify(e)
            except BotoCoreError as e:
                logger.warning("TransactWriteItems unreachable: %s", type(e).__name__)
                raise ResourceShareStoreError(
                    f"TransactWriteItems unreachable: {type(e).__name__}"
                ) from e
            if not isinstance(failure, ShareThrottled):
                raise failure
            if attempt < len(THROTTLE_BACKOFF_SECONDS):
                self._sleep(THROTTLE_BACKOFF_SECONDS[attempt])
        raise failure

    @staticmethod
    def _classify(error: ClientError) -> ResourceShareStoreError:
        code = error.response.get("Error", {}).get("Code", "")
        if code in _THROTTLE_CODES:
            return ShareThrottled(f"TransactWriteItems throttled: {code}")
        if code != "TransactionCanceledException":
            logger.warning("TransactWriteItems failed: %s", code)
            return ResourceShareStoreError(f"TransactWriteItems failed: {code}")
        reasons = [
            str(r.get("Code") or "None")
            for r in error.response.get("CancellationReasons", [])
        ]
        if "TransactionConflict" in reasons:
            return ShareTransactionConflict("transaction conflict")
        if any(r in _THROTTLE_CODES for r in reasons):
            return ShareThrottled("transaction throttled")
        if reasons and reasons[0] == "ConditionalCheckFailed":
            return ShareConditionFailed("the body changed after it was read")
        logger.warning("TransactWriteItems cancelled: %s", ",".join(reasons))
        return ResourceShareStoreError("transaction cancelled for another reason")

    # --- indexes --------------------------------------------------------------------

    def query_owner(
        self,
        table: str,
        owner_sub: str,
        *,
        limit: int,
        start_key: Mapping[str, Any] | None = None,
    ) -> tuple[list[str], dict[str, Any] | None]:
        """One newest-first page of ``OwnerIndex`` (BR3.2).

        Returns:
            The IDs and the ``LastEvaluatedKey`` (``None`` on the last page).

        Raises:
            ShareStartKeyInvalid: DynamoDB refused ``start_key``.
        """
        params: dict[str, Any] = {
            "TableName": table,
            "IndexName": "OwnerIndex",
            "KeyConditionExpression": "owner_sub = :o",
            "ExpressionAttributeValues": {":o": {"S": owner_sub}},
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if start_key:
            params["ExclusiveStartKey"] = dict(start_key)
        response = self._call("query", **params)
        ids = [item[_ID]["S"] for item in response.get("Items", [])]
        return ids, response.get("LastEvaluatedKey")

    def count_owner(self, table: str, owner_sub: str) -> int:
        """Count the ``OwnerIndex`` entries of ``owner_sub`` (BR3.4)."""
        return self._count(
            "query",
            TableName=table,
            IndexName="OwnerIndex",
            KeyConditionExpression="owner_sub = :o",
            ExpressionAttributeValues={":o": {"S": owner_sub}},
        )

    def query_group(
        self, group_id: str, resource_type: ResourceType
    ) -> list[ResourceShareEntry]:
        """Every share of one type with one department, newest first (BR3.3).

        Strongly consistent: the index is written in the body's transaction.
        """
        params: dict[str, Any] = {
            "TableName": self.shares_table,
            "KeyConditionExpression": "group_id = :g AND begins_with(share_key, :p)",
            "ExpressionAttributeValues": {
                ":g": {"S": group_id},
                ":p": {"S": f"{resource_type.value}#"},
            },
            "ConsistentRead": True,
            "ScanIndexForward": False,
        }
        entries: list[ResourceShareEntry] = []
        while True:
            response = self._call("query", **params)
            for item in response.get("Items", []):
                entry = self._to_entry(item)
                if entry is not None and entry.resource_type is resource_type:
                    entries.append(entry)
            last = response.get("LastEvaluatedKey")
            if not last:
                return entries
            params["ExclusiveStartKey"] = last

    def _to_entry(self, item: Mapping[str, Any]) -> ResourceShareEntry | None:
        data = self._plain(item)
        try:
            return ResourceShareEntry(
                group_id=str(data[_GROUP]),
                resource_type=ResourceType(str(data["resource_type"])),
                resource_id=str(data["resource_id"]),
                created_at=str(data["created_at"]),
                owner_sub=str(data.get("owner_sub") or ""),
            )
        except (KeyError, ValueError):
            logger.warning("ResourceShares item without the expected columns")
            return None

    # --- scans (admins and the rebuild script only) ----------------------------------

    def scan_page(
        self,
        table: str,
        *,
        limit: int,
        attributes: Sequence[str],
        start_key: Mapping[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """One ``Scan`` page of a body table (BR3.9, ``resource:read_all`` only).

        Raises:
            ShareStartKeyInvalid: DynamoDB refused ``start_key``.
        """
        params: dict[str, Any] = {
            "TableName": table,
            "Limit": limit,
            **_projection(attributes),
        }
        if start_key:
            params["ExclusiveStartKey"] = dict(start_key)
        response = self._call("scan", **params)
        items = [self._plain(item) for item in response.get("Items", [])]
        return items, response.get("LastEvaluatedKey")

    def scan_count(self, table: str) -> int:
        """Count the items of a body table with ``Scan`` (BR3.4)."""
        return self._count("scan", TableName=table)

    def scan_items(self, table: str, attributes: Sequence[str]) -> list[dict[str, Any]]:
        """Every item of a body table (the rebuild script)."""
        items: list[dict[str, Any]] = []
        start: dict[str, Any] | None = None
        while True:
            page, start = self.scan_page(
                table, limit=1000, attributes=attributes, start_key=start
            )
            items += page
            if not start:
                return items

    def scan_shares(self) -> list[ResourceShareEntry]:
        """Every ``ResourceShares`` item (the rebuild script, admin credentials)."""
        entries: list[ResourceShareEntry] = []
        params: dict[str, Any] = {"TableName": self.shares_table}
        while True:
            response = self._call("scan", **params)
            for item in response.get("Items", []):
                entry = self._to_entry(item)
                if entry is not None:
                    entries.append(entry)
            last = response.get("LastEvaluatedKey")
            if not last:
                return entries
            params["ExclusiveStartKey"] = last

    def put_entries(self, entries: Iterable[ResourceShareEntry]) -> None:
        """Write index items (the rebuild script's ``--apply``)."""
        for entry in entries:
            self._call(
                "put_item", TableName=self.shares_table, Item=self._entry_item(entry)
            )

    def delete_entries(self, entries: Iterable[ResourceShareEntry]) -> None:
        """Delete index items (the rebuild script's ``--apply``)."""
        for entry in entries:
            self._call(
                "delete_item", **self._share_delete(entry.group_id, entry.share_key)
            )

    # --- shared -----------------------------------------------------------------------

    def _count(self, operation: str, **params: Any) -> int:
        params["Select"] = "COUNT"
        total = 0
        while True:
            response = self._call(operation, **params)
            total += int(response.get("Count", 0))
            last = response.get("LastEvaluatedKey")
            if not last:
                return total
            params["ExclusiveStartKey"] = last

    def _plain(self, item: Mapping[str, Any]) -> dict[str, Any]:
        return {k: self._deserializer.deserialize(v) for k, v in item.items()}

    def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(len(THROTTLE_BACKOFF_SECONDS) + 1):
            try:
                response: dict[str, Any] = getattr(self.client, operation)(**kwargs)
                return response
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code in _THROTTLE_CODES and attempt < len(THROTTLE_BACKOFF_SECONDS):
                    self._sleep(THROTTLE_BACKOFF_SECONDS[attempt])
                    continue
                if code in _THROTTLE_CODES:
                    raise ShareThrottled(f"{operation} throttled") from e
                if code == "ValidationException" and "ExclusiveStartKey" in kwargs:
                    raise ShareStartKeyInvalid(
                        f"{operation} refused the start key"
                    ) from e
                logger.warning("%s failed: %s", operation, code)
                raise ResourceShareStoreError(f"{operation} failed: {code}") from e
            except BotoCoreError as e:
                logger.warning("%s unreachable: %s", operation, type(e).__name__)
                raise ResourceShareStoreError(
                    f"{operation} unreachable: {type(e).__name__}"
                ) from e
        raise ShareThrottled(
            f"{operation} throttled"
        )  # pragma: no cover - loop always returns


def _projection(attributes: Sequence[str]) -> dict[str, Any]:
    names = {f"#a{i}": column for i, column in enumerate(attributes)}
    return {"ProjectionExpression": ", ".join(names), "ExpressionAttributeNames": names}
