"""DynamoDB reads and writes of uploaded source files (``SourceFiles``).

PK ``source_file_id``; GSI ``owner_sub-created_at`` lists one user's files.
Every rule that decides *whether* something may happen lives in
``SourceUploadManager`` / ``SourceFileCatalog``; this store only runs the
conditional writes, so that nothing is compared after reading:

- ``begin_inspection``: ``uploading -> inspecting`` once, or again when an
  inspection has been stuck past ``stale_before``. The condition also
  carries the owner and the 24-hour age of the upload.
- ``mark_ready`` / ``mark_error``: only from the states that lead there.
- ``record_url_refresh``: counts the part URL refreshes of the owner's
  uploading file up to the limit.
- ``clear_in_use``: only when the mark belongs to the same generation.
  The mark itself is set inside the start transaction of
  ``AdvancedGenerationStore``, which also stamps the last use.
- ``begin_delete`` / ``delete_record``: ``deleting`` first, the record
  last, so a failed S3 deletion leaves a ``deleting`` row to retry.
- ``fill_expires_at`` / ``set_checksum_if_empty``: write a value only when
  the record has none (older records lack them).

``expires_at`` is the table's TTL attribute, so it is the one time written as
a Number of epoch seconds (``_expiry_number``); every other time is ISO text.

A failed condition raises :class:`SourceFileUnavailable` (or returns
``False``) whatever the reason: someone else's, missing, or the wrong state.
Retries are botocore's standard ones.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode
from src.models.source_file import (
    DocumentMetadata,
    SourceFile,
    SourceFileStatus,
    SourceMetadata,
)

logger = logging.getLogger(__name__)

#: GSI that lists one user's files, newest first.
OWNER_INDEX = "owner_sub-created_at"

_KEY = "source_file_id"


class SourceFileStoreError(CodedError):
    """A read or write of ``SourceFiles`` failed."""

    code = ErrorCode.DATABASE_OPERATION_FAILED


class SourceFileUnavailable(SourceFileStoreError):
    """A condition failed (not told apart: owner, state, age, missing)."""


def _stamp(value: datetime) -> str:
    """Comparable UTC text, the same format the model writes."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _epoch(value: datetime) -> int:
    """Epoch seconds of ``value`` (the TTL attribute ``expires_at``)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def _expiry_number(value: datetime) -> dict[str, str]:
    """``expires_at`` as the DynamoDB Number the TTL reads."""
    return {"N": str(_epoch(value))}


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
    """``Decimal`` back to ``int`` / ``float``."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    return value


