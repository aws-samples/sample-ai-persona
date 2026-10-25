"""DynamoDB reads and writes of discussions (U6, ADR-002, NFR Design Q1 A).

Three tables:

- ``Discussions`` (PK ``id``; ``OwnerIndex`` for "mine"): the body, judged by
  ``AccessPolicy``.
- ``DiscussionMessages`` (PK ``discussion_id``, SK ``seq`` N): the messages.
- ``DiscussionReports`` (PK ``discussion_id``, SK ``report_id``): the reports.

Messages and reports have no owner; they are read only through their
discussion, after its body was checked (``DiscussionRecords``). Every rule
about *whether* something may happen lives in ``DiscussionRecords`` and the
Managers; this service runs the reads, the conditional writes and the
transactions (``reliability-design.md`` §1, §2, §6):

- the body is written last, "id does not exist yet", in one transaction with
  the caller's items (``put_new``);
- partial updates write only the changed columns, conditioned on the body
  (``update_insights``, ``advance_message_count``);
- a report and the body's ``report_count`` move in one transaction, so the
  limit holds under concurrent saves (``put_report`` / ``delete_report``).

Failures: a failed condition -> :class:`DiscussionConditionFailed`,
``TransactionConflict`` -> :class:`DiscussionConflict`, anything else ->
:class:`DiscussionStoreError`. Batch writes and reads re-send what DynamoDB
left unprocessed three times (with back-off) and then raise.
"""

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.discussion_report import DiscussionReport
from src.models.errors import CodedError, ErrorCode
from src.models.message import DiscussionMessage

logger = logging.getLogger(__name__)

#: Largest body written (BR2.7; DynamoDB's own limit is 400KB).
MAX_BODY_BYTES = 350 * 1024
#: ``BatchWriteItem`` accepts at most 25 requests.
WRITE_BATCH_LIMIT = 25
#: ``BatchGetItem`` accepts at most 100 keys.
READ_BATCH_LIMIT = 100
#: Re-sends of unprocessed items / keys before giving up.
UNPROCESSED_RETRIES = 3
#: Waits before each re-send (jittered).
UNPROCESSED_BACKOFF_SECONDS: tuple[float, ...] = (0.05, 0.1, 0.2)

_ID = "id"
_DISCUSSION_ID = "discussion_id"
_SEQ = "seq"
_REPORT_ID = "report_id"
_WRITTEN_AT = "written_at"
_CREATED_AT = "created_at"


@dataclass(frozen=True)
class PartitionSummary:
    """What the cleanup script needs of one discussion's messages or reports.

    Attributes:
        count: Number of items.
        newest: Epoch seconds of the newest item (``written_at``, else
            ``created_at``); ``None`` when no item carries a time.
    """

    count: int
    newest: float | None


class DiscussionStoreError(CodedError):
    """A discussion read or write failed.

    Attributes:
        reason: ``condition``, ``conflict``, ``unprocessed`` or ``failed``.
    """

    code = ErrorCode.DISCUSSION_OPERATION_FAILED

    def __init__(self, message: str, *, reason: str = "failed") -> None:
        super().__init__(message)
        self.reason = reason


class DiscussionConditionFailed(DiscussionStoreError):
    """A write condition failed (the body is gone, a count moved, an ID exists).

    Attributes:
        failed_indexes: Positions of the transaction items whose condition
            failed (empty for a single write).
    """

    def __init__(self, message: str, failed_indexes: Sequence[int] = ()) -> None:
        super().__init__(message, reason="condition")
        self.failed_indexes = tuple(failed_indexes)


class DiscussionConflict(DiscussionStoreError):
    """Another transaction touched the same item at the same time."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="conflict")


def _item_time(item: Mapping[str, Any]) -> float | None:
    """Epoch seconds of an item: ``written_at``, else ``created_at`` (ISO 8601)."""
    written = item.get(_WRITTEN_AT)
    if written is not None:
        return float(written)
    created = item.get(_CREATED_AT)
    if not created:
        return None
    try:
        return datetime.fromisoformat(str(created)).timestamp()
    except ValueError:
        return None


def _to_dynamo(value: Any) -> Any:
    """Floats become ``Decimal`` (``TypeSerializer`` refuses floats)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


