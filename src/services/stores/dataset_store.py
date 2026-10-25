"""DynamoDB reads and writes of registered datasets (U5, U7).

``Datasets``: PK ``id``. An organization asset has no ``owner_persona_id``;
``shared_group_ids`` is its usage scope (BR5.1), and without it only admins
may use it. A persona-dedicated dataset has ``owner_persona_id`` (U5 BR6.1)
and never a usage scope (BR5.6).

- A new registration is written only when the ``id`` does not exist yet, so
  a stored usage scope is never overwritten (U7 BR1.5).
- An edit writes only the given columns (NFR1.77); the scope, the owner
  persona and ``created_at`` are never part of it.
- The usage scope changes only through :meth:`DatasetStore.update_scope`
  (``ADD`` / ``DELETE``, BR5.2).
- Listing returns organization assets only, filtered in the scan (U5 BR6.2).

Who may register, edit, delete or scope a dataset is decided by
``DatasetManager`` (and by ``PersonaManager`` for dedicated datasets).
"""

import logging
from datetime import datetime
from typing import Any, Optional, Sequence

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.models.dataset import Dataset, DatasetColumn
from src.models.errors import CodedError, ErrorCode

logger = logging.getLogger(__name__)

#: The condition of a scope change: the dataset exists and is an organization asset.
_ORG_ASSET = "attribute_exists(id) AND attribute_not_exists(owner_persona_id)"


class DatasetStoreError(CodedError):
    """A dataset read or write failed."""

    code = ErrorCode.DATABASE_OPERATION_FAILED


class _ConditionFailed(Exception):
    """The condition of a conditional write did not hold."""


class DatasetStore:
    """Keyed reads and writes of ``Datasets``."""

    def __init__(self, *, region: str, table_name: str, client: Any = None) -> None:
        """Create the store.

        Args:
            region: AWS region of the table.
            table_name: Physical name of ``Datasets``.
            client: Optional pre-built DynamoDB client (tests).
        """
        self.table_name = table_name
        self.client = client or boto3.client("dynamodb", region_name=region)
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    def put_new(self, dataset: Dataset) -> None:
        """Register a dataset whose ``id`` does not exist yet (never overwrites).

        Raises:
            DatasetStoreError: The ``id`` exists already, or the call failed.
        """
        values: dict[str, Any] = {
            "id": dataset.id,
            "name": dataset.name,
            "description": dataset.description,
            "s3_path": dataset.s3_path,
            "columns": _columns(dataset.columns),
            "row_count": dataset.row_count,
            "created_at": dataset.created_at.isoformat(),
            "updated_at": dataset.updated_at.isoformat(),
            "notes": dataset.notes,
        }
        # Persona-dedicated datasets only (U5 BR6.1); org assets have no column
        if dataset.owner_persona_id:
            values["owner_persona_id"] = dataset.owner_persona_id
        item = {k: self._serializer.serialize(v) for k, v in values.items()}
        try:
            self._call(
                "put_item",
                conditional=True,
                TableName=self.table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(id)",
            )
        except _ConditionFailed as e:
            raise DatasetStoreError("the dataset exists already") from e

    def get(self, dataset_id: str, *, consistent: bool = False) -> Optional[Dataset]:
        """One dataset, or ``None`` when it is not registered."""
        response = self._call(
            "get_item",
            TableName=self.table_name,
            Key={"id": {"S": dataset_id}},
            ConsistentRead=consistent,
        )
        item = response.get("Item")
        return self._dataset(item) if item else None

    def update_fields(
        self,
        dataset_id: str,
        *,
        updated_at: datetime,
        name: Optional[str] = None,
        description: Optional[str] = None,
        notes: Optional[str] = None,
        columns: Optional[Sequence[DatasetColumn]] = None,
    ) -> bool:
        """Write only the given columns of a dataset (U7 BR1.5, NFR1.77).

        Returns:
            ``False`` when the dataset does not exist (nothing is created).
        """
        fields: dict[str, Any] = {"updated_at": updated_at.isoformat()}
        if name is not None:
            fields["name"] = name
        if description is not None:
            fields["description"] = description
        if notes is not None:
            fields["notes"] = notes
        if columns is not None:
            fields["columns"] = _columns(columns)
        names = {f"#f{i}": column for i, column in enumerate(fields)}
        values = {
            f":f{i}": self._serializer.serialize(value)
            for i, value in enumerate(fields.values())
        }
        try:
            self._call(
                "update_item",
                conditional=True,
                TableName=self.table_name,
                Key={"id": {"S": dataset_id}},
                UpdateExpression="SET "
                + ", ".join(f"{n} = {v}" for n, v in zip(names, values)),
                ConditionExpression="attribute_exists(id)",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except _ConditionFailed:
            return False
        return True

    def list_org_assets(self) -> list[Dataset]:
        """Every organization-asset dataset, every scan page.

        Persona-dedicated datasets are left out in the scan itself (U5 BR6.2).
        The table holds organization assets and dedicated datasets only, so
        a scan stays small (not a per-user listing, NFR2).
        """
        params: dict[str, Any] = {
            "TableName": self.table_name,
            "FilterExpression": "attribute_not_exists(owner_persona_id)",
        }
        found: list[Dataset] = []
        while True:
            response = self._call("scan", **params)
            found.extend(self._dataset(i) for i in response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                return found
            params["ExclusiveStartKey"] = last

    def delete(self, dataset_id: str) -> None:
        """Delete a registration (no error when it does not exist)."""
        self._call(
            "delete_item", TableName=self.table_name, Key={"id": {"S": dataset_id}}
        )

    def update_scope(
        self, dataset_id: str, *, add: frozenset[str], remove: frozenset[str]
    ) -> bool:
        """``ADD`` then ``DELETE`` on ``shared_group_ids`` (BR5.2).

        Two separate updates (DynamoDB refuses both actions on one attribute in
        one expression), each conditioned on the dataset existing and being an
        organization asset (BR5.6). A side with no department is not sent;
        when the second update fails the first one stays (the screen reads the
        scope again). Removing every department drops the column: the dataset
        is admin only again.

        Returns:
            ``False`` when the dataset does not exist or is persona-dedicated
            (nothing is created).
        """
        for action, groups in (("ADD", add), ("DELETE", remove)):
            if not groups:
                continue
            try:
                self._call(
                    "update_item",
                    conditional=True,
                    TableName=self.table_name,
                    Key={"id": {"S": dataset_id}},
                    UpdateExpression=f"{action} #shared :groups",
                    ConditionExpression=_ORG_ASSET,
                    ExpressionAttributeNames={"#shared": "shared_group_ids"},
                    ExpressionAttributeValues={":groups": {"SS": sorted(groups)}},
                )
            except _ConditionFailed:
                return False
        return True

    def _dataset(self, item: dict[str, Any]) -> Dataset:
        data = {k: self._deserializer.deserialize(v) for k, v in item.items()}
        data["row_count"] = int(data.get("row_count", 0))
        return Dataset.from_dict(data)

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
            logger.warning("%s on datasets failed: %s", operation, code)
            raise DatasetStoreError(f"{operation} failed: {code}") from e
        except BotoCoreError as e:
            logger.warning(
                "%s on datasets unreachable: %s", operation, type(e).__name__
            )
            raise DatasetStoreError(
                f"{operation} unreachable: {type(e).__name__}"
            ) from e


def _columns(columns: Sequence[DatasetColumn]) -> list[dict[str, str]]:
    return [
        {"name": c.name, "data_type": c.data_type, "description": c.description}
        for c in columns
    ]
