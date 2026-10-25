"""DynamoDB reads and writes of persona bodies (U4, U5).

``Personas``: PK ``id``. ``owner_sub`` is the creator (BR6.1);
``shared_group_ids`` is the copy of the shares, written only by
``ResourceSharing`` together with ``ResourceShares`` (BR1.6).

- A new persona is written only when the ``id`` does not exist yet, so a save
  never overwrites a stored owner or shares (R-02).
- An edit writes only the columns the edit screen can change (BR6.7); the
  owner, the shares, the avatar, the generation log and ``created_at`` stay.
  Optional columns that became empty are removed.
- The avatar path changes with a compare-and-set on the path read before.
- Batch reads are strongly consistent; unprocessed keys are read again and
  are never silently dropped (NFR2.5).
- Only the admin's ``all`` tab search scans the table (U4 BR3.5).

Who may save, read or edit a persona is decided by ``PersonaManager``.
"""

import logging
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.errors import CodedError, ErrorCode
from src.models.persona import Persona

logger = logging.getLogger(__name__)

#: ``BatchGetItem`` accepts at most 100 keys.
BATCH_LIMIT = 100
#: Re-reads of ``UnprocessedKeys`` before giving up.
UNPROCESSED_REREADS = 3


class PersonaStoreError(CodedError):
    """A persona read or write failed."""

    code = ErrorCode.DATABASE_OPERATION_FAILED


class _ConditionFailed(Exception):
    """The condition of a conditional write did not hold."""


