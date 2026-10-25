"""DynamoDB reads and writes of registered knowledge bases (U7).

``KnowledgeBases``: PK ``id``. ``shared_group_ids`` is the usage scope of the
organization asset (BR5.1); without it only admins may use the knowledge base.

- A new registration is written only when the ``id`` does not exist yet
  (BR1.5), and never writes the usage scope.
- The usage scope changes only through :meth:`KnowledgeBaseStore.update_scope`
  (``ADD`` / ``DELETE``, BR5.2).
- Listing scans to the last page (NFR9.37).

Who may register, delete or scope a knowledge base is decided by
``SettingsManager``.
"""

import logging
from typing import Any, Optional

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode
from src.models.knowledge_base import KnowledgeBase

logger = logging.getLogger(__name__)


class KnowledgeBaseStoreError(CodedError):
    """A knowledge base read or write failed."""

    code = ErrorCode.DATABASE_OPERATION_FAILED


class _ConditionFailed(Exception):
    """The condition of a conditional write did not hold."""


class KnowledgeBaseStore:
    """Keyed reads and writes of ``KnowledgeBases``."""

    def __init__(self, *, region: str, table_name: str, client: Any = None) -> None:
        """Create the store.

        Args:
            region: AWS region of the table.
            table_name: Physical name of ``KnowledgeBases``.
            client: Optional pre-built DynamoDB client (tests).
        """
        self.table_name = table_name
        self.client = client or boto3.client("dynamodb", region_name=region)
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    def put_new(self, kb: KnowledgeBase) -> None:
        """Register a knowledge base whose ``id`` does not exist yet.

        Raises:
            KnowledgeBaseStoreError: The ``id`` exists already, or the call failed.
        """
        data = kb.to_dict()
        data.pop("shared_group_ids", None)
        item = {k: self._serializer.serialize(v) for k, v in data.items()}
        try:
            self._call(
                "put_item",
                conditional=True,
                TableName=self.table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(id)",
            )
        except _ConditionFailed as e:
            raise KnowledgeBaseStoreError("the knowledge base exists already") from e

    def get(self, kb_id: str, *, consistent: bool = False) -> Optional[KnowledgeBase]:
        """One knowledge base, or ``None`` when it is not registered."""
        response = self._call(
            "get_item",
            TableName=self.table_name,
            Key={"id": {"S": kb_id}},
            ConsistentRead=consistent,
        )
        item = response.get("Item")
        return self._knowledge_base(item) if item else None

    def list_all(self) -> list[KnowledgeBase]:
        """Every registered knowledge base, every scan page (NFR9.37)."""
        params: dict[str, Any] = {"TableName": self.table_name}
        found: list[KnowledgeBase] = []
        while True:
            response = self._call("scan", **params)
            found.extend(self._knowledge_base(i) for i in response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                return found
            params["ExclusiveStartKey"] = last

    def delete(self, kb_id: str) -> None:
        """Delete a registration (no error when it does not exist)."""
        self._call("delete_item", TableName=self.table_name, Key={"id": {"S": kb_id}})

    def update_scope(
        self, kb_id: str, *, add: frozenset[str], remove: frozenset[str]
    ) -> bool:
        """``ADD`` then ``DELETE`` on ``shared_group_ids`` (BR5.2).

        Two separate updates (DynamoDB refuses both actions on one attribute in
        one expression), each conditioned on the knowledge base existing. A
        side with no department is not sent; when the second update fails the
        first one stays (the screen reads the scope again). Removing every
        department drops the column: the knowledge base is admin only again.

        Returns:
            ``False`` when the knowledge base does not exist (nothing is created).
        """
        for action, groups in (("ADD", add), ("DELETE", remove)):
            if not groups:
                continue
            try:
                self._call(
                    "update_item",
                    conditional=True,
                    TableName=self.table_name,
                    Key={"id": {"S": kb_id}},
                    UpdateExpression=f"{action} #shared :groups",
                    ConditionExpression="attribute_exists(id)",
                    ExpressionAttributeNames={"#shared": "shared_group_ids"},
                    ExpressionAttributeValues={":groups": {"SS": sorted(groups)}},
                )
            except _ConditionFailed:
                return False
        return True

    def _knowledge_base(self, item: dict[str, Any]) -> KnowledgeBase:
        return KnowledgeBase.from_dict(
            {k: self._deserializer.deserialize(v) for k, v in item.items()}
        )

    def _call(
        self, operation: str, *, conditional: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            response: dict[str, Any] = getattr(self.client, operation)(**kwargs)
            return response
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if conditional and code == "ConditionalCheckFailedException":
                raise _ConditionFailed(f"{operation} condition failed") from e
            logger.warning("%s on knowledge bases failed: %s", operation, code)
            raise KnowledgeBaseStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            logger.warning(
                "%s on knowledge bases unreachable: %s", operation, type(e).__name__
            )
            raise KnowledgeBaseStoreError(
                f"{operation} unreachable: {type(e).__name__}"
            ) from e