class SourceFileStore:
    """Conditional reads and writes of ``SourceFiles``."""

    def __init__(self, *, region: str, table_name: str, client: Any = None) -> None:
        """Create the store.

        Args:
            region: AWS region of the table.
            table_name: Physical name of ``SourceFiles``.
            client: Optional pre-built DynamoDB client (tests).
        """
        self.table_name = table_name
        self.client = client or boto3.client("dynamodb", region_name=region)
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    # --- items ----------------------------------------------------------------------

    def _serialize(self, value: Any) -> dict[str, Any]:
        result: dict[str, Any] = self._serializer.serialize(_to_dynamo(value))
        return result

    def _item(self, source_file: SourceFile) -> dict[str, Any]:
        item = {
            k: self._serialize(v)
            for k, v in source_file.to_dict().items()
            if k != "expires_at"
        }
        if source_file.expires_at is not None:
            item["expires_at"] = _expiry_number(source_file.expires_at)
        return item

    def _source_file(self, item: Mapping[str, Any]) -> SourceFile:
        plain = {
            k: _from_dynamo(self._deserializer.deserialize(v)) for k, v in item.items()
        }
        return SourceFile.from_dict(plain)

    @staticmethod
    def _key(source_file_id: str) -> dict[str, Any]:
        return {_KEY: {"S": source_file_id}}

    # --- writes ---------------------------------------------------------------------

    def put_new(self, source_file: SourceFile) -> None:
        """Write a new upload.

        Raises:
            SourceFileStoreError: The ID already exists or DynamoDB failed.
        """
        self._call(
            "put_item",
            TableName=self.table_name,
            Item=self._item(source_file),
            ConditionExpression="attribute_not_exists(source_file_id)",
        )

    def begin_inspection(
        self,
        source_file_id: str,
        owner_sub: str,
        *,
        now: datetime,
        stale_before: datetime,
        created_after: datetime,
    ) -> SourceFile:
        """Move the owner's upload to ``inspecting``.

        Args:
            source_file_id: The upload.
            owner_sub: The caller; someone else's file fails the condition.
            now: Written as ``updated_at``.
            stale_before: An ``inspecting`` file last updated before this may
                start again (a stuck inspection).
            created_after: Uploads created before this no longer complete.

        Returns:
            The file as written.

        Raises:
            SourceFileUnavailable: Someone else's, missing, too old, already
                completed or still being inspected.
            SourceFileStoreError: DynamoDB failed.
        """
        response = self._call(
            "update_item",
            TableName=self.table_name,
            Key=self._key(source_file_id),
            UpdateExpression="SET #status = :inspecting, updated_at = :now",
            ConditionExpression=(
                "owner_sub = :owner AND created_at > :created_after AND "
                "(#status = :uploading OR "
                "(#status = :inspecting AND updated_at < :stale))"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":inspecting": {"S": SourceFileStatus.INSPECTING.value},
                ":uploading": {"S": SourceFileStatus.UPLOADING.value},
                ":now": {"S": _stamp(now)},
                ":owner": {"S": owner_sub},
                ":created_after": {"S": _stamp(created_after)},
                ":stale": {"S": _stamp(stale_before)},
            },
            ReturnValues="ALL_NEW",
            unavailable=True,
        )
        return self._source_file(response["Attributes"])

    def mark_ready(
        self,
        source_file_id: str,
        metadata: SourceMetadata | DocumentMetadata,
        checksum: str,
        *,
        now: datetime,
    ) -> SourceFile:
        """Write the inspection result and move ``inspecting -> ready``.

        The multipart upload ID is dropped (the upload is complete).

        Raises:
            SourceFileUnavailable: The file is not ``inspecting``.
            SourceFileStoreError: DynamoDB failed.
        """
        response = self._call(
            "update_item",
            TableName=self.table_name,
            Key=self._key(source_file_id),
            UpdateExpression=(
                "SET #status = :ready, source_metadata = :metadata, "
                "checksum = :checksum, updated_at = :now "
                "REMOVE multipart_upload_id"
            ),
            ConditionExpression="#status = :inspecting",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":ready": {"S": SourceFileStatus.READY.value},
                ":inspecting": {"S": SourceFileStatus.INSPECTING.value},
                ":metadata": self._serialize(metadata.to_dict()),
                ":checksum": {"S": checksum},
                ":now": {"S": _stamp(now)},
            },
            ReturnValues="ALL_NEW",
            unavailable=True,
        )
        return self._source_file(response["Attributes"])

    def mark_error(
        self, source_file_id: str, error_code: str, *, now: datetime
    ) -> bool:
        """Move an uploading or inspecting file to ``error`` with only the code.

        Returns:
            ``False`` when the file is missing or already ready / error.
        """
        return self._conditional(
            "update_item",
            TableName=self.table_name,
            Key=self._key(source_file_id),
            UpdateExpression=(
                "SET #status = :error, error_code = :code, updated_at = :now"
            ),
            ConditionExpression="#status IN (:uploading, :inspecting)",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":error": {"S": SourceFileStatus.ERROR.value},
                ":code": {"S": error_code},
                ":uploading": {"S": SourceFileStatus.UPLOADING.value},
                ":inspecting": {"S": SourceFileStatus.INSPECTING.value},
                ":now": {"S": _stamp(now)},
            },
        )

    def record_url_refresh(
        self,
        source_file_id: str,
        owner_sub: str,
        *,
        max_refreshes: int,
        now: datetime,
    ) -> SourceFile:
        """Count one part URL refresh of the owner's uploading file.

        Returns:
            The file with the new count.

        Raises:
            SourceFileUnavailable: Someone else's, missing, not uploading, or
                ``max_refreshes`` already reached.
            SourceFileStoreError: DynamoDB failed.
        """
        response = self._call(
            "update_item",
            TableName=self.table_name,
            Key=self._key(source_file_id),
            UpdateExpression="ADD url_refresh_count :one SET updated_at = :now",
            ConditionExpression=(
                "owner_sub = :owner AND #status = :uploading AND "
                "(attribute_not_exists(url_refresh_count) OR "
                "url_refresh_count < :max)"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":one": {"N": "1"},
                ":now": {"S": _stamp(now)},
                ":owner": {"S": owner_sub},
                ":uploading": {"S": SourceFileStatus.UPLOADING.value},
                ":max": {"N": str(int(max_refreshes))},
            },
            ReturnValues="ALL_NEW",
            unavailable=True,
        )
        return self._source_file(response["Attributes"])

    def delete_uploading(self, source_file_id: str, owner_sub: str) -> bool:
        """Delete the owner's upload that is still ``uploading`` (cancel, W2-6).

        Returns:
            ``False`` when it is someone else's, missing or past uploading.
        """
        return self._conditional(
            "delete_item",
            TableName=self.table_name,
            Key=self._key(source_file_id),
            ConditionExpression="owner_sub = :owner AND #status = :uploading",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":owner": {"S": owner_sub},
                ":uploading": {"S": SourceFileStatus.UPLOADING.value},
            },
        )

    def clear_in_use(self, source_file_id: str, generation_id: str) -> bool:
        """Remove the in-use mark of ``generation_id``.

        The last use is written at the start, not here.

        Returns:
            ``False`` when the mark belongs to another generation or is gone.
        """
        return self._conditional(
            "update_item",
            TableName=self.table_name,
            Key=self._key(source_file_id),
            UpdateExpression="REMOVE in_use_by, in_use_until",
            ConditionExpression="in_use_by = :generation",
            ExpressionAttributeValues={":generation": {"S": generation_id}},
        )

    # --- Deletion, expiry and checksum ---------------

    def begin_delete(
        self,
        source_file_id: str,
        owner_sub: str,
        *,
        now: datetime,
        stale_before: datetime,
    ) -> SourceFile:
        """Move the owner's file to ``deleting``.

        Passes for ``ready``, ``error``, ``deleting`` (a retry after a failed
        S3 deletion) and an ``inspecting`` file stuck since ``stale_before``,
        when the file has not expired and no generation holds a valid mark.
        A record without ``expires_at`` passes the expiry part; the
        caller checks its computed expiry first.

        Raises:
            SourceFileUnavailable: Someone else's, missing, expired, in use,
                uploading or inspecting recently.
            SourceFileStoreError: DynamoDB failed.
        """
        response = self._call(
            "update_item",
            TableName=self.table_name,
            Key=self._key(source_file_id),
            UpdateExpression="SET #status = :deleting, updated_at = :now",
            ConditionExpression=(
                "owner_sub = :owner AND "
                "(attribute_not_exists(expires_at) OR expires_at > :epoch) AND "
                "(#status IN (:ready, :error, :deleting) OR "
                "(#status = :inspecting AND updated_at < :stale)) AND "
                "(attribute_not_exists(in_use_by) OR in_use_until < :now)"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":deleting": {"S": SourceFileStatus.DELETING.value},
                ":ready": {"S": SourceFileStatus.READY.value},
                ":error": {"S": SourceFileStatus.ERROR.value},
                ":inspecting": {"S": SourceFileStatus.INSPECTING.value},
                ":owner": {"S": owner_sub},
                ":now": {"S": _stamp(now)},
                ":epoch": _expiry_number(now),
                ":stale": {"S": _stamp(stale_before)},
            },
            ReturnValues="ALL_NEW",
            unavailable=True,
        )
        return self._source_file(response["Attributes"])

    def delete_record(self, source_file_id: str, owner_sub: str) -> bool:
        """Delete the owner's record once it is ``deleting``.

        Returns:
            ``False`` when it is someone else's, missing or in another state.
        """
        return self._conditional(
            "delete_item",
            TableName=self.table_name,
            Key=self._key(source_file_id),
            ConditionExpression="owner_sub = :owner AND #status = :deleting",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":owner": {"S": owner_sub},
                ":deleting": {"S": SourceFileStatus.DELETING.value},
            },
        )

    def fill_expires_at(
        self, source_file_id: str, owner_sub: str, expires_at: datetime
    ) -> bool:
        """Write ``expires_at`` on the owner's record that has none.

        Returns:
            ``False`` when it already has one, or is someone else's or missing.
        """
        return self._conditional(
            "update_item",
            TableName=self.table_name,
            Key=self._key(source_file_id),
            UpdateExpression="SET expires_at = :expires",
            ConditionExpression=(
                "owner_sub = :owner AND attribute_not_exists(expires_at)"
            ),
            ExpressionAttributeValues={
                ":owner": {"S": owner_sub},
                ":expires": _expiry_number(expires_at),
            },
        )

    def set_checksum_if_empty(self, source_file_id: str, checksum: str) -> bool:
        """Write the checksum when the record holds none or an empty one.

        Returns:
            ``False`` when a checksum is already there or the file is missing.
        """
        return self._conditional(
            "update_item",
            TableName=self.table_name,
            Key=self._key(source_file_id),
            UpdateExpression="SET checksum = :checksum",
            ConditionExpression=(
                "attribute_exists(source_file_id) AND "
                "(attribute_not_exists(checksum) OR checksum = :empty)"
            ),
            ExpressionAttributeValues={
                ":checksum": {"S": checksum},
                ":empty": {"S": ""},
            },
        )

    def max_display_seq(self, owner_sub: str, filename: str, *, now: datetime) -> int:
        """Highest ``display_seq`` of the owner's unexpired files of ``filename``.

        A record without ``display_seq`` counts as 1; one without
        ``expires_at`` is counted (its computed expiry is the Catalog's).

        Returns:
            0 when there is none.
        """
        highest = 0
        start_key: dict[str, Any] | None = None
        while True:
            kwargs: dict[str, Any] = {
                "TableName": self.table_name,
                "IndexName": OWNER_INDEX,
                "KeyConditionExpression": "owner_sub = :owner",
                "FilterExpression": (
                    "filename = :filename AND "
                    "(attribute_not_exists(expires_at) OR expires_at > :epoch)"
                ),
                "ExpressionAttributeValues": {
                    ":owner": {"S": owner_sub},
                    ":filename": {"S": filename},
                    ":epoch": _expiry_number(now),
                },
            }
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            response = self._call("query", **kwargs)
            for item in response.get("Items", []):
                seq = item.get("display_seq", {}).get("N")
                highest = max(highest, int(seq) if seq else 1)
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return highest

    # --- reads ----------------------------------------------------------------------

    def get(self, source_file_id: str) -> SourceFile | None:
        """Read one file with a strongly consistent ``GetItem``."""
        response = self._call(
            "get_item",
            TableName=self.table_name,
            Key=self._key(source_file_id),
            ConsistentRead=True,
        )
        item = response.get("Item")
        return self._source_file(item) if item else None

    def get_for_owner(self, source_file_id: str, owner_sub: str) -> SourceFile | None:
        """The file when it belongs to ``owner_sub``; ``None`` otherwise."""
        found = self.get(source_file_id)
        if found is None or found.owner_sub != owner_sub:
            return None
        return found

    def list_for_owner(self, owner_sub: str) -> list[SourceFile]:
        """All files of ``owner_sub``, newest first (GSI, eventually consistent)."""
        files: list[SourceFile] = []
        start_key: dict[str, Any] | None = None
        while True:
            kwargs: dict[str, Any] = {
                "TableName": self.table_name,
                "IndexName": OWNER_INDEX,
                "KeyConditionExpression": "owner_sub = :owner",
                "ExpressionAttributeValues": {":owner": {"S": owner_sub}},
                "ScanIndexForward": False,
            }
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            response = self._call("query", **kwargs)
            files.extend(self._source_file(i) for i in response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return files

    # --- plumbing -------------------------------------------------------------------

    def _conditional(self, operation: str, **kwargs: Any) -> bool:
        try:
            self._call(operation, unavailable=True, **kwargs)
        except SourceFileUnavailable:
            return False
        return True

    def _call(
        self, operation: str, *, unavailable: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            response: dict[str, Any] = getattr(self.client, operation)(**kwargs)
            return response
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if unavailable and code == "ConditionalCheckFailedException":
                raise SourceFileUnavailable(f"{operation} condition failed") from e
            logger.warning("%s on source files failed: %s", operation, code)
            raise SourceFileStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            logger.warning(
                "%s on source files unreachable: %s", operation, type(e).__name__
            )
            raise SourceFileStoreError(
                f"{operation} unreachable: {type(e).__name__}"
            ) from e