class PersonaStore:
    """Keyed reads and writes of ``Personas``."""

    def __init__(self, *, region: str, table_name: str, client: Any = None) -> None:
        """Create the store.

        Args:
            region: AWS region of the table.
            table_name: Physical name of ``Personas``.
            client: Optional pre-built DynamoDB client (tests).
        """
        self.table_name = table_name
        self.client = client or boto3.client("dynamodb", region_name=region)
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    def put_new(self, persona: Persona) -> None:
        """Write a persona whose ``id`` does not exist yet, without shares.

        Raises:
            PersonaStoreError: The ``id`` exists already, or the call failed.
        """
        values: dict[str, Any] = {
            "id": persona.id,
            "name": persona.name,
            "age": persona.age,
            "occupation": persona.occupation,
            "background": persona.background,
            "values": persona.values,
            "pain_points": persona.pain_points,
            "goals": persona.goals,
            "created_at": persona.created_at.isoformat(),
            "updated_at": persona.updated_at.isoformat(),
            "type": "persona",
            "gender": persona.gender,
            "country": persona.country,
            "city": persona.city,
            "tags": persona.tags or None,
            "generation_log": persona.generation_log,
            "generation_context": persona.generation_context,
            "avatar_path": persona.avatar_path,
            "owner_sub": persona.owner_sub,
        }
        try:
            self._call(
                "put_item",
                conditional=True,
                TableName=self.table_name,
                Item=self._item({k: v for k, v in values.items() if v is not None}),
                ConditionExpression="attribute_not_exists(id)",
            )
        except _ConditionFailed as e:
            raise PersonaStoreError("the persona exists already") from e

    def get(self, persona_id: str, *, consistent: bool = False) -> Optional[Persona]:
        """One persona, or ``None`` when it does not exist.

        Authorization reads pass ``consistent=True`` so a share removed a
        moment ago is never seen (NFR1.12).
        """
        response = self._call(
            "get_item",
            TableName=self.table_name,
            Key={"id": {"S": persona_id}},
            ConsistentRead=consistent,
        )
        item = response.get("Item")
        return self._persona(item) if item else None

    def get_many(self, persona_ids: Sequence[str]) -> dict[str, Persona]:
        """Read personas with strongly consistent ``BatchGetItem`` calls.

        Returns:
            id -> persona, for the IDs that exist.

        Raises:
            PersonaStoreError: Keys were still unprocessed after three re-reads
                (rows are never silently dropped).
        """
        ids = [i for i in dict.fromkeys(persona_ids) if i]
        found: dict[str, Persona] = {}
        for start in range(0, len(ids), BATCH_LIMIT):
            found.update(self._batch_get(ids[start : start + BATCH_LIMIT]))
        return found

    def scan_all(self) -> list[Persona]:
        """Every persona, every scan page (the admin's ``all`` tab, BR3.5)."""
        params: dict[str, Any] = {"TableName": self.table_name}
        found: list[Persona] = []
        while True:
            response = self._call("scan", **params)
            found.extend(self._persona(i) for i in response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                return found
            params["ExclusiveStartKey"] = last

    def update_editable(self, persona: Persona) -> bool:
        """Write the columns the edit screen can change (BR6.7).

        Returns:
            ``False`` when the persona does not exist (nothing is created).
        """
        columns: dict[str, Any] = {
            "name": persona.name,
            "age": persona.age,
            "occupation": persona.occupation,
            "background": persona.background,
            "values": persona.values,
            "pain_points": persona.pain_points,
            "goals": persona.goals,
            "updated_at": persona.updated_at.isoformat(),
            "gender": persona.gender,
            "country": persona.country,
            "city": persona.city,
            "tags": persona.tags or None,
        }
        names = {f"#f{i}": column for i, column in enumerate(columns)}
        sets = {n: v for n, v in zip(names, columns.values()) if v is not None}
        removes = [n for n, v in zip(names, columns.values()) if v is None]
        values = {f":{n[1:]}": v for n, v in sets.items()}
        expression = "SET " + ", ".join(f"{n} = :{n[1:]}" for n in sets)
        if removes:
            expression += " REMOVE " + ", ".join(removes)
        try:
            self._call(
                "update_item",
                conditional=True,
                TableName=self.table_name,
                Key={"id": {"S": persona.id}},
                UpdateExpression=expression,
                ConditionExpression="attribute_exists(id)",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=self._item(values),
            )
        except _ConditionFailed:
            return False
        return True

    def update_avatar(
        self,
        persona_id: str,
        *,
        new_path: Optional[str],
        expected_path: Optional[str],
        updated_at: datetime,
    ) -> bool:
        """Set or remove the avatar path if it is still ``expected_path``.

        Only ``avatar_path`` and ``updated_at`` are written, so fields changed
        by other operations stay. ``expected_path`` ``None`` means "no avatar".

        Returns:
            ``False`` when the persona is gone or its path changed meanwhile.
        """
        values: dict[str, Any] = {":updated_at": updated_at.isoformat()}
        if new_path is None:
            expression = "SET updated_at = :updated_at REMOVE avatar_path"
        else:
            expression = "SET avatar_path = :new_path, updated_at = :updated_at"
            values[":new_path"] = new_path
        if expected_path is None:
            condition = "attribute_exists(id) AND attribute_not_exists(avatar_path)"
        else:
            condition = "attribute_exists(id) AND avatar_path = :expected_path"
            values[":expected_path"] = expected_path
        try:
            self._call(
                "update_item",
                conditional=True,
                TableName=self.table_name,
                Key={"id": {"S": persona_id}},
                UpdateExpression=expression,
                ConditionExpression=condition,
                ExpressionAttributeValues=self._item(values),
            )
        except _ConditionFailed:
            return False
        return True

    def _batch_get(self, ids: Sequence[str]) -> dict[str, Persona]:
        keys: list[dict[str, Any]] = [{"id": {"S": i}} for i in ids]
        found: dict[str, Persona] = {}
        for _ in range(UNPROCESSED_REREADS + 1):
            response = self._call(
                "batch_get_item",
                RequestItems={self.table_name: {"Keys": keys, "ConsistentRead": True}},
            )
            for item in response.get("Responses", {}).get(self.table_name, []):
                persona = self._persona(item)
                found[persona.id] = persona
            keys = (
                response.get("UnprocessedKeys", {})
                .get(self.table_name, {})
                .get("Keys", [])
            )
            if not keys:
                return found
        logger.warning("BatchGetItem left %d personas unprocessed", len(keys))
        raise PersonaStoreError("personas left unprocessed after three re-reads")

    def _item(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return {k: self._serializer.serialize(v) for k, v in values.items()}

    def _persona(self, item: Mapping[str, Any]) -> Persona:
        data = {k: self._deserializer.deserialize(v) for k, v in item.items()}
        return Persona(
            id=data["id"],
            name=data["name"],
            # Numbers come back as Decimal
            age=int(data["age"]),
            occupation=data["occupation"],
            background=data["background"],
            values=data["values"],
            pain_points=data["pain_points"],
            goals=data["goals"],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            gender=data.get("gender"),
            country=data.get("country"),
            city=data.get("city"),
            tags=data.get("tags", []),
            generation_log=data.get("generation_log"),
            generation_context=data.get("generation_context"),
            avatar_path=data.get("avatar_path"),
            owner_sub=data.get("owner_sub") or None,
            shared_group_ids=frozenset(data.get("shared_group_ids") or ()),
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
            logger.warning("%s on personas failed: %s", operation, code)
            raise PersonaStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            logger.warning(
                "%s on personas unreachable: %s", operation, type(e).__name__
            )
            raise PersonaStoreError(
                f"{operation} unreachable: {type(e).__name__}"
            ) from e
