"""DynamoDB reads and writes of custom persona data records (U7, Q2 A).

``CustomPersonaData`` (PK ``id``, no index) holds two kinds of items:

- the record of one imported file (``id`` = UUID, :class:`CustomPersonaData`);
- the name reservation ``id = "name#{name}"`` that keeps names unique
  (BR4.4). It carries ``data_id`` and is never returned as a record
  (NFR1.74).

Who may read or change a record is decided by ``SurveyDatasetManager``; this
service only runs the reads, the conditional writes and the transactions
(``reliability-design.md`` §1):

- registration writes the record and its reservation in one transaction,
  both "id does not exist yet";
- renaming deletes the old reservation, adds the new one and updates ``name``
  in one transaction;
- the scope is changed with ``ADD`` and ``DELETE`` on the string set in two
  separate updates conditioned on the record (DynamoDB refuses both actions on
  one attribute in one expression); an empty side sends nothing;
- deleting removes the record and its reservation in one transaction.

Failures: a taken name -> :class:`CustomPersonaDataNameTaken`, another failed
condition -> :class:`CustomPersonaDataConditionFailed`, anything else ->
:class:`CustomPersonaDataStoreError`.
"""

import logging
import random
import time
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.custom_persona_data import (
    NAME_RESERVATION_PREFIX,
    CustomPersonaData,
    is_name_reservation,
    reservation_id,
)
from src.models.errors import CodedError, ErrorCode

logger = logging.getLogger(__name__)

#: ``BatchGetItem`` accepts at most 100 keys.
READ_BATCH_LIMIT = 100
#: Re-reads of ``UnprocessedKeys`` before giving up.
UNPROCESSED_RETRIES = 3
#: Waits before each re-read (jittered).
UNPROCESSED_BACKOFF_SECONDS: tuple[float, ...] = (0.05, 0.1, 0.2)
#: Columns a caller may change with :meth:`CustomPersonaDataStore.update_fields`.
#: ``id``, ``name`` and ``shared_group_ids`` have their own operations (BR1.5).
UPDATABLE_FIELDS = frozenset({"columns", "row_count", "standard_mapping"})

_ID = "id"
_SHARED = "shared_group_ids"


class CustomPersonaDataStoreError(CodedError):
    """A custom persona data read or write failed.

    Attributes:
        reason: ``condition``, ``name_taken``, ``unprocessed`` or ``failed``.
    """

    code = ErrorCode.DATABASE_OPERATION_FAILED

    def __init__(self, message: str, *, reason: str = "failed") -> None:
        super().__init__(message)
        self.reason = reason


