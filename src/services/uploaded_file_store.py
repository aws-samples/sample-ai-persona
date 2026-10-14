"""DynamoDB reads and writes of upload metadata (U5, BR5.1〜BR5.3).

``UploadedFiles`` (PK ``id``, TTL ``expires_at``). Reads are strongly
consistent so a just-attached or just-detached file is never seen in its old
state (NFR9.29). Attaching and detaching are single transactions of at most
100 items (the ``TransactWriteItems`` limit, security-design.md §3):

- attach: every item ``owner_sub = :owner AND attribute_not_exists(parent_id)
  AND expires_at > :now``; ``SET parent_type, parent_id REMOVE expires_at``
- detach: every item ``parent_type = :pt AND parent_id = :pid``;
  ``REMOVE parent_type, parent_id SET expires_at = :original``

Failures are classified (``reliability-design.md`` §4): a failed condition ->
:class:`AttachConditionFailed`, ``TransactionConflict`` ->
:class:`AttachConflict`, anything else -> :class:`UploadedFileStoreError`.
Whether a user may attach or read a file is decided by ``FileAttachments``.
"""

import logging
from typing import Any, Mapping, Sequence

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode
from src.models.uploaded_file import UploadedFile

logger = logging.getLogger(__name__)

#: ``TransactWriteItems`` accepts at most 100 items.
MAX_TRANSACTION_ITEMS = 100
#: ``BatchGetItem`` accepts at most 100 keys.
BATCH_LIMIT = 100
#: Re-reads of ``UnprocessedKeys`` before giving up.
UNPROCESSED_REREADS = 3


class UploadedFileStoreError(CodedError):
    """An upload metadata read or write failed."""

    code = ErrorCode.FILE_OPERATION_FAILED


class AttachConditionFailed(UploadedFileStoreError):
    """An item did not meet the attach / detach condition."""


class AttachConflict(UploadedFileStoreError):
    """Another transaction touched one of the items at the same time."""


