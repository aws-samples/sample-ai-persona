"""DynamoDB reads and writes of survey bodies (U7).

``Surveys``: PK ``id``, ``OwnerIndex`` (``owner_sub`` / ``created_at``).
``shared_group_ids`` is the copy of the shares; only ``ResourceSharing``
writes it, together with ``ResourceShares`` (contract C7).

- A new survey is written only when the ``id`` does not exist yet, and never
  with shares (BR1.1).
- An update writes only the given columns (status, results, report, ...);
  the owner, the shares, the keys and the template copy are refused
  (BR1.5, NFR1.87).
- Batch reads are strongly consistent; unprocessed keys are read again and
  are never silently dropped (NFR2.21).

Who may create, read or change a survey is decided by
``SurveyExecutionManager`` and ``SurveyAnalysisManager``.
"""

import logging
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode
from src.models.survey import Survey

logger = logging.getLogger(__name__)

#: ``BatchGetItem`` accepts at most 100 keys.
BATCH_LIMIT = 100
#: Re-reads of ``UnprocessedKeys`` before giving up.
UNPROCESSED_REREADS = 3
#: Columns :meth:`SurveyStore.update_fields` never writes: the owner and the
#: shares belong to ``ResourceSharing`` (contract C7), the keys and the
#: template copy never change (BR1.5).
_PROTECTED_COLUMNS = frozenset(
    {"id", "owner_sub", "shared_group_ids", "created_at", "template_snapshot"}
)


class SurveyStoreError(CodedError):
    """A survey read or write failed."""

    code = ErrorCode.DATABASE_OPERATION_FAILED


class _ConditionFailed(Exception):
    """The condition of a conditional write did not hold."""


class SurveyStore:
    """Keyed reads and writes of ``Surveys``."""

    def __init__(self, *, region: str, table_name: str, client: Any = None) -> None:
        """Create the store.

        Args:
            region: AWS region of the table.
            table_name: Physical name of ``Surveys``.
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

    def put_new(self, survey: Survey) -> None:
        """Write a survey whose ``id`` does not exist yet, without shares.

        Raises:
            SurveyStoreError: The ``id`` exists already, or the call failed.
        """
        data = survey.to_dict()
        data.pop("shared_group_ids", None)
        try:
            self._call(
                "put_item",
                conditional=True,
                TableName=self.table_name,
                Item=self._item({k: v for k, v in data.items() if v is not None}),
                ConditionExpression="attribute_not_exists(id)",
            )
        except _ConditionFailed as e:
            raise SurveyStoreError("the survey exists already") from e

    def get(self, survey_id: str, *, consistent: bool = False) -> Optional[Survey]:
        """One survey, or ``None`` when it does not exist."""
        response = self._call(
            "get_item",
            TableName=self.table_name,
            Key={"id": {"S": survey_id}},
            ConsistentRead=consistent,
        )
        item = response.get("Item")
        return self._survey(item) if item else None

    def get_many(self, survey_ids: Sequence[str]) -> dict[str, Survey]:
        """Read surveys with strongly consistent ``BatchGetItem`` calls.

        Returns:
            id -> survey, for the IDs that exist.

        Raises:
            SurveyStoreError: Keys were still unprocessed after three re-reads
                (rows are never silently dropped).
        """
        ids = [i for i in dict.fromkeys(survey_ids) if i]
        found: dict[str, Survey] = {}
        for start in range(0, len(ids), BATCH_LIMIT):
            found.update(self._batch_get(ids[start : start + BATCH_LIMIT]))
        return found

    def update_fields(
        self,
        survey_id: str,
        fields: Mapping[str, Any],
        *,
        remove: Sequence[str] = (),
    ) -> bool:
        """Write only the given columns of a survey (BR1.5, NFR1.87).

        Args:
            survey_id: The survey.
            fields: Column -> new value (status, results, report, ...).
            remove: Columns to remove.

        Returns:
            ``False`` when the survey does not exist (nothing is created).

        Raises:
            ValueError: A column the survey's owner, shares, keys or template
                copy live in.
        """
        refused = (set(fields) | set(remove)) & _PROTECTED_COLUMNS
        if refused:
            raise ValueError(f"columns not updatable here: {sorted(refused)}")
        names = {f"#f{i}": column for i, column in enumerate(fields)}
        values = {f":f{i}": value for i, value in enumerate(fields.values())}
        clauses = []
        if names:
            clauses.append(
                "SET " + ", ".join(f"{n} = {v}" for n, v in zip(names, values))
            )
        removed = {f"#r{i}": column for i, column in enumerate(remove)}
        if removed:
            clauses.append("REMOVE " + ", ".join(removed))
        if not clauses:
            return True
        params: dict[str, Any] = {
            "TableName": self.table_name,
            "Key": {"id": {"S": survey_id}},
            "UpdateExpression": " ".join(clauses),
            "ConditionExpression": "attribute_exists(id)",
            "ExpressionAttributeNames": {**names, **removed},
        }
        if values:
            params["ExpressionAttributeValues"] = self._item(values)
        try:
            self._call("update_item", conditional=True, **params)
        except _ConditionFailed:
            return False
        return True

    def _batch_get(self, ids: Sequence[str]) -> dict[str, Survey]:
        keys: list[dict[str, Any]] = [{"id": {"S": i}} for i in ids]
        found: dict[str, Survey] = {}
        for _ in range(UNPROCESSED_REREADS + 1):
            response = self._call(
                "batch_get_item",
                RequestItems={self.table_name: {"Keys": keys, "ConsistentRead": True}},
            )
            for item in response.get("Responses", {}).get(self.table_name, []):
                survey = self._survey(item)
                found[survey.id] = survey
            keys = (
                response.get("UnprocessedKeys", {})
                .get(self.table_name, {})
                .get("Keys", [])
            )
            if not keys:
                return found
        logger.warning("BatchGetItem left %d surveys unprocessed", len(keys))
        raise SurveyStoreError("surveys left unprocessed after three re-reads")

    def _item(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return {k: self._serializer.serialize(_to_dynamo(v)) for k, v in values.items()}

    def _survey(self, item: Mapping[str, Any]) -> Survey:
        data = {k: self._deserializer.deserialize(v) for k, v in item.items()}
        # Numbers come back as Decimal; the persona count is an int
        data["persona_count"] = int(data["persona_count"])
        return Survey.from_dict(data)

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
            logger.warning("%s on surveys failed: %s", operation, code)
            raise SurveyStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            logger.warning("%s on surveys unreachable: %s", operation, type(e).__name__)
            raise SurveyStoreError(
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