def _from_dynamo(value: Any) -> Any:
    """``Decimal`` back to ``int`` / ``float``, sets back to lists of strings."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    if isinstance(value, set):
        return sorted(_from_dynamo(v) for v in value)
    return value


def _attribute_size(value: Mapping[str, Any]) -> int:
    """Approximate DynamoDB size of one typed attribute value."""
    ((kind, inner),) = value.items()
    if kind == "S":
        return len(str(inner).encode("utf-8"))
    if kind == "N":
        return len(str(inner)) // 2 + 2
    if kind == "B":
        return len(inner)
    if kind in ("BOOL", "NULL"):
        return 1
    if kind == "M":
        return 3 + sum(
            len(k.encode("utf-8")) + _attribute_size(v) + 1 for k, v in inner.items()
        )
    if kind == "L":
        return 3 + sum(_attribute_size(v) + 1 for v in inner)
    return sum(len(str(v).encode("utf-8")) for v in inner)  # SS / NS / BS


class DiscussionStore:
    """Reads, conditional writes and transactions of the three discussion tables."""

    def __init__(
        self,
        *,
        region: str,
        discussions_table: str,
        messages_table: str,
        reports_table: str,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the store.

        Args:
            region: AWS region of the tables.
            discussions_table: Physical name of ``Discussions``.
            messages_table: Physical name of ``DiscussionMessages``.
            reports_table: Physical name of ``DiscussionReports``.
            client: Optional pre-built DynamoDB client (tests).
            sleep: Back-off between re-sends (tests pass a no-op).
            jitter: 0-1 jitter of the back-off (tests).
            clock: Epoch seconds stamped on messages as ``written_at`` (tests).
        """
        self.discussions_table = discussions_table
        self.messages_table = messages_table
        self.reports_table = reports_table
        self.client = client or boto3.client("dynamodb", region_name=region)
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    # --- size --------------------------------------------------------------------

    @staticmethod
    def item_size(body: Mapping[str, Any]) -> int:
        """Size of ``body`` as an item: attribute names plus values (BR2.7)."""
        serializer = TypeSerializer()
        item = _to_dynamo(body)
        return sum(
            len(name.encode("utf-8")) + _attribute_size(serializer.serialize(value))
            for name, value in item.items()
        )

    # --- bodies ------------------------------------------------------------------

    def put_new(
        self, body: Mapping[str, Any], extra_items: Sequence[dict[str, Any]] = ()
    ) -> None:
        """Write a new body with the caller's items in one transaction (BR1.2 (3)).

        Args:
            body: The body (``Discussion.to_dict()``).
            extra_items: More ``TransactWriteItems`` entries (the share index).

        Raises:
            DiscussionConditionFailed: The ID exists (or an extra item failed).
            DiscussionConflict: A concurrent transaction.
            DiscussionStoreError: Anything else.
        """
        put = {
            "Put": {
                "TableName": self.discussions_table,
                "Item": self._item(body),
                "ConditionExpression": "attribute_not_exists(#id)",
                "ExpressionAttributeNames": {"#id": _ID},
            }
        }
        self._transact([put, *extra_items])

    def get(self, discussion_id: str) -> dict[str, Any] | None:
        """Read one body with a strongly consistent ``GetItem`` (plain values)."""
        response = self._call(
            "get_item",
            TableName=self.discussions_table,
            Key={_ID: {"S": discussion_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return self._plain(item) if item else None

    def get_many(
        self, discussion_ids: Sequence[str], attributes: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Read some columns of many bodies, strongly consistent (NFR2.15).

        Returns:
            id -> body (only the given attributes), for the IDs that exist.

        Raises:
            DiscussionStoreError: Keys were still unprocessed after three
                re-reads (``reason="unprocessed"``); rows are never dropped.
        """
        ids = list(dict.fromkeys(discussion_ids))
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(ids), READ_BATCH_LIMIT):
            found.update(
                self._batch_get(ids[start : start + READ_BATCH_LIMIT], attributes)
            )
        return found

    def _batch_get(
        self, ids: Sequence[str], attributes: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        names = {f"#a{i}": a for i, a in enumerate(dict.fromkeys([_ID, *attributes]))}
        request: dict[str, Any] = {
            "ConsistentRead": True,
            "ProjectionExpression": ", ".join(names),
            "ExpressionAttributeNames": names,
        }
        keys: list[dict[str, Any]] = [{_ID: {"S": i}} for i in ids]
        found: dict[str, dict[str, Any]] = {}
        for attempt in range(UNPROCESSED_RETRIES + 1):
            response = self._call(
                "batch_get_item",
                RequestItems={self.discussions_table: {**request, "Keys": keys}},
            )
            for item in response.get("Responses", {}).get(self.discussions_table, []):
                plain = self._plain(item)
                found[str(plain[_ID])] = {
                    k: v for k, v in plain.items() if k in attributes
                }
            keys = (
                response.get("UnprocessedKeys", {})
                .get(self.discussions_table, {})
                .get("Keys", [])
            )
            if not keys:
                return found
            if attempt < UNPROCESSED_RETRIES:
                self._backoff(attempt)
        logger.warning("BatchGetItem left %d discussions unprocessed", len(keys))
        raise DiscussionStoreError(
            "discussions left unprocessed after three re-reads", reason="unprocessed"
        )

    def existing(self, discussion_ids: Sequence[str]) -> set[str]:
        """The IDs among ``discussion_ids`` whose body exists (the cleanup script)."""
        return set(self.get_many(discussion_ids, [_ID]))

    def update_insights(
        self, discussion_id: str, insights: list[dict[str, Any]], updated_at: str
    ) -> None:
        """Replace ``insights`` and ``updated_at`` only (BR1.4).

        Raises:
            DiscussionConditionFailed: The body does not exist.
        """
        self._update_body(
            discussion_id,
            "SET #insights = :insights, #updated = :updated",
            "attribute_exists(#id)",
            names={"#insights": "insights", "#updated": "updated_at"},
            values={":insights": insights, ":updated": updated_at},
        )

    def advance_message_count(
        self,
        discussion_id: str,
        *,
        expected: int,
        new: int,
        updated_at: str,
        documents: list[dict[str, Any]] | None = None,
    ) -> None:
        """Move ``message_count`` from ``expected`` to ``new`` (BR3.8, NFR1.62).

        Args:
            documents: The whole new ``documents`` list, when files were added.

        Raises:
            DiscussionConditionFailed: The body is gone or the count moved.
        """
        expression = "SET #count = :new, #updated = :updated"
        names = {"#count": "message_count", "#updated": "updated_at"}
        values: dict[str, Any] = {
            ":new": new,
            ":updated": updated_at,
            ":expected": expected,
        }
        if documents is not None:
            expression += ", #documents = :documents"
            names["#documents"] = "documents"
            values[":documents"] = documents
        self._update_body(
            discussion_id,
            expression,
            "attribute_exists(#id) AND #count = :expected",
            names=names,
            values=values,
        )

    def _update_body(
        self,
        discussion_id: str,
        expression: str,
        condition: str,
        *,
        names: dict[str, str],
        values: dict[str, Any],
    ) -> None:
        try:
            self.client.update_item(
                TableName=self.discussions_table,
                Key={_ID: {"S": discussion_id}},
                UpdateExpression=expression,
                ConditionExpression=condition,
                ExpressionAttributeNames={**names, "#id": _ID},
                ExpressionAttributeValues=self._values(values),
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise DiscussionConditionFailed("body update condition failed") from e
            logger.warning("UpdateItem on discussions failed: %s", code)
            raise DiscussionStoreError(f"update_item failed: {code}") from e
        except BotoCoreError as e:
            raise self._unreachable("update_item", e) from e

    # --- messages ----------------------------------------------------------------

    def write_messages(self, messages: Sequence[DiscussionMessage]) -> None:
        """Write messages, 25 per ``BatchWriteItem`` (BR1.2 (2)).

        Raises:
            DiscussionStoreError: Items stayed unprocessed after three re-sends.
        """
        # written_at: 書いた時刻。インタビューの保存は古い発言を移すため、created_at では
        # 保存の途中かどうかを見分けられない（後始末のスクリプト、BR1.6）
        written_at = int(self._clock())
        requests = [
            {
                "PutRequest": {
                    "Item": self._item({**m.to_dict(), _WRITTEN_AT: written_at})
                }
            }
            for m in messages
        ]
        self._batch_write(self.messages_table, requests)

    def query_messages(
        self, discussion_id: str, *, up_to: int | None = None
    ) -> list[DiscussionMessage]:
        """The messages in ``seq`` order, through every page (BR1.3, NFR9.34).

        Args:
            up_to: Read only ``seq <= up_to`` (the body's ``message_count``).
        """
        condition = "#d = :d"
        names = {"#d": _DISCUSSION_ID}
        values: dict[str, Any] = {":d": {"S": discussion_id}}
        if up_to is not None:
            condition += " AND #s <= :n"
            names["#s"] = _SEQ
            values[":n"] = {"N": str(up_to)}
        items = self._query_all(
            TableName=self.messages_table,
            KeyConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ScanIndexForward=True,
            ConsistentRead=True,
        )
        return [DiscussionMessage.from_dict(self._plain(i)) for i in items]

    def delete_messages(self, discussion_id: str) -> int:
        """Delete every message of a discussion (BR1.5 (2)). Returns the count."""
        return self._delete_partition(self.messages_table, discussion_id, _SEQ)

    # --- reports -----------------------------------------------------------------

    def query_reports(self, discussion_id: str) -> list[DiscussionReport]:
        """The reports of a discussion, oldest first."""
        items = self._query_all(
            TableName=self.reports_table,
            KeyConditionExpression="#d = :d",
            ExpressionAttributeNames={"#d": _DISCUSSION_ID},
            ExpressionAttributeValues={":d": {"S": discussion_id}},
            ConsistentRead=True,
        )
        reports = [DiscussionReport.from_dict(self._plain(i)) for i in items]
        return sorted(reports, key=lambda r: r.created_at)

    def put_report(
        self, report: DiscussionReport, *, max_reports: int, updated_at: str
    ) -> None:
        """Save a report and add one to ``report_count`` (BR4.4, NFR1.62).

        Raises:
            DiscussionConditionFailed: The body is gone or already holds
                ``max_reports`` reports.
        """
        self._transact(
            [
                {
                    "Put": {
                        "TableName": self.reports_table,
                        "Item": self._item(report.to_dict()),
                        "ConditionExpression": "attribute_not_exists(#r)",
                        "ExpressionAttributeNames": {"#r": _REPORT_ID},
                    }
                },
                self._body_update(
                    report.discussion_id,
                    "SET #c = if_not_exists(#c, :zero) + :one, #u = :u",
                    "attribute_exists(#id) AND (attribute_not_exists(#c) OR #c < :max)",
                    {":zero": 0, ":one": 1, ":u": updated_at, ":max": max_reports},
                ),
            ]
        )

    def update_report(self, report: DiscussionReport, *, updated_at: str) -> None:
        """Write a report's new content and the body's ``updated_at`` (BR4.4).

        Raises:
            DiscussionConditionFailed: The report or the body is gone.
        """
        self._transact(
            [
                {
                    "Update": {
                        "TableName": self.reports_table,
                        "Key": self._report_key(report.discussion_id, report.report_id),
                        "UpdateExpression": "SET #content = :c, #updated = :u",
                        "ConditionExpression": "attribute_exists(#r)",
                        "ExpressionAttributeNames": {
                            "#content": "content",
                            "#updated": "updated_at",
                            "#r": _REPORT_ID,
                        },
                        "ExpressionAttributeValues": self._values(
                            {
                                ":c": report.content,
                                ":u": (
                                    report.updated_at.isoformat()
                                    if report.updated_at
                                    else updated_at
                                ),
                            }
                        ),
                    }
                },
                self._body_update(
                    report.discussion_id,
                    "SET #u = :u",
                    "attribute_exists(#id)",
                    {":u": updated_at},
                ),
            ]
        )

    def delete_report(
        self, discussion_id: str, report_id: str, *, updated_at: str
    ) -> None:
        """Delete one report and take one from ``report_count`` (BR4.4).

        Raises:
            DiscussionConditionFailed: The report or the body is gone.
        """
        self._transact(
            [
                {
                    "Delete": {
                        "TableName": self.reports_table,
                        "Key": self._report_key(discussion_id, report_id),
                        "ConditionExpression": "attribute_exists(#r)",
                        "ExpressionAttributeNames": {"#r": _REPORT_ID},
                    }
                },
                self._body_update(
                    discussion_id,
                    "SET #c = #c - :one, #u = :u",
                    "attribute_exists(#id) AND #c > :zero",
                    {":one": 1, ":zero": 0, ":u": updated_at},
                ),
            ]
        )

    def delete_reports(self, discussion_id: str) -> int:
        """Delete every report of a discussion (BR1.5 (2)). Returns the count."""
        return self._delete_partition(self.reports_table, discussion_id, _REPORT_ID)

    # --- the cleanup script (BR1.6; operators' credentials, not the task role) ------

    def message_partitions(self) -> dict[str, PartitionSummary]:
        """Discussion ID -> count and newest write time of its messages."""
        return self._scan_partitions(self.messages_table)

    def report_partitions(self) -> dict[str, PartitionSummary]:
        """Discussion ID -> count and newest time of its reports."""
        return self._scan_partitions(self.reports_table)

    def _scan_partitions(self, table: str) -> dict[str, PartitionSummary]:
        counts: dict[str, int] = {}
        newest: dict[str, float | None] = {}
        start: dict[str, Any] | None = None
        while True:
            params: dict[str, Any] = {
                "TableName": table,
                "ProjectionExpression": "#d, #w, #c",
                "ExpressionAttributeNames": {
                    "#d": _DISCUSSION_ID,
                    "#w": _WRITTEN_AT,
                    "#c": _CREATED_AT,
                },
            }
            if start:
                params["ExclusiveStartKey"] = start
            response = self._call("scan", **params)
            for item in response.get("Items", []):
                plain = self._plain(item)
                key = str(plain[_DISCUSSION_ID])
                counts[key] = counts.get(key, 0) + 1
                when = _item_time(plain)
                old = newest.get(key)
                newest[key] = when if old is None else max(old, when or old)
            start = response.get("LastEvaluatedKey")
            if not start:
                return {k: PartitionSummary(counts[k], newest.get(k)) for k in counts}

    # --- plumbing ----------------------------------------------------------------

    def _body_update(
        self,
        discussion_id: str,
        expression: str,
        condition: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "Update": {
                "TableName": self.discussions_table,
                "Key": {_ID: {"S": discussion_id}},
                "UpdateExpression": expression,
                "ConditionExpression": condition,
                "ExpressionAttributeNames": {
                    "#id": _ID,
                    **({"#c": "report_count"} if "#c" in expression else {}),
                    "#u": "updated_at",
                },
                "ExpressionAttributeValues": self._values(values),
            }
        }

    @staticmethod
    def _report_key(discussion_id: str, report_id: str) -> dict[str, Any]:
        return {_DISCUSSION_ID: {"S": discussion_id}, _REPORT_ID: {"S": report_id}}

    def _query_all(self, **params: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            response = self._call("query", **params)
            items.extend(response.get("Items", []))
            start = response.get("LastEvaluatedKey")
            if not start:
                return items
            params["ExclusiveStartKey"] = start

    def _delete_partition(self, table: str, discussion_id: str, sort_key: str) -> int:
        keys = self._query_all(
            TableName=table,
            KeyConditionExpression="#d = :d",
            ExpressionAttributeNames={"#d": _DISCUSSION_ID, "#s": sort_key},
            ExpressionAttributeValues={":d": {"S": discussion_id}},
            ProjectionExpression="#d, #s",
        )
        self._batch_write(table, [{"DeleteRequest": {"Key": key}} for key in keys])
        return len(keys)

    def _batch_write(self, table: str, requests: Iterable[dict[str, Any]]) -> None:
        pending_all = list(requests)
        for start in range(0, len(pending_all), WRITE_BATCH_LIMIT):
            pending = pending_all[start : start + WRITE_BATCH_LIMIT]
            for attempt in range(UNPROCESSED_RETRIES + 1):
                response = self._call("batch_write_item", RequestItems={table: pending})
                pending = response.get("UnprocessedItems", {}).get(table, [])
                if not pending:
                    break
                if attempt < UNPROCESSED_RETRIES:
                    self._backoff(attempt)
            else:
                logger.warning("BatchWriteItem left %d items unprocessed", len(pending))
                raise DiscussionStoreError(
                    "items left unprocessed after three re-sends", reason="unprocessed"
                )

    def _backoff(self, attempt: int) -> None:
        base = UNPROCESSED_BACKOFF_SECONDS[
            min(attempt, len(UNPROCESSED_BACKOFF_SECONDS) - 1)
        ]
        self._sleep(base * (0.5 + self._jitter()))

    def _transact(self, items: list[dict[str, Any]]) -> None:
        try:
            self.client.transact_write_items(TransactItems=items)
        except ClientError as e:
            raise self._classify(e) from e
        except BotoCoreError as e:
            raise self._unreachable("transact_write_items", e) from e

    @staticmethod
    def _classify(error: ClientError) -> DiscussionStoreError:
        code = error.response.get("Error", {}).get("Code", "")
        if code != "TransactionCanceledException":
            logger.warning("TransactWriteItems on discussions failed: %s", code)
            return DiscussionStoreError(f"TransactWriteItems failed: {code}")
        reasons = [
            str(r.get("Code") or "None")
            for r in error.response.get("CancellationReasons", [])
        ]
        if "TransactionConflict" in reasons:
            return DiscussionConflict("transaction conflict")
        failed = [i for i, r in enumerate(reasons) if r == "ConditionalCheckFailed"]
        if failed:
            return DiscussionConditionFailed("a condition failed", failed)
        logger.warning("TransactWriteItems cancelled: %s", ",".join(reasons))
        return DiscussionStoreError("transaction cancelled for another reason")

    def _item(self, data: Mapping[str, Any]) -> dict[str, Any]:
        return {k: self._serializer.serialize(v) for k, v in _to_dynamo(data).items()}

    def _values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return {k: self._serializer.serialize(_to_dynamo(v)) for k, v in values.items()}

    def _plain(self, item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            k: _from_dynamo(self._deserializer.deserialize(v)) for k, v in item.items()
        }

    @staticmethod
    def _unreachable(operation: str, error: BaseException) -> DiscussionStoreError:
        logger.warning(
            "%s on discussions unreachable: %s", operation, type(error).__name__
        )
        return DiscussionStoreError(f"{operation} unreachable: {type(error).__name__}")

    def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response: dict[str, Any] = getattr(self.client, operation)(**kwargs)
            return response
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            logger.warning("%s on discussions failed: %s", operation, code)
            raise DiscussionStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            raise self._unreachable(operation, e) from e