class CustomPersonaDataConditionFailed(CustomPersonaDataStoreError):
    """The record is missing (or exists already, or changed under the write)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="condition")


class CustomPersonaDataNameTaken(CustomPersonaDataStoreError):
    """Another record holds the name (BR4.4)."""

    code = ErrorCode.CUSTOM_PERSONA_DATA_NAME_TAKEN

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="name_taken")


class CustomPersonaDataStore:
    """Records and name reservations of the ``CustomPersonaData`` table."""

    def __init__(
        self,
        *,
        region: str,
        table_name: str,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        """Create the store.

        Args:
            region: AWS region of the table.
            table_name: Physical name of ``CustomPersonaData``.
            client: Optional pre-built DynamoDB client (tests).
            sleep: Back-off between re-reads (tests pass a no-op).
            jitter: 0-1 jitter of the back-off (tests).
            clock: Time stamped on name reservations.
        """
        self.table_name = table_name
        self.client = client or boto3.client("dynamodb", region_name=region)
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    # --- writes ----------------------------------------------------------------

    def put_new(self, data: CustomPersonaData) -> None:
        """Write a new record and its name reservation in one transaction.

        Raises:
            CustomPersonaDataNameTaken: The name is held by another record.
            CustomPersonaDataConditionFailed: The ID exists already.
            CustomPersonaDataStoreError: Anything else.
        """
        item = data.to_dict()
        self._transact(
            [
                self._put_if_absent(self._item(item, string_sets=(_SHARED,))),
                self._put_if_absent(self._reservation(data.name, data.id)),
            ],
            name_index=1,
        )

    def rename(self, data_id: str, *, old_name: str, new_name: str) -> None:
        """Move the reservation and change ``name`` together (BR4.4).

        Raises:
            CustomPersonaDataNameTaken: ``new_name`` is held by another record.
            CustomPersonaDataConditionFailed: The record is gone or was renamed.
            CustomPersonaDataStoreError: Anything else.
        """
        delete_old = {
            "Delete": {
                "TableName": self.table_name,
                "Key": {_ID: {"S": reservation_id(old_name)}},
                "ConditionExpression": "attribute_exists(#id) AND #data_id = :id",
                "ExpressionAttributeNames": {"#id": _ID, "#data_id": "data_id"},
                "ExpressionAttributeValues": {":id": {"S": data_id}},
            }
        }
        update_record = {
            "Update": {
                "TableName": self.table_name,
                "Key": {_ID: {"S": data_id}},
                "UpdateExpression": "SET #name = :new",
                "ConditionExpression": "attribute_exists(#id) AND #name = :old",
                "ExpressionAttributeNames": {"#id": _ID, "#name": "name"},
                "ExpressionAttributeValues": {
                    ":new": {"S": new_name.strip()},
                    ":old": {"S": old_name.strip()},
                },
            }
        }
        self._transact(
            [
                delete_old,
                self._put_if_absent(self._reservation(new_name, data_id)),
                update_record,
            ],
            name_index=1,
        )

    def update_fields(self, data_id: str, fields: Mapping[str, Any]) -> None:
        """Change only the given columns, conditioned on the record (BR1.5).

        Raises:
            ValueError: A column outside :data:`UPDATABLE_FIELDS`.
            CustomPersonaDataConditionFailed: The record does not exist.
        """
        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"columns not updatable here: {sorted(unknown)}")
        if not fields:
            return
        names = {f"#f{i}": column for i, column in enumerate(fields)}
        values = {
            f":f{i}": self._serializer.serialize(value)
            for i, value in enumerate(fields.values())
        }
        self._conditional_update(
            data_id,
            "SET " + ", ".join(f"#f{i} = :f{i}" for i in range(len(fields))),
            names=names,
            values=values,
        )

    def update_scope(
        self, data_id: str, *, add: frozenset[str], remove: frozenset[str]
    ) -> None:
        """``ADD`` the added departments, then ``DELETE`` the removed ones (BR5.2).

        Two separate updates, each conditioned on the record; a side with no
        department is not sent. When the second update fails the first one is
        kept (the screen reads the scope again).

        Raises:
            CustomPersonaDataConditionFailed: The record does not exist.
            CustomPersonaDataStoreError: Anything else.
        """
        for action, groups in (("ADD", add), ("DELETE", remove)):
            if not groups:
                continue
            self._conditional_update(
                data_id,
                f"{action} #shared :groups",
                names={"#shared": _SHARED},
                values={":groups": {"SS": sorted(groups)}},
            )

    def delete(self, data_id: str, name: str) -> bool:
        """Delete the record and its reservation in one transaction (BR4.6).

        Returns:
            ``False`` when the record did not exist.

        Raises:
            CustomPersonaDataStoreError: Anything other than a missing record.
        """
        try:
            self._transact(
                [
                    {
                        "Delete": {
                            "TableName": self.table_name,
                            "Key": {_ID: {"S": data_id}},
                            "ConditionExpression": "attribute_exists(#id)",
                            "ExpressionAttributeNames": {"#id": _ID},
                        }
                    },
                    {
                        "Delete": {
                            "TableName": self.table_name,
                            "Key": {_ID: {"S": reservation_id(name)}},
                        }
                    },
                ]
            )
        except CustomPersonaDataConditionFailed:
            return False
        return True

    # --- reads -----------------------------------------------------------------

    def get(self, data_id: str) -> CustomPersonaData | None:
        """One record, strongly consistent; reservations read as missing."""
        if not data_id or is_name_reservation(data_id):
            return None
        response = self._call(
            "get_item",
            TableName=self.table_name,
            Key={_ID: {"S": data_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return self._record(item) if item else None

    def get_many(self, data_ids: Sequence[str]) -> dict[str, CustomPersonaData]:
        """Records of many IDs, strongly consistent (reservations left out).

        Raises:
            CustomPersonaDataStoreError: Keys were still unprocessed after
                three re-reads (``reason="unprocessed"``).
        """
        ids = [i for i in dict.fromkeys(data_ids) if i and not is_name_reservation(i)]
        found: dict[str, CustomPersonaData] = {}
        for start in range(0, len(ids), READ_BATCH_LIMIT):
            found.update(self._batch_get(ids[start : start + READ_BATCH_LIMIT]))
        return found

    def list_all(self) -> list[CustomPersonaData]:
        """Every record, reading every scan page (NFR2.23, NFR9.37)."""
        params: dict[str, Any] = {
            "TableName": self.table_name,
            "FilterExpression": "NOT begins_with(#id, :prefix)",
            "ExpressionAttributeNames": {"#id": _ID},
            "ExpressionAttributeValues": {":prefix": {"S": NAME_RESERVATION_PREFIX}},
        }
        records: list[CustomPersonaData] = []
        while True:
            response = self._call("scan", **params)
            for item in response.get("Items", []):
                record = self._record(item)
                if record is not None:
                    records.append(record)
            last = response.get("LastEvaluatedKey")
            if not last:
                return records
            params["ExclusiveStartKey"] = last

    # --- helpers ---------------------------------------------------------------

    def _batch_get(self, ids: Sequence[str]) -> dict[str, CustomPersonaData]:
        keys: list[dict[str, Any]] = [{_ID: {"S": i}} for i in ids]
        found: dict[str, CustomPersonaData] = {}
        for attempt in range(UNPROCESSED_RETRIES + 1):
            response = self._call(
                "batch_get_item",
                RequestItems={self.table_name: {"Keys": keys, "ConsistentRead": True}},
            )
            for item in response.get("Responses", {}).get(self.table_name, []):
                record = self._record(item)
                if record is not None:
                    found[record.id] = record
            keys = (
                response.get("UnprocessedKeys", {})
                .get(self.table_name, {})
                .get("Keys", [])
            )
            if not keys:
                return found
            if attempt < UNPROCESSED_RETRIES:
                base = UNPROCESSED_BACKOFF_SECONDS[attempt]
                self._sleep(base * (0.5 + self._jitter()))
        raise CustomPersonaDataStoreError(
            "records left unprocessed after three re-reads", reason="unprocessed"
        )

    def _record(self, item: Mapping[str, Any]) -> CustomPersonaData | None:
        data = {k: self._deserializer.deserialize(v) for k, v in item.items()}
        if is_name_reservation(str(data.get(_ID, ""))):
            return None
        try:
            return CustomPersonaData.from_dict(data)
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping a malformed custom persona data item")
            return None

    def _item(
        self, data: Mapping[str, Any], *, string_sets: Sequence[str] = ()
    ) -> dict[str, Any]:
        item: dict[str, Any] = {}
        for key, value in data.items():
            if key in string_sets:
                item[key] = {"SS": sorted(value)}
            else:
                item[key] = self._serializer.serialize(value)
        return item

    def _reservation(self, name: str, data_id: str) -> dict[str, Any]:
        return {
            _ID: {"S": reservation_id(name)},
            "data_id": {"S": data_id},
            "created_at": {"S": self._clock().isoformat()},
        }

    def _put_if_absent(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self.table_name,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(#id)",
                "ExpressionAttributeNames": {"#id": _ID},
            }
        }

    def _conditional_update(
        self,
        data_id: str,
        expression: str,
        *,
        names: dict[str, str],
        values: dict[str, Any],
    ) -> None:
        try:
            self.client.update_item(
                TableName=self.table_name,
                Key={_ID: {"S": data_id}},
                UpdateExpression=expression,
                ConditionExpression="attribute_exists(#id)",
                ExpressionAttributeNames={**names, "#id": _ID},
                ExpressionAttributeValues=values,
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise CustomPersonaDataConditionFailed(
                    "the record does not exist"
                ) from e
            logger.warning("UpdateItem on custom persona data failed: %s", code)
            raise CustomPersonaDataStoreError(f"update_item failed: {code}") from e
        except BotoCoreError as e:
            raise CustomPersonaDataStoreError(
                f"update_item unreachable: {type(e).__name__}"
            ) from e

    def _transact(
        self, items: list[dict[str, Any]], *, name_index: int | None = None
    ) -> None:
        try:
            self.client.transact_write_items(TransactItems=items)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "TransactionCanceledException":
                logger.warning("TransactWriteItems failed: %s", code)
                raise CustomPersonaDataStoreError(
                    f"transact_write_items failed: {code}"
                ) from e
            reasons = [
                r.get("Code") for r in e.response.get("CancellationReasons") or []
            ]
            failed = [i for i, r in enumerate(reasons) if r == "ConditionalCheckFailed"]
            if name_index is not None and name_index in failed:
                raise CustomPersonaDataNameTaken("the name is taken") from e
            if failed:
                raise CustomPersonaDataConditionFailed(
                    "a transaction condition failed"
                ) from e
            logger.warning("Transaction cancelled: %s", reasons)
            raise CustomPersonaDataStoreError("transaction cancelled") from e
        except BotoCoreError as e:
            raise CustomPersonaDataStoreError(
                f"transact_write_items unreachable: {type(e).__name__}"
            ) from e

    def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response: dict[str, Any] = getattr(self.client, operation)(**kwargs)
            return response
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            logger.warning("%s on custom persona data failed: %s", operation, code)
            raise CustomPersonaDataStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            raise CustomPersonaDataStoreError(
                f"{operation} unreachable: {type(e).__name__}"
            ) from e
