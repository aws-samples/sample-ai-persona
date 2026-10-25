"""DynamoDB reads and writes of survey template bodies (U7).

``SurveyTemplates``: PK ``id``, ``OwnerIndex`` (``owner_sub`` / ``created_at``).
``shared_group_ids`` is the copy of the shares; only ``ResourceSharing``
writes it, together with ``ResourceShares`` (contract C7).

- A new template is written only when the ``id`` does not exist yet, and
  never with shares (BR1.1).
- An edit writes only the name, questions, images and ``updated_at``
  (BR1.5); the owner, the shares and ``created_at`` stay.
- Batch reads are strongly consistent; unprocessed keys are read again and
  are never silently dropped (NFR2.21).

Who may create, read or edit a template is decided by
``SurveyTemplateManager``.
"""

import logging
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode
from src.models.survey_template import SurveyTemplate

logger = logging.getLogger(__name__)

#: ``BatchGetItem`` accepts at most 100 keys.
BATCH_LIMIT = 100
#: Re-reads of ``UnprocessedKeys`` before giving up.
UNPROCESSED_REREADS = 3


class SurveyTemplateStoreError(CodedError):
    """A survey template read or write failed."""

    code = ErrorCode.DATABASE_OPERATION_FAILED


class _ConditionFailed(Exception):
    """The condition of a conditional write did not hold."""


class SurveyTemplateStore:
    """Keyed reads and writes of ``SurveyTemplates``."""

    def __init__(self, *, region: str, table_name: str, client: Any = None) -> None:
        """Create the store.

        Args:
            region: AWS region of the table.
            table_name: Physical name of ``SurveyTemplates``.
            client: Optional pre-built DynamoDB client (tests).
        """
        self.table_name = table_name
        self.client = client or boto3.client("dynamodb", region_name=region)
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    @staticmethod
    def item_size(body: Mapping[str, Any]) -> int:
        """Approximate DynamoDB size of ``body`` as an item (NFR2.25)."""
        serializer = TypeSerializer()
        return sum(
            len(str(name).encode("utf-8"))
            + _attribute_size(serializer.serialize(_to_dynamo(value)))
            for name, value in body.items()
            if value is not None
        )

    def put_new(self, template: SurveyTemplate) -> None:
        """Write a template whose ``id`` does not exist yet, without shares.

        Raises:
            SurveyTemplateStoreError: The ``id`` exists already, or the call
                failed.
        """
        data = template.to_dict()
        data.pop("shared_group_ids", None)
        try:
            self._call(
                "put_item",
                conditional=True,
                TableName=self.table_name,
                Item=self._item(data),
                ConditionExpression="attribute_not_exists(id)",
            )
        except _ConditionFailed as e:
            raise SurveyTemplateStoreError("the template exists already") from e

    def get(
        self, template_id: str, *, consistent: bool = False
    ) -> Optional[SurveyTemplate]:
        """One template, or ``None`` when it does not exist."""
        response = self._call(
            "get_item",
            TableName=self.table_name,
            Key={"id": {"S": template_id}},
            ConsistentRead=consistent,
        )
        item = response.get("Item")
        return self._template(item) if item else None

    def get_many(self, template_ids: Sequence[str]) -> dict[str, SurveyTemplate]:
        """Read templates with strongly consistent ``BatchGetItem`` calls.

        Returns:
            id -> template, for the IDs that exist.

        Raises:
            SurveyTemplateStoreError: Keys were still unprocessed after three
                re-reads (rows are never silently dropped).
        """
        ids = [i for i in dict.fromkeys(template_ids) if i]
        found: dict[str, SurveyTemplate] = {}
        for start in range(0, len(ids), BATCH_LIMIT):
            found.update(self._batch_get(ids[start : start + BATCH_LIMIT]))
        return found

    def update_content(self, template: SurveyTemplate) -> bool:
        """Write only the name, questions, images and ``updated_at`` (BR1.5).

        Images taken off entirely remove the column.

        Returns:
            ``False`` when the template does not exist (nothing is created).
        """
        data = template.to_dict()
        fields: dict[str, Any] = {
            "name": data["name"],
            "questions": data["questions"],
            "updated_at": data["updated_at"],
        }
        if data.get("images"):
            fields["images"] = data["images"]
        names = {f"#f{i}": column for i, column in enumerate(fields)}
        values = {f":f{i}": value for i, value in enumerate(fields.values())}
        expression = "SET " + ", ".join(f"{n} = {v}" for n, v in zip(names, values))
        if "images" not in fields:
            names["#images"] = "images"
            expression += " REMOVE #images"
        try:
            self._call(
                "update_item",
                conditional=True,
                TableName=self.table_name,
                Key={"id": {"S": template.id}},
                UpdateExpression=expression,
                ConditionExpression="attribute_exists(id)",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=self._item(values),
            )
        except _ConditionFailed:
            return False
        return True

    def _batch_get(self, ids: Sequence[str]) -> dict[str, SurveyTemplate]:
        keys: list[dict[str, Any]] = [{"id": {"S": i}} for i in ids]
        found: dict[str, SurveyTemplate] = {}
        for _ in range(UNPROCESSED_REREADS + 1):
            response = self._call(
                "batch_get_item",
                RequestItems={self.table_name: {"Keys": keys, "ConsistentRead": True}},
            )
            for item in response.get("Responses", {}).get(self.table_name, []):
                template = self._template(item)
                found[template.id] = template
            keys = (
                response.get("UnprocessedKeys", {})
                .get(self.table_name, {})
                .get("Keys", [])
            )
            if not keys:
                return found
        logger.warning("BatchGetItem left %d templates unprocessed", len(keys))
        raise SurveyTemplateStoreError(
            "templates left unprocessed after three re-reads"
        )

    def _item(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return {k: self._serializer.serialize(_to_dynamo(v)) for k, v in values.items()}

    def _template(self, item: Mapping[str, Any]) -> SurveyTemplate:
        data = {k: self._deserializer.deserialize(v) for k, v in item.items()}
        # Numbers come back as Decimal; the scale bounds are ints
        for question in data.get("questions", []):
            for bound in ("scale_min", "scale_max"):
                if bound in question:
                    question[bound] = int(question[bound])
        return SurveyTemplate.from_dict(data)

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
            logger.warning("%s on survey templates failed: %s", operation, code)
            raise SurveyTemplateStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            logger.warning(
                "%s on survey templates unreachable: %s", operation, type(e).__name__
            )
            raise SurveyTemplateStoreError(
                f"{operation} unreachable: {type(e).__name__}"
            ) from e


def _to_dynamo(value: Any) -> Any:
    """Floats become ``Decimal`` (``TypeSerializer`` refuses floats)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
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
