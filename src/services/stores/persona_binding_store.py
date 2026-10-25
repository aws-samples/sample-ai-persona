"""DynamoDB reads and writes of persona bindings (U5, ADR-007).

``PersonaBindings``: PK ``persona_id``, SK ``target_key`` (``DATASET#{id}`` or
the fixed ``KB``), GSI ``TargetIndex`` (PK ``target_id``, ``KEYS_ONLY``).

- Reading a persona's bindings is always a ``Query`` on ``persona_id``, read to
  the last page; this service never scans (NFR2.9, BR8.2).
- Deleting is by ``(persona_id, target_key)`` (BR2.5), by ``persona_id``
  (``delete_all``, BR3.1) or by the target through ``TargetIndex``
  (``delete_for_target``, when a dataset or knowledge base is deleted).
- ``delete_all`` and ``delete_for_target`` re-send ``UnprocessedItems`` up to
  three times, 0.2 / 0.4 / 0.8 seconds apart (``reliability-design.md`` §4).

Who may read or change a binding is decided by ``PersonaManager``; the
bindings of a deleted dataset or knowledge base are removed by
``DatasetManager`` and ``SettingsManager``.
"""

import logging
import time
from typing import Any, Callable, Mapping, Sequence

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode
from src.models.persona_binding import PersonaBinding

logger = logging.getLogger(__name__)

#: Index from a dataset / knowledge base back to the personas bound to it.
TARGET_INDEX = "TargetIndex"
#: ``BatchWriteItem`` accepts at most 25 requests.
WRITE_BATCH_LIMIT = 25
#: Waits before the re-sends of ``UnprocessedItems``.
UNPROCESSED_BACKOFF_SECONDS: tuple[float, ...] = (0.2, 0.4, 0.8)


class PersonaBindingStoreError(CodedError):
    """A binding read or write failed."""

    code = ErrorCode.DATABASE_OPERATION_FAILED


class PersonaBindingStore:
    """Query-only reads and keyed writes of ``PersonaBindings``."""

    def __init__(
        self,
        *,
        region: str,
        table_name: str,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the store.

        Args:
            region: AWS region of the table.
            table_name: Physical name of ``PersonaBindings``.
            client: Optional pre-built DynamoDB client (tests).
            sleep: Sleep function for the re-send back-off (tests).
        """
        self.table_name = table_name
        self.client = client or boto3.client("dynamodb", region_name=region)
        self._sleep = sleep
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    def list_for_persona(self, persona_id: str) -> list[PersonaBinding]:
        """Every binding of a persona, strongly consistent, all pages."""
        params: dict[str, Any] = {
            "TableName": self.table_name,
            "KeyConditionExpression": "persona_id = :pid",
            "ExpressionAttributeValues": {":pid": {"S": persona_id}},
            "ConsistentRead": True,
        }
        bindings: list[PersonaBinding] = []
        while True:
            response = self._call("query", **params)
            for item in response.get("Items", []):
                binding = self._binding(item)
                if binding is not None:
                    bindings.append(binding)
            last = response.get("LastEvaluatedKey")
            if not last:
                return bindings
            params["ExclusiveStartKey"] = last

    def put(self, binding: PersonaBinding) -> None:
        """Write one binding (replaces the item with the same key, BR2.9)."""
        item = {k: self._serializer.serialize(v) for k, v in binding.to_dict().items()}
        self._call("put_item", TableName=self.table_name, Item=item)

    def delete(self, persona_id: str, target_key: str) -> bool:
        """Delete one binding of one persona; ``False`` when it did not exist."""
        response = self._call(
            "delete_item",
            TableName=self.table_name,
            Key=self._key(persona_id, target_key),
            ReturnValues="ALL_OLD",
        )
        return bool(response.get("Attributes"))

    def delete_all(
        self, persona_id: str, bindings: Sequence[PersonaBinding] | None = None
    ) -> int:
        """Delete every binding of a persona (BR3.1).

        Args:
            persona_id: The persona.
            bindings: The bindings already read (read again when omitted).

        Returns:
            How many bindings were deleted.

        Raises:
            PersonaBindingStoreError: A call failed or items stayed
                unprocessed after three re-sends.
        """
        targets = (
            list(bindings)
            if bindings is not None
            else self.list_for_persona(persona_id)
        )
        requests = [
            {"DeleteRequest": {"Key": self._key(persona_id, b.target_key)}}
            for b in targets
        ]
        for start in range(0, len(requests), WRITE_BATCH_LIMIT):
            self._batch_write(requests[start : start + WRITE_BATCH_LIMIT])
        return len(requests)

    def delete_for_target(self, target_id: str) -> int:
        """Delete every binding to a dataset or knowledge base (all index pages).

        Used when the dataset or knowledge base itself is deleted. The
        ``TargetIndex`` keys carry the table keys, so the bindings are deleted
        without being read again. The index is eventually consistent: a binding
        written a moment before may be missed, and readers skip it because its
        target no longer exists.

        Returns:
            How many bindings were deleted.

        Raises:
            PersonaBindingStoreError: A call failed or items stayed
                unprocessed after three re-sends.
        """
        params: dict[str, Any] = {
            "TableName": self.table_name,
            "IndexName": TARGET_INDEX,
            "KeyConditionExpression": "target_id = :tid",
            "ExpressionAttributeValues": {":tid": {"S": target_id}},
        }
        keys: list[tuple[str, str]] = []
        while True:
            response = self._call("query", **params)
            keys += [
                (item["persona_id"]["S"], item["target_key"]["S"])
                for item in response.get("Items", [])
            ]
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            params["ExclusiveStartKey"] = last
        requests = [
            {"DeleteRequest": {"Key": self._key(persona_id, target_key)}}
            for persona_id, target_key in dict.fromkeys(keys)
        ]
        for start in range(0, len(requests), WRITE_BATCH_LIMIT):
            self._batch_write(requests[start : start + WRITE_BATCH_LIMIT])
        return len(requests)

    # --- plumbing -------------------------------------------------------------------

    @staticmethod
    def _key(persona_id: str, target_key: str) -> dict[str, Any]:
        return {"persona_id": {"S": persona_id}, "target_key": {"S": target_key}}

    def _binding(self, item: Mapping[str, Any]) -> PersonaBinding | None:
        plain = {k: self._deserializer.deserialize(v) for k, v in item.items()}
        try:
            return PersonaBinding.from_dict(plain)
        except (KeyError, ValueError):
            logger.warning("Skipped a malformed binding item")
            return None

    def _batch_write(self, requests: list[dict[str, Any]]) -> None:
        pending = requests
        for attempt in range(len(UNPROCESSED_BACKOFF_SECONDS) + 1):
            if attempt:
                self._sleep(UNPROCESSED_BACKOFF_SECONDS[attempt - 1])
            response = self._call(
                "batch_write_item", RequestItems={self.table_name: pending}
            )
            pending = response.get("UnprocessedItems", {}).get(self.table_name, [])
            if not pending:
                return
        logger.warning("BatchWriteItem left %d bindings unprocessed", len(pending))
        raise PersonaBindingStoreError("bindings left unprocessed after three re-sends")

    def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response: dict[str, Any] = getattr(self.client, operation)(**kwargs)
            return response
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            logger.warning("%s on bindings failed: %s", operation, code)
            raise PersonaBindingStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            logger.warning(
                "%s on bindings unreachable: %s", operation, type(e).__name__
            )
            raise PersonaBindingStoreError(
                f"{operation} unreachable: {type(e).__name__}"
            ) from e