class UploadedFileStore:
    """Strong reads and transactional attach / detach of ``UploadedFiles``."""

    def __init__(self, *, region: str, table_name: str, client: Any = None) -> None:
        """Create the store.

        Args:
            region: AWS region of the table.
            table_name: Physical name of ``UploadedFiles``.
            client: Optional pre-built DynamoDB client (tests).
        """
        self.table_name = table_name
        self.client = client or boto3.client("dynamodb", region_name=region)
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    # --- reads and writes -----------------------------------------------------------

    def put(self, uploaded: UploadedFile) -> None:
        """Write the metadata of a new upload."""
        item = {k: self._serializer.serialize(v) for k, v in uploaded.to_dict().items()}
        self._call("put_item", TableName=self.table_name, Item=item)

    def get(self, file_id: str) -> UploadedFile | None:
        """Read one file with a strongly consistent ``GetItem``."""
        response = self._call(
            "get_item",
            TableName=self.table_name,
            Key={"id": {"S": file_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return self._file(item) if item else None

    def get_many(self, file_ids: Sequence[str]) -> dict[str, UploadedFile]:
        """Read files with strongly consistent ``BatchGetItem`` calls.

        Returns:
            id -> file, for the IDs that exist.

        Raises:
            UploadedFileStoreError: Keys were still unprocessed after three
                re-reads (rows are never silently dropped).
        """
        ids = list(dict.fromkeys(file_ids))
        found: dict[str, UploadedFile] = {}
        for start in range(0, len(ids), BATCH_LIMIT):
            found.update(self._batch_get(ids[start : start + BATCH_LIMIT]))
        return found

    def _batch_get(self, ids: Sequence[str]) -> dict[str, UploadedFile]:
        keys: list[dict[str, Any]] = [{"id": {"S": i}} for i in ids]
        found: dict[str, UploadedFile] = {}
        for _ in range(UNPROCESSED_REREADS + 1):
            response = self._call(
                "batch_get_item",
                RequestItems={self.table_name: {"Keys": keys, "ConsistentRead": True}},
            )
            for item in response.get("Responses", {}).get(self.table_name, []):
                uploaded = self._file(item)
                found[uploaded.id] = uploaded
            keys = (
                response.get("UnprocessedKeys", {})
                .get(self.table_name, {})
                .get("Keys", [])
            )
            if not keys:
                return found
        logger.warning("BatchGetItem left %d files unprocessed", len(keys))
        raise UploadedFileStoreError("files left unprocessed after three re-reads")

    # --- transactions ---------------------------------------------------------------

    def transact_attach(
        self,
        owner_sub: str,
        file_ids: Sequence[str],
        parent_type: str,
        parent_id: str,
        now_epoch: int,
    ) -> None:
        """Attach every file to the parent, or none (BR5.3).

        Raises:
            ValueError: More than 100 files (a caller bug).
            AttachConditionFailed: A file is someone else's, expired, already
                attached or missing.
            AttachConflict: A concurrent transaction touched a file.
            UploadedFileStoreError: Any other failure.
        """
        self._check_count(file_ids)
        items = [
            {
                "Update": {
                    "TableName": self.table_name,
                    "Key": {"id": {"S": file_id}},
                    "UpdateExpression": (
                        "SET parent_type = :pt, parent_id = :pid REMOVE expires_at"
                    ),
                    "ConditionExpression": (
                        "owner_sub = :owner AND attribute_not_exists(parent_id)"
                        " AND expires_at > :now"
                    ),
                    "ExpressionAttributeValues": {
                        ":pt": {"S": parent_type},
                        ":pid": {"S": parent_id},
                        ":owner": {"S": owner_sub},
                        ":now": {"N": str(int(now_epoch))},
                    },
                }
            }
            for file_id in file_ids
        ]
        self._transact(items)

    def transact_detach(
        self,
        original_expires_at: Mapping[str, int],
        parent_type: str,
        parent_id: str,
    ) -> None:
        """Undo an attach: back to unattached with the original expiry.

        Raises:
            ValueError: More than 100 files (a caller bug).
            AttachConditionFailed: A file is not attached to this parent.
            AttachConflict: A concurrent transaction touched a file.
            UploadedFileStoreError: Any other failure.
        """
        self._check_count(list(original_expires_at))
        items = [
            {
                "Update": {
                    "TableName": self.table_name,
                    "Key": {"id": {"S": file_id}},
                    "UpdateExpression": (
                        "REMOVE parent_type, parent_id SET expires_at = :original"
                    ),
                    "ConditionExpression": "parent_type = :pt AND parent_id = :pid",
                    "ExpressionAttributeValues": {
                        ":pt": {"S": parent_type},
                        ":pid": {"S": parent_id},
                        ":original": {"N": str(int(expires))},
                    },
                }
            }
            for file_id, expires in original_expires_at.items()
        ]
        self._transact(items)

    @staticmethod
    def _check_count(file_ids: Sequence[str]) -> None:
        if len(file_ids) > MAX_TRANSACTION_ITEMS:
            raise ValueError(
                f"at most {MAX_TRANSACTION_ITEMS} files per transaction, "
                f"got {len(file_ids)}"
            )

    def _transact(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        try:
            self.client.transact_write_items(TransactItems=items)
        except ClientError as e:
            raise self._classify(e) from e
        except BotoCoreError as e:
            logger.warning("TransactWriteItems unreachable: %s", type(e).__name__)
            raise UploadedFileStoreError(
                f"TransactWriteItems unreachable: {type(e).__name__}"
            ) from e

    @staticmethod
    def _classify(error: ClientError) -> UploadedFileStoreError:
        code = error.response.get("Error", {}).get("Code", "")
        if code != "TransactionCanceledException":
            logger.warning("TransactWriteItems on files failed: %s", code)
            return UploadedFileStoreError(f"TransactWriteItems failed: {code}")
        reasons = [
            str(r.get("Code") or "None")
            for r in error.response.get("CancellationReasons", [])
        ]
        if "TransactionConflict" in reasons:
            return AttachConflict("transaction conflict")
        if "ConditionalCheckFailed" in reasons:
            return AttachConditionFailed("a file did not meet the condition")
        logger.warning("TransactWriteItems on files cancelled: %s", ",".join(reasons))
        return UploadedFileStoreError("transaction cancelled for another reason")

    # --- plumbing -------------------------------------------------------------------

    def _file(self, item: Mapping[str, Any]) -> UploadedFile:
        plain = {k: self._deserializer.deserialize(v) for k, v in item.items()}
        return UploadedFile.from_dict(plain)

    def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response: dict[str, Any] = getattr(self.client, operation)(**kwargs)
            return response
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            logger.warning("%s on files failed: %s", operation, code)
            raise UploadedFileStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            logger.warning("%s on files unreachable: %s", operation, type(e).__name__)
            raise UploadedFileStoreError(
                f"{operation} unreachable: {type(e).__name__}"
            ) from e
